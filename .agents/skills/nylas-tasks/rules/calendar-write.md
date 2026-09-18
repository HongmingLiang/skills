---
title: Calendar create, update, delete, answer
section: calendar
---

# Calendar writes -- `nylas calendar events`, `nylas calendar recurring`

An RSVP emails the organizer, so that one waits for the user's go-ahead, the way
[mail-send.md](mail-send.md) does for mail. Everything else stays on the user's
own calendar: events are made without participants. The reads are
[calendar-list.md](calendar-list.md).

## Recipes

```bash
# create -- title and start are required, end defaults to one hour later
nylas calendar events create -t "Review" -s "2026-01-15 14:00"

# on a calendar other than the primary one -- and never with participants
uv run src/agent_nylas/event_list.py --calendars              # calendar ids
nylas calendar events create -c <calendar-id> -t "Review" -s "2026-01-15 14:00" \
  -e "2026-01-15 15:00" -D "scope" -l "Room 519"

# a date-only start makes an all-day event; two dates span days
nylas calendar events create -t "Vacation" -s 2026-01-15 --all-day
nylas calendar events create -t "Trip" -s 2026-01-15 -e 2026-01-18 --all-day

# reschedule or retitle one event: same flags, no participants involved
nylas calendar events update <event-id> -s "2026-01-15 16:00" -e "2026-01-15 17:00"

# answer an invitation as the account owner: yes | no | maybe
nylas calendar events rsvp <event-id> yes
nylas calendar events rsvp <event-id> maybe --comment "if the room is free"

# one instance of a series (needs -c; RFC3339 times, unlike create)
nylas calendar recurring list <series-event-id> -c <calendar-id>
nylas calendar recurring update <instance-id> -c <calendar-id> \
  --start 2026-01-15T16:00:00+08:00
nylas calendar recurring delete <instance-id> -c <calendar-id> -y

# delete -- only with -f, and only after the user approved this exact event
nylas calendar events delete <event-id> -f
```

## What to know

* **No participants, ever**: `-p/--participant` emails an invitation to those
  addresses, and inviting people is not this repo's job -- it schedules the
  user's own time. `update -p` would also replace the whole list. An event made
  here has nobody to notify.
* Times: `'YYYY-MM-DD HH:MM'` (a `T` works too, and so do seconds and RFC3339),
  read in the system timezone unless `--timezone` names an IANA zone. A bare
  `YYYY-MM-DD` makes a date (all-day) event, which is what `--all-day` requires.
* `-c/--calendar` takes an id (`primary` works) and defaults to the account's
  primary writable calendar; `[grant-id]` picks the account, else the active
  grant acts.
* Two checks can interrupt a create: a DST conflict, and working hours (that one
  only when `~/.nylas/config.yaml` configures them, and there is no such file
  here). Both ask `Create anyway? [y/N]`, so with no terminal they cancel:
  `Cancelled.`, **exit 0**, nothing created. If the time is what the user asked
  for, rerun with `--ignore-dst-warning` (or `--ignore-working-hours`).
* Success prints `✓ Event created successfully!` and `ID: <id>`: keep the id and
  verify with `nylas calendar events show <id>`.
* `update` asks nothing and has no "nothing changed" guard: every flag you pass
  lands at once. Editing an event that already has participants -- a meeting
  somebody else invited us to -- can email them a change notice (Microsoft always
  sends one), so leave those to the user.
* `rsvp` answers as the owner and notifies the organizer; `yes|no|maybe` is the
  whole status set and `--comment` rides along. It is the only call here that
  mails somebody, so it waits for approval.
* `recurring` acts on a single **instance** and needs `-c`; its `--start/--end`
  are RFC3339, not the create format. `recurring delete` takes `-y`, not `-f`.
* `delete` prompts `Are you sure you want to delete event <id>? [y/N]: ` and
  without `-f` cancels with **exit 0** -- the same silent cancel as mail, so `-f`
  means "the user asked for this exact event to go".

All the flags: `nylas calendar events --help`, `nylas calendar recurring --help`.
