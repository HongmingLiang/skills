---
title: Setup
section: setup
---

# Setup

Two tools have to be on `PATH`: **`uv`** (runs `uv run scripts/<name>.py`; each
script declares its own dependencies) and the **`nylas` CLI** (v3.1.29 here;
every write path). Install them from [uv](https://docs.astral.sh/uv/getting-started/installation/) and the [Nylas CLI](https://cli.nylas.com/docs/commands) -- nothing here ships them.

Both read one variable, `NYLAS_FILE_STORE_PASSPHRASE`, and it is the Nylas API
key: the scripts use it as their API key, the CLI as the passphrase for its
encrypted credential store (`~/.config/nylas/.secrets.enc`).

```bash
# the key the CLI already stores, or one from https://dashboard-v3.nylas.com
export NYLAS_FILE_STORE_PASSPHRASE=$(nylas auth token)

# headless/CI only, once: store it in the CLI under the same value
NYLAS_DISABLE_KEYRING=true nylas auth config --api-key "$NYLAS_FILE_STORE_PASSPHRASE"
```

It has to be in the environment of whatever runs the command -- for an agent,
the agent's shell, not only the user's terminal.

```bash
nylas auth status --json           # "configured": true
uv run scripts/email_list.py -n 1  # the same accounts, through the scripts
```

Unset, the scripts exit 2 (`NYLAS_FILE_STORE_PASSPHRASE is not set`) and a CLI
without a keyring reports `"configured": false` -- an empty environment, not a
broken grant.
