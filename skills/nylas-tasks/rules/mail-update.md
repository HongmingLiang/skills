---
title: Mail unread, star, move, delete
section: mail
---

# Mail unread, star, move, delete -- `nylas email mark|move|delete`

Marking a message **read** belongs to the reading flow:
[mail-read.md](mail-read.md). Everything here changes the mailbox, and only the
last section cannot be undone.

## Recipes

```bash
# back to unread, or a star on and off
nylas email mark unread <message-id>
nylas email mark starred <message-id>        # unstarred takes it off again
nylas email threads mark <thread-id> --unread

# move: --folder takes an ID, so ask for ids first
uv run scripts/email_list.py --folders -a google
nylas email move <message-id> --folder <folder-id>
nylas email move <message-id> --archive      # clears every folder/label

# delete -- only with -f, and only after the user approved this exact message
nylas email delete <message-id> -f
nylas email threads delete <thread-id> -f
```

## What to know

* `delete` prompts. With no stdin it prints `Are you sure you want to delete
  message <id>? [y/N]: ` and then `Cancelled.` with **exit 0** -- a delete that
  looks like a success if you read the exit code. It only happens with
  `-f/--force`, which therefore means "the user asked for this exact message to
  go": confirm the target first, the way [mail-send.md](mail-send.md) does for
  sending. Marks and moves need no such gate.
* One target per call and no prompt; a batch is a loop, one call per target.
* `move --folder` wants a folder **ID** -- `email_list.py --folders` prints them;
  `--archive` clears every folder and label. Nothing here undoes in one step, but
  moving back is one more call.

All the flags: `nylas email mark --help`, `nylas email threads mark --help`,
`nylas email move --help`, `nylas email delete --help`.
