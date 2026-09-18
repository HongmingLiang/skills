---
title: Mail list and query
section: mail
---

# Mail list and query -- `email_list.py`

Read-only (`messages.list` only, no bodies): one block per account, newest first,
never merged. Listing marks nothing read -- see [mail-read.md](mail-read.md).

## Recipes

```bash
# newest 10 per account -- the default
uv run src/agent_nylas/email_list.py

# unread, last three days, one account
uv run src/agent_nylas/email_list.py -u --days 3 -a outlook

# one folder by name (a unique substring is enough); pair it with -a, since a
# folder that only exists in one account fails the others
uv run src/agent_nylas/email_list.py -a outlook -f dev

# ids for the reader, or a digest for a script
uv run src/agent_nylas/email_list.py -u --days 3 --ids | uv run src/agent_nylas/email_read.py -
uv run src/agent_nylas/email_list.py -u --days 3 -j | python3 -c 'import json,sys
for a in json.load(sys.stdin)["accounts"]:
    for m in a["messages"]:
        print(m["date_iso"], m["sender_email"], m["subject"])'

# is anything unread anywhere? ask for folder counts, not messages: one request
# per account, and the UNREAD column answers it
nylas email folders list
```

## What to know

* `-n N` is the per-account limit (default 10). `--max N` only caps a `--all` run
  -- alone it is ignored, so `--max 3` still lists 10.
* Rows start with `U` unread and/or `S` starred. Zero unread is normal here, so an
  empty `-u` is an answer, not a broken filter.
* A folder resolves by id, then exact name, then unique substring; a miss exits 1
  with the candidates, and the "nothing matches" error lists every folder.
  Without `-f` the inbox is found through the provider's `\Inbox` attribute, so
  localized names work.
* `--days N` filters server-side (`--days 0` matches nothing), and the count in
  the header is what the run fetched, not what the folder holds.
* `-j` rows carry `date` (Unix seconds) and `date_iso` (local): use `date_iso`.
* `--all-folders --all --max 50` is the heaviest form of all (8-78s measured
  here) -- only when the answer really has to cover every folder. The folder
  counts above answer "is anything unread", and the folder cache a run leaves in
  `~/.cache/email-helper/` answers it for free but only as fresh as that run.

## When it is not enough

No sender, subject, body or attachment filter here: that is `nylas email search`
(`--from`, `--subject`, `--to`, `--has-attachment`, `--after`, `-l 20`, `-q` for
ids). Keep this script for the cross-account overview.

All the flags: `uv run src/agent_nylas/email_list.py --help`.
