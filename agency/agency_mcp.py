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

import pipeline as P  # noqa: E402

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
            draft = P.load_draft(con, lead_id, 0)
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
        with P.writing(con):
            mid = P.save_draft(con, lead_id, lead.get("campaign_id"), 0,
                               subject, body, claims)
    return {"saved": True, "message_id": mid,
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
        draft = P.load_draft(con, lead_id, 0)
        if not draft:
            return {"error": "no draft to review for %s" % lead_id}

        if status != "approved":
            with P.writing(con):
                P.record_qa(con, lead_id, 0, status, issues)
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
            P.record_qa(con, lead_id, 0, "approved", issues,
                        approval_id=str(res.get("id")))
    return {"recorded": "approved", "lead_id": lead_id,
            "approval_id": res.get("id"), "content_hash": res.get("content_hash")}


TOOLS: Dict[str, Dict[str, Any]] = {
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
