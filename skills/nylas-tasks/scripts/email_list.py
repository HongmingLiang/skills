#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "nylas>=6.17.0",
#     "requests>=2.32",
# ]
# ///
"""
email_list -- read-only mail viewer for every account, built on the Nylas Python SDK.

Read-only guarantee: the only API call this script makes is messages.list, so it
never sends, deletes, moves, or marks anything.

Usage (from the skill directory):
    uv run scripts/email_list.py [flags]
    (no flags)                every account, newest 10 in the inbox
    -n 20                     newest 20 per account
    -u                        unread only
    -a google                 one account: a provider, an address, a grant id
    -f dev                    one folder, by name
    -a google --all-folders   every folder, subfolders included
    --days 3                  only mail received in the last 3 days
    --all --max 50            paginate, at most 50 per account
    --folders                 list folders with their unread counts, read nothing
    --ids                     bare message ids, one per line (for piping)
    -j                        JSON output for piping
    --refresh                 ignore the cached account/folder lists

Output legend:  U unread   S starred, followed by sender and subject.

See also: `email_read.py`, next to this script, reads one or more messages in
full and takes its ids from a listing:
    uv run scripts/email_list.py -j | uv run scripts/email_read.py -

Notes:
  * Folder names match case-insensitively, and a unique substring is enough.
  * A folder missing in one account skips that account only, and the exit code
    is 1 when any account failed, so callers can detect partial runs.
  * Cached ids self-heal: a stale folder id triggers one refresh and one retry.
  * Microsoft answers about 0.1-0.2s per message, so pages there are capped at
    50 and larger requests paginate; Google and IMAP are roughly ten times
    faster.
"""

import argparse
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, cast

import config
import nylib

if TYPE_CHECKING:
    from nylas.models.messages import ListMessagesQueryParams, Message

# Fields this view asks the API for. Everything else (notably the HTML body)
# stays on the server, which is where most of the payload saving comes from.
# The projection belongs to the view that needs it, not to the shared config.
SELECT = "id,grant_id,object,thread_id,subject,from,date,unread,starred,folders"

# Table layout is this view's own business, so the widths live here rather than
# in config.py.
SENDER_WIDTH = 26
STAMP_WIDTH = 11
FOLDER_WIDTH = 24


class MessageRow(TypedDict):
    """One message, flattened to the fields the display and JSON output need."""

    id: str
    thread_id: str | None
    date: int | None
    date_iso: str | None
    unread: bool
    starred: bool
    sender_name: str
    sender_email: str | None
    subject: str
    folder_names: list[str]


class AccountReport(TypedDict):
    """One mailbox worth of results, or the reason there are none."""

    email: str
    provider: str | None
    grant_status: str | None
    folder: str
    folder_id: str | None
    count: int
    messages: list[MessageRow]


class FolderListing(TypedDict):
    """One folder as --folders shows it: name, counts, id, and whether it is the
    inbox a run without -f would read."""

    name: str
    id: str
    is_inbox: bool
    system_folder: bool
    total_count: int
    unread_count: int


class AccountFolders(TypedDict):
    """One account's folder list, for --folders."""

    email: str
    provider: str | None
    folders: list[FolderListing]


@dataclass(frozen=True)
class Filters:
    """Server-side filters; only the ones the user asked for are sent."""

    unread: bool = False
    starred: bool = False
    days: int | None = None


@dataclass(frozen=True)
class Options:
    """Everything the read path needs, assembled once from the CLI."""

    account: str = "all"
    target: int = config.DEFAULT_MESSAGE_LIMIT
    filters: Filters = Filters()
    folder: str | None = None
    all_folders: bool = False
    folders: bool = False
    refresh: bool = False
    ids: bool = False


def build_query(
    folder_id: str | None, limit: int, filters: Filters, page_token: str | None = None
) -> ListMessagesQueryParams:
    """Assemble the messages.list query parameters for one request.

    The cast is needed because the SDK builds ListMessagesQueryParams with a
    functional TypedDict that unpacks ListQueryParams at runtime, so the
    inherited keys (limit, page_token) are invisible to type checkers.
    """
    query: dict[str, Any] = {"limit": limit, "select": SELECT}
    if folder_id:
        query["in"] = folder_id
    if filters.unread:
        query["unread"] = True
    if filters.starred:
        query["starred"] = True
    if filters.days is not None:
        query["received_after"] = int(time.time()) - filters.days * 86400
    if page_token:
        query["page_token"] = page_token
    return cast("ListMessagesQueryParams", query)


def fetch_page(
    grant_id: str,
    folder_id: str | None,
    limit: int,
    filters: Filters,
    page_token: str | None = None,
) -> tuple[list[Message], str | None]:
    """One page of messages. Returns (messages, next_cursor)."""
    response = nylib.client().messages.list(
        identifier=grant_id,
        query_params=build_query(folder_id, limit, filters, page_token),
    )
    data, next_cursor = nylib.list_data(response)
    return data, next_cursor


def to_row(message: Message) -> MessageRow:
    """Flatten one SDK message into the row the display and JSON use."""
    # to_dict() is generated by the dataclasses_json decorator on the SDK model,
    # so it exists at runtime but is invisible to type checkers.
    data: dict[str, Any] = message.to_dict()  # pyright: ignore[reportAttributeAccessIssue]
    # The first sender, or an empty mapping when the API omits the field.
    sender = (data.get("from") or [{}])[0]
    # The cast is for the SDK's untyped payload: to_dict() yields Any | None
    # values, which pyright refuses to assign into a TypedDict field by field.
    return cast(
        MessageRow,
        {
            "id": data.get("id"),
            "thread_id": data.get("thread_id"),
            "date": data.get("date"),
            "date_iso": nylib.ts_iso(data.get("date")),
            "unread": bool(data.get("unread")),
            "starred": bool(data.get("starred")),
            "sender_name": str(
                sender.get("name") or sender.get("email") or "?"
            ).strip(),
            "sender_email": sender.get("email"),
            "subject": data.get("subject") or "",
            "folder_names": data.get("folders") or [],
        },
    )


def resolve_folder(
    grant: nylib.Grant, opts: Options, refresh: bool = False
) -> tuple[nylib.Folder | None, str, dict[str, str]]:
    """Decide which folder to read for one account.

    Returns (folder_or_None, label, folder_names_by_id). Raises LookupError when
    a requested folder name cannot be resolved in this account. `refresh` forces
    a fresh folder list, which is how a stale cached id is recovered from.
    """
    folders = nylib.load_folders(grant["id"], refresh=opts.refresh or refresh)
    by_id = {f["id"]: f["name"] for f in folders}

    if opts.all_folders:
        # No folder filter, but the id -> name map is still needed to label
        # each message with the folders it lives in.
        return None, "all folders", by_id

    if opts.folder:
        folder = nylib.resolve_named(folders, opts.folder, kind="folder")
        return folder, folder["name"], by_id

    inbox = nylib.find_inbox(folders)
    if inbox is None:
        return None, "all folders (no inbox detected)", by_id
    return inbox, inbox["name"], by_id


def read_messages(
    grant: nylib.Grant, folder: nylib.Folder | None, opts: Options
) -> list[MessageRow]:
    """Fetch up to the requested number of messages, paginating as needed.

    Both -n and --all end up here: the target is the requested limit either way.
    Pages are sized per provider (see nylib.page_limit) because Microsoft
    answers a large page far more slowly than Google or IMAP.
    """
    size = config.page_size(grant.get("provider"))
    rows: list[MessageRow] = []
    page_token: str | None = None
    while len(rows) < opts.target:
        want = min(size, opts.target - len(rows))
        messages, page_token = fetch_page(
            grant["id"],
            folder["id"] if folder else None,
            want,
            opts.filters,
            page_token,
        )
        rows.extend(to_row(message) for message in messages)
        if not messages or not page_token:
            break
    return rows


def fetch_account(grant: nylib.Grant, opts: Options) -> AccountReport:
    """Read one mailbox. Raises on failure; the caller isolates accounts."""
    folder, label, folders_by_id = resolve_folder(grant, opts)

    try:
        rows = read_messages(grant, folder, opts)
    except Exception as exc:  # retried below only when a cached folder id went stale
        if not (folder and opts.folder and nylib.is_not_found(exc)):
            raise
        # The cached folder id went stale (folder renamed or recreated): reread
        # the folder list and retry exactly once.
        folder, label, folders_by_id = resolve_folder(grant, opts, refresh=True)
        rows = read_messages(grant, folder, opts)

    rows.sort(key=lambda row: row["date"] or 0, reverse=True)
    for row in rows:
        row["folder_names"] = [
            folders_by_id.get(fid, fid) for fid in row["folder_names"]
        ]
    return {
        "email": grant["email"],
        "provider": grant.get("provider"),
        "grant_status": grant.get("grant_status"),
        "folder": label,
        "folder_id": folder["id"] if folder else None,
        "count": len(rows),
        "messages": rows,
    }


def print_account(report: AccountReport, opts: Options, term_width: int) -> None:
    """Render one mailbox: bare ids with --ids, a plain-text table otherwise."""
    if opts.ids:
        for row in report["messages"]:
            print(row["id"])
        return
    print(
        f"\n=== {report['email']} [{report['provider']}]"
        f" - folder: {report['folder']} - {report['count']} messages ==="
    )
    if not report["messages"]:
        print("  (no messages)")
        return

    for row in report["messages"]:
        mark = ("U" if row["unread"] else "-") + ("S" if row["starred"] else "-")
        tags = ""
        if opts.all_folders and row["folder_names"]:
            tags = "[" + ",".join(row["folder_names"]) + "] "
        used = STAMP_WIDTH + len(mark) + SENDER_WIDTH + 4
        subject = nylib.trunc(
            tags + (row["subject"] or "(no subject)"), max(20, term_width - used)
        )
        stamp = nylib.local_time(row["date"])
        date_label = stamp.strftime("%m-%d %H:%M") if stamp else "??-?? ??:??"
        print(
            f" {nylib.pad(date_label, STAMP_WIDTH)}"
            f" {mark}  {nylib.pad(row['sender_name'], SENDER_WIDTH)} {subject}"
        )


def list_folders(grant: nylib.Grant) -> AccountFolders:
    """--folders: what exists and what is unread, without reading a message.

    The list is read live instead of from the cache: its unread counts are the
    answer to "is anything unread?", and a cached answer would be a stale one.
    Still one folders.list per account, and it refreshes the same cache that -f
    resolves against.
    """
    folders = nylib.load_folders(grant["id"], refresh=True)
    inbox = nylib.find_inbox(folders)
    return {
        "email": grant["email"],
        "provider": grant.get("provider"),
        "folders": [
            FolderListing(
                name=folder["name"],
                id=folder["id"],
                is_inbox=inbox is not None and folder["id"] == inbox["id"],
                system_folder=bool(folder["system_folder"]),
                total_count=int(folder["total_count"] or 0),
                unread_count=int(folder["unread_count"] or 0),
            )
            for folder in folders
        ],
    }


def print_folders(account: AccountFolders, opts: Options) -> None:
    """Render one account's folders: the counts, then the id -f accepts."""
    if opts.ids:
        for folder in account["folders"]:
            print(folder["id"])
        return
    print(f"\n=== {account['email']} [{account['provider']}] ===")
    if not account["folders"]:
        print("  (no folders)")
        return
    for folder in account["folders"]:
        mark = "inbox" if folder["is_inbox"] else "     "
        # A name is not truncated: it is also what -f accepts as input.
        print(
            f"  {mark}  {nylib.pad(folder['name'], FOLDER_WIDTH, truncate=False)}"
            f" {folder['total_count']:>4} total"
            f" {folder['unread_count']:>4} unread  {folder['id']}"
        )


def report_folders(
    targets: list[nylib.Grant],
    opts: Options,
    errors: list[dict[str, str]],
    as_json: bool,
) -> int:
    """--folders: list what exists, including the counts, and read no messages."""
    accounts = nylib.collect(targets, list_folders, errors)

    if as_json:
        nylib.print_json_report(accounts, errors)
    else:
        if not accounts:
            print("! nothing to show", file=sys.stderr)
        for account in accounts:
            print_folders(account, opts)

    return 1 if errors else 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse and validate the command line."""
    parser = argparse.ArgumentParser(
        prog="email_list",
        description="Read-only mail viewer (never sends, deletes, moves, or marks mail).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-a",
        "--account",
        default="all",
        help="account selector: all (default) | provider | address | grant id",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        help=f"messages per account (default {config.DEFAULT_MESSAGE_LIMIT})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="paginate up to --max (default 200)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=config.DEFAULT_MESSAGE_MAX,
        help=f"upper bound per account when --all is used (default {config.DEFAULT_MESSAGE_MAX})",
    )
    parser.add_argument("-u", "--unread", action="store_true", help="unread only")
    parser.add_argument("-s", "--starred", action="store_true", help="starred only")
    parser.add_argument(
        "-f", "--folder", help="folder name (case-insensitive, substring allowed)"
    )
    parser.add_argument(
        "--all-folders",
        action="store_true",
        help="skip the folder filter and search every folder",
    )
    parser.add_argument(
        "--folders",
        action="store_true",
        help="list folders with their counts and ids, and read no messages",
    )
    parser.add_argument(
        "--days",
        type=int,
        help="only mail received in the last N days (server-side filter)",
    )
    parser.add_argument(
        "--ids",
        action="store_true",
        help="print ids only, one per line (folders with --folders)",
    )
    parser.add_argument("-j", "--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="rebuild the cached account and folder lists",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=config.DEFAULT_WORKERS,
        help=f"concurrent accounts (default {config.DEFAULT_WORKERS})",
    )
    args = parser.parse_args(argv)

    if args.all and args.limit is not None:
        parser.error("--all cannot be combined with -n/--limit (use --max)")
    if args.all_folders and args.folder:
        parser.error("--all-folders cannot be combined with -f/--folder")
    if args.limit is not None and args.limit < 1:
        parser.error("-n/--limit must be positive")
    if args.all and args.max < 1:
        parser.error("--max must be positive")
    if args.days is not None and args.days < 0:
        parser.error("--days must not be negative")
    if args.workers < 1:
        parser.error("--workers must be positive")
    return args


def run(args: argparse.Namespace) -> int:
    """Read every selected account and report the result."""
    nylib.client()  # fail fast: one clean config error instead of one per account
    opts = Options(
        account=args.account,
        target=args.max if args.all else (args.limit or config.DEFAULT_MESSAGE_LIMIT),
        filters=Filters(unread=args.unread, starred=args.starred, days=args.days),
        folder=args.folder,
        all_folders=args.all_folders,
        folders=args.folders,
        refresh=args.refresh,
        ids=args.ids,
    )

    targets, unknown = nylib.select_targets(opts.account, opts.refresh)
    errors: list[dict[str, str]] = [
        {"account": term, "error": nylib.NO_ACCOUNT} for term in unknown
    ]
    if not targets and not errors:
        print("! no accounts available for this API key", file=sys.stderr)

    if opts.folders:
        return report_folders(targets, opts, errors, as_json=args.json)

    def read_one_account(grant: nylib.Grant) -> AccountReport:
        """Fetch one account, with a timing line on each side of the wait."""
        started = nylib.progress_start(grant["email"])
        report = fetch_account(grant, opts)
        nylib.progress_done(
            grant["email"], started, nylib.count_of(len(report["messages"]), "message")
        )
        return report

    accounts = nylib.collect(targets, read_one_account, errors, workers=args.workers)

    if args.json:
        nylib.print_json_report(accounts, errors)
    else:
        if not accounts:
            print("! nothing to show", file=sys.stderr)
        width = nylib.terminal_width()
        for report in accounts:
            print_account(report, opts, width)

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
