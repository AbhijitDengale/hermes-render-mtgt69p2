#!/usr/bin/env python3
"""Supabase lead sync. Hermes cron, --no-agent, every 2 minutes.

Claims ready leads into agency.db and drains pending write-backs. It does no
research and sends no mail: once a lead is at NEW, maya-orchestrator picks it
up exactly as it would a lead typed in by hand.

Silent when there is nothing to do — a job that speaks every two minutes
trains you to stop reading it.
"""

import os
import sys

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

# The Supabase secret lives in the root profile's env and is read here only.
# NOVA, ARIA, SENTINEL and LEO never see it.
for path in ("/opt/data/.env",):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.partition("=")
                key = key.strip()
                if sep and key in ("SUPABASE_URL", "SUPABASE_SECRET_KEY",
                                   "SUPABASE_LEAD_BATCH_SIZE",
                                   "AGENCY_DAILY_LEAD_TARGET",
                                   "SUPABASE_CAMPAIGN"):
                    os.environ.setdefault(key, value.strip())

import supabase_sync as S  # noqa: E402

if not S.configured():
    sys.exit(0)            # not wired up yet; nothing to say

lines = []
claimed = S.claim()
if claimed.get("imported") or claimed.get("rejected") or claimed.get("errors"):
    lines.append("claimed %d, imported %d, duplicate %d, released %d"
                 % (claimed.get("claimed", 0), claimed.get("imported", 0),
                    claimed.get("duplicate", 0), claimed.get("released", 0)))
    for e in (claimed.get("errors") or [])[:3]:
        lines.append("  error: %s" % e)

drained = S.drain()
if drained.get("synced") or drained.get("failed"):
    lines.append("write-back: %d synced, %d deferred, %d failed"
                 % (drained.get("synced", 0), drained.get("deferred", 0),
                    drained.get("failed", 0)))

if lines:
    print("SUPABASE SYNC")
    for line in lines:
        print("  " + line)
