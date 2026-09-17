#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "nylas>=6.17.0",
# ]
# ///
"""
mail -- read-only mail viewer for every account, built on the Nylas Python SDK.

Dependencies are declared inline (PEP 723), so `uv run` builds an isolated
environment on first use and `./mail.py` works from any directory.

Read-only guarantee: the only API call this script makes is messages.list.
It never sends, deletes, moves, or marks anything.

Why the SDK rather than the `nylas` CLI:
  * accounts are fetched concurrently, so three mailboxes cost one round trip
    instead of six sequential CLI invocations,
  * `select` keeps HTML bodies off the wire (10x-17x less payload),
  * `received_after` filters by date on the server, and `select` means no
    client-side date window guesswork,
  * failures arrive as typed exceptions, not as prose on stdout.

Usage:
    ./mail.py                             every account, newest 10 in the inbox
    ./mail.py -n 20                       newest 20 per account
    ./mail.py -u                          unread only
    ./mail.py -a outlook                  one account: outlook | gmail | pku | email
    ./mail.py -f dev                      one folder, by name
    ./mail.py -a outlook --all-folders    every folder (Outlook subfolders included)
    ./mail.py --days 3                    only mail received in the last 3 days
    ./mail.py --all --max 50              paginate, at most 50 per account
    ./mail.py --ids                       print message ids (for follow-up actions)
    ./mail.py -j                          JSON output for piping
    ./mail.py --refresh                   ignore the cached account/folder lists

Output legend:  U unread   S starred, followed by sender and subject.

Notes:
  * Folder names are matched case-insensitively; a unique substring works too.
  * A folder that does not exist in one account skips that account only.
  * Exit code is 1 when any account failed, so callers can detect partial runs.
  * Cached ids self-heal: a stale folder id triggers one refresh and one retry.
  * Microsoft accounts answer slowly (about 0.1-0.2s per message), so pages are
    capped at 50 there and bigger requests paginate; Google and IMAP are roughly
    ten times faster. Pulling several hundred Microsoft messages takes minutes.
"""

import argparse
import sys
from typing import cast

import nylib
from nylas.models.messages import ListMessagesQueryParams

# Fields requested from the API. Everything else (notably the HTML body) stays
# on the server, which is where most of the payload saving comes from.
SELECT = "id,grant_id,object,thread_id,subject,from,date,unread,starred,folders"

SENDER_WIDTH = 26
STAMP_WIDTH = 11


def build_query(folder, limit, args, page_token=None):
    """Assemble the messages.list query parameters for one request."""
    query = {"limit": limit, "select": SELECT}
    if folder:
        query["in"] = folder["id"]
    if args.unread:
        query["unread"] = True
    if args.starred:
        query["starred"] = True
    if args.days is not None:
        query["received_after"] = nylib.days_ago_timestamp(args.days)
    if page_token:
        query["page_token"] = page_token
    return query


def fetch_page(grant_id, folder, limit, args, page_token=None):
    """One page of messages. Returns (messages, next_cursor)."""
    response = nylib.client().messages.list(
        identifier=grant_id,
        # TypedDict keys "limit"/"page_token" are invisible to type checkers
        # (the SDK builds it with **get_type_hints), hence the cast.
        query_params=cast(ListMessagesQueryParams, build_query(folder, limit, args, page_token)),
    )
    return nylib.list_data(response)


def resolve_folder(grant, args):
    """Decide which folder to read for one account.

    Returns (folder_or_None, label, folders_by_id). Raises LookupError when a
    requested folder name cannot be resolved in this account.
    """
    folders = nylib.load_folders(grant["id"], refresh=args.refresh)
    by_id = {f["id"]: f["name"] for f in folders}

    if args.all_folders:
        # No folder filter, but the id -> name map is still needed to label
        # each message with the folders it lives in.
        return None, "all folders", by_id

    if args.folder:
        folder = nylib.find_folder(folders, args.folder)
        return folder, folder["name"], by_id

    inbox = nylib.find_inbox(folders)
    if inbox is None:
        return None, "all folders (no inbox detected)", by_id
    return inbox, inbox["name"], by_id


def to_row(message):
    """Flatten one SDK message into the dict the display and JSON use."""
    data = message.to_dict()
    sender = data.get("from") or []
    return {
        "id": data.get("id"),
        "date": data.get("date"),
        "date_iso": nylib.ts_iso(data.get("date")),
        "stamp": nylib.fmt_ts(data.get("date")),
        "unread": bool(data.get("unread")),
        "starred": bool(data.get("starred")),
        "from": sender,
        "sender": nylib.display_name(sender),
        "sender_email": (sender[0].get("email") if sender else None),
        "subject": data.get("subject") or "",
        "thread_id": data.get("thread_id"),
        "folders": data.get("folders") or [],
    }


def fetch_account(grant, args):
    """Read one mailbox. Raises on failure; the caller isolates accounts."""
    folder, label, folders_by_id = resolve_folder(grant, args)

    try:
        rows = read_messages(grant, folder, args)
    except Exception as exc:  # noqa: BLE001 - retried only for a stale folder id
        if not (folder and args.folder and nylib.is_not_found(exc)):
            raise
        # The cached folder id went stale (folder renamed or recreated):
        # drop the cache, resolve again, and retry exactly once.
        nylib.invalidate_folders(grant["id"])
        folder, label, folders_by_id = resolve_folder(grant, args)
        rows = read_messages(grant, folder, args)

    rows.sort(key=lambda row: row["date"] or 0, reverse=True)
    for row in rows:
        row["folder_names"] = [folders_by_id.get(fid, fid) for fid in row["folders"]]
    return {
        "email": grant["email"],
        "provider": grant.get("provider"),
        "grant_status": grant.get("grant_status"),
        "folder": label,
        "folder_id": folder["id"] if folder else None,
        "count": len(rows),
        "messages": rows,
    }


def read_messages(grant, folder, args):
    """Fetch up to the requested number of messages, paginating as needed.

    Both -n and --all end up here: the target is --limit normally and --max with
    --all. Pages are sized per provider (see nylib.page_limit) because Microsoft
    answers a large page far more slowly than Google or IMAP.
    """
    target = args.max if args.all else args.limit
    size = nylib.page_limit(grant.get("provider"))
    rows, page_token = [], None
    while len(rows) < target:
        want = min(size, target - len(rows))
        messages, page_token = fetch_page(grant["id"], folder, want, args, page_token)
        rows.extend(to_row(message) for message in messages)
        if not messages or not page_token:
            break
    return rows


def print_account(report, args, term_width):
    """Render one mailbox as a plain-text block."""
    header = (
        f"\n=== {report['email']} [{report['provider']}]"
        f" - folder: {report['folder']} - {report['count']} messages ==="
    )
    print(header)
    if not report["messages"]:
        print("  (no messages)")
        return

    for row in report["messages"]:
        mark = ("U" if row["unread"] else "-") + ("S" if row["starred"] else "-")
        tags = ""
        if args.all_folders:
            names = row["folder_names"]
            if names:
                tags = "[" + ",".join(names) + "] "
        used = STAMP_WIDTH + len(mark) + SENDER_WIDTH + 4
        subject = nylib.trunc(
            tags + (row["subject"] or "(no subject)"), max(20, term_width - used)
        )
        print(
            f" {nylib.pad(row['stamp'], STAMP_WIDTH)}"
            f" {mark}  {nylib.pad(row['sender'], SENDER_WIDTH)} {subject}"
        )
        if args.ids:
            print(f"{'':<{STAMP_WIDTH + 1}}      id: {row['id']}")


def terminal_width(default=120):
    """Terminal width, with a fallback for non-interactive callers."""
    import shutil

    return shutil.get_terminal_size((default, 24)).columns


def main():
    parser = argparse.ArgumentParser(
        prog="mail",
        description="Read-only mail viewer (never sends, deletes, moves, or marks mail).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-a",
        "--account",
        default="all",
        help="account selector: all (default) | outlook | gmail | pku | email",
    )
    parser.add_argument(
        "-n", "--limit", type=int, default=10, help="messages per account (default 10)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="paginate through everything, bounded by --max",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=200,
        help="upper bound per account when --all is used (default 200)",
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
        "--days",
        type=int,
        help="only mail received in the last N days (server-side filter)",
    )
    parser.add_argument("--ids", action="store_true", help="print message ids")
    parser.add_argument("-j", "--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="rebuild the cached account and folder lists",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=nylib.DEFAULT_WORKERS,
        help=f"concurrent accounts (default {nylib.DEFAULT_WORKERS})",
    )
    args = parser.parse_args()

    if args.all and args.limit != 10:
        parser.error("--all cannot be combined with -n/--limit (use --max)")
    if args.all_folders and args.folder:
        parser.error("--all-folders cannot be combined with -f/--folder")
    if args.limit < 1 or args.max < 1:
        parser.error("--limit and --max must be positive")

    grants = nylib.load_grants(refresh=args.refresh)
    targets, unknown = nylib.select_grants(grants, args.account)

    errors = [
        {"account": term, "error": "no account matches this selector"}
        for term in unknown
    ]
    for item in errors:
        print(f"! {item['account']}: {item['error']}", file=sys.stderr)
    if not targets and not errors:
        print("! no accounts available for this API key", file=sys.stderr)

    reports, failures = nylib.gather(
        lambda grant: fetch_account(grant, args), targets, workers=args.workers
    )
    for grant, failure in zip(targets, failures):
        if failure is not None:
            message = nylib.describe_error(failure)
            errors.append({"account": grant["email"], "error": message})
            print(f"! {grant['email']}: {message}", file=sys.stderr)

    accounts = [report for report in reports if report is not None]

    if args.json:
        import json

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
        if not accounts:
            print("! nothing to show", file=sys.stderr)
        width = max(80, terminal_width())
        for report in accounts:
            print_account(report, args, width)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
