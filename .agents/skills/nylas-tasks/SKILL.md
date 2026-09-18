---
name: nylas-tasks
description: "Pick the right tool for mail and calendar work in this repo: the read-only scripts in src/agent_nylas (email_list.py, email_read.py, event_list.py) for listing and reading, the nylas CLI for everything that writes. Use when the user asks to see unread mail, read a message or its attachments, search mail, list upcoming events, or to send, reply to, delete, move, mark, schedule or RSVP mail and calendar events."
---

# Mail and calendar: pick the tool, then read its rule

Two toolboxes over the same accounts, both using the `NYLAS_FILE_STORE_PASSPHRASE`
store:

* `src/agent_nylas/*.py` -- read-only by construction (list/find only), built for
  piping.
* the `nylas` CLI -- everything that changes state.

A script command below is shorthand for `uv run src/agent_nylas/<name>.py` from
the repository root (always works; `./src/agent_nylas/<name>.py` also works while
this project's virtualenv is active). `--help` has the flags.

| Rule file | When |
| --- | --- |
| [mail-list.md](rules/mail-list.md) | find mail: the listings and their gotchas |
| [mail-read.md](rules/mail-read.md) | read one or many, attachments, mark what you read |
| [mail-send.md](rules/mail-send.md) | send, reply, draft, schedule -- the user approves first |
| [mail-update.md](rules/mail-update.md) | unread, stars, move, delete |
| [calendar-list.md](rules/calendar-list.md) | find time: events and calendars |
| [calendar-write.md](rules/calendar-write.md) | create, update, delete, answer an invitation |

Search is routed below but not written up yet: use `--help`.

## Mail

| Job | Tool | Command |
| --- | --- | --- |
| Read: list and filter | script | `email_list.py -u --days 3 -a pku` -> [rule](rules/mail-list.md) |
| Read: folders, counts and ids | script | `email_list.py --folders` (one request per account, no messages) -> [rule](rules/mail-list.md) |
| Read: one or many in full, attachments | script | `email_read.py <id> <id>`, `email_read.py -` (stdin), `--save DIR` -> [rule](rules/mail-read.md) |
| Read: sender, subject, attachment filters | CLI | `nylas email search --from ... --has-attachment`, `nylas email search "invoice"` |
| Create: send, reply, draft, schedule | CLI | `nylas email send --to A --subject S --body B -y`, `nylas email reply <id> --body B -y`, `nylas email drafts`, `nylas email scheduled` -> [rule](rules/mail-send.md) |
| Update: mark read after reading | CLI | `nylas email mark read <id>`, `nylas email threads mark <thread-id> --read` -> [rule](rules/mail-read.md) |
| Update: unread, star, move, folders | CLI | `nylas email mark unread\|starred <id>`, `nylas email move <id> --folder <folder-id>` (ids from `email_list.py --folders`) -> [rule](rules/mail-update.md) |
| Update: tracking, metadata, threads | CLI | `nylas email tracking-info <id>`, `nylas email metadata`, `nylas email threads` |
| Delete: message, thread | CLI | `nylas email delete <id> -f`, `nylas email threads delete <id> -f` -> [rule](rules/mail-update.md) |

## Calendar

| Job | Tool | Command |
| --- | --- | --- |
| Read: upcoming events | script | `event_list.py --days 7`, `-c <calendar>`, `--expand`, `--calendars` -> [rule](rules/calendar-list.md) |
| Read: one event, or a calendar as JSON | CLI | `nylas calendar events show <id>`, `nylas calendar events import` -> [rule](rules/calendar-list.md) |
| Create: event | CLI | `nylas calendar events create -t T -s "2026-01-15 14:00"` -> [rule](rules/calendar-write.md) |
| Update: fields, one instance of a series | CLI | `nylas calendar events update <id>`, `nylas calendar recurring` -> [rule](rules/calendar-write.md) |
| Update: answering an invitation | CLI | `nylas calendar events rsvp <id> yes\|no\|maybe` -> [rule](rules/calendar-write.md) |
| Delete: event, instance | CLI | `nylas calendar events delete <id> -f`, `nylas calendar recurring delete <id> -c <cal> -y` -> [rule](rules/calendar-write.md) |

```bash
# the two things asked for most
uv run src/agent_nylas/email_list.py -u --days 3 --ids | uv run src/agent_nylas/email_read.py -
uv run src/agent_nylas/event_list.py --days 7
```

## Ground rules

* Exit codes: `0` ok, `1` something failed (the rest of the output is still
  usable, failures on stderr), `2` bad arguments or environment. stdout is data;
  stderr also carries the `… <account>` / `✓ <account>: 2 messages in 0.9s`
  progress lines that slow runs print so a wait is not silent.
* Anything that leaves the mailbox (send, reply, draft-send, scheduled send) or
  destroys mail (delete) happens only after the user has seen what will happen
  and said go -- and so does an RSVP, the one calendar call that mails the
  organizer. `-y` and `-f` come after that approval, never instead of it: the
  CLI's own prompts are inert without a terminal, and they exit `0` when they
  cancel.
* Events stay personal: never pass `-p/--participant` (or invite anyone
  otherwise) -- an event with participants emails them an invitation, and this
  repo only schedules the user's own time.
* The CLI acts on the active grant (here `hongming.liang@outlook.com`, per
  `nylas auth list`) unless the account is passed. The scripts find each id's
  account themselves, which is why ids from different accounts mix freely
  (Outlook base64, Gmail hex, IMAP `<...@...>`).
* The API is reached through a local proxy with seconds of latency: a listing
  takes a few seconds, a stalled proxy a minute. A per-message loop costs that
  much per message -- say so before starting one.
* In the scripts `-a` takes one comma-separated value of `all`, an email
  substring (`outlook`, `pku`), a provider (`google`, `imap`) or a grant id, and
  does not accumulate: `-a gmail -a pku` keeps only `pku`.
* Keep the read-only guarantee when extending the scripts -- the only API calls
  are `messages.list`/`find`, `folders.list`, `grants.list`, `events.list` -- and
  run `uv run pyright` plus `uvx ruff check src/agent_nylas` after editing.
