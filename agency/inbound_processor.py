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
import delivery_status as DS
import pipeline as P
import tenants   # noqa: E402

MAILHUB_BASE = os.getenv("MAILHUB_BASE_URL", "").rstrip("/")
MAILHUB_TOKEN = os.getenv("MAILHUB_API_TOKEN", "")
HERMES = os.getenv("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
AGENT = "maya"


def mailhub(method: str, path: str,
            body: Optional[Dict[str, Any]] = None,
            token: Optional[str] = None) -> Dict[str, Any]:
    """One MailHub call as one tenant.

    /api/v1/inbound only ever returns replies belonging to the calling key's
    own mailboxes, so the token decides which inbox is being read. Reading
    with the wrong one is not an error -- it is an empty result, which looks
    exactly like nobody having replied.
    """
    tok = token or MAILHUB_TOKEN
    if not MAILHUB_BASE or not tok:
        return {"error": "MailHub not configured"}
    req = urllib.request.Request(
        MAILHUB_BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + tok)
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
        " (tenant_user_id, provider_message_id, provider_thread_id,"
        "  mailhub_inbound_id, lead_id,"
        "  campaign_id, account_id, from_email, to_email, subject, body_text,"
        "  received_at, matched_by, is_bounce, is_auto_reply) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ev.get("tenant_user_id"),
         ev.get("provider_message_id"), ev.get("provider_thread_id"),
         ev.get("inbound_id"), ev.get("lead_id"), ev.get("campaign_id"),
         ev.get("account_id"), ev.get("from"), ev.get("to_email"),
         ev.get("subject"), ev.get("body_text"), ev.get("received_at"),
         ev.get("matched_by"), 1 if ev.get("is_bounce") else 0,
         1 if ev.get("is_auto_reply") else 0))
    if cur.rowcount == 0:
        return None
    # Scoped by tenant as well as id: two tenants may legitimately report the
    # same provider_message_id, and matching on the id alone would return the
    # other tenant's row.
    row = con.execute(
        "SELECT id FROM inbound_replies"
        " WHERE provider_message_id=?"
        "   AND COALESCE(tenant_user_id,-1)=COALESCE(?,-1)",
        (ev.get("provider_message_id"), ev.get("tenant_user_id"))).fetchone()
    return row["id"] if row else None


def _mirror(con, lead_id: str, event: str, payload: dict) -> None:
    """Queue a Supabase write-back. Never raises into the pipeline."""
    try:
        import supabase_sync
        supabase_sync.enqueue(lead_id, event, payload, con)
    except Exception:
        pass


def _handle_permanent_bounce(con, lead, ev, reply_id, verdict, cancelled):
    """A proven dead address: record it, suppress that one address, stop.

    The order matters and is the order the follow-ups were already cancelled
    in above: nothing here can run before the scheduled mail is stopped.

    Exactly one address is suppressed -- the one the reporting server named as
    having failed. Not the domain, and not whatever address appears first in a
    bounce body, which is frequently our own sender.
    """
    recipient = verdict["recipient"]
    with P.writing(con):
        con.execute("UPDATE inbound_replies SET classification='hard_bounce',"
                    "       confidence=?, summary=?, requires_human=0,"
                    "       recommended_action=?, draft_reply=NULL,"
                    "       classified_at=datetime('now') WHERE id=?",
                    (verdict["confidence"],
                     ("Delivery to %s failed permanently: %s"
                      % (recipient, verdict.get("reason") or verdict["code"]))[:1000],
                     "; ".join(DS.recommended_actions(verdict))[:500], reply_id))
        try:
            P.transition(con, lead["id"], "BOUNCED", "inbound",
                         "permanent delivery failure (%s)" % verdict["code"])
        except P.TransitionError:
            pass                       # already BOUNCED, or somewhere final

    # Suppression is per tenant in MailHub: filing it under the wrong tenant
    # leaves this person mailable by the mailbox that would actually write to
    # them next. LEO's credential is the one carrying the suppress scope.
    suppressed = None
    rt = ev.get("tenant_user_id")
    tenant = tenants.by_user_id(int(rt)) if rt is not None else None
    token = (tenant or {}).get("leo")
    if not token:
        suppressed = {"error": "no LEO suppress credential for this tenant"}
    else:
        suppressed = mailhub("POST", "/api/v1/suppression",
                             {"email": recipient, "reason": "bounced",
                              "detail": "permanent delivery failure %s"
                                        % verdict["code"]}, token=token)

    _mirror(con, lead["id"], "reply_received", {
        "tenant_user_id": rt,
        "provider_message_id": ev.get("provider_message_id"),
        "is_bounce": True, "is_auto_reply": False,
        "followups_cancelled": cancelled, "state": "BOUNCED",
    })
    _mirror(con, lead["id"], "bounced", {
        "recipient": recipient, "code": verdict["code"],
        "reason": (verdict.get("reason") or "")[:300],
    })
    # Fully handled, so it is consumed. Leaving it would have MailHub return
    # the same bounce on every poll; the UNIQUE on provider_message_id would
    # discard it each time, but only after fetching it again.
    if ev.get("inbound_id"):
        mailhub("POST", "/api/v1/inbound/%s/consume" % ev["inbound_id"],
                token=ev.get("_token"))
    return ("reply %d: permanent bounce for %s (%s); %d follow-up(s) cancelled, "
            "address suppressed%s - no human review needed"
            % (reply_id, recipient, verdict["code"], cancelled,
               "" if not (suppressed or {}).get("error")
               else " FAILED: " + suppressed["error"]))


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

    # --- 3. a delivery report answers itself --------------------------------
    # A bounce is a machine-generated report with the answer written in it as
    # an RFC 3463 code. Handing that to a model produced the thing this
    # replaces: every bounce arriving for a person to read, labelled
    # "unclear", while the address that failed stayed mailable.
    #
    # Only a permanent failure whose code the reporting server generated, and
    # whose failed recipient the report names, is acted on without a person.
    # A relay refusal, a full mailbox, a temporary defer and an unparsable
    # bounce all fall through to the ordinary path.
    verdict = DS.classify(ev.get("from_email") or "", ev.get("subject") or "",
                          ev.get("body_text") or "", lead["email"])
    if DS.may_suppress(verdict):
        return _handle_permanent_bounce(con, lead, ev, reply_id, verdict, n)
    if verdict["status"] != DS.NOT_A_BOUNCE:
        # Still a delivery failure, just not one we may act on alone. Record
        # what it is so the card can say so instead of guessing.
        with P.writing(con):
            con.execute("UPDATE inbound_replies SET classification=?,"
                        "       confidence=?, summary=?, requires_human=1,"
                        "       classified_at=datetime('now') WHERE id=?",
                        (verdict["status"].lower(), verdict["confidence"],
                         (verdict.get("reason") or "")[:1000], reply_id))

    # --- 4. otherwise, ask LEO what it means --------------------------------
    task = dispatch_leo(lead_id, reply_id)
    if task:
        with P.writing(con):
            con.execute("UPDATE inbound_replies SET leo_task_id=? WHERE id=?",
                        (task, reply_id))

    # --- 5. mirror the outcome ---------------------------------------------
    # Queued, not written through: the reply is already recorded and the
    # follow-ups already cancelled, so a Supabase outage is a retry rather
    # than a reason to reprocess a reply that was handled correctly here.
    _mirror(con, lead_id, "reply_received", {
        "tenant_user_id": ev.get("tenant_user_id"),
        "provider_message_id": ev.get("provider_message_id"),
        "is_bounce": bool(ev.get("is_bounce")),
        "is_auto_reply": bool(ev.get("is_auto_reply")),
        "followups_cancelled": n,
        "state": moved,
    })

    # --- 6. consume, but only what was actually handled ---------------------
    # If LEO could not be dispatched, the reply has been recorded and its
    # follow-ups cancelled -- the protective half is done -- but nothing has
    # classified it yet. Leaving it unconsumed lets the next tick try again;
    # consuming it here would lose the classification silently. The record row
    # makes the retry safe: it will not be processed twice.
    if ev.get("inbound_id") and task:
        mailhub("POST", "/api/v1/inbound/%s/consume" % ev["inbound_id"],
                token=ev.get("_token"))
        consumed = "consumed"
    elif ev.get("inbound_id"):
        consumed = "left for retry (LEO not dispatched)"
    else:
        consumed = "nothing to consume"

    return ("reply %d lead=%s cancelled=%d -> %s leo=%s (%s)"
            % (reply_id, lead_id, n, moved, task or "not dispatched", consumed))


def poll(limit: int = 25) -> List[str]:
    """Read every configured tenant's inbox in turn.

    Every tenant is polled, not only the ready ones: a tenant that has stopped
    being routable may still be holding a reply to outreach it already sent,
    and an unread unsubscribe is worse than an unroutable tenant.

    One tenant failing does not stop the others. A tenant whose credential is
    refused is reported rather than skipped silently, because silence here is
    indistinguishable from an empty inbox.
    """
    out: List[str] = []
    pool = tenants.load()
    if not pool:
        return ["no MailHub tenant configured"]

    with P.connect() as con:
        for t in pool:
            tok = t.get("leo") or t.get("queue")
            if not tok:
                out.append("tenant %s: no read credential, inbox not polled"
                           % t["name"])
                continue
            res = mailhub("GET", "/api/v1/inbound?limit=%d" % limit, token=tok)
            if res.get("error"):
                out.append("tenant %s: MailHub %s" % (t["name"], res))
                continue
            for ev in res.get("messages", []):
                # Stamped from the credential that fetched it, not from
                # anything in the payload, so a reply cannot claim to belong
                # to a tenant it did not arrive in.
                ev = dict(ev)
                ev["tenant_user_id"] = t["user_id"]
                ev["_token"] = tok
                try:
                    out.append("[%s] %s" % (t["name"], process_one(con, ev)))
                except Exception as exc:
                    out.append("[%s] error on %s: %s: %s"
                               % (t["name"], ev.get("provider_message_id"),
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
