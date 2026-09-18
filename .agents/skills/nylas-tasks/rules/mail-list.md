---
title: Mail list and query
section: mail
---

# Mail list and query -- `email_list.py`

Read-only: the only API call is `messages.list`, with a narrow projection (no
bodies, so a listing costs a few KiB per account). Output is one block per
account, newest first, never merged across accounts.

## Recipes

```bash
# newest 10 per account -- the default, no flags needed
uv run src/agent_nylas/email_list.py

# unread, last three days, one account
uv run src/agent_nylas/email_list.py -u --days 3 -a outlook

# one folder by name (case-insensitive, a unique substring is enough);
# scope it with -a, a folder that only exists in one account fails the others
uv run src/agent_nylas/email_list.py -a outlook -f dev

# everything, any folder, newest 50 per account
uv run src/agent_nylas/email_list.py --all-folders --all --max 50

# bare ids, one per line -> read what the listing showed
uv run src/agent_nylas/email_list.py -u --days 3 --ids | uv run src/agent_nylas/email_read.py -

# prove nothing is unread anywhere (also the answer when -u looks suspiciously empty)
uv run src/agent_nylas/email_list.py -u --all-folders --all --max 50

# machine readable: date, sender, subject
uv run src/agent_nylas/email_list.py -u --days 3 -j \
  | python3 -c 'import json,sys
for acct in json.load(sys.stdin)["accounts"]:
    for msg in acct["messages"]:
        print(msg["date_iso"], msg["sender_email"], msg["subject"])'

# unread counts per folder, from the cache, without touching the API
python3 -c 'import glob,json,os
for path in glob.glob(os.path.expanduser("~/.cache/email-helper/folders-*.json")):
    for folder in json.load(open(path))["data"]:
        print(folder["unread_count"], folder["name"])'
```

## What to know

* How many: `-n N` is the per-account limit (default 10). `--max N` only caps a
  `--all` run -- given alone it is ignored silently, so `--max 3` lists 10.
* Every row starts with a two-character mark: `U` unread, `S` starred, `-` for
  neither, so `-S` is starred but already read.
* Zero unread is normal here (the accounts are mostly read), so an empty `-u`
  result is a real answer, not a broken filter.
* `-a` takes one comma-separated value: `all` (default), an email substring
  (`outlook`, `pku`), a provider (`google`, `imap`) or a grant id. Repeating the
  flag does not accumulate -- `-a gmail -a pku` keeps only `pku`.
* Without `-f` the script reads the inbox, found through the provider's `\Inbox`
  attribute, so localized names work. A `-f` that matches nothing or several
  folders exits 1 with the candidates in the error, and the "nothing matches"
  error lists every folder -- that is how you discover the names. Folders are
  per account, so pair `-f` with `-a` unless every account has that folder.
* `--days N` filters server-side, so `--days 0` matches nothing. The message
  count in the header is what the run fetched, not what the folder holds.
* `-j` rows carry `date` (Unix seconds) and `date_iso` (local): use `date_iso`.
  `--ids` prints bare ids only, from any account -- `email_read.py` finds the
  right account for each id itself.
* Exit `0` clean; `1` something failed (the rest is still printed and failures go
  to stderr); `2` bad arguments or a missing API key.

## When it is not enough

There is no sender, subject, body or attachment filter here -- that is what
`nylas email search` is for (`--from`, `--subject`, `--to`, `--has-attachment`,
`--after`, `-l 20`, `-q` for ids). Keep our script for the cross-account
overview.

All the flags: `uv run src/agent_nylas/email_list.py --help`.
