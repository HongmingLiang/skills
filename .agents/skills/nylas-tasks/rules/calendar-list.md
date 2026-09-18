---
title: Calendar and event listing
section: calendar
---

# Calendar and event listing -- `event_list.py`, `nylas calendar`

Read-only, nothing here writes, invites or answers anything (that is
[calendar-write.md](calendar-write.md)): one block per account, then one per
calendar, times in local time.

## Recipes

```bash
# what is coming up -- --days defaults to a whole year, so always pass it
uv run src/agent_nylas/event_list.py --days 7

# every account (the script starts with the Outlook one), one calendar by name
uv run src/agent_nylas/event_list.py --days 14 -a all -c Work

# calendar ids, with primary/read-only flags and what is skipped
uv run src/agent_nylas/event_list.py --calendars

# every occurrence of a recurring event instead of one row per series
uv run src/agent_nylas/event_list.py --days 60 --expand

# one event, or a whole calendar as raw JSON (expanded series, for export)
nylas calendar events show <event-id>
nylas calendar events import -c <calendar-id> --start 2026-01-01 --end 2026-12-31 --json
```

## What to know

* `--days` defaults to 365 -- pass it. `--since` moves the window start
  (`YYYY-MM-DD` or ISO 8601), `--ids` prints just the ids, `-j` is JSON.
* `-a` selects accounts like the mail scripts do (`all`, a provider, a substring),
  but defaults to the Outlook account alone, where those default to all of them.
* A recurring event is **one row per series**, dated at its nearest occurrence
  (that is the series, not a missing meeting); `--expand` lists every occurrence
  in the window.
* Calendars with CJK names are skipped (`(skipped calendars: ...)` in the header):
  `--include-cjk` reads them all, `-c <name>` reads one of them. The provider's
  holiday calendar here is read-only, so it takes no writes.
* `-c` resolves by keyword (`primary`), id, exact name or unique substring;
  `--exclude NAME` drops calendars by substring.
* Cost: one request per calendar, seconds each through the proxy (`✓ 5 events
  from 5 calendars in 3.9s` here).
* The CLI reads one calendar at a time too (`-c <calendar-id>`, primary by
  default), but can convert the display: `nylas calendar events list -c <id> -d 7
  -n 20 --timezone Europe/Berlin`. `nylas calendar list` truncates ids in its
  table (`-w` for the full ones; its `-q` is a struct dump, not ids).

All the flags: `uv run src/agent_nylas/event_list.py --help`, `nylas calendar --help`.
