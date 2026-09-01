#!/usr/bin/env python3
"""Human review actions — what happens after an escalation reaches Discord.

Every action is deterministic and audited. None of them can send mail on their
own: an approved reply still goes through SENTINEL and MailHub like any other
message, because a human saying "yes" is a decision about intent, not a
bypass of the gate that checks the words actually leaving.

    python3 review.py list
    python3 review.py approve <id> [--note "..."]
    python3 review.py reject  <id> [--note "..."]
    python3 review.py edit    <id> --text "the reply I actually want sent"
    python3 review.py close   <id>
    python3 review.py dnc     <id>
    python3 review.py resume  <id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import followups as F  # noqa: E402
import pipeline as P   # noqa: E402

MAILHUB_BASE = os.getenv("MAILHUB_BASE_URL", "").rstrip("/")
MAILHUB_TOKEN = os.getenv("MAILHUB_API_TOKEN", "")

# What each action may leave the lead as. Anything outside the state machine's
# legal set is refused by transition() anyway; this is the intent.
ACTIONS = ("approve", "reject", "edit", "close", "dnc", "resume")


def mailhub(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if not MAILHUB_BASE or not MAILHUB_TOKEN:
        return {"error": "MailHub not configured"}
    req = urllib.request.Request(MAILHUB_BASE + path,
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + MAILHUB_TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": "http %d" % e.code, "detail": e.read().decode()[:300]}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def audit(con, esc_id: str, lead_id: Optional[str], action: str,
          detail: str, actor: str) -> None:
    con.execute(
        "INSERT INTO audit_logs (actor, action, subject_type, subject_id, detail) "
        "VALUES (?,?, 'escalation', ?, ?)",
        (actor, "review.%s" % action, esc_id, detail[:1000]))
    if lead_id:
        con.execute(
            "INSERT INTO events (lead_id, agent, event_type, detail) "
            "VALUES (?,?,?,?)",
            (lead_id, actor, "review.%s" % action, detail[:400]))


def get(con, esc_id: str):
    return con.execute("SELECT * FROM human_escalations WHERE id=?",
                       (esc_id,)).fetchone()


def act(esc_id: str, action: str, actor: str, note: str = "",
        text: str = "") -> Dict[str, Any]:
    if action not in ACTIONS:
        return {"error": "unknown action %r; valid: %s" % (action, ", ".join(ACTIONS))}

    with P.connect() as con:
        esc = get(con, esc_id)
        if not esc:
            return {"error": "no such escalation: %s" % esc_id}
        if esc["status"] != "open" and action != "resume":
            # Acting twice on one escalation is almost always a double-click or
            # a repeated Discord command, not a second decision.
            return {"error": "escalation %s is already %s"
                             % (esc_id, esc["status"]), "no_change": True}

        lead_id = esc["lead_id"]
        lead = P.get_lead(con, lead_id) if lead_id else None
        out: Dict[str, Any] = {"escalation": esc_id, "action": action,
                               "lead": lead_id}

        if action == "approve":
            # Approval records intent. It does NOT send: the draft has to be
            # written as a real message and pass SENTINEL like anything else.
            with P.writing(con):
                con.execute(
                    "UPDATE human_escalations SET status='approved',"
                    "       human_response=?, resolved_at=datetime('now'),"
                    "       resolved_by=?, action='approve' WHERE id=?",
                    (note or None, actor, esc_id))
                audit(con, esc_id, lead_id, "approve", note or "approved", actor)
            out["note"] = ("recorded. Any reply still requires a SENTINEL "
                           "approval before MailHub will queue it.")

        elif action == "reject":
            with P.writing(con):
                con.execute(
                    "UPDATE human_escalations SET status='rejected',"
                    "       human_response=?, resolved_at=datetime('now'),"
                    "       resolved_by=?, action='reject' WHERE id=?",
                    (note or None, actor, esc_id))
                audit(con, esc_id, lead_id, "reject", note or "rejected", actor)
            out["note"] = "nothing will be sent"

        elif action == "edit":
            if not text.strip():
                return {"error": "edit needs --text"}
            # Edited text is NEW content. It has never been reviewed, so the
            # old approval cannot cover it — writing it as a draft clears any
            # verdict, and SENTINEL must look at it again.
            stage = int((lead["followup_stage"] if lead else 0) or 0) + 1
            with P.writing(con):
                try:
                    P.save_draft(con, lead_id, esc["campaign_id"], stage,
                                 "Re: your message", text.strip())
                except P.TransitionError as exc:
                    return {"error": str(exc)}
                con.execute(
                    "UPDATE human_escalations SET status='edited',"
                    "       human_response=?, resolved_at=datetime('now'),"
                    "       resolved_by=?, action='edit' WHERE id=?",
                    (text[:2000], actor, esc_id))
                audit(con, esc_id, lead_id, "edit",
                      "human supplied replacement text (%d chars)" % len(text),
                      actor)
            out["stage"] = stage
            out["note"] = ("saved as a NEW draft with no QA verdict — SENTINEL "
                           "must approve it before it can be queued")

        elif action == "close":
            with P.writing(con):
                n = F.cancel_all(con, lead_id, "closed by human review", actor)
                try:
                    P.transition(con, lead_id, "CLOSED", actor,
                                 "closed at human review")
                    out["lead_state"] = "CLOSED"
                except P.TransitionError as exc:
                    out["lead_state"] = "refused: %s" % exc
                con.execute(
                    "UPDATE human_escalations SET status='resolved',"
                    "       resolved_at=datetime('now'), resolved_by=?,"
                    "       action='close' WHERE id=?", (actor, esc_id))
                audit(con, esc_id, lead_id, "close",
                      "cancelled %d follow-up(s)" % n, actor)
            out["followups_cancelled"] = n

        elif action == "dnc":
            with P.writing(con):
                n = F.cancel_all(con, lead_id, "do not contact", actor)
                try:
                    P.transition(con, lead_id, "UNSUBSCRIBED", actor,
                                 "do-not-contact at human review")
                    out["lead_state"] = "UNSUBSCRIBED"
                except P.TransitionError as exc:
                    out["lead_state"] = "refused: %s" % exc
            # Our own state is not enough — the address has to reach MailHub's
            # suppression list or another campaign could mail them tomorrow.
            if lead and lead["email"]:
                out["suppression"] = mailhub(
                    "/api/v1/suppression",
                    {"email": lead["email"], "reason": "do_not_contact",
                     "detail": "human review %s" % esc_id})
            with P.writing(con):
                con.execute(
                    "UPDATE human_escalations SET status='resolved',"
                    "       resolved_at=datetime('now'), resolved_by=?,"
                    "       action='dnc' WHERE id=?", (actor, esc_id))
                audit(con, esc_id, lead_id, "dnc",
                      "suppressed, cancelled %d follow-up(s)" % n, actor)
            out["followups_cancelled"] = n

        elif action == "resume":
            # Only where the state machine allows it. CLOSED and UNSUBSCRIBED
            # deliberately have no route back into outreach.
            target = "FOLLOWUP_WAITING"
            with P.writing(con):
                try:
                    P.transition(con, lead_id, target, actor,
                                 "resumed by human review")
                    out["lead_state"] = target
                    con.execute(
                        "UPDATE human_escalations SET status='resolved',"
                        "       resolved_at=datetime('now'), resolved_by=?,"
                        "       action='resume' WHERE id=?", (actor, esc_id))
                    audit(con, esc_id, lead_id, "resume", "resumed", actor)
                except P.TransitionError as exc:
                    out["error"] = ("cannot resume from %s: %s"
                                    % (lead["state"] if lead else "?", exc))
                    out["lead_state"] = lead["state"] if lead else None
        return out


def listing() -> int:
    with P.connect() as con:
        rows = list(con.execute(
            "SELECT h.id, h.lead_id, h.reason, h.status, h.created_at,"
            "       h.notified_at, l.business_name, l.state "
            "  FROM human_escalations h LEFT JOIN leads l ON l.id = h.lead_id "
            " ORDER BY h.created_at DESC LIMIT 40"))
    if not rows:
        print("  no escalations")
        return 0
    print("%-8s %-22s %-18s %-10s %-18s %s"
          % ("ID", "LEAD", "REASON", "STATUS", "LEAD STATE", "COMPANY"))
    for r in rows:
        print("%-8s %-22s %-18s %-10s %-18s %s"
              % (r["id"], r["lead_id"] or "-", (r["reason"] or "")[:18],
                 r["status"], r["state"] or "-",
                 (r["business_name"] or "")[:26]))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for a in ACTIONS:
        p = sub.add_parser(a)
        p.add_argument("esc_id")
        p.add_argument("--note", default="")
        p.add_argument("--text", default="")
        p.add_argument("--actor", default=os.getenv("REVIEW_ACTOR", "human"))
    args = ap.parse_args(argv)

    if args.cmd == "list":
        return listing()
    res = act(args.esc_id, args.cmd, args.actor, args.note, args.text)
    for k, v in res.items():
        print("  %-20s %s" % (k, v))
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
