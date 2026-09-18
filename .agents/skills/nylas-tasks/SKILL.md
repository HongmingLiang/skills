---
name: nylas-tasks
description: "Pick the right tool for mail and calendar work in this repo: the read-only scripts in src/agent_nylas (email_list.py, email_read.py, event_list.py) for listing and reading, the nylas CLI for everything that writes. Use when the user asks to see unread mail, read a message or its attachments, search mail, list upcoming events, or to send, reply to, delete, move, mark, schedule or RSVP mail and calendar events."
---

# Mail and calendar: pick the tool, then read the rule for it

Two toolboxes over the same accounts:

* `src/agent_nylas/*.py` -- read-only by construction (they only ever call
  list/find). Listing and reading live here, with output built for piping.
* the `nylas` CLI -- everything that changes state. Both read the same account
  store (`NYLAS_FILE_STORE_PASSPHRASE`), so accounts and credentials never
  drift apart.

Script commands below are shorthand for `uv run src/agent_nylas/<name>.py` from
the repository root -- the form that always works. `./src/agent_nylas/<name>.py`
also works while this project's virtualenv is active; without it the script stops
with a hint to use `uv run`, since a bare `python3` has no Nylas SDK. Add `--help`
for their flags. Details live in `rules/`:

| Read this | When |
| --- | --- |
| [rules/mail-list.md](rules/mail-list.md) | mail list and query: the recipes you will actually use, plus the gotchas |
| [rules/mail-send.md](rules/mail-send.md) | sending, replying, drafts and scheduled sends -- and the approval rule that every one of them needs first |

The other paths (read a message, search, delete/mark, calendar list, calendar
writes) are routed by the tables below but not written up yet, so use `--help`
for their flags.

## Mail

| Job | Tool | Command |
| --- | --- | --- |
| Create: send, reply, draft, schedule | CLI | `nylas email send --to A --subject S --body B -y`, `nylas email reply <id> --body B -y`, `nylas email drafts`, `nylas email scheduled` -> [rules/mail-send.md](rules/mail-send.md) |
| Delete: message, thread | CLI | `nylas email delete <id>`, `nylas email threads delete <id>` |
| Read: list and filter | script | `email_list.py -u --days 3 -a pku` -> [rules/mail-list.md](rules/mail-list.md) |
| Read: one or many messages in full | script | `email_read.py <id> [<id> ...]`, `email_read.py -` takes ids from stdin |
| Read: attachments | script | `email_read.py <id> --save DIR` (listed by default, downloaded only with `--save`) |
| Read: sender, subject, has-attachment filters | CLI | `nylas email search --from ... --subject ... --has-attachment`, `nylas email search "invoice"` |
| Update: read/unread, star, move, folders | CLI | `nylas email mark read\|unread\|starred\|unstarred <id>`, `nylas email move <id> --folder F`, `nylas email folders` |
| Update: tracking, metadata, threads | CLI | `nylas email tracking-info <id>`, `nylas email metadata`, `nylas email threads` |

## Calendar

| Job | Tool | Command |
| --- | --- | --- |
| Create: event, invitation | CLI | `nylas calendar events create -t T -s "2026-01-15 14:00" -p a@b.c` |
| Delete: event | CLI | `nylas calendar events delete <id>` |
| Read: upcoming events | script | `event_list.py --days 7`, `-c <calendar>`, `--expand`, `--calendars` |
| Read: one event, other calendars, free time | CLI | `nylas calendar events show <id>`, `nylas calendar list`, `nylas calendar availability`, `nylas calendar find-time` |
| Update: event fields, recurring series | CLI | `nylas calendar events update <id>`, `nylas calendar recurring` |
| Update: answer an invitation | CLI | `nylas calendar events rsvp <id>` |

## Quickstart

```bash
# what is unread, then read it (one account auto-detected per id)
uv run src/agent_nylas/email_list.py -u --days 3 --ids | uv run src/agent_nylas/email_read.py -

# the coming week
uv run src/agent_nylas/event_list.py --days 7
```

## Ground rules

* Exit codes: `0` ok, `1` something failed (partial output is still usable and
  the failure is on stderr), `2` the environment or the arguments are wrong.
  stdout is data, stderr is diagnostics.
* `-a` takes a comma-separated list of `all`, an email substring (`outlook`,
  `pku`), a provider (`google`, `imap`) or a grant id; it means the same in all
  three scripts.
* Message ids are opaque and account-local: Outlook base64, Gmail hex, IMAP
  `<...@...>`. `email_read.py` finds the right account itself, so ids from
  different accounts can be mixed in one call.
* `jq` is not installed here: parse `-j` output with `python3 -c` (stdlib) or
  pipe `--ids` into `email_read.py`.
* Flags must not sit between two ids (`email_read.py <id> -j <id>` fails): give
  flags first or last.
* Anything that leaves the mailbox -- send, reply, forward, draft-send, scheduled
  send -- happens only after the user has seen the exact text (account,
  recipients, subject, body) and said go; `-y` comes after that approval, never
  instead of it, because the CLI's own prompt is inert without a terminal. See
  [rules/mail-send.md](rules/mail-send.md).
* Keep the read-only guarantee when extending the scripts: the only API calls
  are `messages.list`/`find`, `folders.list`, `grants.list` and `events.list`.
  Anything that writes belongs to the CLI. After editing, run `uv run pyright`
  and `uvx ruff check src/agent_nylas`.
