---
title: Mail send and reply
section: mail
---

# Mail send and reply -- `nylas email send|reply|drafts`

Sending changes state, so it lives in the `nylas` CLI and never in
`src/agent_nylas/`. Behaviour below is from nylas 3.1.29 (source and live runs).

## The rule: the user approves the text, then the agent sends

`nylas email send` prints an `Email preview:` and then asks
`Send this email? [y/N]: ` on stdin. An agent shell has no stdin, so that line
reads an empty answer, takes the default (`No`), prints `Cancelled.` and **exits
0** -- a cancelled send looks exactly like a successful one if you go by the exit
code. The prompt is not an approval step, it is a no-op here.

The approval is the conversation:

1. Show the user the message as it will go out: account, To/Cc/Bcc, subject, the
   body itself (not a summary), attachments, and the send time if scheduled.
2. Wait for an explicit go-ahead. Silence, a topic change or a vague "looks
   fine?" is not one.
3. Only then run the send with `-y`, never before it.
4. If the user changes any of it, show the new text and ask again -- the first
   yes covered the first draft, not the edited one.
5. Report the `Message ID:` the CLI prints, and check the send happened (below).

Draft-first is the better route when the text is long, the user wants to read it
in their own mail client, there are attachments, or the answer may take a while:
`drafts create` sends nothing, so a draft nobody approves simply stays a draft.
`email send` is the shorter path once the text is agreed.

## Recipes

```bash
# which account is which; the DEFAULT column is what a bare send would use
nylas auth list

# SEND -- only after the user approved this exact text
nylas email send hm.liang.ac@gmail.com --to bob@example.com --cc lead@example.com \
  --subject "Q3 numbers" --body "$(cat /tmp/body.txt)" -y

# REPLY -- message id from `email_list.py --ids`, or `nylas email search "<query>" -q`;
# `--all` adds the other original recipients (Cc), never yourself
nylas email reply <message-id> hm.liang.ac@gmail.com --body "$(cat /tmp/body.txt)" -y

# DRAFT-FIRST -- create, show the user, send only on approval
nylas email drafts create hm.liang.ac@gmail.com --to bob@example.com \
  --subject "Q3 numbers" --body "$(cat /tmp/body.txt)" -a /tmp/q3.pdf
nylas email drafts show <draft-id>            # what the user is approving
nylas email drafts send <draft-id> -y         # after the go-ahead
nylas email drafts delete <draft-id> -f       # if it is not going out

# SCHEDULE -- goes out later without anyone watching, so approval counts twice
nylas email send hm.liang.ac@gmail.com --to bob@example.com --subject S \
  --body "$(cat /tmp/body.txt)" --schedule "tomorrow 9am" -y
nylas email scheduled list                    # what is queued
nylas email scheduled cancel <id> -f

# VERIFY -- the id the CLI printed: this finds the message and shows its folder
uv run src/agent_nylas/email_read.py <message-id>
```

## What to know

* The account is the first positional argument (`<grant-id|email>`). Without it
  the send uses the active grant, which here is `hongming.liang@outlook.com`, so
  pass the account explicitly unless that is the one you mean. A wrong one fails
  fast (`no grant found for email: ...`, exit 1) before anything is sent.
* `-y` and `-q` both skip the prompt: `-y` because the user already approved,
  `-q` by falling back to the default -- which cancels. So `-q` without `-y` is a
  silent no-op that exits 0. Never reach for `-q` on a send.
* `email send` has neither `--body-file` nor `--attach`: write the body to a file
  and pass `--body "$(cat /tmp/body.txt)"`, and put attachments on a draft
  (`drafts create -a FILE`, repeatable). `--body` takes HTML as well as text.
* Reply, draft-send and scheduled-send are all sends: same approval, same `-y`.
  `nylas email send --reply-to <message-id>` is the long form of a reply.
* A successful send prints `Email sent successfully! Message ID: <id>`
  (reply: `Reply sent successfully! ...`, draft: `Draft sent successfully!`).
  That line is the evidence, not the exit code.
* Extras stay off unless the user asks for them: `--track-opens` /
  `--track-links` (the recipient can see them), `--sign` / `--encrypt` (GPG),
  `--metadata`, `--signature-id`.
* `-i/--interactive`, a bare `drafts create` without `--body`, and
  `--list-gpg-keys` style wizards want a human at a terminal: not usable here.
* The CLI animates spinners even when piped, so a captured output has junk in it
  -- `tail`/`grep 'Message ID\|Error'` it before showing the user.

## When it is not enough

Everything else about composing is a flag on `nylas email send`; nothing in this
repo duplicates it. Reading back what was sent is ours: `email_read.py
<message-id>`.

All the flags: `nylas email send --help`, `nylas email reply --help`,
`nylas email drafts --help`, `nylas email scheduled --help`.
