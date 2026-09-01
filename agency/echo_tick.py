#!/usr/bin/env python3
"""ECHO's scheduled tick. Installed as a Hermes cron job with --no-agent.

Deliberately has no LLM in it. This is the code that decides whether a
prospect who has already replied gets chased anyway, and that decision must be
a state check, not a judgement call. Hermes cron supplies the timing; the
verdict is deterministic.

ECHO holds no MailHub credential and cannot send. When a follow-up is genuinely
due it hands the lead to MAYA by moving it to FOLLOWUP_PENDING, and the normal
pipeline takes over — ARIA writes, SENTINEL approves the exact text, MAYA
queues. A follow-up gets its own approval like any other message.

Empty stdout means nothing happened, which is what --no-agent expects.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import followups as F  # noqa: E402
import pipeline as P   # noqa: E402


def tick(limit: int = 20) -> list:
    out = []
    with P.connect() as con:
        for row in F.due(con, limit):
            lead_id, stage, fid = row["lead_id"], row["stage"], row["id"]

            # Live state, read now — not what was true when this was scheduled.
            reason = F.blocked_reason(row)
            if reason:
                with P.writing(con):
                    F.mark_skipped(con, fid, reason)
                    con.execute(
                        "INSERT INTO events (lead_id, campaign_id, agent,"
                        "  event_type, detail) VALUES (?,?,?,?,?)",
                        (lead_id, row["campaign_id"], "echo",
                         "followup.skipped",
                         "stage %d: %s" % (stage, reason)))
                out.append("skip %s stage %d: %s" % (lead_id, stage, reason))
                continue

            try:
                with P.writing(con):
                    con.execute(
                        "UPDATE leads SET followup_stage=?,"
                        "       updated_at=datetime('now') WHERE id=?",
                        (stage, lead_id))
                    P.transition(con, lead_id, "FOLLOWUP_PENDING", "echo",
                                 "follow-up %d is due" % stage)
                    F.touch(con, fid)
                out.append("due  %s stage %d -> FOLLOWUP_PENDING" % (lead_id, stage))
            except P.TransitionError as exc:
                # Losing this race is normal and safe: something else moved the
                # lead, which is exactly the situation where not sending is right.
                with P.writing(con):
                    F.mark_skipped(con, fid, str(exc)[:180])
                out.append("skip %s stage %d: %s" % (lead_id, stage, exc))
    return out


if __name__ == "__main__":
    lines = tick()
    if lines:
        print("ECHO: %d follow-up(s) evaluated" % len(lines))
        for line in lines:
            print("  " + line)
    # Silence when there is nothing to do — a cron job that speaks every
    # minute trains you to ignore it.
