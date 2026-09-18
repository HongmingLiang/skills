# agent-nylas

Read-only Nylas mail and calendar scripts, packaged as an agent skill for pi and
other Agent Skills clients.

Everything here lists and reads: the only API calls are `messages.list`,
`messages.find`, `folders.list`, `grants.list`, and `events.list`. Sending,
replying, deleting, moving, starring, and RSVPing are done with the `nylas` CLI,
and the skill points at the rule file for each of those paths.

## Layout

| Path | What it is |
| --- | --- |
| `skills/nylas-tasks/` | The skill authored here: `SKILL.md`, on-demand `rules/`, and its `scripts/` |
| `skills/nylas-tasks/scripts/` | `email_list.py`, `email_read.py`, `event_list.py` |
| `.agents/skills/` | Discovery and install directory: a symlink to the skill above (tracked) plus installed third-party skills (ignored) |

## Use

With [uv](https://docs.astral.sh/uv/) and the `nylas` CLI installed. Paths are
relative to the skill directory, and each script declares its own dependencies,
so it runs wherever the skill is installed:

```bash
cd skills/nylas-tasks
uv run scripts/email_list.py             # newest 10 per account
uv run scripts/email_list.py -u --days 3 # unread, last three days
uv run scripts/email_read.py <message-id>
uv run scripts/event_list.py --days 7
```

Each script takes `--help`. `skills/nylas-tasks/rules/` documents the flags and
the CLI fallbacks in more detail.

## Environment

| Variable | Used for |
| --- | --- |
| `NYLAS_FILE_STORE_PASSPHRASE` | Nylas API key, shared with the `nylas` CLI |
| `NYLAS_API_URI` | Override the endpoint (default `https://api.us.nylas.com`) |
| `NYLAS_CALENDAR_ACCOUNT` | Default account for `event_list.py` when `-a` is absent (default `microsoft`) |
| `XDG_CACHE_HOME` | Where the cached grant/folder/calendar ids live (default `~/.cache/nylas-tasks`) |

## Development

```bash
uv run pyright
uvx ruff check skills
```

The entry scripts repeat the dependency pins inline (PEP 723) so the skill runs
standalone; keep them in sync with `[project].dependencies` in `pyproject.toml`.

### Third-party skills

`nylas-api` and `nylas-cli` are dev-time reference skills from
[nylas/skills](https://github.com/nylas/skills). Neither they nor the local pin
file are tracked: `skills-lock.json` and `.agents/skills/` are git-ignored, so a
fresh clone installs them once.

```bash
npx skills add nylas/skills -a universal -y   # install or refresh
npx skills update                             # move to the upstream latest
```

`-a universal` keeps the install inside `.agents/skills/`; without it the CLI
targets Pi and writes symlinks into `.pi/skills/`.
