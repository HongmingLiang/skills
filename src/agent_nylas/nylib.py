"""
nylib -- shared helpers for the read-only Nylas commands in this package
(agent_nylas.mail today, agent_nylas.cal next).

Read-only by construction: it never sends, deletes, moves, or marks anything.
It only builds an SDK client, resolves identifiers (grants, folders, calendars)
behind a small TTL cache, runs per-account work concurrently with error
isolation, and renders plain text that aligns correctly for wide (CJK) glyphs.

Credential: the API key is read from the environment variable named in
API_KEY_ENV -- the same variable the `nylas` CLI stores, so the CLI and these
commands cannot drift apart.
"""

import json
import os
import shutil
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

import nylas
import requests
import requests.adapters
import requests.exceptions
from nylas.handler import http_client as sdk_http
from nylas.models.errors import NylasApiError, NylasSdkTimeoutError

# --------------------------------------------------------------- configuration

API_KEY_ENV = "NYLAS_FILE_STORE_PASSPHRASE"
API_URI = os.environ.get("NYLAS_API_URI", "https://api.us.nylas.com")

# Seconds. Measured through a local HTTP proxy: a 50-message Microsoft page
# needs 5-10s, and a slow proxy handshake can add seconds more.
REQUEST_TIMEOUT = 60

# Transport failures are retried, because a single flaky connection used to
# fail a whole account. Safe here only because every call in this package is a
# read: do not reuse this session for send or delete work without dropping the
# retry, which would risk duplicate writes.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 1.0  # seconds, multiplied by the attempt number
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

CACHE_TTL = 12 * 60 * 60  # identifier caches are considered fresh for 12 hours
CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "email-helper"
)

PAGE_LIMIT = 200  # the Nylas API caps a single list page at 200 items

# Measured throughput differs a lot per provider: Google and IMAP deliver a
# 100-item page in about 3s, while Microsoft needs ~0.1-0.2s per message, so a
# 200-item page blows past the request timeout. Microsoft therefore paginates in
# smaller pages.
PAGE_LIMIT_BY_PROVIDER = {"microsoft": 50}

DEFAULT_WORKERS = 4

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


def page_limit(provider: str | None) -> int:
    """Safe page size for one provider."""
    return PAGE_LIMIT_BY_PROVIDER.get((provider or "").lower(), PAGE_LIMIT)


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
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        for scheme in ("http://", "https://"):
            self._session.mount(scheme, adapter)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send one request, retrying transport failures and 5xx/429 answers."""
        failure: Exception | None = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            last_attempt = attempt == RETRY_ATTEMPTS
            try:
                response = self._session.request(method, url, **kwargs)
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ) as exc:
                failure = exc
            else:
                if last_attempt or response.status_code not in RETRY_STATUS:
                    return response
                failure = None
                response.close()
            if not last_attempt:
                time.sleep(RETRY_BACKOFF * attempt)
        raise failure or RuntimeError(
            f"{method} {url} failed after {RETRY_ATTEMPTS} attempts"
        )


_client: nylas.Client | None = None


def client() -> nylas.Client:
    """Return the process-wide Nylas SDK client, creating it on first use."""
    global _client
    if _client is None:
        key = os.environ.get(API_KEY_ENV)
        if not key:
            raise ConfigError(
                f"{API_KEY_ENV} is not set\n"
                f"hint: export {API_KEY_ENV}=$(nylas auth token)"
            )
        # Swap the SDK's HTTP seam for one pooled, retrying Session. This is the
        # widest and most stable seam available: it covers every SDK resource,
        # including private methods we would otherwise have to mirror.
        sdk_http.requests = _PooledHttp()
        _client = nylas.Client(api_key=key, api_uri=API_URI, timeout=REQUEST_TIMEOUT)
    return _client


def list_data(response: Any) -> tuple[list[Any], str | None]:
    """Unwrap an SDK ListResponse tuple (data, request_id, next_cursor, headers)."""
    return response[0], response[2] if len(response) > 2 else None


# -------------------------------------------------------------------- 2. cache


def _cache_file(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def cache_load(name: str, ttl: int = CACHE_TTL) -> Any:
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
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file(name).write_text(
            json.dumps({"fetched_at": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def cache_drop(name: str) -> None:
    """Forget one cache entry (used when a cached identifier stops working)."""
    try:
        _cache_file(name).unlink()
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


def invalidate_folders(grant_id: str) -> None:
    """Drop the cached folder list of one grant."""
    cache_drop(f"folders-{grant_id}")


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


def invalidate_calendars(grant_id: str) -> None:
    """Drop the cached calendar list of one grant."""
    cache_drop(f"calendars-{grant_id}")


def resolve_named[T](
    items: Sequence[T],
    wanted: str,
    *,
    kind: str,
    id_of: Callable[[T], str],
    name_of: Callable[[T], str],
    aliases: Mapping[str, Callable[[T], bool]] | None = None,
) -> T:
    """Resolve one item by keyword alias, id, exact name, or unique name substring.

    Matching is case-insensitive, so both "dev" and "DEV" work, and names in any
    script work as-is. `aliases` maps a keyword such as "primary" to a predicate.
    Raises LookupError listing the candidates when nothing matches or the term is
    ambiguous.
    """
    wanted = (wanted or "").strip()
    low = wanted.lower()
    if not low:
        raise LookupError(f"empty {kind} name")

    if aliases:
        matches = aliases.get(low)
        if matches is not None:
            for item in items:
                if matches(item):
                    return item

    for item in items:
        if id_of(item) == wanted:
            return item

    exact = [i for i in items if name_of(i).strip().lower() == low]
    if len(exact) == 1:
        return exact[0]

    hits = [i for i in items if low in name_of(i).lower()]
    if len(hits) == 1:
        return hits[0]

    available = ", ".join(sorted(name_of(i) for i in items))
    if not hits:
        raise LookupError(f"no {kind} matches {wanted!r}; available: {available}")
    found = ", ".join(sorted(name_of(i) for i in hits))
    raise LookupError(f"{kind} {wanted!r} is ambiguous: {found}")


def find_folder(folders: Sequence[Folder], wanted: str) -> Folder:
    """Resolve a folder by id, exact name, or unique name substring."""
    return resolve_named(
        folders,
        wanted,
        kind="folder",
        id_of=lambda f: f["id"],
        name_of=lambda f: f["name"],
    )


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


def find_calendar(calendars: Sequence[Calendar], wanted: str) -> Calendar:
    """Resolve a calendar by id, exact name, or unique name substring.

    The literal "primary" resolves to the account's primary calendar, which is
    also what the API accepts for calendar_id.
    """
    return resolve_named(
        calendars,
        wanted,
        kind="calendar",
        id_of=lambda c: c["id"],
        name_of=lambda c: c["name"],
        aliases={"primary": lambda c: bool(c["is_primary"])},
    )


# ------------------------------------------- 5. concurrency and error text


def gather[T](
    fn: Callable[[T], Any], items: Iterable[T], workers: int = DEFAULT_WORKERS
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


def describe_error(exc: Exception) -> str:
    """One-line description of an exception, with HTTP status and request id."""
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
    if isinstance(exc, NylasApiError):
        if exc.status_code == 404:
            return True
        return "not found" in str(exc).lower()
    return False


# --------------------------------------------------------- 6. display helpers


def dwidth(text: str) -> int:
    """Terminal display width: wide (CJK) characters count as two columns."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def trunc(text: str | None, width: int) -> str:
    """Truncate to a display width, collapsing newlines and adding an ellipsis."""
    text = (text or "").replace("\n", " ").strip()
    if width <= 1:
        return ""
    if dwidth(text) <= width:
        return text
    out, used = "", 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + step > width - 1:
            break
        out += char
        used += step
    return out + "\u2026"


def pad(text: str | None, width: int) -> str:
    """Left-align text inside a display width."""
    text = trunc(text, width)
    return text + " " * max(0, width - dwidth(text))


def terminal_width(default: int = 120) -> int:
    """Terminal width, with a fallback for non-interactive callers."""
    return shutil.get_terminal_size((default, 24)).columns


def fmt_ts(seconds: int | None) -> str:
    """Unix seconds -> local 'MM-DD HH:MM'."""
    if not seconds:
        return "??-?? ??:??"
    try:
        return (
            datetime.fromtimestamp(int(seconds), tz=UTC)
            .astimezone()
            .strftime("%m-%d %H:%M")
        )
    except OverflowError, OSError, ValueError:
        return "??-?? ??:??"


def ts_iso(seconds: int | None) -> str | None:
    """Unix seconds -> local ISO 8601, so callers never do epoch math."""
    if not seconds:
        return None
    try:
        return (
            datetime.fromtimestamp(int(seconds))
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except OverflowError, OSError, ValueError:
        return None


def display_name(people: Sequence[Mapping[str, Any]] | None) -> str:
    """Short label for a table: the display name, else the bare address."""
    if not people:
        return "?"
    first = people[0]
    return str(first.get("name") or first.get("email") or "?").strip()


def days_ago_timestamp(days: int) -> int:
    """Unix timestamp for `days` days before now, for received_after filters."""
    return int(time.time() - max(0, days) * 86400)


def now_iso() -> str:
    """Current local time as ISO 8601, for report headers."""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")
