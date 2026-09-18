# agent-nylas

Read-only Nylas mail and calendar scripts, packaged as the `nylas-tasks` skill
for pi and other Agent Skills clients. Everything here lists and reads; sending,
replying, deleting, moving and RSVPing are left to the `nylas` CLI.

## Use

The skill lives in `skills/nylas-tasks` (discovered through the
`.agents/skills/nylas-tasks` symlink) and needs a Nylas API key in
`NYLAS_FILE_STORE_PASSPHRASE`. Each script declares its own dependencies, so
[uv](https://docs.astral.sh/uv/) can run it from anywhere:

```bash
cd skills/nylas-tasks
uv run scripts/email_list.py            # newest mail per account
uv run scripts/email_read.py <id>       # one message in full
uv run scripts/event_list.py --days 7   # upcoming events
```

Each script takes `--help`; `SKILL.md` and its `rules/` hold the flags and the
CLI fallbacks.

## Development

The two official Nylas skills are installed for reference while developing this
one, and are not tracked here:

```bash
npx skills add nylas/skills -a universal -y
```

`-a universal` keeps the install inside `.agents/skills/` -- without it the CLI
targets Pi and writes into `.pi/skills/`. Refresh them with `npx skills update`.
