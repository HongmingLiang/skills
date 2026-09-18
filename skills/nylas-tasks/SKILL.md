---
name: nylas-tasks
description: "Pick the right tool for mail and calendar work: the read-only scripts shipped with this skill (email_list.py, email_read.py, event_list.py) for listing and reading, the nylas CLI for everything that writes. Use when the user asks to see unread mail, read a message or its attachments, search mail, list upcoming events, or to send, reply, delete, move, mark, schedule or RSVP mail and calendar events."
compatibility: "uv for the scripts (each declares its own dependencies); the nylas CLI v3.1.29 for everything else."
license: MIT
disable-model-invocation: true
---

# Mail and calendar: pick the tool, then read its rule

Two toolboxes over the same accounts, both reading `NYLAS_FILE_STORE_PASSPHRASE`:
the scripts next to this file (`scripts/`) are read-only by construction (list
and find only, built for piping), and the `nylas` CLI is everything that changes
state. Run a script from this skill's directory as `uv run scripts/<name>.py`;
each one declares its own dependencies (PEP 723), so uv builds an isolated
environment and the skill works wherever it is installed. The rule files hold
the flags.

## Rules

| Rule file | Covers | Tool |
| --- | --- | --- |
| [mail-list.md](rules/mail-list.md) | read: list and filter; a folder's counts and ids (`--folders`) | script `email_list.py` |
| [mail-read.md](rules/mail-read.md) | read: one or many in full, attachments; then mark what you read | script `email_read.py`, CLI `nylas email mark read`, `nylas email threads mark` |
| [mail-send.md](rules/mail-send.md) | create: send, reply, draft, schedule | CLI `nylas email send\|reply\|drafts\|scheduled` |
| [mail-update.md](rules/mail-update.md) | update: unread, star, move; delete: message, thread | CLI `nylas email mark\|move\|delete`, `nylas email threads delete` |
| [calendar-list.md](rules/calendar-list.md) | read: upcoming events, calendars, one event, a calendar as JSON | script `event_list.py`, CLI `nylas calendar events show\|import` |
| [calendar-write.md](rules/calendar-write.md) | events and single instances: create, update, delete, RSVP | CLI `nylas calendar events`, `nylas calendar recurring` |

Not written up yet, use `--help` instead: `nylas email search` (sender, subject
and attachment filters), `nylas email tracking-info\|metadata\|threads`.

## Ground rules

* Exit codes `0` ok, `1` partial failure (the output is still usable, failures go
  to stderr), `2` bad arguments or environment. stdout is data, the `…` / `✓`
  progress lines are stderr. An environment failure -- `uv` or `nylas` missing,
  `NYLAS_FILE_STORE_PASSPHRASE` unset, the CLI reporting `API key not
  configured` -- is in [reference/setup.md](reference/setup.md).
* Sends, drafts, scheduled sends, deletes and RSVPs -- the one calendar call that
  mails the organizer -- happen only after the user has seen what will happen and
  said go: `-y`/`-f` follow that approval, never replace it, and the CLI's own
  prompts are inert without a terminal (they exit `0` when they cancel).
* Events stay personal: never pass `-p/--participant` -- it emails an invitation,
  and this skill only schedules the user's own time.
* The CLI acts on the active grant unless the account is passed; the scripts find
  each id's account themselves, so ids from different accounts mix freely.
* A local proxy adds seconds of latency (a stalled one, a minute) and a
  per-message loop pays it per message: say so before starting one.
* In the scripts `-a` takes one comma-separated value (`all`, a provider, a
  substring, a grant id) and does not accumulate: `-a google -a imap` keeps `imap`.
* The calendar view reads the Microsoft (Outlook) account unless `-a` says
  otherwise, and `NYLAS_CALENDAR_ACCOUNT` sets that default; the mail views read
  every account until `-a` narrows them.
* Keep the read-only guarantee when extending the scripts: the only API calls are
  `messages.list`/`find`, `folders.list`, `grants.list`, `events.list`.
