#!/usr/bin/env python3
"""MAYA's orchestration loop — advance leads one stage at a time.

MAYA decides *what happens next*; she never does the work. Research, copy and
QA are dispatched to their own profiles as Kanban tasks, and this loop only
reads what those agents persisted and moves the lead accordingly. An agent
therefore cannot advance its own work, which is what stops a confident model
from declaring itself finished.

Every step is idempotent. Dispatch uses Kanban's own idempotency key, so
re-running a tick returns the existing task instead of creating a second one;
state changes are compare-and-swap; and the MailHub queue key is derived from
the approved content hash, so a retried send returns the original message.

    python3 orchestrator.py tick            # one pass
    python3 orchestrator.py status          # counts by state
    python3 orchestrator.py timeline <lead> # full history of one lead
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

HERMES = os.getenv("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
MAILHUB_BASE = os.getenv("MAILHUB_BASE_URL", "").rstrip("/")
MAILHUB_TOKEN = os.getenv("MAILHUB_API_TOKEN", "")
AGENT = "maya"

# Research that found nothing verifiable must not become outreach. A partial
# result needs at least this many cited observations to be worth writing from.
MIN_OBSERVATIONS = int(os.getenv("AGENCY_MIN_OBSERVATIONS", "1"))


# --- MailHub ----------------------------------------------------------------

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
        return {"error": "http %d" % e.code, "detail": e.read().decode()[:400]}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


# --- Kanban -----------------------------------------------------------------

def gen(lead) -> int:
    """This lead's lifecycle generation, or 1 if it predates the counter."""
    try:
        return int(lead["lifecycle_generation"] or 1)
    except (IndexError, KeyError, TypeError):
        return 1


def dispatch(lead_id: str, profile: str, title: str, body: str,
             stage_key: str, generation: int = 1) -> Dict[str, Any]:
    """Create the task, or return the existing one.

    The idempotency key is (lead, lifecycle generation, stage). Within one
    lifecycle a tick that runs twice — or a restart mid-stage — collapses onto
    the same task, which is the protection worth keeping. Across lifecycles it
    does not: a lead that was deleted and re-ingested used to match the
    COMPLETED task from its previous life, so Kanban handed that task back,
    no worker was spawned, and the lead sat in RESEARCHING forever.
    """
    key = "agency:%s:gen:%d:%s" % (lead_id, generation, stage_key)
    cmd = [HERMES, "kanban", "create", title, "--assignee", profile,
           "--body", body, "--idempotency-key", key, "--json"]
    env = {**os.environ, "HOME": os.getenv("HERMES_HOME", "/opt/data"),
           "HERMES_HOME": os.getenv("HERMES_HOME", "/opt/data")}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                             env=env)
        text = (out.stdout or out.stderr).strip()
        try:
            return json.loads(text)
        except Exception:
            return {"raw": text[:300], "ok": out.returncode == 0}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def brief(assignment: Dict[str, Any], instruction: str) -> str:
    return (instruction + "\n\nLead id: " + assignment["lead_id"]
            + "\nCall get_assignment with that lead_id for the full record. "
              "Work only from what it returns.")


# --- stage handlers ---------------------------------------------------------
# Each returns a short string describing what it did, for the tick log.

def admit(con, lead) -> str:
    with P.writing(con):
        P.transition(con, lead["id"], "RESEARCH_PENDING", AGENT,
                     "admitted to the pipeline", expect="NEW")
    return "NEW -> RESEARCH_PENDING"


def dispatch_research(con, lead) -> str:
    res = dispatch(
        lead["id"], "nova", "Research lead %s" % lead["id"],
        brief({"lead_id": lead["id"]},
              "You are NOVA. Research this business using the research tool, "
              "then call save_research with your findings. Every observation "
              "must carry a source_url and a quoted evidence string. If you "
              "cannot fetch anything, report research_status 'failed' — never "
              "write findings you did not fetch this session."),
        "research", gen(lead))
    with P.writing(con):
        P.transition(con, lead["id"], "RESEARCHING", AGENT,
                     "dispatched to NOVA", expect="RESEARCH_PENDING",
                     detail=json.dumps(res)[:400])
    return "RESEARCH_PENDING -> RESEARCHING (task %s)" % res.get("id", "?")


def collect_research(con, lead) -> Optional[str]:
    research = P.load_research(con, lead["id"])
    if not research:
        return None                      # NOVA still working
    status = (research.get("research_status") or "").lower()
    obs = research.get("verified_observations") or []

    if status == "failed":
        # A failed fetch is not an error in our code — it is a lead we cannot
        # personalise. A human decides whether to drop it or supply context.
        with P.writing(con):
            P.transition(con, lead["id"], "HUMAN_REVIEW", AGENT,
                         "research failed: %s"
                         % (research.get("failure_reason") or "unknown"),
                         expect="RESEARCHING")
        return "RESEARCHING -> HUMAN_REVIEW (research failed)"

    if len(obs) < MIN_OBSERVATIONS:
        with P.writing(con):
            P.transition(con, lead["id"], "HUMAN_REVIEW", AGENT,
                         "research returned %d verified observations" % len(obs),
                         expect="RESEARCHING")
        return "RESEARCHING -> HUMAN_REVIEW (too little evidence)"

    with P.writing(con):
        P.transition(con, lead["id"], "RESEARCH_COMPLETE", AGENT,
                     "%s, %d observations" % (status, len(obs)),
                     expect="RESEARCHING")
    return "RESEARCHING -> RESEARCH_COMPLETE (%d observations)" % len(obs)


def to_copy(con, lead) -> str:
    with P.writing(con):
        P.transition(con, lead["id"], "COPY_PENDING", AGENT,
                     "ready for copy", expect="RESEARCH_COMPLETE")
    return "RESEARCH_COMPLETE -> COPY_PENDING"


def dispatch_copy(con, lead) -> Optional[str]:
    stage = lead["followup_stage"] or 0
    draft = P.load_draft(con, lead["id"], stage)
    if draft and draft["status"] == "draft" and not draft["qa_status"]:
        with P.writing(con):
            P.transition(con, lead["id"], "COPY_READY", AGENT,
                         "draft %s" % draft["id"], expect="COPY_PENDING")
        return "COPY_PENDING -> COPY_READY"

    # Re-dispatch is safe: same idempotency key, same task. After a QA
    # rejection the key changes with the attempt number so ARIA gets a new
    # task carrying the reasons.
    attempt = con.execute(
        "SELECT COUNT(*) c FROM events WHERE lead_id=? AND to_state='QA_REJECTED'",
        (lead["id"],)).fetchone()["c"]
    issues = ""
    if attempt:
        prev = P.load_draft(con, lead["id"], stage)
        if prev and prev["qa_issues"]:
            issues = ("\n\nSENTINEL rejected the previous draft for these "
                      "reasons — fix them:\n" + prev["qa_issues"])
    dispatch(lead["id"], "aria", "Write outreach for %s" % lead["id"],
             brief({"lead_id": lead["id"]},
                   "You are ARIA. Call get_assignment, then write the outreach "
                   "email and save it with save_draft. Every personalised "
                   "claim must cite a source_url that appears in NOVA's "
                   "research — you have no browser and may not add facts."
                   + issues),
             "copy:%d" % attempt, gen(lead))
    return None


def to_qa(con, lead) -> str:
    with P.writing(con):
        P.transition(con, lead["id"], "QA_PENDING", AGENT,
                     "sent for QA", expect="COPY_READY")
    return "COPY_READY -> QA_PENDING"


def dispatch_qa(con, lead) -> Optional[str]:
    draft = P.load_draft(con, lead["id"], lead["followup_stage"] or 0)
    if draft and draft["qa_status"] == "approved":
        with P.writing(con):
            P.transition(con, lead["id"], "READY_TO_SEND", AGENT,
                         "approval %s" % draft["approval_id"],
                         expect="QA_PENDING")
        return "QA_PENDING -> READY_TO_SEND (approval %s)" % draft["approval_id"]
    if draft and draft["qa_status"] == "rejected":
        with P.writing(con):
            P.transition(con, lead["id"], "QA_REJECTED", AGENT,
                         (draft["qa_issues"] or "")[:200], expect="QA_PENDING")
        return "QA_PENDING -> QA_REJECTED"
    if draft and draft["qa_status"] == "needs_review":
        with P.writing(con):
            P.transition(con, lead["id"], "HUMAN_REVIEW", AGENT,
                         (draft["qa_issues"] or "")[:200], expect="QA_PENDING")
        return "QA_PENDING -> HUMAN_REVIEW"

    attempt = con.execute(
        "SELECT COUNT(*) c FROM events WHERE lead_id=? AND to_state='QA_PENDING'",
        (lead["id"],)).fetchone()["c"]
    dispatch(lead["id"], "sentinel", "QA review for %s" % lead["id"],
             brief({"lead_id": lead["id"]},
                   "You are SENTINEL. Call get_assignment, check the draft "
                   "against NOVA's research, then call submit_verdict. Reject "
                   "any claim not supported by a cited observation, any "
                   "invented metric, any guarantee, and any unresolved "
                   "placeholder."),
             "qa:%d" % attempt, gen(lead))
    return None


def rewrite(con, lead) -> str:
    with P.writing(con):
        P.transition(con, lead["id"], "COPY_PENDING", AGENT,
                     "returned to ARIA for rewrite", expect="QA_REJECTED")
    return "QA_REJECTED -> COPY_PENDING (rewrite loop)"


def queue_and_send(con, lead) -> Optional[str]:
    """Queue the approved message, then wait for the provider to confirm.

    SENT is set only on a provider-confirmed send. Marking it on `queued` would
    make the state machine claim something happened that had not.
    """
    stage = lead["followup_stage"] or 0
    draft = P.load_draft(con, lead["id"], stage)
    if not draft or draft["qa_status"] != "approved":
        with P.writing(con):
            P.transition(con, lead["id"], "HUMAN_REVIEW", AGENT,
                         "reached READY_TO_SEND without an approval",
                         expect="READY_TO_SEND")
        return "READY_TO_SEND -> HUMAN_REVIEW (no approval)"

    # Derived from the approved content, so a retry presents the same key and
    # a changed message cannot reuse the old one.
    key = "lead:%s:%s:stage%d:%s" % (lead["id"], lead["campaign_id"],
                                     stage, draft["content_hash"][:16])

    if not draft["mailhub_queue_id"]:
        res = mailhub("POST", "/api/v1/messages", {
            "to_email": lead["email"], "to_name": lead["contact_name"] or "",
            "subject": draft["subject"], "body_text": draft["body"],
            "idempotency_key": key,
            "meta": {"lead_id": lead["id"], "campaign_id": lead["campaign_id"],
                     "stage": stage},
        })
        if res.get("status") not in ("queued", "duplicate"):
            with P.writing(con):
                con.execute("UPDATE leads SET last_error=?, error_count=error_count+1"
                            " WHERE id=?", (json.dumps(res)[:400], lead["id"]))
            return "READY_TO_SEND: queue refused (%s)" % json.dumps(res)[:160]
        with P.writing(con):
            con.execute(
                "UPDATE messages SET mailhub_queue_id=?, idempotency_key=?,"
                "       status='queued', updated_at=datetime('now') WHERE id=?",
                (str(res.get("id")), key, draft["id"]))
        return "READY_TO_SEND: queued as MailHub #%s (%s)" % (
            res.get("id"), res.get("status"))

    # Already queued — ask MailHub whether the provider took it.
    status = mailhub("GET", "/api/v1/messages/%s" % draft["mailhub_queue_id"])
    st = status.get("status")
    if st in ("sent", "simulated"):
        with P.writing(con):
            con.execute(
                "UPDATE messages SET status=?, provider_message_id=?,"
                "       provider_thread_id=?, mailhub_account_id=?,"
                "       sent_at=?, dry_run=?, updated_at=datetime('now') "
                " WHERE id=?",
                (st, status.get("provider_message_id"),
                 status.get("provider_thread_id"), status.get("account_id"),
                 status.get("sent_at"), 1 if st == "simulated" else 0,
                 draft["id"]))
            P.transition(con, lead["id"], "SENT", AGENT,
                         "provider id %s" % (status.get("provider_message_id")
                                             or st),
                         expect="READY_TO_SEND",
                         detail=json.dumps({k: status.get(k) for k in
                                            ("status", "provider_message_id",
                                             "provider_thread_id",
                                             "account_id")}))
        return "READY_TO_SEND -> SENT (%s, provider %s)" % (
            st, status.get("provider_message_id"))
    if st in ("failed", "dead", "needs_review", "cancelled"):
        with P.writing(con):
            P.transition(con, lead["id"], "ERROR", AGENT,
                         "MailHub reported %s: %s"
                         % (st, (status.get("error") or "")[:160]),
                         expect="READY_TO_SEND")
        return "READY_TO_SEND -> ERROR (%s)" % st
    return None                          # still pending or claimed



def after_sent(con, lead) -> Optional[str]:
    """Schedule the next follow-up, or leave the lead alone.

    Runs once per send: if a schedule row for the next stage already exists,
    there is nothing to do, so a repeated tick cannot stack follow-ups.
    """
    stage = (lead["followup_stage"] or 0) + 1
    existing = con.execute(
        "SELECT 1 FROM followups WHERE lead_id=? AND stage=?",
        (lead["id"], stage)).fetchone()
    if existing:
        return None
    due_at = F.next_due(con, lead["campaign_id"], stage)
    if not due_at:
        return None                      # campaign schedule exhausted
    with P.writing(con):
        F.schedule(con, lead["id"], lead["campaign_id"], stage, due_at)
        P.transition(con, lead["id"], "FOLLOWUP_WAITING", AGENT,
                     "follow-up %d scheduled for %s" % (stage, due_at),
                     expect="SENT")
    return "SENT -> FOLLOWUP_WAITING (stage %d due %s)" % (stage, due_at)


def followup_copy(con, lead) -> Optional[str]:
    """A follow-up is due. It goes through ARIA and SENTINEL like any message —
    reusing a pre-written draft without a fresh approval would mean sending
    text nothing had reviewed."""
    stage = lead["followup_stage"] or 1
    draft = P.load_draft(con, lead["id"], stage)
    if draft and draft["status"] == "draft" and not draft["qa_status"]:
        with P.writing(con):
            P.transition(con, lead["id"], "QA_PENDING", AGENT,
                         "follow-up %d drafted" % stage,
                         expect="FOLLOWUP_PENDING")
        return "FOLLOWUP_PENDING -> QA_PENDING (stage %d)" % stage

    # A task can finish having produced nothing — a model that answered in prose
    # instead of calling the tool, say. Re-dispatching under the same key just
    # returns that dead task, so the lead would sit here forever. Count the
    # attempts and vary the key, bounded so a persistently failing stage
    # escalates instead of looping.
    retries = con.execute(
        "SELECT COUNT(*) c FROM events WHERE lead_id=? AND event_type='followup.retry'"
        "   AND detail = ?", (lead["id"], "stage %d" % stage)).fetchone()["c"]
    if retries >= 3:
        with P.writing(con):
            P.transition(con, lead["id"], "HUMAN_REVIEW", AGENT,
                         "follow-up %d produced no draft after %d attempts"
                         % (stage, retries), expect="FOLLOWUP_PENDING")
        return "FOLLOWUP_PENDING -> HUMAN_REVIEW (no draft after %d tries)" % retries
    with P.writing(con):
        con.execute(
            "INSERT INTO events (lead_id, campaign_id, agent, event_type, detail)"
            " VALUES (?,?,?,?,?)",
            (lead["id"], lead["campaign_id"], AGENT, "followup.retry",
             "stage %d" % stage))

    prior = P.load_draft(con, lead["id"], 0)
    context = ""
    if prior:
        context = ("\n\nThe first email said:\n" + (prior["body"] or "")[:600])
    dispatch(lead["id"], "aria", "Follow-up %d for %s" % (stage, lead["id"]),
             brief({"lead_id": lead["id"]},
                   "You are ARIA. Call get_assignment, then write FOLLOW-UP "
                   "number %d and save it with save_draft using stage %d. "
                   "It must be shorter than the first email, add something "
                   "new rather than repeating it, and cite only sources "
                   "already present in NOVA's research. Pass stage=%d to "
                   "save_draft — omitting it would overwrite the original "
                   "email." % (stage, stage, stage)
                   + context),
             "followup:%d:r%d" % (stage, retries), gen(lead))
    return None


HANDLERS = [
    ("NEW", admit),
    ("RESEARCH_PENDING", dispatch_research),
    ("RESEARCHING", collect_research),
    ("RESEARCH_COMPLETE", to_copy),
    ("COPY_PENDING", dispatch_copy),
    ("COPY_READY", to_qa),
    ("QA_PENDING", dispatch_qa),
    ("QA_REJECTED", rewrite),
    ("READY_TO_SEND", queue_and_send),
    ("SENT", after_sent),
    ("FOLLOWUP_PENDING", followup_copy),
]


def tick(limit: int = 5, only: Optional[str] = None) -> List[str]:
    """One orchestration pass. Safe to run repeatedly and concurrently."""
    log: List[str] = []
    worker = "maya-%d" % os.getpid()
    with P.connect() as con:
        for state, handler in HANDLERS:
            if only and state != only:
                continue
            for lead in P.eligible(con, state, limit):
                lid = lead["id"]
                with P.writing(con):
                    got = P.claim(con, lid, worker, 300)
                if not got:
                    continue
                try:
                    msg = handler(con, lead)
                    if msg:
                        log.append("[%s] %s" % (lid, msg))
                except P.TransitionError as exc:
                    log.append("[%s] refused: %s" % (lid, exc))
                except Exception as exc:
                    log.append("[%s] %s: %s" % (lid, type(exc).__name__, exc))
                finally:
                    with P.writing(con):
                        P.release(con, lid, worker)
    return log


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick"); t.add_argument("--limit", type=int, default=5)
    t.add_argument("--only", default=None)
    t.add_argument("--loops", type=int, default=1)
    sub.add_parser("status")
    tl = sub.add_parser("timeline"); tl.add_argument("lead_id")
    args = ap.parse_args(argv)

    if args.cmd == "tick":
        for i in range(max(1, args.loops)):
            out = tick(args.limit, args.only)
            for line in out:
                print("  " + line)
            if not out and args.loops > 1:
                print("  (nothing to do)")
        return 0

    if args.cmd == "status":
        with P.connect() as con:
            for state, n in sorted(P.counts(con).items()):
                print("  %-20s %d" % (state, n))
        return 0

    with P.connect() as con:
        print("%-21s %-10s %-16s %-18s %s"
              % ("WHEN", "AGENT", "FROM", "TO", "DETAIL"))
        for e in P.timeline(con, args.lead_id):
            print("%-21s %-10s %-16s %-18s %s"
                  % (e["created_at"], e["agent"] or "", e["from_state"] or "",
                     e["to_state"] or "", (e["detail"] or "")[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
