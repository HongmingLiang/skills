---
title: Mail read
section: mail
---

# Mail read -- `email_read.py`

One `messages.find` per id: headers, body as text (HTML rendered), folders and
attachments. Read-only, so reading changes nothing -- not even the read flag.

## Recipes

```bash
# one, or several in the order given (fetched concurrently)
uv run scripts/email_read.py <message-id> <id-2>

# ids from the pipe: a listing, a JSON list, or a file
uv run scripts/email_list.py -u --days 3 --ids | uv run scripts/email_read.py -
uv run scripts/email_read.py - < /tmp/ids.txt

# attachments: listed on every read; --save downloads them into DIR, or into a
# fresh temp dir when DIR is omitted (never overwriting; keep it after the ids)
uv run scripts/email_read.py <message-id>
uv run scripts/email_read.py <message-id> --save
uv run scripts/email_read.py <message-id> --save /tmp/att

# raw payload, for something else to chew on
uv run scripts/email_read.py <message-id> -j
```

## Marking what you read

Reading leaves the message unread, which is usually the wrong end state. That flag
belongs to the CLI, one call per target:

```bash
nylas email mark read <message-id>          # or threads mark <thread-id> --read, for a whole thread
uv run scripts/email_list.py -u --days 3 --ids | while read -r id; do
  nylas email mark read "$id"
done
```

* Marking read is cheap, reversible and needs no approval gate -- that gate is for
  [send](mail-send.md) and [delete](mail-update.md). Do it when the user asked for
  it, or to leave the mailbox as you found it.
* Thread ids come from `email_list.py -j` (`thread_id`) or `nylas email threads
  list`; `--unread`, `--star` and `--unstar` are the other marks.

## What to know

* `-` reads stdin as plain ids, the JSON that `email_list.py -j` prints, or a JSON
  list; dialects mix, because each id is looked up in its own account.
* Output starts with `date  account [provider]`, plus `U`/`S` when unread and/or
  starred, then `Title`, `From`, `To`, `Cc`, `Folders`, `Attachments`, `ID`, then
  the body.
* Attachments are metadata on a plain read. `--save` fetches the bytes -- into
  `DIR`, or a fresh temp directory when `DIR` is omitted -- and each `saved_to`
  path in the output (`-j` included) says where they landed.
* A failed attachment download still prints the message and exits 1.

All the flags: `uv run scripts/email_read.py --help`.
