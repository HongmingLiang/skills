"""
nylib -- shared helpers for the read-only Nylas commands in this package.

This module is read-only by construction: it never sends, deletes, moves, or
marks anything. It only

  1. builds a Nylas Python SDK client,
  2. resolves identifiers (grants, folders, calendars) behind a small TTL cache,
  3. runs per-account work concurrently with error isolation,
  4. renders plain text that aligns correctly for wide (CJK) glyphs.

It is imported by the command modules (agent_nylas.mail today, more to come
such as agent_nylas.cal). The `nylas` dependency lives in the project's
pyproject.toml, which also exposes each command as a console script, so this
file carries no dependency block of its own.

Credential: the API key is read from the environment variable named in
API_KEY_ENV. That is the same value the `nylas` CLI stores, so the CLI and
these commands stay in sync with a single variable.
"""

import json
import os
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nylas
from nylas.models.errors import NylasApiError

# --------------------------------------------------------------- configuration

API_KEY_ENV = "NYLAS_FILE_STORE_PASSPHRASE"
API_URI = os.environ.get("NYLAS_API_URI", "https://api.us.nylas.com")
REQUEST_TIMEOUT = 30  # seconds

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


def page_limit(provider):
    """Safe page size for one provider."""
    return PAGE_LIMIT_BY_PROVIDER.get((provider or "").lower(), PAGE_LIMIT)


DEFAULT_WORKERS = 4


# ------------------------------------------------------------------- 1. client

_client = None


def client():
    """Return the process-wide Nylas SDK client, creating it on first use."""
    global _client
    if _client is None:
        key = os.environ.get(API_KEY_ENV)
        if not key:
            sys.exit(
                f"error: {API_KEY_ENV} is not set.\n"
                f"hint: export {API_KEY_ENV}=$(nylas auth token)"
            )
        _client = nylas.Client(api_key=key, api_uri=API_URI, timeout=REQUEST_TIMEOUT)
    return _client


def list_data(response):
    """Unwrap an SDK ListResponse tuple (data, request_id, next_cursor, headers)."""
    return response[0], response[2] if len(response) > 2 else None


# -------------------------------------------------------------------- 2. cache


def _cache_file(name):
    return CACHE_DIR / f"{name}.json"


def cache_load(name, ttl=CACHE_TTL):
    """Return cached data if present and younger than ttl, else None."""
    try:
        blob = json.loads(_cache_file(name).read_text(encoding="utf-8"))
        if time.time() - float(blob["fetched_at"]) <= ttl:
            return blob["data"]
    except OSError, ValueError, KeyError, TypeError:
        pass
    return None


def cache_save(name, data):
    """Best-effort cache write; a cache problem must never break a script."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file(name).write_text(
            json.dumps({"fetched_at": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def cache_drop(name):
    """Forget one cache entry (used when a cached identifier stops working)."""
    try:
        _cache_file(name).unlink()
    except OSError:
        pass


# --------------------------------------------------- 3. identity: accounts


def load_grants(refresh=False):
    """All grants reachable with the API key.

    Returns a list of {id, email, provider, grant_status}, cached because the
    list is stable and the call costs a full round trip.
    """
    if not refresh:
        cached = cache_load("grants")
        if cached is not None:
            return cached

    data, _ = list_data(client().grants.list())
    grants = [
        {
            "id": g.id,
            "email": g.email,
            "provider": g.provider,
            "grant_status": g.grant_status,
        }
        for g in data
    ]
    cache_save("grants", grants)
    return grants


def select_grants(grants, selector="all"):
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

    matched, missing = [], []
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


# ------------------------------------------------ 4. identity: folders


def load_folders(grant_id, refresh=False):
    """Folders/labels of one grant, cached.

    Keys: id, name, parent_id, system_folder, total_count, unread_count,
    attributes. `attributes` carries the provider-agnostic markers such as
    "\\Inbox", which is how find_inbox() stays provider neutral.
    """
    name = f"folders-{grant_id}"
    if not refresh:
        cached = cache_load(name)
        if cached is not None:
            return cached

    data, _ = list_data(client().folders.list(identifier=grant_id))
    folders = [
        {
            "id": f.id,
            "name": f.name,
            "parent_id": f.parent_id,
            "system_folder": f.system_folder,
            "total_count": f.total_count,
            "unread_count": f.unread_count,
            "attributes": f.attributes,
        }
        for f in data
    ]
    cache_save(name, folders)
    return folders


def invalidate_folders(grant_id):
    """Drop the cached folder list of one grant."""
    cache_drop(f"folders-{grant_id}")


def find_inbox(folders):
    """Return the inbox folder, or None.

    Primary probe is the "\\Inbox" attribute, which every provider exposes
    (Microsoft, Google, IMAP) and which survives localized folder names.
    """
    for folder in folders:
        if "inbox" in (folder.get("attributes") or "").lower():
            return folder
    for folder in folders:  # fallback: literal name
        if folder["name"].strip().lower() in ("inbox", "in"):
            return folder
    return None


def find_folder(folders, wanted):
    """Resolve a folder by id, exact name, or unique name substring.

    Matching is case-insensitive, so both "dev" and "DEV" work, as do folder
    names written in any script. Raises LookupError with the candidates when
    nothing matches or the term is ambiguous.
    """
    wanted = (wanted or "").strip()
    for folder in folders:
        if folder["id"] == wanted:
            return folder

    low = wanted.lower()
    exact = [f for f in folders if f["name"].strip().lower() == low]
    if len(exact) == 1:
        return exact[0]

    hits = [f for f in folders if low in f["name"].lower()]
    if len(hits) == 1:
        return hits[0]

    names = ", ".join(sorted(f["name"] for f in folders))
    if not hits:
        raise LookupError(f"no folder matches {wanted!r}; available: {names}")
    found = ", ".join(sorted(f["name"] for f in hits))
    raise LookupError(f"folder {wanted!r} is ambiguous: {found}")


# ---------------------------------------------- 5. identity: calendars


def load_calendars(grant_id, refresh=False):
    """Calendars of one grant, cached.

    Keys: id, name, is_primary, read_only, timezone, description, hex_color.
    """
    name = f"calendars-{grant_id}"
    if not refresh:
        cached = cache_load(name)
        if cached is not None:
            return cached

    data, _ = list_data(client().calendars.list(identifier=grant_id))
    calendars = [
        {
            "id": c.id,
            "name": c.name,
            "is_primary": c.is_primary,
            "read_only": c.read_only,
            "timezone": c.timezone,
            "description": c.description,
            "hex_color": c.hex_color,
        }
        for c in data
    ]
    cache_save(name, calendars)
    return calendars


def invalidate_calendars(grant_id):
    """Drop the cached calendar list of one grant."""
    cache_drop(f"calendars-{grant_id}")


def find_calendar(calendars, wanted):
    """Resolve a calendar by id, exact name, or unique name substring.

    The literal "primary" resolves to the account's primary calendar, which is
    also what the API accepts for calendar_id.
    """
    wanted = (wanted or "").strip()
    if wanted.lower() == "primary":
        for cal in calendars:
            if cal.get("is_primary"):
                return cal

    for cal in calendars:
        if cal["id"] == wanted:
            return cal

    low = wanted.lower()
    exact = [c for c in calendars if c["name"].strip().lower() == low]
    if len(exact) == 1:
        return exact[0]

    hits = [c for c in calendars if low in c["name"].lower()]
    if len(hits) == 1:
        return hits[0]

    names = ", ".join(sorted(c["name"] for c in calendars))
    if not hits:
        raise LookupError(f"no calendar matches {wanted!r}; available: {names}")
    found = ", ".join(sorted(c["name"] for c in hits))
    raise LookupError(f"calendar {wanted!r} is ambiguous: {found}")


# ------------------------------------------- 6. concurrency and error text


def gather(fn, items, workers=DEFAULT_WORKERS):
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


def describe_error(exc):
    """One-line description of an exception, with HTTP status and request id."""
    if isinstance(exc, LookupError):
        return str(exc)  # our own resolution failures are already readable
    if isinstance(exc, NylasApiError):
        parts = []
        if exc.status_code:
            parts.append(f"HTTP {exc.status_code}")
        if getattr(exc, "type", None):
            parts.append(str(exc.type))
        message = str(exc) or exc.__class__.__name__
        parts.append(message)
        if exc.request_id:
            parts.append(f"request_id={exc.request_id}")
        return " | ".join(parts)
    return f"{exc.__class__.__name__}: {exc}"


def is_not_found(exc):
    """True when the API said the object is gone, i.e. a cached id went stale."""
    if isinstance(exc, NylasApiError):
        if exc.status_code == 404:
            return True
        return "not found" in str(exc).lower()
    return False


# --------------------------------------------------------- 7. display helpers


def dwidth(text):
    """Terminal display width: wide (CJK) characters count as two columns."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def trunc(text, width):
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


def pad(text, width):
    """Left-align text inside a display width."""
    text = trunc(text, width)
    return text + " " * max(0, width - dwidth(text))


def fmt_ts(seconds):
    """Unix seconds -> local 'MM-DD HH:MM'."""
    if not seconds:
        return "??-?? ??:??"
    try:
        return datetime.fromtimestamp(int(seconds)).strftime("%m-%d %H:%M")
    except OverflowError, OSError, ValueError:
        return "??-?? ??:??"


def ts_iso(seconds):
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


def addr(people):
    """Format an SDK address list ([{name, email}, ...]) as 'Name <email>'."""
    if not people:
        return "?"
    first = people[0]
    name = (first.get("name") or "").strip()
    email = (first.get("email") or "").strip()
    if name and email:
        return f"{name} <{email}>"
    return name or email or "?"


def display_name(people):
    """Short label for a table: the display name, else the bare address."""
    if not people:
        return "?"
    first = people[0]
    return (first.get("name") or first.get("email") or "?").strip()


def days_ago_timestamp(days):
    """Unix timestamp for `days` days before now, for received_after filters."""
    return int(time.time() - max(0, days) * 86400)


def now_iso():
    """Current local time as ISO 8601, for report headers."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
