#!/usr/bin/env python3
"""ORBIT's daily report. Hermes cron, --no-agent --deliver discord.

Runs from the root profile because that is the one with Discord configured,
but reads ORBIT's own read-only MailHub credential from ORBIT's profile
env rather than the ambient environment. Putting that key in the root .env
would shadow MAYA's queue-capable token and quietly take away her ability to
send, so the narrower credential is loaded here and nowhere else.

Every figure comes from a query, not from a model, so the report cannot drift
from the data it describes.
"""

import os
import pathlib
import sys

ENV = pathlib.Path("/opt/data/profiles/orbit/.env")

# Only the two variables the report needs, and only if the process does not
# already carry them. Loading the whole file would import unrelated secrets.
if ENV.exists():
    for line in ENV.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key in ("MAILHUB_BASE_URL", "MAILHUB_API_TOKEN"):
            os.environ.setdefault(key, value.strip())

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

import orbit  # noqa: E402

print(orbit.report(orbit.collect()))
