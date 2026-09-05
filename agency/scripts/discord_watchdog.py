#!/usr/bin/env python3
"""Cron entry point for the Discord credential watchdog.

Hermes cron, --no-agent, delivered locally: the whole point of this job is to
run when Discord is broken, so it must never depend on Discord to report. Its
output is the run log.

Cheap by design. When Discord is healthy — which is almost always — a tick is
two file reads and no network call at all.
"""

import os
import sys

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("HERMES_HOME", "/opt/data")

import discord_health as DH  # noqa: E402

print(DH.format_line(DH.tick()))
