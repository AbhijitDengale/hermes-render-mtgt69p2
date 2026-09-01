#!/usr/bin/env python3
"""Announce new human-review escalations to Discord.

Runs as a Hermes cron job with --no-agent --deliver discord: whatever this
prints is posted, and printing nothing posts nothing. No model is involved in
deciding what needs a human — that was already decided deterministically when
the escalation row was written.

Each escalation is announced once. `notified_at` is set in the same breath as
the output, so a tick that runs twice does not repost.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline as P  # noqa: E402

MAX_PER_TICK = int(os.getenv("REVIEW_ALERTS_PER_TICK", "5"))


def clip(text, n):
    text = " ".join((text or "").split())
    return text[:n] + ("…" if len(text) > n else "")


def alerts() -> list:
    lines = []
    with P.connect() as con:
        rows = list(con.execute(
            "SELECT h.*, l.business_name, l.state AS lead_state, l.country,"
            "       l.niche, r.classification, r.confidence, r.from_email,"
            "       r.subject, r.body_text, r.received_at "
            "  FROM human_escalations h "
            "  LEFT JOIN leads l ON l.id = h.lead_id "
            # The newest reply only. Joining the whole table would post one
            # copy of the same escalation for every reply the lead has sent.
            "  LEFT JOIN inbound_replies r ON r.id = ("
            "        SELECT id FROM inbound_replies WHERE lead_id = h.lead_id"
            "         ORDER BY COALESCE(received_at, '') DESC, id DESC LIMIT 1) "
            " WHERE h.notified_at IS NULL AND h.status = 'open' "
            " ORDER BY h.created_at LIMIT ?", (MAX_PER_TICK,)))

        for r in rows:
            lines.append("**HUMAN REVIEW — %s**" % (r["reason"] or "escalation"))
            lines.append("`%s`  ·  %s  ·  %s"
                         % (r["id"], r["business_name"] or "unknown company",
                            r["lead_id"] or "-"))
            lines.append("campaign `%s`  ·  state `%s`%s"
                         % (r["campaign_id"] or "-", r["lead_state"] or "-",
                            ("  ·  " + r["country"]) if r["country"] else ""))
            if r["classification"]:
                lines.append("LEO: **%s** (confidence %.2f)"
                             % (r["classification"], r["confidence"] or 0))
            if r["from_email"]:
                lines.append("reply from %s — %s"
                             % (r["from_email"], clip(r["subject"], 70)))
            if r["body_text"]:
                lines.append("> %s" % clip(r["body_text"], 260))
            if r["reply_summary"]:
                lines.append("summary: %s" % clip(r["reply_summary"], 200))
            if r["recommended_action"]:
                lines.append("suggested: %s" % clip(r["recommended_action"], 180))
            if r["draft_response"]:
                lines.append("draft reply: %s" % clip(r["draft_response"], 260))
            lines.append("raised %s" % (r["created_at"] or ""))
            lines.append("Reply with: `review approve %s` · `reject` · "
                         "`edit --text \"…\"` · `close` · `dnc` · `resume`"
                         % r["id"])
            lines.append("")

        if rows:
            with P.writing(con):
                for r in rows:
                    con.execute(
                        "UPDATE human_escalations SET notified_at=datetime('now') "
                        " WHERE id=? AND notified_at IS NULL", (r["id"],))
                    con.execute(
                        "INSERT INTO audit_logs (actor, action, subject_type,"
                        "  subject_id, detail) VALUES "
                        "('orbit-review','escalation.notified','escalation',?,?)",
                        (r["id"], "posted to Discord"))
    return lines


if __name__ == "__main__":
    out = alerts()
    if out:
        print("\n".join(out).rstrip())
    # Silence when there is nothing waiting.
