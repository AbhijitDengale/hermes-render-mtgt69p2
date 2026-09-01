#!/usr/bin/env python3
"""ECHO's cron entrypoint. Lives in ECHO's own scripts directory.

A thin wrapper so the scheduled job and the module under /opt/data/agency stay
one implementation. Run by Hermes cron with --no-agent: no LLM decides whether
a prospect who already replied gets chased.

Silent when there is nothing to do — a job that speaks every tick trains you to
stop reading it.
"""

import os
import sys

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

import echo_tick  # noqa: E402

lines = echo_tick.tick()
if lines:
    print("ECHO: %d follow-up(s) evaluated" % len(lines))
    for line in lines:
        print("  " + line)
