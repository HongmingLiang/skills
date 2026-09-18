---
title: Mail send and reply
section: mail
---

# Mail send and reply -- `nylas email send|reply|drafts`

The CLI's prompt is not an approval step: `nylas email send` prints an
`Email preview:` and asks `Send this email? [y/N]: ` on stdin, an agent shell has
none, so the default (`No`) wins, `Cancelled.` is printed and the exit code stays
`0` -- a cancelled send that looks successful. Approval is the conversation:

1. Show the message as it will go out: account, To/Cc/Bcc, subject, the body
   itself (not a summary), attachments, and the time if it is scheduled.
2. Wait for an explicit go-ahead. Silence, a topic change or a vague "looks
   fine?" is not one.
3. Only then send, with `-y`; if any field changes, show the new text and ask
   again -- the first yes covered the first draft.
4. Report the `Message ID:` the CLI prints, and read the message back (below).

Draft-first when the text is long, an attachment is involved, the user wants to
read it in their own mail client, or the answer may take a while: `drafts create`
sends nothing, so an unapproved draft simply stays a draft.

## Recipes

```bash
# SEND -- the approved text only
nylas email send hm.liang.ac@gmail.com --to bob@example.com --subject "Q3 numbers" \
  --body "$(cat /tmp/body.txt)" -y

# REPLY -- id from `email_list.py --ids` or `nylas email search "<query>" -q`
nylas email reply <message-id> hm.liang.ac@gmail.com --body "$(cat /tmp/body.txt)" -y

# DRAFT-FIRST -- create, show, send only on approval, delete if it is not going
nylas email drafts create hm.liang.ac@gmail.com --to bob@example.com \
  --subject "Q3 numbers" --body "$(cat /tmp/body.txt)" -a /tmp/q3.pdf
nylas email drafts show <draft-id>
nylas email drafts send <draft-id> -y        # after the go-ahead
nylas email drafts delete <draft-id> -f

# SCHEDULE -- goes out later unwatched, so approve the exact text and time
nylas email send hm.liang.ac@gmail.com --to bob@example.com --subject S \
  --body "$(cat /tmp/body.txt)" --schedule "tomorrow 9am" -y
nylas email scheduled list                   # what is queued
nylas email scheduled cancel <id> -f

# VERIFY -- the id the CLI printed: this finds it and shows its folder
uv run src/agent_nylas/email_read.py <message-id>
```

## What to know

* `-y` and `-q` both skip the prompt: `-y` because the user approved, `-q` by
  falling back to the default, which cancels -- so `-q` without `-y` is a silent
  no-op that exits `0`. Never reach for `-q` on a send.
* No `--body-file` and no `--attach`: write the body to a file and pass
  `--body "$(cat /tmp/body.txt)"`; attachments go on a draft.
* Reply, draft-send and scheduled-send are sends: same approval, same `-y`.
  `reply --all` adds the other original recipients (never yourself), and
  `--reply-to <message-id>` is the long form of a reply.
* Success prints `Email sent successfully! Message ID: <id>` (reply and draft say
  it their own way) -- that line is the evidence, not the exit code.
* Extras stay off unless the user asks: `--track-opens`/`--track-links` (visible
  to the recipient), `--sign`/`--encrypt`, `--metadata`, `--signature-id`.
* `-i/--interactive` and a bare `drafts create` want a terminal: not usable here.

All the flags: `nylas email send --help`, `nylas email reply --help`,
`nylas email drafts --help`, `nylas email scheduled --help`.
