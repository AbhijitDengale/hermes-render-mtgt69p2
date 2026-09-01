#!/usr/bin/env python3
"""Post new human-review escalations to Discord. --no-agent --deliver discord.

No model decides what needs a human: that was settled deterministically when
LEO wrote the escalation row. This only announces what is already waiting, once
each, and stays silent when the queue is empty.
"""

import os
import sys

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

import review_tick  # noqa: E402

lines = review_tick.alerts()
if lines:
    print("\n".join(lines).rstrip())
