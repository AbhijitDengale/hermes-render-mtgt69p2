#!/usr/bin/env python3
"""Agency MCP — the only way an agent profile touches agency.db.

One server, role-scoped. `AGENCY_ROLE` decides which tools a profile is even
offered, so the separation of duties is a property of the deployment rather
than of a prompt:

    nova      read the assignment, save research
    aria      read the assignment + research, save a draft
    sentinel  read everything, submit a verdict -- and ONLY sentinel holds a
              MailHub credential with the `approve` scope

No role can both approve copy and send it. SENTINEL can record an approval and
cannot queue; MAYA can queue and cannot approve. That is why the QA gate is a
control and not an instruction: subverting it needs two credentials that are
never issued to the same profile.

Agents never write `leads.state`. They persist their output here; the
orchestrator reads it and moves the lead through the state machine, so a
confused model cannot advance its own work.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import followups as F  # noqa: E402
import pipeline as P   # noqa: E402

ROLE = os.getenv("AGENCY_ROLE", "").strip().lower()
MAILHUB_BASE = os.getenv("MAILHUB_BASE_URL", "").rstrip("/")
MAILHUB_TOKEN = os.getenv("MAILHUB_API_TOKEN", "")


# --- helpers ----------------------------------------------------------------

def _lead(con, lead_id: str) -> Dict[str, Any]:
    row = P.get_lead(con, lead_id)
    if row is None:
        raise ValueError("no such lead: %s" % lead_id)
    return dict(row)


def _campaign(con, campaign_id: str) -> Dict[str, Any]:
    row = con.execute("SELECT * FROM campaigns WHERE id=?",
                      (campaign_id,)).fetchone()
    return dict(row) if row else {}


def _stage(con, lead_id: str) -> int:
    """Which message the lead is currently working on: 0 initially, then the
    follow-up number. Read from the lead so SENTINEL reviews the same draft
    ARIA just wrote."""
    row = P.get_lead(con, lead_id)
    return int((row["followup_stage"] if row else 0) or 0)


def _assignment(lead_id: str) -> Dict[str, Any]:
    """Everything an agent legitimately needs, and nothing else.

    Deliberately omits the recipient's address for NOVA and ARIA: neither has
    any use for it, and not handing it over means a prompt-injected agent has
    nothing to exfiltrate.
    """
    with P.connect() as con:
        lead = _lead(con, lead_id)
        camp = _campaign(con, lead.get("campaign_id") or "")
        out = {
            "lead_id": lead["id"],
            "business_name": lead.get("business_name"),
            "contact_name": lead.get("contact_name"),
            "website": lead.get("website"),
            "city": lead.get("city"),
            "region": lead.get("region"),
            "country": lead.get("country"),
            "niche": lead.get("niche"),
            "notes": lead.get("notes"),
            "state": lead.get("state"),
            "campaign": {
                "id": camp.get("id"),
                "name": camp.get("name"),
                "persona": camp.get("persona"),
                "service_offer": camp.get("service_offer"),
                "country": camp.get("country"),
                "niche": camp.get("niche"),
            },
        }
        if ROLE in ("aria", "sentinel"):
            out["research"] = P.load_research(con, lead_id)
        if ROLE == "sentinel":
            draft = P.load_draft(con, lead_id, _stage(con, lead_id))
            if draft:
                out["draft"] = {
                    "subject": draft["subject"], "body": draft["body"],
                    "claims_used": draft["claims_used"],
                    "content_hash": draft["content_hash"],
                }
            out["recipient"] = lead.get("email")
        return out


def _mailhub(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if not MAILHUB_BASE or not MAILHUB_TOKEN:
        return {"error": "MailHub is not configured for this profile"}
    req = urllib.request.Request(
        MAILHUB_BASE + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + MAILHUB_TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": "http %d" % e.code, "detail": e.read().decode()[:400]}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


# --- tools ------------------------------------------------------------------

def t_get_assignment(a: Dict[str, Any]) -> Dict[str, Any]:
    return _assignment(a["lead_id"])


def t_save_research(a: Dict[str, Any]) -> Dict[str, Any]:
    """NOVA's output.

    An audited Steel fetch is required whenever the lead has a website. NOVA
    has already once produced a confident report from memory alone, having
    fetched nothing; refusing the write here means that failure is loud rather
    than silent.
    """
    lead_id = a["lead_id"]
    research = a.get("research") or {}
    status = (research.get("research_status") or "").lower()
    if status not in ("complete", "partial", "failed"):
        return {"error": "research_status must be complete, partial or failed"}

    with P.connect() as con:
        lead = _lead(con, lead_id)
        if status != "failed" and lead.get("website"):
            n = con.execute(
                "SELECT COUNT(*) c FROM research_fetches "
                " WHERE status='ok' AND created_at >= "
                "       (SELECT state_changed_at FROM leads WHERE id=?)",
                (lead_id,)).fetchone()["c"]
            if n == 0:
                return {"error": "no successful fetch is recorded for this "
                                 "session — research cannot be reported as %s "
                                 "without evidence" % status,
                        "hint": "call the research tool before reporting"}
        obs = research.get("verified_observations") or []
        for o in obs:
            if not o.get("source_url") or not o.get("evidence"):
                return {"error": "every observation needs source_url and evidence"}
        with P.writing(con):
            P.save_research(con, lead_id, research)
        # Close the timing record. The duration is measured by the research
        # server from the lead's first fetch, not reported by the agent, so it
        # cannot be flattered.
        with P.writing(con):
            con.execute(
                "UPDATE research_runs SET completed_at=datetime('now'),"
                "  observations_count=?, research_status=?,"
                "  duration_ms=COALESCE(duration_ms,"
                "    CAST((julianday('now') - julianday(started_at))"
                "         * 86400000 AS INTEGER))"
                " WHERE lead_id=?", (len(obs), status, lead_id))
    return {"saved": True, "lead_id": lead_id, "status": status,
            "observations": len(obs)}


def t_save_draft(a: Dict[str, Any]) -> Dict[str, Any]:
    """ARIA's output. Every claim must name a source URL present in research."""
    lead_id = a["lead_id"]
    subject = (a.get("subject") or "").strip()
    body = (a.get("body") or "").strip()
    claims = a.get("claims_used") or []
    if not subject or not body:
        return {"error": "subject and body are required"}
    for token in ("[", "]", "{{", "}}"):
        if token in subject or token in body:
            return {"error": "unresolved placeholder %r in the copy" % token}

    with P.connect() as con:
        lead = _lead(con, lead_id)
        research = P.load_research(con, lead_id)
        sources = {o.get("source_url") for o
                   in (research.get("verified_observations") or [])}
        for c in claims:
            if c.get("source_url") not in sources:
                return {"error": "claim cites %r, which is not in NOVA's "
                                 "verified research" % c.get("source_url")}
        # The stage decides WHICH message this is. Without it every follow-up
        # would overwrite the original outreach -- which is precisely what
        # happened before the parameter existed.
        stage = int(a.get("stage") or 0)
        try:
            with P.writing(con):
                mid = P.save_draft(con, lead_id, lead.get("campaign_id"), stage,
                                   subject, body, claims)
        except P.TransitionError as exc:
            return {"error": str(exc)}
    return {"saved": True, "message_id": mid, "stage": stage,
            "content_hash": P.content_hash(subject, body)}


def t_submit_verdict(a: Dict[str, Any]) -> Dict[str, Any]:
    """SENTINEL's verdict. On approval this ALSO records it with MailHub.

    The approval is bound to the hash of the exact reviewed text. This is the
    only place in the system holding a credential with the `approve` scope.
    """
    lead_id = a["lead_id"]
    status = (a.get("status") or "").lower()
    if status not in ("approved", "rejected", "needs_review"):
        return {"error": "status must be approved, rejected or needs_review"}
    issues = a.get("issues") or []

    with P.connect() as con:
        lead = _lead(con, lead_id)
        stage = _stage(con, lead_id)
        draft = P.load_draft(con, lead_id, stage)
        if not draft:
            return {"error": "no draft to review for %s" % lead_id}

        if status != "approved":
            with P.writing(con):
                P.record_qa(con, lead_id, stage, status, issues)
            return {"recorded": status, "lead_id": lead_id, "issues": issues}

        # Approving must review the text that is actually stored. Accepting a
        # subject/body from the caller would let a rewritten message be
        # approved against a hash nobody checked.
        res = _mailhub("/api/v1/approvals", {
            "subject": draft["subject"], "body_text": draft["body"],
            "qa_status": "approved", "qa_agent": "sentinel",
            "qa_reason": (a.get("reason") or "")[:500],
            "lead_id": lead_id, "campaign_id": lead.get("campaign_id"),
        })
        if res.get("error"):
            return {"error": "MailHub refused the approval", "detail": res}
        with P.writing(con):
            P.record_qa(con, lead_id, stage, "approved", issues,
                        approval_id=str(res.get("id")))
    return {"recorded": "approved", "lead_id": lead_id,
            "approval_id": res.get("id"), "content_hash": res.get("content_hash")}


# --- LEO --------------------------------------------------------------------
# LEO picks a label; the label-to-state mapping is code. A model cannot invent
# a transition, and anything commercial routes to a human by construction
# rather than by the model choosing to be careful.

CLASSIFICATIONS = (
    "positive", "interested", "question", "pricing_question", "objection",
    "not_now", "negative", "unsubscribe", "wrong_person", "referral",
    "out_of_office", "meeting_request", "proposal_request",
    "contract_request", "unclear",
)

CLASS_TO_STATE = {
    "unsubscribe": "UNSUBSCRIBED",
    "negative": "NEGATIVE",
    "positive": "POSITIVE",
    "interested": "POSITIVE",
    "meeting_request": "MEETING_STAGE",
    "pricing_question": "HUMAN_REVIEW",
    "proposal_request": "HUMAN_REVIEW",
    "contract_request": "HUMAN_REVIEW",
    "objection": "HUMAN_REVIEW",
    "wrong_person": "HUMAN_REVIEW",
    "referral": "HUMAN_REVIEW",
    "unclear": "HUMAN_REVIEW",
    "question": "HUMAN_REVIEW",
    "not_now": "HUMAN_REVIEW",
    # out_of_office is handled separately — an autoresponder is not an answer.
}

# Never left to the model's discretion, whatever it reports.
ALWAYS_HUMAN = frozenset({"pricing_question", "proposal_request",
                          "contract_request", "objection", "referral",
                          "wrong_person", "unclear"})


def t_get_reply(a: Dict[str, Any]) -> Dict[str, Any]:
    """The reply to classify, plus the lead it belongs to."""
    with P.connect() as con:
        r = con.execute("SELECT * FROM inbound_replies WHERE id=?",
                        (a["reply_id"],)).fetchone()
        if not r:
            return {"error": "no such reply"}
        lead = P.get_lead(con, r["lead_id"]) if r["lead_id"] else None
        return {
            "reply_id": r["id"], "lead_id": r["lead_id"],
            "campaign_id": r["campaign_id"], "from": r["from_email"],
            "subject": r["subject"], "body_text": r["body_text"],
            "received_at": r["received_at"], "is_bounce": bool(r["is_bounce"]),
            "is_auto_reply": bool(r["is_auto_reply"]),
            "business_name": lead["business_name"] if lead else None,
            "lead_state": lead["state"] if lead else None,
            "followups_already_cancelled": bool(r["followups_cancelled"]),
        }


def t_submit_classification(a: Dict[str, Any]) -> Dict[str, Any]:
    """Record what the reply means and move the lead to the mapped state."""
    reply_id = a["reply_id"]
    cls = (a.get("classification") or "").lower().strip()
    if cls not in CLASSIFICATIONS:
        return {"error": "classification must be one of %s" % (CLASSIFICATIONS,)}
    requires_human = bool(a.get("requires_human_review")) or cls in ALWAYS_HUMAN

    with P.connect() as con:
        r = con.execute("SELECT * FROM inbound_replies WHERE id=?",
                        (reply_id,)).fetchone()
        if not r:
            return {"error": "no such reply"}
        if r["classified_at"]:
            # Exactly once: a redispatched task must not re-run the effects.
            return {"already_classified": r["classification"],
                    "reply_id": reply_id}
        lead_id = r["lead_id"]

        with P.writing(con):
            con.execute(
                "UPDATE inbound_replies SET classification=?, confidence=?,"
                "       summary=?, recommended_action=?, draft_reply=?,"
                "       requires_human=?, classified_at=datetime('now') "
                " WHERE id=?",
                (cls, float(a.get("confidence") or 0),
                 (a.get("summary") or "")[:1000],
                 (a.get("recommended_action") or "")[:500],
                 (a.get("draft_reply") or "")[:4000],
                 1 if requires_human else 0, reply_id))

        if not lead_id:
            return {"recorded": cls, "lead": None}

        if cls == "out_of_office":
            # Reschedule only on an unambiguous date. Guessing means emailing
            # someone while they are still away, so anything less becomes a
            # human decision.
            until = F.parse_return_date(r["body_text"] or "")
            if until:
                with P.writing(con):
                    F.hold_for_ooo(con, lead_id, until, 1, r["campaign_id"])
                return {"recorded": cls, "rescheduled_until": until,
                        "lead_state": "unchanged"}
            with P.writing(con):
                try:
                    P.transition(con, lead_id, "HUMAN_REVIEW", "leo",
                                 "out of office, no parseable return date")
                except P.TransitionError:
                    pass
            return {"recorded": cls, "rescheduled_until": None, "escalated": True}

        target = "HUMAN_REVIEW" if requires_human else CLASS_TO_STATE.get(cls)
        moved = None
        if target:
            with P.writing(con):
                try:
                    P.transition(con, lead_id, target, "leo",
                                 "%s (confidence %.2f)"
                                 % (cls, float(a.get("confidence") or 0)))
                    moved = target
                except P.TransitionError as exc:
                    moved = "refused: %s" % exc

        suppressed = None
        if cls == "unsubscribe" and r["from_email"]:
            # An opt-out has to reach MailHub's suppression list, not just our
            # own state. LEO holds a suppress-scoped credential and no other.
            suppressed = _mailhub("/api/v1/suppression",
                                  {"email": r["from_email"],
                                   "reason": "unsubscribed",
                                   "detail": "LEO reply %d" % reply_id})

        if requires_human:
            with P.writing(con):
                con.execute(
                    "INSERT OR IGNORE INTO human_escalations "
                    " (id, lead_id, campaign_id, raised_by, reason,"
                    "  reply_summary, recommended_action, draft_response) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    ("H-%d" % reply_id, lead_id, r["campaign_id"], "leo", cls,
                     (a.get("summary") or "")[:1000],
                     (a.get("recommended_action") or "")[:500],
                     (a.get("draft_reply") or "")[:4000]))

    return {"recorded": cls, "lead_state": moved,
            "requires_human_review": requires_human,
            "suppression": suppressed}


TOOLS: Dict[str, Dict[str, Any]] = {
    "get_reply": {
        "roles": ("leo",),
        "fn": t_get_reply,
        "description": "Read the prospect reply you have been asked to classify.",
        "schema": {"type": "object", "properties": {
            "reply_id": {"type": "integer"}}, "required": ["reply_id"]},
    },
    "submit_classification": {
        "roles": ("leo",),
        "fn": t_submit_classification,
        "description": ("Record what the reply means. You do not negotiate: "
                        "price, discounts, legal or payment terms, guarantees "
                        "and contracts always go to a human."),
        "schema": {"type": "object", "properties": {
            "reply_id": {"type": "integer"},
            "classification": {"type": "string"},
            "confidence": {"type": "number"},
            "summary": {"type": "string"},
            "recommended_action": {"type": "string"},
            "draft_reply": {"type": "string"},
            "requires_human_review": {"type": "boolean"}},
            "required": ["reply_id", "classification"]},
    },
    "get_assignment": {
        "roles": ("nova", "aria", "sentinel"),
        "fn": t_get_assignment,
        "description": "Fetch everything you need to work this lead.",
        "schema": {"type": "object", "properties": {
            "lead_id": {"type": "string"}}, "required": ["lead_id"]},
    },
    "save_research": {
        "roles": ("nova",),
        "fn": t_save_research,
        "description": ("Persist verified research. Refused unless a "
                        "successful fetch is on record for this session."),
        "schema": {"type": "object", "properties": {
            "lead_id": {"type": "string"},
            "research": {"type": "object",
                         "description": "research_status, verified_observations "
                                        "(claim/source_url/evidence/confidence), "
                                        "services, locations, contact_methods, "
                                        "social_links, opportunities, "
                                        "personalization_angles, failure_reason"}},
            "required": ["lead_id", "research"]},
    },
    "save_draft": {
        "roles": ("aria",),
        "fn": t_save_draft,
        "description": ("Persist outreach copy. Every claim must cite a "
                        "source_url that appears in NOVA's research."),
        "schema": {"type": "object", "properties": {
            "lead_id": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"},
            "stage": {"type": "integer",
                      "description": "0 for the first email, 1+ for follow-ups. "
                                     "Required for follow-ups: omitting it "
                                     "would target the original message."},
            "claims_used": {"type": "array", "items": {"type": "object"}}},
            "required": ["lead_id", "subject", "body"]},
    },
    "submit_verdict": {
        "roles": ("sentinel",),
        "fn": t_submit_verdict,
        "description": ("Record the QA verdict. Approving also lodges the "
                        "approval with MailHub against the exact text hash."),
        "schema": {"type": "object", "properties": {
            "lead_id": {"type": "string"},
            "status": {"type": "string",
                       "enum": ["approved", "rejected", "needs_review"]},
            "issues": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"}},
            "required": ["lead_id", "status"]},
    },
}


def visible() -> List[str]:
    return [n for n, t in TOOLS.items() if ROLE in t["roles"]]


# --- MCP stdio loop ---------------------------------------------------------
# Raw JSON-RPC: the bundled mcp package dropped FastMCP, and the protocol is
# small enough that a dependency is not worth the coupling.

def _send(msg: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        mid, method = req.get("id"), req.get("method")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agency-%s" % (ROLE or "none"),
                               "version": "1.0.0"}}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": n, "description": TOOLS[n]["description"],
                 "inputSchema": TOOLS[n]["schema"]} for n in visible()]}})
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            if name not in visible():
                out = {"error": "tool %r is not available to role %r"
                                % (name, ROLE)}
            else:
                try:
                    out = TOOLS[name]["fn"](params.get("arguments") or {})
                except Exception as exc:
                    out = {"error": "%s: %s" % (type(exc).__name__, exc)}
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(out)}]}})
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": "unknown method"}})
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print("role: %s" % (ROLE or "(unset)"))
        print("tools: %s" % ", ".join(visible()) or "(none)")
        print("mailhub: %s" % ("configured" if MAILHUB_BASE and MAILHUB_TOKEN
                               else "not configured"))
        sys.exit(0)
    sys.exit(serve())
