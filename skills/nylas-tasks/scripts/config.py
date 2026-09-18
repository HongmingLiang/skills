"""
config -- tunables shared by the read-only Nylas commands in this directory.

Everything an operator might want to change lives here: how the API key is
found, which endpoint is used, HTTP timeouts and retry policy, where the
identifier cache is kept, and the default fetch sizes.

What does not belong here: anything a single command owns -- the field
projection it asks the API for, its column widths, and its own flag defaults
that are not shared (those stay next to the code that uses them), so this file
never turns into a grab bag.

The API key itself is never stored; it is read at runtime from the environment
variable named in API_KEY_ENV.
"""

import os
from pathlib import Path

# ----------------------------------------------------------------- credentials

# Name of the environment variable holding the Nylas API key. The `nylas` CLI
# uses the same variable as its file-store passphrase, so the CLI and these
# commands cannot drift apart.
API_KEY_ENV = "NYLAS_FILE_STORE_PASSPHRASE"

# Nylas endpoint; override with NYLAS_API_URI for EU/self-hosted deployments.
API_URI = os.environ.get("NYLAS_API_URI", "https://api.us.nylas.com")

# ------------------------------------------------------------------------ HTTP

# Seconds. A 50-message Microsoft page can need 5-10s, and a proxy in the path
# adds more.
REQUEST_TIMEOUT_SECONDS = 60

# Transport failures are retried, because a single flaky connection used to
# fail a whole account. Safe here only because every call in these scripts is a
# read: dropping the retry would also drop the risk of duplicate writes, so
# write paths must not reuse it as-is.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0  # multiplied by the attempt number
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# ------------------------------------------------------------ identifier cache

# Cached grants/folders/calendars are considered fresh for this long.
CACHE_TTL_SECONDS = 12 * 60 * 60
CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "nylas-tasks"
)

# --------------------------------------------------------------------- fetching

# The Nylas API caps a single list page at 200 items.
MAX_PAGE_SIZE = 200

# Safe page size per provider. Measured throughput differs a lot: Google and
# IMAP deliver a 100-item page in about 3s, while Microsoft needs ~0.1-0.2s per
# message, so a 200-item Microsoft page blows past the request timeout.
PAGE_SIZE_BY_PROVIDER = {"microsoft": 50}

DEFAULT_WORKERS = 4  # concurrent accounts

# Default fetch sizes for the mail view: the per-account message limit when -n
# is omitted, and the per-account cap that --all paginates up to.
DEFAULT_MESSAGE_LIMIT = 10
DEFAULT_MESSAGE_MAX = 200

# -------------------------------------------------------------- calendar view

# Default window length for event_list.py, in days from the window start
# (today).
DEFAULT_CALENDAR_DAYS = 365

# Default account selector for event_list.py. The calendar view is scoped to the
# one account that owns the user's calendar, Microsoft (Outlook) by default;
# NYLAS_CALENDAR_ACCOUNT overrides it (a provider, an address, or a grant id),
# and -a overrides both.
DEFAULT_CALENDAR_ACCOUNT = os.environ.get("NYLAS_CALENDAR_ACCOUNT", "microsoft")


def page_size(provider: str | None) -> int:
    """Page size to use for one provider."""
    return PAGE_SIZE_BY_PROVIDER.get((provider or "").lower(), MAX_PAGE_SIZE)
