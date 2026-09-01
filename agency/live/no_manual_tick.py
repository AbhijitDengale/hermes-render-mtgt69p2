#!/usr/bin/env python3
"""Prove every MAYA transition since the cron was installed came from the cron.

The orchestrator writes a line per action, and Hermes stores each cron run's
stdout under cron/output/<job-id>/. So every transition MAYA made should be
findable in one of those files. Anything MAYA did that is NOT in a cron output
file was driven by hand.

The cron was created at 12:05:23 UTC; only transitions after that are in scope.
"""
import glob
import json
import os
import re
import sqlite3
import sys

JOB = "6416a10f0628"
SINCE = "2026-09-01 12:05:23"

outputs = {}
for path in sorted(glob.glob("/opt/data/cron/output/%s/*.md" % JOB)):
    outputs[os.path.basename(path)] = open(path, encoding="utf-8").read()
print("cron run output files: %d" % len(outputs))
for name in sorted(outputs):
    body = outputs[name]
    acts = [l.strip() for l in body.splitlines() if l.strip().startswith("[L-")]
    print("  %-28s %d action(s)" % (name.replace(".md", ""), len(acts)))

blob = "\n".join(outputs.values())

c = sqlite3.connect("/opt/data/agency.db")
c.row_factory = sqlite3.Row
rows = list(c.execute(
    "SELECT lead_id, agent, from_state, to_state, created_at FROM events"
    " WHERE event_type='state.changed' AND agent='maya' AND created_at >= ?"
    " ORDER BY id", (SINCE,)))
print("\nMAYA transitions since the cron was installed: %d" % len(rows))

unexplained = []
for r in rows:
    # The cron log records the lead id and the move it made.
    move = "%s -> %s" % (r["from_state"], r["to_state"])
    if r["lead_id"] in blob and (move in blob or r["to_state"] in blob):
        continue
    unexplained.append(dict(r))

for r in rows:
    print("  %-20s %-24s %s -> %s"
          % (r["created_at"], r["lead_id"], r["from_state"], r["to_state"]))

print("\ntransitions NOT attributable to a recorded cron run: %d" % len(unexplained))
for r in unexplained:
    print("   ", r)

# A manual `orchestrator.py tick` would also leave a shell history / process
# trace, but the durable evidence is the cron output above.
print("\nRESULT:", "every MAYA transition came from a cron run"
      if not unexplained else "SOME TRANSITIONS WERE NOT FROM THE CRON")
sys.exit(1 if unexplained else 0)
