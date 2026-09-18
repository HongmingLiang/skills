#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "nylas>=6.17.0",
#     "requests>=2.32",
# ]
# ///
"""
email_read -- read-only message reader: one or more messages, in full.

Read-only by construction: the only calls this script makes are messages.find
plus the cached folders.list / grants.list lookups used to name things. It never
sends, deletes, moves, or marks anything, and it downloads nothing unless
--save [DIR] is given.

Usage (from the skill directory):
    uv run scripts/email_read.py <id> [<id> ...] [flags]
    <id>                  one message, in full
    <id> <id> ...         several messages, fetched concurrently
    - < ids.txt           ids from stdin, whitespace separated
    <id> -a google        pin the account instead of auto-detecting
    <id> --save [DIR]     save the attachments: DIR, or a fresh temp dir
    <id> -j               JSON output: raw HTML body, no rendering

Reading several messages needs no config file: the ids are the whole input, so
they are passed as arguments or piped in. `-` accepts plain ids, the JSON that
`email_list.py -j` prints, or a JSON list -- a selection is expressed with the
listing's own filters instead of with a second query language implemented here:

    uv run scripts/email_list.py -u --days 3 --ids | uv run scripts/email_read.py -
    uv run scripts/email_list.py -j --days 3 | uv run scripts/email_read.py -

Flags apply to the whole run -- none of them has a per-message variant worth a
schema -- and ids are deduplicated, so piping a listing that repeats one is safe.

Notes:
  * The account is auto-detected: every selected account is asked, and the one
    that owns the id answers. A miss is a 404 and costs one request per account
    (about 0.5s), so pass -a when you already know where the message lives.
  * Bodies arrive as HTML from every provider. The table renders them as plain
    text paragraph by paragraph, never truncated; JSON keeps the raw HTML.
  * Attachments are listed only: the bytes are fetched when --save asks for
    them, into DIR or a fresh temp directory (never overwriting a file).
  * Exit code is 1 when any requested id failed, so partial runs stay
    detectable, and 2 for a missing API key or unreadable input.
"""

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import config
import nylib

if TYPE_CHECKING:
    from nylas.models.attachments import Attachment

# The projection this view asks for. grant_id has to be in it: the SDK decodes
# the response into Message, whose only field without a default is grant_id, so
# a projection that leaves it out fails inside dataclasses_json with a KeyError.
SELECT = (
    "grant_id,id,thread_id,subject,from,to,cc,date,unread,starred,"
    "folders,snippet,body,attachments"
)

DETAIL_INDENT = " " * 8  # detail labels sit under the date/account line
LABELS = ("Title", "From", "To", "Cc", "Folders", "Attachments", "ID")
PREFIX = nylib.prefixes(LABELS, DETAIL_INDENT)


class ContactRow(TypedDict):
    """One recipient, as displayed and stored."""

    name: str | None
    email: str | None


class AttachmentRow(TypedDict):
    """One attachment's metadata, plus where --save put it."""

    id: str | None
    filename: str
    size: int | None
    content_type: str | None
    saved_to: str | None


class MessageDetail(TypedDict):
    """One message, flattened to the fields the display and JSON need."""

    id: str
    account: str
    provider: str | None
    thread_id: str | None
    date: int | None
    date_iso: str | None
    unread: bool
    starred: bool
    sender_name: str
    sender_email: str | None
    to: list[ContactRow]
    cc: list[ContactRow]
    subject: str
    folder_names: list[str]
    snippet: str
    body: str
    attachments: list[AttachmentRow]


class ReadError(TypedDict):
    """One failure: an unmatched account selector, a failed fetch, or a miss."""

    id: str | None
    account: str | None
    error: str


@dataclass(frozen=True)
class Options:
    """Everything the read path needs, assembled once from the CLI."""

    account: str = "all"
    refresh: bool = False
    save: Path | None = None
    workers: int = config.DEFAULT_WORKERS


def extract_ids(document: Any) -> list[str]:
    """Message ids from a JSON report, in order.

    Understands the shapes these scripts print (email_list.py's
    accounts[].messages and this script's messages[]), plus a bare list of ids or
    of message objects, so
    anything jq produces from one of those pipes in unchanged. Raises ValueError
    when the document holds no messages at all, which is the difference between
    "nothing matched" and "the input was not a listing".
    """
    if isinstance(document, dict):
        if isinstance(document.get("messages"), list):
            items: list[Any] = document["messages"]
        else:
            items = [
                message
                for account in document.get("accounts") or []
                if isinstance(account, dict)
                for message in account.get("messages") or []
            ]
    elif isinstance(document, list):
        items = document
    else:
        items = []
    if not items:
        raise ValueError("the input is not a listing with message ids")
    ids = [
        item if isinstance(item, str) else item.get("id")
        for item in items
        if isinstance(item, (str, dict))
    ]
    if not ids:
        raise ValueError("the input is not a listing with message ids")
    return [str(value) for value in ids if value]


def ids_from_stdin() -> list[str]:
    """Read ids from stdin: whitespace separated, or a JSON listing."""
    text = sys.stdin.read()
    if text.lstrip().startswith(("{", "[")):
        return extract_ids(json.loads(text))
    return text.split()


def requested_ids(args: argparse.Namespace) -> list[str]:
    """Every requested id, in order, with duplicates dropped."""
    ids: list[str] = []
    for value in args.ids:
        ids.extend(ids_from_stdin() if value == "-" else [value])
    return list(dict.fromkeys(ids))


def attachment_name(attachment: Attachment) -> str:
    """A safe filename for an attachment download.

    The name comes from the message and is untrusted, so any directory part and
    any non-printable character is dropped: "../../x" cannot escape the target
    directory. Providers that report no filename leave it embedded in the
    content type ("image/jpeg; name=qrcode.jpg"), which is used as a fallback.
    """
    name = (attachment.filename or "").strip()
    if not name:
        _, _, embedded = (attachment.content_type or "").partition("name=")
        name = embedded.strip().strip('"')
    name = "".join(c for c in name.replace("\\", "/") if c.isprintable())
    name = name.rpartition("/")[2].strip()
    return (
        name
        if name not in ("", ".", "..")
        else f"attachment-{attachment.id or 'unnamed'}"
    )


def save_attachment(
    grant_id: str, message_id: str, attachment: Attachment, directory: Path
) -> str:
    """Download one attachment into `directory`. Raises if the file exists."""
    if attachment.id is None:
        raise ValueError(f"{attachment_name(attachment)} has no attachment id")
    target = directory / attachment_name(attachment)
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    data = nylib.client().attachments.download_bytes(
        identifier=grant_id,
        attachment_id=attachment.id,
        query_params={"message_id": message_id},
    )
    target.write_bytes(data)
    return str(target)


def attachment_rows(
    grant_id: str, message_id: str, attachments: Sequence[Attachment], opts: Options
) -> tuple[list[AttachmentRow], list[str]]:
    """Attachment metadata, downloading the bytes only when --save asked for it.

    Returns the rows plus one line per download that failed, so a file that is
    already there still leaves the mail itself readable while the run as a whole
    keeps reporting that something did not work.
    """
    rows: list[AttachmentRow] = []
    problems: list[str] = []
    for attachment in attachments:
        saved_to: str | None = None
        if opts.save:
            try:
                saved_to = save_attachment(grant_id, message_id, attachment, opts.save)
            except (OSError, ValueError) as exc:
                problems.append(f"{attachment_name(attachment)}: {exc}")
        rows.append(
            AttachmentRow(
                id=attachment.id,
                filename=attachment_name(attachment),
                size=attachment.size,
                content_type=attachment.content_type,
                saved_to=saved_to,
            )
        )
    return rows, problems


def fetch_detail(
    grant: nylib.Grant, message_id: str, opts: Options
) -> tuple[MessageDetail, list[str]]:
    """Read one message from one grant. Raises on failure; the caller isolates ids."""
    message = (
        nylib.client()
        .messages.find(
            identifier=grant["id"],
            message_id=message_id,
            query_params={"select": SELECT},
        )
        .data
    )
    folders = {
        folder["id"]: folder["name"]
        for folder in nylib.load_folders(grant["id"], refresh=opts.refresh)
    }
    sender = message.from_[0] if message.from_ else None
    attachments, problems = attachment_rows(
        grant["id"], message_id, message.attachments or [], opts
    )
    return (
        MessageDetail(
            id=message.id or message_id,
            account=grant["email"],
            provider=grant.get("provider"),
            thread_id=message.thread_id,
            date=message.date,
            date_iso=nylib.ts_iso(message.date),
            unread=bool(message.unread),
            starred=bool(message.starred),
            sender_name=str(
                (sender.get("name") or sender["email"] or "?") if sender else "?"
            ).strip(),
            sender_email=sender["email"] if sender else None,
            to=[
                ContactRow(name=p.get("name"), email=p["email"])
                for p in message.to or []
            ],
            cc=[
                ContactRow(name=p.get("name"), email=p["email"])
                for p in message.cc or []
            ],
            subject=message.subject or "",
            folder_names=[folders.get(fid, fid) for fid in message.folders or []],
            snippet=message.snippet or "",
            body=message.body or "",
            attachments=attachments,
        ),
        problems,
    )


def read_all(
    ids: Sequence[str], grants: Sequence[nylib.Grant], opts: Options
) -> tuple[list[MessageDetail], list[ReadError]]:
    """Resolve every id among the selected accounts, keeping the request order.

    Each (id, account) pair is asked concurrently, so the account that owns a
    message answers in the same batch. The first account in selector order wins,
    which is deterministic because select_grants keeps the grant order. A 404
    just means "not here" and moves on to the next account.
    """
    tasks = [(message_id, grant) for message_id in ids for grant in grants]
    results, failures = nylib.gather(
        lambda task: fetch_detail(task[1], task[0], opts),
        tasks,
        workers=opts.workers,
    )
    details: dict[str, MessageDetail] = {}
    misses: dict[str, list[str]] = {message_id: [] for message_id in ids}
    problems: dict[str, list[ReadError]] = {message_id: [] for message_id in ids}
    for (message_id, grant), result, failure in zip(tasks, results, failures):
        if failure is not None:
            if nylib.is_not_found(failure):
                misses[message_id].append(grant["email"])
            else:
                problems[message_id].append(
                    ReadError(
                        id=message_id,
                        account=grant["email"],
                        error=nylib.describe_error(failure),
                    )
                )
            continue
        detail, notes = result if result is not None else (None, [])
        if detail is not None and message_id not in details:
            details[message_id] = detail
            problems[message_id].extend(
                ReadError(id=message_id, account=grant["email"], error=note)
                for note in notes
            )

    errors: list[ReadError] = []
    for message_id in ids:
        if message_id not in details:
            searched = ", ".join(misses[message_id])
            problems[message_id].append(
                ReadError(
                    id=message_id,
                    account=None,
                    error=(
                        f"not found in {searched}"
                        if searched
                        else "not searched: no account matched the selector"
                    ),
                )
            )
        errors.extend(problems[message_id])
    return [details[message_id] for message_id in ids if message_id in details], errors


def size_label(size: int | None) -> str:
    """Attachment size in the unit a reader scans for."""
    if not size:
        return "?"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def attachment_line(row: AttachmentRow) -> str:
    """One attachment as 'name (size, type)', plus where --save wrote it."""
    media = (row["content_type"] or "").split(";")[0].strip()
    described = ", ".join(part for part in (size_label(row["size"]), media) if part)
    saved = f" -> {row['saved_to']}" if row["saved_to"] else ""
    return f"{row['filename']} ({described}){saved}"


def address_line(rows: Sequence[ContactRow]) -> str:
    """Recipients as 'Name <email>', address only when no name is known."""
    return ", ".join(
        f"{row['name']} <{row['email']}>" if row["name"] else (row["email"] or "")
        for row in rows
    ).strip()


def print_message(detail: MessageDetail, term_width: int) -> None:
    """Render one message: a date/account line, labelled fields, then the body."""
    print()
    stamp = nylib.local_time(detail["date"])
    when = stamp.strftime("%m-%d %H:%M") if stamp else "??-?? ??:??"
    flags = "".join(
        marker
        for marker, on in (("U", detail["unread"]), ("S", detail["starred"]))
        if on
    )
    print(
        f" {when}  {detail['account']} [{detail['provider']}]"
        f"{f'  {flags}' if flags else ''}"
    )
    fields = (
        ("Title", detail["subject"] or "(no subject)"),
        (
            "From",
            f"{detail['sender_name']} <{detail['sender_email']}>"
            if detail["sender_email"]
            else detail["sender_name"],
        ),
        ("To", address_line(detail["to"])),
        ("Cc", address_line(detail["cc"])),
        ("Folders", ", ".join(detail["folder_names"])),
        ("Attachments", " / ".join(attachment_line(r) for r in detail["attachments"])),
        ("ID", detail["id"]),
    )
    for label, value in fields:
        for line in nylib.hanging(PREFIX[label], value, term_width):
            print(line)

    body = nylib.html_to_text(detail["body"], keep_blank_lines=True)
    if body:
        print()
    for line in body.splitlines():
        for wrapped in nylib.wrap(line, term_width):
            print(wrapped)


def save_dir(value: str | None) -> Path | None:
    """Where --save writes: its DIR, a fresh temp dir, or nowhere when absent."""
    if value is None:
        return None
    path = Path(value) if value else Path(tempfile.mkdtemp(prefix="nylas-attachments-"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse and validate the command line."""
    parser = argparse.ArgumentParser(
        prog="email_read",
        description=(
            "Read-only message reader (never sends, deletes, moves, or marks "
            "mail; downloads nothing unless --save is given)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "ids",
        nargs="+",
        metavar="ID",
        help="message id(s) to read; - reads ids (or a email_list.py -j report) from stdin",
    )
    parser.add_argument(
        "-a",
        "--account",
        default="all",
        help="account selector: all (default) | provider | address | grant id",
    )
    parser.add_argument(
        "--save",
        nargs="?",
        const="",
        metavar="DIR",
        help=(
            "download the attachments into DIR, or into a fresh temp dir "
            "when DIR is omitted; never overwrites an existing file"
        ),
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
        help=f"concurrent requests (default {config.DEFAULT_WORKERS})",
    )
    args = parser.parse_args(argv)

    if args.workers < 1:
        parser.error("--workers must be positive")
    if "-" in args.ids and sys.stdin.isatty():
        parser.error("- reads ids from stdin, but stdin is a terminal")
    return args


def run(args: argparse.Namespace) -> int:
    """Read every requested message and report the result."""
    nylib.client()  # fail fast: one clean config error instead of one per id
    opts = Options(
        account=args.account,
        refresh=args.refresh,
        save=save_dir(args.save),
        workers=args.workers,
    )
    ids = requested_ids(args)

    targets, unknown = nylib.select_targets(opts.account, opts.refresh)
    errors: list[ReadError] = [
        ReadError(id=None, account=term, error=nylib.NO_ACCOUNT) for term in unknown
    ]

    if not ids:
        print("! the input contained no message ids", file=sys.stderr)
    note = nylib.count_of(len(ids), "message")
    started = nylib.progress_start(f"reading {note}") if ids else 0.0
    details, failures = read_all(ids, targets, opts)
    if ids:
        nylib.progress_done(f"{len(details)} of {note}", started)
    errors.extend(failures)
    for item in failures:
        who = item["account"] or "accounts"
        print(f"! {item['id']} ({who}): {item['error']}", file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {
                    "generated_at": nylib.now_iso(),
                    "messages": details,
                    "errors": errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        width = nylib.terminal_width()
        for detail in details:
            print_message(detail, width)
        if not details:
            print("! nothing to show", file=sys.stderr)

    return 1 if errors else 0


def main() -> int:
    """Entry point: turn bad input into a clean message and exit code."""
    args = parse_args()
    try:
        return run(args)
    except (nylib.ConfigError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"! {exc}".replace("\n", "\n  "), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
