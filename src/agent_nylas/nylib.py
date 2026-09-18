"""
nylib -- shared helpers for the read-only Nylas commands in this package
(email_list, email_read and event_list).

Read-only by construction: it never sends, deletes, moves, or marks anything.
It only builds an SDK client, resolves identifiers (grants, folders, calendars)
behind a small TTL cache, runs per-account work concurrently with error
isolation, and renders plain text that aligns correctly for wide (CJK) glyphs.

Credential: the API key is read from the environment variable named in
API_KEY_ENV -- the same variable the `nylas` CLI stores, so the CLI and these
commands cannot drift apart.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict, cast

import config

# The SDK and requests are imported where they are used: together they cost
# about half a second to import, which the argument-parsing paths never need.
if TYPE_CHECKING:
    import nylas
    import requests

# ---------------------------------------------------------------------- types


class Grant(TypedDict):
    """One connected account as returned by grants.list."""

    id: str
    email: str
    provider: str
    grant_status: str


class Folder(TypedDict):
    """One mail folder or label. `attributes` carries provider markers such as
    "\\Inbox", which is how find_inbox() stays provider neutral."""

    id: str
    name: str
    parent_id: str | None
    system_folder: bool
    total_count: int
    unread_count: int
    attributes: str | None


class Calendar(TypedDict):
    """One calendar of one account."""

    id: str
    name: str
    is_primary: bool
    read_only: bool
    timezone: str | None
    description: str | None
    hex_color: str | None


class ConfigError(RuntimeError):
    """The environment is unusable, e.g. the API key is missing."""


# ------------------------------------------------------------------- 1. client


class _PooledHttp:
    """Drop-in replacement for the module-level `requests.request`.

    The SDK calls `requests.request(...)` for every request, which builds a
    throwaway Session and therefore a fresh TLS handshake each time: measured
    through the local proxy, 2-5s against 0.5s for a reused connection. Routing
    all SDK traffic through one pooled Session fixes that, and transport errors
    are retried so a flaky connection no longer fails a whole account.
    """

    def __init__(self) -> None:
        import requests
        import requests.adapters

        # The SDK reaches for `requests.exceptions.Timeout` inside its own error
        # handling, resolved on whatever object replaced `sdk_http.requests`.
        # Without this the attribute lookup fails first and every transport error
        # (a stopped proxy, a refused connection, a bad URL) is reported as
        # "AttributeError: '_PooledHttp' object has no attribute 'exceptions'".
        self.exceptions = requests.exceptions
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        for scheme in ("http://", "https://"):
            self._session.mount(scheme, adapter)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send one request, retrying transport failures and 5xx/429 answers."""
        import requests.exceptions

        failure: Exception | None = None
        for attempt in range(1, config.RETRY_ATTEMPTS + 1):
            last_attempt = attempt == config.RETRY_ATTEMPTS
            try:
                response = self._session.request(method, url, **kwargs)
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ) as exc:
                failure = exc
            else:
                if (
                    last_attempt
                    or response.status_code not in config.RETRY_STATUS_CODES
                ):
                    return response
                failure = None
                response.close()
            if not last_attempt:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
        raise failure or RuntimeError(
            f"{method} {url} failed after {config.RETRY_ATTEMPTS} attempts"
        )


_client: nylas.Client | None = None


def client() -> nylas.Client:
    """Return the process-wide Nylas SDK client, creating it on first use."""
    global _client
    if _client is None:
        key = os.environ.get(config.API_KEY_ENV)
        if not key:
            raise ConfigError(
                f"{config.API_KEY_ENV} is not set\n"
                f"hint: export {config.API_KEY_ENV}=$(nylas auth token)"
            )
        try:
            import nylas
            from nylas.handler import http_client as sdk_http
        except ImportError as exc:
            # A bare `python3` outside this project's virtualenv gets here; say
            # what to do instead of dying on a ModuleNotFoundError traceback.
            raise ConfigError(
                f"the Nylas SDK is not importable ({exc})\n"
                "hint: run this command with uv, e.g. "
                "uv run src/agent_nylas/email_list.py"
            ) from exc

        # Swap the SDK's HTTP seam for one pooled, retrying Session. This is the
        # widest and most stable seam available: it covers every SDK resource,
        # including private methods we would otherwise have to mirror.
        sdk_http.requests = _PooledHttp()
        _client = nylas.Client(
            api_key=key, api_uri=config.API_URI, timeout=config.REQUEST_TIMEOUT_SECONDS
        )
    return _client


def list_data(response: Any) -> tuple[list[Any], str | None]:
    """Unwrap an SDK ListResponse tuple (data, request_id, next_cursor, headers)."""
    return response[0], response[2] if len(response) > 2 else None


# -------------------------------------------------------------------- 2. cache


def _cache_file(name: str) -> Path:
    return config.CACHE_DIR / f"{name}.json"


def cache_load(name: str, ttl: int = config.CACHE_TTL_SECONDS) -> Any:
    """Return cached data if present and younger than ttl, else None."""
    try:
        blob = json.loads(_cache_file(name).read_text(encoding="utf-8"))
        if time.time() - float(blob["fetched_at"]) <= ttl:
            return blob["data"]
    except OSError, ValueError, KeyError, TypeError:
        pass
    return None


def cache_save(name: str, data: Any) -> None:
    """Best-effort cache write; a cache problem must never break a script."""
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file(name).write_text(
            json.dumps({"fetched_at": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _cached[T](
    name: str, fetch: Callable[[], list[T]], refresh: bool = False
) -> list[T]:
    """Return the list cached under `name`, re-fetching it when stale or on demand."""
    if not refresh:
        cached = cache_load(name)
        if cached is not None:
            return cast(list[T], cached)
    data = fetch()
    cache_save(name, data)
    return data


# ------------------------------------------------- 3. identity: accounts


def load_grants(refresh: bool = False) -> list[Grant]:
    """All grants reachable with the API key, cached.

    The list is stable and the call costs a full round trip, so it is cached
    alongside the per-grant folder and calendar lists.
    """

    def fetch() -> list[Grant]:
        data, _ = list_data(client().grants.list())
        return [
            Grant(
                id=g.id,
                email=g.email,
                provider=g.provider,
                grant_status=g.grant_status,
            )
            for g in data
        ]

    return _cached("grants", fetch, refresh)


def select_grants(
    grants: Sequence[Grant], selector: str = "all"
) -> tuple[list[Grant], list[str]]:
    """Filter grants by selector.

    selector accepts a comma-separated list of: "all", an email substring
    ("outlook", "pku", a full address), a provider name ("google", "imap"),
    or a full grant id.

    Returns (matched, unmatched_terms). Order follows the grant list, so output
    order stays stable between runs.
    """
    terms = [t.strip().lower() for t in (selector or "all").split(",") if t.strip()]
    if not terms or "all" in terms:
        return list(grants), []

    matched: list[Grant] = []
    missing: list[str] = []
    for term in terms:
        hits = [
            g
            for g in grants
            if term == g["id"].lower()
            or term in g["email"].lower()
            or term == g.get("provider", "").lower()
        ]
        if not hits:
            missing.append(term)
        for hit in hits:
            if hit not in matched:
                matched.append(hit)
    return matched, missing


# ----------------------------------------- 4. identity: folders and calendars


def load_folders(grant_id: str, refresh: bool = False) -> list[Folder]:
    """Folders/labels of one grant, cached.

    Keys: id, name, parent_id, system_folder, total_count, unread_count,
    attributes. `attributes` carries the provider-agnostic markers such as
    "\\Inbox", which is how find_inbox() stays provider neutral.
    """

    def fetch() -> list[Folder]:
        data, _ = list_data(client().folders.list(identifier=grant_id))
        return [
            Folder(
                id=f.id,
                name=f.name,
                parent_id=f.parent_id,
                system_folder=f.system_folder,
                total_count=f.total_count,
                unread_count=f.unread_count,
                attributes=f.attributes,
            )
            for f in data
        ]

    return _cached(f"folders-{grant_id}", fetch, refresh)


def load_calendars(grant_id: str, refresh: bool = False) -> list[Calendar]:
    """Calendars of one grant, cached.

    Keys: id, name, is_primary, read_only, timezone, description, hex_color.
    """

    def fetch() -> list[Calendar]:
        data, _ = list_data(client().calendars.list(identifier=grant_id))
        return [
            Calendar(
                id=c.id,
                name=c.name,
                is_primary=c.is_primary,
                read_only=c.read_only,
                timezone=c.timezone,
                description=c.description,
                hex_color=c.hex_color,
            )
            for c in data
        ]

    return _cached(f"calendars-{grant_id}", fetch, refresh)


def resolve_named[T: Mapping[str, Any]](
    items: Sequence[T],
    wanted: str,
    *,
    kind: str,
    aliases: Mapping[str, Callable[[T], bool]] | None = None,
) -> T:
    """Resolve one item by keyword alias, id, exact name, or unique name substring.

    Items are the typed dicts of this package (folders, calendars), which all
    carry an "id" and a "name", so a caller passes its list plus the kind for
    error text and gets that same item type back. Matching is case-insensitive,
    so both "dev" and "DEV" work, and names in any script work as-is. `aliases`
    maps a keyword such as "primary" to a predicate. Raises LookupError listing
    the candidates when nothing matches or the term is ambiguous.
    """
    wanted = (wanted or "").strip()
    low = wanted.lower()
    if not low:
        raise LookupError(f"empty {kind} name")

    if aliases:
        alias = aliases.get(low)
        if alias is not None:
            for item in items:
                if alias(item):
                    return item

    for item in items:
        if item["id"] == wanted:
            return item

    exact = [i for i in items if str(i["name"]).strip().lower() == low]
    if len(exact) == 1:
        return exact[0]

    hits = [i for i in items if low in str(i["name"]).lower()]
    if len(hits) == 1:
        return hits[0]

    available = ", ".join(sorted(str(i["name"]) for i in items))
    if not hits:
        raise LookupError(f"no {kind} matches {wanted!r}; available: {available}")
    found = ", ".join(sorted(str(i["name"]) for i in hits))
    raise LookupError(f"{kind} {wanted!r} is ambiguous: {found}")


def find_inbox(folders: Sequence[Folder]) -> Folder | None:
    """Return the inbox folder, or None.

    Primary probe is the "\\Inbox" attribute, which every provider exposes
    (Microsoft, Google, IMAP) and which survives localized folder names.
    """
    for folder in folders:
        if "inbox" in (folder["attributes"] or "").lower():
            return folder
    for folder in folders:  # fallback: literal name
        if folder["name"].strip().lower() in ("inbox", "in"):
            return folder
    return None


# ------------------------------------------- 5. concurrency and error text


def gather[T](
    fn: Callable[[T], Any], items: Iterable[T], workers: int = config.DEFAULT_WORKERS
) -> tuple[list[Any], list[Exception | None]]:
    """Run fn(item) concurrently, one task per item, isolating failures.

    Returns two lists aligned with `items`: results and errors (None where the
    other one holds a value). A failing item never cancels the others, which is
    what keeps one broken account from hiding the other two.
    """
    items = list(items)
    results: list[Any] = [None] * len(items)
    errors: list[Exception | None] = [None] * len(items)
    if not items:
        return results, errors

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - reported verbatim to the caller
                errors[index] = exc
    return results, errors


def count_of(count: int, singular: str) -> str:
    """'1 message', '3 messages' -- for a note that counts something."""
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def progress_start(label: str) -> float:
    """Announce work on stderr and return the moment it started.

    A listing through this machine's proxy takes seconds and a stalled proxy a
    minute, and a minute of silence is indistinguishable from a hang. stdout
    stays data, so these lines go to stderr beside the error report.
    """
    print(f"… {label}", file=sys.stderr, flush=True)
    return time.monotonic()


def progress_done(label: str, started: float, note: str = "") -> None:
    """Close a progress_start line with what came of it and how long it took."""
    detail = f"{label}: {note}" if note else label
    print(
        f"✓ {detail} in {time.monotonic() - started:.1f}s", file=sys.stderr, flush=True
    )


def describe_error(exc: Exception) -> str:
    """One-line description of an exception, with HTTP status and request id."""
    import requests.exceptions
    from nylas.models.errors import NylasApiError, NylasSdkTimeoutError

    if isinstance(exc, LookupError):
        return str(exc)  # our own resolution failures are already readable
    if isinstance(exc, NylasApiError):
        parts = []
        if exc.status_code:
            parts.append(f"HTTP {exc.status_code}")
        if getattr(exc, "type", None):
            parts.append(str(exc.type))
        parts.append(str(exc) or exc.__class__.__name__)
        if exc.request_id:
            parts.append(f"request_id={exc.request_id}")
        return " | ".join(parts)
    if isinstance(exc, NylasSdkTimeoutError):
        return f"timed out after {exc.timeout}s"
    if isinstance(exc, requests.exceptions.RequestException):
        return f"network error: {exc.__class__.__name__}: {exc}"
    return f"{exc.__class__.__name__}: {exc}"


def is_not_found(exc: Exception) -> bool:
    """True when the API said the object is gone, i.e. a cached id went stale."""
    from nylas.models.errors import NylasApiError

    if isinstance(exc, NylasApiError):
        if exc.status_code == 404:
            return True
        return "not found" in str(exc).lower()
    return False


# --------------------------------------------------------- 6. display helpers


def dwidth(text: str) -> int:
    """Terminal display width: wide (CJK) characters count as two columns."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def html_to_text(text: str | None, keep_blank_lines: bool = False) -> str:
    """Render provider HTML (Outlook event descriptions) as plain text.

    The stdlib parser (rather than a regex) leaves a plain-text description be,
    including a bare "a < b", and drops the text of style/script elements, so
    what reaches the table is what a reader of the HTML would see. Blank lines
    are collapsed by default (a table cell wants that); keep_blank_lines=True
    keeps them, which is what makes paragraphs readable in a full message body.
    """
    parser = _HtmlText()
    parser.feed(text or "")
    parser.close()
    return parser.text(keep_blank_lines=keep_blank_lines)


class _HtmlText(HTMLParser):
    """Collect visible text, turning block-level boundaries into line breaks."""

    BREAKS: ClassVar[frozenset[str]] = frozenset(
        {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
    )
    SKIP: ClassVar[frozenset[str]] = frozenset({"head", "style", "script"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BREAKS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skipping = max(0, self._skipping - 1)

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._parts.append(data)

    def text(self, keep_blank_lines: bool = False) -> str:
        """Collapse runs of whitespace, and the blank lines they leave behind."""
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        if keep_blank_lines:
            return "\n".join(lines).strip("\n")
        return "\n".join(line for line in lines if line)


def split_width(text: str, width: int) -> tuple[str, str]:
    """Split text into (head that fits `width` display columns, the rest)."""
    used = 0
    for index, char in enumerate(text):
        step = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + step > width:
            return text[:index], text[index:]
        used += step
    return text, ""


def trunc(text: str | None, width: int) -> str:
    """Truncate to a display width, collapsing newlines and adding an ellipsis."""
    text = (text or "").replace("\n", " ").strip()
    if width <= 1:
        return ""
    if dwidth(text) <= width:
        return text
    head, _ = split_width(text, width - 1)
    return head + "\u2026"


def wrap(text: str | None, width: int) -> list[str]:
    """Wrap text into lines of at most `width` display columns.

    Breaks at whitespace when possible. Runs that cannot fit on a line by
    themselves -- a URL, or CJK text without spaces -- are hard-split, so a
    line never exceeds the width.
    """
    if width <= 1:
        return []
    lines: list[str] = []
    line = ""
    for word in (text or "").split():
        while dwidth(word) > width - (dwidth(line) + 1 if line else 0):
            room = width - (dwidth(line) + 1 if line else 0)
            if room <= 0:
                lines.append(line)
                line = ""
                continue
            head, word = split_width(word, room)
            line = f"{line} {head}" if line else head
            lines.append(line)
            line = ""
        if not word:
            continue
        if not line:
            line = word
        elif dwidth(line) + 1 + dwidth(word) <= width:
            line = f"{line} {word}"
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def prefixes(labels: Iterable[str], indent: str = "") -> dict[str, str]:
    """Padded "Label:" prefixes for a view's detail fields.

    Every label is padded to the widest of them plus its colon and one space, so
    all values start in the same column and a wrapped value keeps that column.
    Views own their label set and indent; this is the one place that decides how
    the column is computed.
    """
    width = max(len(label) for label in labels) + 2
    return {label: f"{indent}{label + ':':<{width}}" for label in labels}


def hanging(prefix: str, text: str | None, width: int) -> list[str]:
    """Lines of one labelled block: text after `prefix`, continuations aligned to it.

    Used for the detail fields of both views, so every value starts in the same
    column and a wrapped value keeps that column instead of falling back to the
    label. An empty value yields no lines at all, which keeps an absent field
    from printing its label with nothing after it.
    """
    value = (text or "").strip()
    if not value:
        return []
    lines = wrap(value, max(20, width - len(prefix)))
    padding = " " * len(prefix)
    return [f"{prefix}{lines[0]}"] + [f"{padding}{line}" for line in lines[1:]]


def pad(text: str | None, width: int, *, truncate: bool = True) -> str:
    """Left-align text inside a display width.

    `width` is a column, so text longer than it is truncated by default. Pass
    truncate=False to align without losing anything, which matters when the text
    is also valid input elsewhere (a calendar name is also a -c argument).
    """
    text = trunc(text, width) if truncate else (text or "").strip()
    return text + " " * max(0, width - dwidth(text))


def terminal_width(floor: int = 80) -> int:
    """Terminal width in columns, never narrower than `floor`.

    Non-interactive callers have no terminal, so get_terminal_size falls back to
    its own 120x24 default rather than to the minimum it is given.
    """
    return max(floor, shutil.get_terminal_size((120, 24)).columns)


def local_time(seconds: int | None) -> datetime | None:
    """Unix seconds as a local datetime, or None when the value is unusable."""
    if not seconds:
        return None
    try:
        return datetime.fromtimestamp(int(seconds), tz=UTC).astimezone()
    except OverflowError, OSError, ValueError:
        return None


def ts_iso(seconds: int | None) -> str | None:
    """Unix seconds -> local ISO 8601, so callers never do epoch math."""
    stamp = local_time(seconds)
    return stamp.isoformat(timespec="seconds") if stamp else None


def now_iso() -> str:
    """Current local time as ISO 8601, for report headers."""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")
