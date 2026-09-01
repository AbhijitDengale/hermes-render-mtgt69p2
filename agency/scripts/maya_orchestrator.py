#!/usr/bin/env python3
"""MAYA's orchestration tick. Hermes cron, --no-agent, every 2 minutes.

This is the job that makes the pipeline run by itself. It contains no LLM and
makes no judgements: it reads lead states, and for each one runs the handler
the state machine says applies. Which agent gets dispatched, whether a message
may be queued, whether a follow-up is due — all of that is already decided by
the tables and by SENTINEL's approval. A model deciding any of it here would be
a second opinion competing with the gate.

Idempotence comes from four places, none of them this file:

  * a per-lead lease, so two ticks cannot work the same lead at once
  * compare-and-swap transitions, so a state can only move from what the
    caller expected
  * Kanban idempotency keys carrying the lead's lifecycle generation, so a
    retried dispatch lands on the same task
  * a content-derived idempotency key on the MailHub queue, so a retried send
    is recognised as a duplicate rather than sent twice

What this file adds is a whole-tick lock. The per-lead lease already prevents
double work, but a tick that overruns its two-minute slot would otherwise have
a second tick walking the same states behind it, and the log becomes very hard
to read. If the lock is held, this run simply does nothing and says nothing.

Terminal and human-owned states — HUMAN_REVIEW, CLOSED, UNSUBSCRIBED,
NEGATIVE, BOUNCED, POSITIVE, MEETING_STAGE, REPLIED, ERROR — have no handler,
so they are respected by never being touched. Paused campaigns are excluded by
pipeline.eligible().

Empty stdout means nothing happened, which is what --no-agent expects.
"""

import errno
import os
import sys
import time

LOCK = "/opt/data/agency/.maya-orchestrator.lock"
STALE_SECONDS = 900          # a tick that has held the lock this long is dead

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

# MAYA's own credential. The queue-capable key lives in the root profile's env;
# loading only what is needed keeps the rest of that file out of this process.
for candidate in ("/opt/data/.env",):
    if os.path.exists(candidate):
        with open(candidate, encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.partition("=")
                key = key.strip()
                if sep and key in ("MAILHUB_BASE_URL", "MAILHUB_API_TOKEN"):
                    os.environ.setdefault(key, value.strip())


def take_lock():
    """Exclusive, and self-healing if a previous tick died holding it."""
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        try:
            age = time.time() - os.path.getmtime(LOCK)
        except OSError:
            return None
        if age < STALE_SECONDS:
            return None                      # a live tick is running
        # The holder is long gone. Reclaim rather than wedging forever.
        try:
            os.unlink(LOCK)
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            return None
    os.write(fd, str(os.getpid()).encode())
    return fd


fd = take_lock()
if fd is None:
    sys.exit(0)                              # previous tick still working

try:
    import orchestrator  # noqa: E402

    lines = orchestrator.tick(limit=5)
    if lines:
        print("MAYA: %d action(s)" % len(lines))
        for line in lines:
            print("  " + line)
finally:
    try:
        os.close(fd)
        os.unlink(LOCK)
    except OSError:
        pass
