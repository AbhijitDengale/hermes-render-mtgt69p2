#!/usr/bin/env python3
"""Pull outreach replies from MailHub and hand them to LEO.

Only messages MailHub has already classified `is_outreach_reply` are visible on
that endpoint, so nothing else in the mailbox can reach an agent.

Order matters more than anything else here:

    1. record the reply (deduplicated)
    2. CANCEL every scheduled follow-up
    3. move the lead
    4. dispatch LEO to classify

Cancellation happens before any reasoning, and never depends on it. LEO can
fail, time out, or be wrong; the prospect must still not be chased. The worst
outcome this system can produce is emailing someone who already answered.

One refinement on the plain reading of "a reply moves the lead to REPLIED": an
autoresponder is not the prospect answering. An out-of-office bounces back
within seconds and would otherwise burn the lead permanently, since REPLIED has
no route back to the follow-up sequence. Auto-replies therefore cancel
follow-ups (safe) and hold the lead where it is, and LEO decides whether to
reschedule against a parsed return date or escalate.

    python3 inbound_processor.py poll
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import followups as F  # noqa: E402
import pipeline as P   # noqa: E402

MAILHUB_BASE = os.getenv("MAILHUB_BASE_URL", "").rstrip("/")
MAILHUB_TOKEN = os.getenv("MAILHUB_API_TOKEN", "")
HERMES = os.getenv("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
AGENT = "maya"


def mailhub(method: str, path: str,
            body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not MAILHUB_BASE or not MAILHUB_TOKEN:
        return {"error": "MailHub not configured"}
    req = urllib.request.Request(
        MAILHUB_BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + MAILHUB_TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": "http %d" % e.code, "detail": e.read().decode()[:300]}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def dispatch_leo(lead_id: str, reply_id: int) -> Optional[str]:
    key = "agency:%s:leo:%d" % (lead_id, reply_id)
    cmd = [HERMES, "kanban", "create", "Classify reply for %s" % lead_id,
           "--assignee", "leo", "--idempotency-key", key, "--json",
           "--body",
           "You are LEO. Call get_reply with reply_id %d to read the prospect's "
           "message, then call submit_classification.\n\n"
           "You do not negotiate. Price, discounts, legal or payment terms, "
           "guarantees and contracts are never yours to answer — classify them "
           "and let a human decide." % reply_id]
    env = {**os.environ, "HOME": os.getenv("HERMES_HOME", "/opt/data"),
           "HERMES_HOME": os.getenv("HERMES_HOME", "/opt/data")}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                             env=env)
        try:
            return (json.loads((out.stdout or "").strip()) or {}).get("id")
        except Exception:
            return None
    except Exception:
        return None


def record(con, ev: Dict[str, Any]) -> Optional[int]:
    """Insert the reply, or return None if we have already seen it.

    The UNIQUE on provider_message_id is what makes "exactly once" a database
    guarantee rather than a code path that must not be re-entered.
    """
    cur = con.execute(
        "INSERT OR IGNORE INTO inbound_replies "
        " (provider_message_id, provider_thread_id, mailhub_inbound_id, lead_id,"
        "  campaign_id, account_id, from_email, to_email, subject, body_text,"
        "  received_at, matched_by, is_bounce, is_auto_reply) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ev.get("provider_message_id"), ev.get("provider_thread_id"),
         ev.get("inbound_id"), ev.get("lead_id"), ev.get("campaign_id"),
         ev.get("account_id"), ev.get("from"), ev.get("to_email"),
         ev.get("subject"), ev.get("body_text"), ev.get("received_at"),
         ev.get("matched_by"), 1 if ev.get("is_bounce") else 0,
         1 if ev.get("is_auto_reply") else 0))
    if cur.rowcount == 0:
        return None
    row = con.execute("SELECT id FROM inbound_replies WHERE provider_message_id=?",
                      (ev.get("provider_message_id"),)).fetchone()
    return row["id"] if row else None


def process_one(con, ev: Dict[str, Any]) -> str:
    lead_id = ev.get("lead_id")

    with P.writing(con):
        reply_id = record(con, ev)
    if reply_id is None:
        return "already processed %s" % ev.get("provider_message_id")

    if not lead_id or P.get_lead(con, lead_id) is None:
        # Matched to our outreach but not to a lead we hold. Stored, not acted
        # on — inventing a lead here would be worse than doing nothing.
        return "reply %d has no known lead; recorded only" % reply_id

    lead = P.get_lead(con, lead_id)

    # --- 1. CANCEL FIRST, before anything reasons about the content ---------
    with P.writing(con):
        n = F.cancel_all(con, lead_id, "reply received", agent="inbound")
        con.execute("UPDATE inbound_replies SET followups_cancelled=1,"
                    "       cancelled_count=? WHERE id=?", (n, reply_id))

    # --- 2. move the lead ---------------------------------------------------
    moved = "held"
    if ev.get("is_bounce"):
        target = "BOUNCED"
    elif ev.get("is_auto_reply"):
        target = None            # an autoresponder is not the prospect replying
    else:
        target = "REPLIED"

    if target:
        try:
            with P.writing(con):
                P.transition(con, lead_id, target, "inbound",
                             "inbound reply %d" % reply_id)
            moved = target
        except P.TransitionError as exc:
            moved = "not moved (%s)" % exc

    # --- 3. only now, ask LEO what it means ---------------------------------
    task = dispatch_leo(lead_id, reply_id)
    if task:
        with P.writing(con):
            con.execute("UPDATE inbound_replies SET leo_task_id=? WHERE id=?",
                        (task, reply_id))

    # Consume it in MailHub so the same reply is not offered again.
    if ev.get("inbound_id"):
        mailhub("POST", "/api/v1/inbound/%s/consume" % ev["inbound_id"])

    return ("reply %d lead=%s cancelled=%d -> %s leo=%s"
            % (reply_id, lead_id, n, moved, task or "not dispatched"))


def poll(limit: int = 25) -> List[str]:
    res = mailhub("GET", "/api/v1/inbound?limit=%d" % limit)
    if res.get("error"):
        return ["MailHub: %s" % res]
    out = []
    with P.connect() as con:
        for ev in res.get("messages", []):
            try:
                out.append(process_one(con, ev))
            except Exception as exc:
                out.append("error on %s: %s: %s"
                           % (ev.get("provider_message_id"),
                              type(exc).__name__, exc))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll"); p.add_argument("--limit", type=int, default=25)
    sub.add_parser("list")
    args = ap.parse_args(argv)

    if args.cmd == "poll":
        for line in poll(args.limit):
            print("  " + line)
        return 0

    with P.connect() as con:
        print("%-4s %-22s %-18s %-16s %-6s %s"
              % ("ID", "LEAD", "CLASSIFICATION", "FROM", "CANC", "SUBJECT"))
        for r in con.execute(
                "SELECT id, lead_id, classification, from_email, cancelled_count,"
                "       subject FROM inbound_replies ORDER BY id"):
            print("%-4s %-22s %-18s %-16s %-6s %s"
                  % (r["id"], r["lead_id"] or "-", r["classification"] or "-",
                     (r["from_email"] or "")[:16], r["cancelled_count"],
                     (r["subject"] or "")[:36]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
