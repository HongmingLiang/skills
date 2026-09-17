#!/usr/bin/env -S uv run
"""
event_list -- read-only event viewer for the Outlook account, built on the Nylas
Python SDK.

Read-only guarantee: the only API call this script makes is events.list, so it
never creates, updates, deletes, or answers an event.

Usage:
    ./event_list.py                           every calendar, next 365 days
    ./event_list.py --days 30                 shorter window
    ./event_list.py --since 2026-01-01        window start (default: now)
    ./event_list.py --calendars               list calendars and their ids, read nothing
    ./event_list.py -c Work -c Education      only these calendars
    ./event_list.py --include-cjk             also read the Chinese-named calendars
    ./event_list.py --exclude Event           skip calendars whose name matches
    ./event_list.py --expand                  one row per occurrence, not per series
    ./event_list.py --ids                     print event ids for follow-up CLI actions
    ./event_list.py -j                        JSON output for piping
    ./event_list.py --refresh                 ignore the cached calendar lists

Scope: only the Microsoft (Outlook) account is read. Other grants are left
alone unless asked for with -a, and their calendars are out of scope for now.

Calendars with Chinese names are skipped by default, because in this account
those are a localized mirror of the primary calendar and a read-only holiday
feed; the requested workflows live in the English-named calendars. The skip is
always reported, and --include-cjk turns it off.

Recurring events are listed one row per series by default, dated at the series'
nearest occurrence -- its original start can be years outside the window, so the
oldest occurrence says nothing useful. --expand lists every occurrence inside
the window instead.

Notes:
  * Times are shown in local time. start/end in the JSON output are ISO 8601;
    all-day events carry dates instead of timestamps, with the API's exclusive
    end date.
  * Each event is a header line (date, time, calendar) followed by a labelled
    block -- Title, Location, Description, and ID with --ids -- whose values all
    start in the same column. Participants and provider links are in the JSON
    output (they are fetched either way).
  * Per-calendar failures do not hide the other calendars, and the exit code is
    1 when anything failed, so callers can detect partial runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, TypedDict, cast

import config
import nylib

if TYPE_CHECKING:
    from nylas.models.events import Event, ListEventQueryParams

# Fields this view asks the API for. `participants` is required: the SDK model
# marks it non-optional, and omitting it makes dataclasses_json warn on every
# event while decoding. `master_event_id` is how an occurrence of a recurring
# series is recognised -- the API clears `recurrence` on occurrences.
SELECT = (
    "id,grant_id,object,calendar_id,title,when,location,status,busy,"
    "participants,html_link,description,master_event_id"
)

# Table layout is this view's own business, so the widths live here.
DATE_WIDTH = 10  # "09-23 Wed"
TIME_WIDTH = 7  # "14:00" or "all-day"
NAME_WIDTH = 20  # calendar names in the --calendars listing
DETAIL_INDENT = " " * 8  # detail labels sit under the date/time line
# Labels are padded to the widest one plus its colon, so every value starts in
# the same column and wrapped continuations line up with those values.
LABELS = ("Title", "Location", "Description", "ID")
PREFIX = nylib.prefixes(LABELS, DETAIL_INDENT)


class EventRow(TypedDict):
    """One event, flattened to the fields the display and JSON output need."""

    id: str
    calendar: str
    calendar_id: str
    title: str
    all_day: bool
    start: str | None
    end: str | None
    sort_key: int
    location: str | None
    description: str | None
    recurring: bool
    series_key: str
    status: str | None
    busy: bool | None
    participants: list[str]
    html_link: str | None


class AccountReport(TypedDict):
    """One account's calendars worth of events."""

    email: str
    provider: str | None
    calendars: list[str]
    skipped: list[str]
    failures: list[str]
    count: int
    events: list[EventRow]


class CalendarListing(TypedDict):
    """One calendar as reported by --calendars."""

    name: str
    id: str
    is_primary: bool
    read_only: bool
    skipped: str | None


class AccountCalendars(TypedDict):
    """The calendars of one account, as reported by --calendars."""

    email: str
    provider: str | None
    calendars: list[CalendarListing]


@dataclass(frozen=True)
class Window:
    """The half-open [since, until) instant range the API is asked about."""

    since: int
    until: int
    days: int


@dataclass(frozen=True)
class Options:
    """Everything the read path needs, assembled once from the CLI."""

    account: str = config.DEFAULT_CALENDAR_ACCOUNT
    days: int = config.DEFAULT_CALENDAR_DAYS
    since: str | None = None
    calendars: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    include_cjk: bool = False
    expand: bool = False
    refresh: bool = False
    ids: bool = False
    listing: bool = False


def parse_since(value: str | None) -> datetime:
    """Parse --since, accepting a date or a full ISO 8601 timestamp."""
    if not value:
        return datetime.now(UTC).astimezone()
    try:
        if len(value) == 10:  # YYYY-MM-DD -> local midnight
            return datetime.combine(
                date.fromisoformat(value), datetime.min.time()
            ).astimezone()
        return datetime.fromisoformat(value).astimezone()
    except ValueError as exc:
        raise ValueError(
            f"--since must be a date or ISO 8601 timestamp, got {value!r}"
        ) from exc


def build_window(opts: Options) -> Window:
    """Turn --since/--days into the unix timestamp range the API expects."""
    start = int(parse_since(opts.since).timestamp())
    return Window(since=start, until=start + opts.days * 86400, days=opts.days)


def has_cjk(text: str | None) -> bool:
    """True when the text contains CJK (Chinese, Japanese or Korean) ideographs.

    CJK ideographs, including extensions and compatibility forms, are how a
    calendar named in Chinese is recognised without hardcoding any name.
    """
    ranges = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2FA1F))
    return any(any(lo <= ord(ch) <= hi for lo, hi in ranges) for ch in (text or ""))


def skip_reason(calendar: nylib.Calendar, opts: Options) -> str | None:
    """Why this calendar is not read, or None when it is.

    A localized (Chinese-named) calendar is skipped by default, which keeps a
    country-specific holiday feed and a localized mirror of the primary
    calendar out of the way; --include-cjk reads them anyway.
    """
    if not opts.include_cjk and has_cjk(calendar["name"]):
        return "chinese name"
    for pattern in opts.exclude:
        if pattern.strip().lower() in calendar["name"].lower():
            return f"excluded by {pattern!r}"
    return None


def build_query(
    calendar_id: str,
    window: Window,
    expand: bool,
    limit: int,
    page_token: str | None = None,
) -> ListEventQueryParams:
    """Assemble the events.list query parameters for one request.

    The cast is needed because the SDK builds its query TypedDicts with a
    functional form that unpacks ListQueryParams at runtime, so inherited keys
    (limit, page_token, select) are invisible to type checkers.
    """
    query: dict[str, Any] = {
        "calendar_id": calendar_id,
        "start": window.since,
        "end": window.until,
        "limit": limit,
        "select": SELECT,
        "expand_recurring": expand,
    }
    if page_token:
        query["page_token"] = page_token
    return cast("ListEventQueryParams", query)


def fetch_page(
    grant_id: str,
    calendar_id: str,
    window: Window,
    expand: bool,
    limit: int,
    page_token: str | None = None,
) -> tuple[list[Event], str | None]:
    """One page of events. Returns (events, next_cursor)."""
    response = nylib.client().events.list(
        identifier=grant_id,
        query_params=build_query(calendar_id, window, expand, limit, page_token),
    )
    data, next_cursor = nylib.list_data(response)
    return data, next_cursor


def read_events(
    grant: nylib.Grant, calendar: nylib.Calendar, opts: Options, window: Window
) -> list[EventRow]:
    """The events of one calendar inside the window, one page at a time.

    The API is always asked to expand recurring series, because an occurrence is
    the only thing that says when a series actually happens next -- the series
    itself carries its original start, which can be years outside the window.
    --expand then decides what the caller sees: every occurrence, or (by
    default) one row per series, dated at its nearest occurrence.

    Unlike the mail view there is no target count to stop at: the window is the
    bound, so this pages until the API runs out.
    """
    size = config.page_size(grant.get("provider"))
    rows: list[EventRow] = []
    page_token: str | None = None
    while True:
        events, page_token = fetch_page(
            grant["id"], calendar["id"], window, True, size, page_token
        )
        rows.extend(to_row(event, calendar) for event in events)
        if not events or not page_token:
            break
    return rows if opts.expand else collapse_series(rows, int(time.time()))


def collapse_series(rows: list[EventRow], now: int) -> list[EventRow]:
    """Keep one row per recurring series: its nearest occurrence to now.

    The next upcoming occurrence wins; a series with nothing left to come falls
    back to its most recent past occurrence, which only happens when the window
    reaches into the past (--since).
    """
    occurrences: dict[str, list[EventRow]] = {}
    singles: list[EventRow] = []
    for row in rows:
        if row["recurring"]:
            occurrences.setdefault(row["series_key"], []).append(row)
        else:
            singles.append(row)

    picked: list[EventRow] = []
    for series in occurrences.values():
        upcoming = [row for row in series if row["sort_key"] >= now]
        pool = upcoming or series
        picked.append(
            min(pool, key=lambda row: row["sort_key"])
            if upcoming
            else max(pool, key=lambda row: row["sort_key"])
        )
    return singles + picked


def date_sort_key(value: str | None) -> int:
    """Local midnight of an all-day date, used only for ordering."""
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        return 0
    return int(datetime.combine(day, datetime.min.time()).astimezone().timestamp())


def to_row(event: Event, calendar: nylib.Calendar) -> EventRow:
    """Flatten one SDK event into the row the display and JSON use."""
    # The cast is for the SDK's untyped payload: to_dict() yields Any | None
    # values, which pyright refuses to assign into a TypedDict field by field.
    data: dict[str, Any] = event.to_dict()  # pyright: ignore[reportAttributeAccessIssue]
    when = data.get("when") or {}
    all_day = when.get("object") == "datespan"
    master = data.get("master_event_id")
    if all_day:
        start_iso: str | None = when.get("start_date")
        end_iso: str | None = when.get("end_date")
        sort_key = date_sort_key(start_iso)
    else:
        start_iso = nylib.ts_iso(when.get("start_time"))
        end_iso = nylib.ts_iso(when.get("end_time"))
        sort_key = int(when.get("start_time") or 0)
    return cast(
        EventRow,
        {
            "id": data.get("id"),
            "calendar": calendar["name"],
            "calendar_id": calendar["id"],
            "title": data.get("title") or "(no title)",
            "all_day": all_day,
            "start": start_iso,
            "end": end_iso,
            "sort_key": sort_key,
            "location": data.get("location"),
            "description": data.get("description"),
            "recurring": bool(master or data.get("recurrence")),
            "series_key": str(master or data.get("id")),
            "status": data.get("status"),
            "busy": data.get("busy"),
            "participants": [
                str(p.get("email") or p.get("name") or "?")
                for p in (data.get("participants") or [])
            ],
            "html_link": data.get("html_link"),
        },
    )


def day_label(row: EventRow) -> str:
    """Date column: 'MM-DD Ddd' from the row's own start representation."""
    if row["all_day"]:
        try:
            parsed = date.fromisoformat(str(row["start"]))
        except ValueError:
            return "??-?? ???"
    else:
        parsed = datetime.fromtimestamp(row["sort_key"], tz=UTC).astimezone().date()
    return parsed.strftime("%m-%d %a")


def time_label(row: EventRow) -> str:
    """Time column: the local start time, or 'all-day' for date spans."""
    if row["all_day"]:
        return "all-day"
    return (
        datetime.fromtimestamp(row["sort_key"], tz=UTC).astimezone().strftime("%H:%M")
    )


def select_calendars(
    grant: nylib.Grant, opts: Options
) -> tuple[list[nylib.Calendar], list[str]]:
    """Decide which calendars of one account to read.

    Returns (calendars_to_read, skip_notes). Raises LookupError when a requested
    calendar name cannot be resolved in this account.
    """
    calendars = nylib.load_calendars(grant["id"], refresh=opts.refresh)

    if opts.calendars:
        # "primary" is an alias the API accepts for calendar_id, so accept it too.
        wanted = [
            nylib.resolve_named(
                calendars,
                name,
                kind="calendar",
                aliases={"primary": lambda c: bool(c["is_primary"])},
            )
            for name in opts.calendars
        ]
        return wanted, []

    keep: list[nylib.Calendar] = []
    skipped: list[str] = []
    for calendar in calendars:
        reason = skip_reason(calendar, opts)
        if reason:
            skipped.append(f"{calendar['name']} ({reason})")
        else:
            keep.append(calendar)
    return keep, skipped


def read_all(
    targets: list[nylib.Grant],
    opts: Options,
    window: Window,
    workers: int,
    errors: list[dict[str, str]],
) -> list[AccountReport]:
    """Read every selected calendar of every account, isolating failures.

    Calendars are planned first -- that only touches cached identifier data --
    and then fetched as one flat task list, so --workers bounds the requests in
    flight no matter how the calendars are spread over accounts. A calendar
    that fails never hides its siblings, and a failing account never cancels
    the others.
    """
    plans, plan_failures = nylib.gather(
        lambda grant: select_calendars(grant, opts), targets, workers=workers
    )
    for grant, failure in zip(targets, plan_failures):
        if failure is not None:
            message = nylib.describe_error(failure)
            errors.append({"account": grant["email"], "error": message})
            print(f"! {grant['email']}: {message}", file=sys.stderr)

    # One flat task list, remembering which account each task came from, so the
    # results can be regrouped without nesting a second thread pool.
    tasks: list[tuple[nylib.Grant, nylib.Calendar]] = []
    owner: list[int] = []
    for index, plan in enumerate(plans):
        if plan is None:
            continue
        for calendar in plan[0]:
            tasks.append((targets[index], calendar))
            owner.append(index)

    results, failures = nylib.gather(
        lambda task: read_events(task[0], task[1], opts, window), tasks, workers=workers
    )

    events: list[list[EventRow]] = [[] for _ in targets]
    calendar_failures: list[list[str]] = [[] for _ in targets]
    for index, (task, rows, failure) in enumerate(zip(tasks, results, failures)):
        account = owner[index]
        if failure is None:
            events[account].extend(rows)
        else:
            calendar_failures[account].append(
                f"{task[1]['name']}: {nylib.describe_error(failure)}"
            )

    reports: list[AccountReport] = []
    for index, grant in enumerate(targets):
        plan = plans[index]
        if plan is None:
            continue
        calendars, skipped = plan
        events[index].sort(key=lambda row: (row["sort_key"], row["title"]))
        reports.append(
            AccountReport(
                email=grant["email"],
                provider=grant.get("provider"),
                calendars=[calendar["name"] for calendar in calendars],
                skipped=skipped,
                failures=calendar_failures[index],
                count=len(events[index]),
                events=events[index],
            )
        )
    return reports


def list_calendars(grant: nylib.Grant, opts: Options) -> AccountCalendars:
    """--calendars: everything that exists, annotated with what a run would skip."""
    calendars = nylib.load_calendars(grant["id"], refresh=opts.refresh)
    return {
        "email": grant["email"],
        "provider": grant.get("provider"),
        "calendars": [
            CalendarListing(
                name=calendar["name"],
                id=calendar["id"],
                is_primary=bool(calendar["is_primary"]),
                read_only=bool(calendar["read_only"]),
                skipped=skip_reason(calendar, opts),
            )
            for calendar in calendars
        ],
    }


def print_calendars(account: AccountCalendars) -> None:
    """Render one account's calendar list."""
    print(f"\n=== {account['email']} [{account['provider']}] ===")
    for calendar in account["calendars"]:
        flags = ("primary " if calendar["is_primary"] else "        ") + (
            "read-only" if calendar["read_only"] else "writable "
        )
        note = f"   (skipped: {calendar['skipped']})" if calendar["skipped"] else ""
        # A name is not truncated: it is also what -c accepts as input.
        print(
            f"  {flags}  {nylib.pad(calendar['name'], NAME_WIDTH, truncate=False)}"
            f" {calendar['id']}{note}"
        )


def plural(count: int, noun: str) -> str:
    """'1 event' vs '3 events', so the header reads as English."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def public_row(row: EventRow) -> dict[str, Any]:
    """The JSON form of a row. sort_key and series_key are internal aids."""
    return {
        key: value
        for key, value in row.items()
        if key not in ("sort_key", "series_key")
    }


def print_account(report: AccountReport, opts: Options, term_width: int) -> None:
    """Render one account's events as a plain-text block."""
    print(
        f"\n=== {report['email']} [{report['provider']}]"
        f" - {plural(report['count'], 'event')} ==="
    )
    if report["skipped"]:
        print(
            f"  (skipped calendars: {', '.join(report['skipped'])}; use --include-cjk)"
        )
    if not report["events"]:
        print("  (no events)")
        return

    for row in report["events"]:
        print()  # a blank line keeps multi-line events apart
        status = "" if row["status"] in (None, "confirmed") else f"({row['status']}) "
        # A collapsed row stands for a whole series, so say so after the title.
        suffix = "  (series)" if row["recurring"] and not opts.expand else ""
        print(
            f" {nylib.pad(day_label(row), DATE_WIDTH)}"
            f" {nylib.pad(time_label(row), TIME_WIDTH)} {row['calendar']}"
        )
        blocks = [
            nylib.hanging(PREFIX["Title"], status + row["title"] + suffix, term_width),
            nylib.hanging(PREFIX["Location"], row["location"], term_width),
            nylib.hanging(
                PREFIX["Description"],
                nylib.html_to_text(row["description"]),
                term_width,
            ),
        ]
        if opts.ids:
            blocks.append(nylib.hanging(PREFIX["ID"], row["id"], term_width))
        for block in blocks:
            for line in block:
                print(line)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse and validate the command line."""
    parser = argparse.ArgumentParser(
        prog="event_list",
        description="Read-only event viewer (never creates, updates, or deletes events).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-a",
        "--account",
        default=config.DEFAULT_CALENDAR_ACCOUNT,
        help=(
            "account selector: microsoft (default, the Outlook account) | "
            "all | outlook | gmail | pku | email"
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=config.DEFAULT_CALENDAR_DAYS,
        help=f"window length in days (default {config.DEFAULT_CALENDAR_DAYS})",
    )
    parser.add_argument(
        "--since", help="window start: YYYY-MM-DD or ISO 8601 (default: now)"
    )
    parser.add_argument(
        "-c",
        "--calendar",
        action="append",
        dest="calendars",
        default=[],
        metavar="NAME",
        help="read only this calendar (repeatable; 'primary' works)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NAME",
        help="skip calendars whose name contains NAME (repeatable)",
    )
    parser.add_argument(
        "--include-cjk",
        action="store_true",
        help="also read calendars with Chinese names (skipped by default)",
    )
    parser.add_argument(
        "--expand",
        action="store_true",
        help="list every occurrence of a recurring event instead of one row per series",
    )
    parser.add_argument(
        "--calendars",
        action="store_true",
        dest="listing",
        help="list calendars with their ids and read nothing",
    )
    parser.add_argument("--ids", action="store_true", help="print event ids")
    parser.add_argument("-j", "--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="rebuild the cached account and calendar lists",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=config.DEFAULT_WORKERS,
        help=f"concurrent calendars (default {config.DEFAULT_WORKERS})",
    )
    args = parser.parse_args(argv)

    if args.days < 0:
        parser.error("--days must not be negative")
    if args.workers < 1:
        parser.error("--workers must be positive")
    if args.since:
        try:
            parse_since(args.since)
        except ValueError as exc:
            parser.error(str(exc))
    return args


def run(args: argparse.Namespace) -> int:
    """Read every selected calendar and report the result."""
    nylib.client()  # fail fast: one clean config error instead of one per account
    opts = Options(
        account=args.account,
        days=args.days,
        since=args.since,
        calendars=list(args.calendars),
        exclude=list(args.exclude),
        include_cjk=args.include_cjk,
        expand=args.expand,
        refresh=args.refresh,
        ids=args.ids,
        listing=args.listing,
    )
    window = build_window(opts)

    grants = nylib.load_grants(refresh=opts.refresh)
    targets, unknown = nylib.select_grants(grants, opts.account)

    errors: list[dict[str, str]] = [
        {"account": term, "error": "no account matches this selector"}
        for term in unknown
    ]
    for item in errors:
        print(f"! {item['account']}: {item['error']}", file=sys.stderr)
    if not targets and not errors:
        print("! no accounts available for this API key", file=sys.stderr)

    if opts.listing:
        return report_calendars(targets, opts, errors, as_json=args.json)

    reports = read_all(targets, opts, window, args.workers, errors)
    for report in reports:
        for failure in report["failures"]:
            errors.append({"account": report["email"], "error": failure})
            print(f"! {report['email']}: {failure}", file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {
                    "generated_at": nylib.now_iso(),
                    "window": {
                        "since": nylib.ts_iso(window.since),
                        "until": nylib.ts_iso(window.until),
                        "days": window.days,
                    },
                    "accounts": [
                        {
                            **report,
                            "events": [public_row(row) for row in report["events"]],
                        }
                        for report in reports
                    ],
                    "errors": errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if not reports:
            print("! nothing to show", file=sys.stderr)
        width = nylib.terminal_width()
        for report in reports:
            print_account(report, opts, width)

    return 1 if errors else 0


def report_calendars(
    targets: list[nylib.Grant],
    opts: Options,
    errors: list[dict[str, str]],
    as_json: bool,
) -> int:
    """--calendars: list what exists, including what a read would skip."""
    results, failures = nylib.gather(lambda grant: list_calendars(grant, opts), targets)
    accounts: list[Any] = []
    for grant, listing, failure in zip(targets, results, failures):
        if failure is not None:
            message = nylib.describe_error(failure)
            errors.append({"account": grant["email"], "error": message})
            print(f"! {grant['email']}: {message}", file=sys.stderr)
            continue
        accounts.append(listing)

    if as_json:
        print(
            json.dumps(
                {
                    "generated_at": nylib.now_iso(),
                    "accounts": accounts,
                    "errors": errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for account in accounts:
            print_calendars(account)
    return 1 if errors else 0


def main() -> int:
    """Entry point: turn a missing API key into a clean message and exit code."""
    args = parse_args()
    try:
        return run(args)
    except nylib.ConfigError as exc:
        print(f"! {exc}".replace("\n", "\n  "), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
