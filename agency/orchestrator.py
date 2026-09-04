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
import datetime
import sqlite3
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
import tenants        # noqa: E402

HERMES = os.getenv("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
MAILHUB_BASE = os.getenv("MAILHUB_BASE_URL", "").rstrip("/")
MAILHUB_TOKEN = os.getenv("MAILHUB_API_TOKEN", "")
AGENT = "maya"

# Research that found nothing verifiable must not become outreach. A partial
# result needs at least this many cited observations to be worth writing from.
MIN_OBSERVATIONS = int(os.getenv("AGENCY_MIN_OBSERVATIONS", "1"))


# --- MailHub ----------------------------------------------------------------

def mailhub(method: str, path: str,
            body: Optional[Dict[str, Any]] = None,
            token: Optional[str] = None) -> Dict[str, Any]:
    """One MailHub call as one tenant.

    The token is explicit because a message and its status live inside the
    tenant that queued it: MailHub answers 404, not 403, when a key asks about
    another tenant's message, so calling with the wrong one looks like the
    message vanished rather than like an authorisation error.
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
        return {"error": "http %d" % e.code, "detail": e.read().decode()[:400]}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


# --- Kanban -----------------------------------------------------------------

def _mirror(con, lead_id: str, event: str, payload: dict) -> None:
    """Queue a Supabase write-back. Never raises into the pipeline.

    The work it describes has already happened and been committed; a mirror
    that cannot be queued is a retry, not a reason to undo a real send.
    """
    try:
        import supabase_sync
        supabase_sync.enqueue(lead_id, event, payload, con)
    except Exception:
        pass


def gen(lead) -> int:
    """This lead's lifecycle generation, or 1 if it predates the counter."""
    try:
        return int(lead["lifecycle_generation"] or 1)
    except (IndexError, KeyError, TypeError):
        return 1


# A task in one of these has stopped for good. Kanban still resolves the
# idempotency key to it, so re-issuing that key returns the finished task and
# creates nothing.
TERMINAL_TASK_STATUSES = frozenset(("done", "archived", "cancelled",
                                    "completed"))

# How many times one stage may be re-offered after a dead-end finish. Bounded,
# so a stage that genuinely cannot succeed stops instead of minting a task
# every two minutes for ever; whatever is left shows up in board_health().
MAX_TASK_RESCUES = int(os.getenv("AGENCY_MAX_TASK_RESCUES", "3"))

# How long a lead may sit in RESEARCHING before its task is checked for having
# died quietly. Comfortably longer than a NOVA run, which takes seconds.
RESEARCH_STALE_MINUTES = int(os.getenv("AGENCY_RESEARCH_STALE_MIN", "20"))

NOVA_BRIEF = ("You are NOVA. Research this business using the research tool, "
              "then call save_research with your findings. Every observation "
              "must carry a source_url and a quoted evidence string. If you "
              "cannot fetch anything, report research_status 'failed' — never "
              "write findings you did not fetch this session.")


def _create_task(key: str, profile: str, title: str,
                 body: str) -> Dict[str, Any]:
    """Create the task for this key, or return whatever already holds it."""
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


def dispatch(lead_id: str, profile: str, title: str, body: str,
             stage_key: str, generation: int = 1) -> Dict[str, Any]:
    """Create the task, or return the existing one.

    The idempotency key is (lead, lifecycle generation, stage). Within one
    lifecycle a tick that runs twice — or a restart mid-stage — collapses onto
    the same task, which is the protection worth keeping. Across lifecycles it
    does not: a lead that was deleted and re-ingested used to match the
    COMPLETED task from its previous life, so Kanban handed that task back,
    no worker was spawned, and the lead sat in RESEARCHING forever.

    The same collapse happens WITHIN a lifecycle when a task finishes without
    doing its work — an agent that answered conversationally and never called
    save_draft, or that was reclaimed and completed having written nothing.
    The key still resolves to that finished task, so every later tick received
    it, created nothing, and the lead waited for a worker that would never be
    spawned again. It cost 108 leads eleven hours on 2026-09-04, and unlike a
    blocked task it left nothing on the board to show for it.

    Callers must have established that the stage's OUTPUT is missing before
    calling — dispatch_copy loads the draft first, dispatch_qa the verdict —
    because a finished task is only a dead end when the work is genuinely
    absent. Given that, re-offering under a fresh key is safe: every write
    these agents make is keyed on (lead, stage), so a second run replaces
    rather than duplicates, and pipeline.save_draft refuses outright to
    overwrite anything already queued or sent.
    """
    base = "agency:%s:gen:%d:%s" % (lead_id, generation, stage_key)
    res = _create_task(base, profile, title, body)
    for attempt in range(1, MAX_TASK_RESCUES + 1):
        status = str((res or {}).get("status") or "").strip().lower()
        if status not in TERMINAL_TASK_STATUSES:
            return res                   # live, queued, or unreadable
        res = _create_task("%s:rescue:%d" % (base, attempt), profile, title,
                           body)
    return res


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
        brief({"lead_id": lead["id"]}, NOVA_BRIEF),
        "research", gen(lead))
    with P.writing(con):
        P.transition(con, lead["id"], "RESEARCHING", AGENT,
                     "dispatched to NOVA", expect="RESEARCH_PENDING",
                     detail=json.dumps(res)[:400])
    return "RESEARCH_PENDING -> RESEARCHING (task %s)" % res.get("id", "?")


def _stale_since(con, lead_id: str, minutes: int) -> bool:
    """Has this lead sat in its current state longer than `minutes`?"""
    row = con.execute(
        "SELECT state_changed_at < datetime('now', ?) FROM leads WHERE id=?",
        ("-%d minutes" % minutes, lead_id)).fetchone()
    return bool(row and row[0])


def collect_research(con, lead) -> Optional[str]:
    research = P.load_research(con, lead["id"])
    if not research:
        # Every other stage re-offers its work each tick, so a dead-end task is
        # noticed there. RESEARCHING only waits, which means a NOVA task that
        # finished without calling save_research strands the lead in silence.
        # Re-offering the same key costs nothing while the task is alive and
        # rescues it once it is not -- but only after long enough that a
        # working NOVA is never interrupted.
        if _stale_since(con, lead["id"], RESEARCH_STALE_MINUTES):
            res = dispatch(lead["id"], "nova", "Research lead %s" % lead["id"],
                           brief({"lead_id": lead["id"]}, NOVA_BRIEF),
                           "research", gen(lead))
            if str((res or {}).get("status") or "").lower() \
                    not in TERMINAL_TASK_STATUSES:
                return "RESEARCHING: NOVA task re-offered (%s)" % res.get("id", "?")
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

    # The tenant SENTINEL filed the approval under, read back rather than
    # recomputed. MailHub matches an approval on (owner_user_id, content_hash),
    # so queueing through any other tenant presents an approval that does not
    # exist there and the message would be refused.
    route = tenants.for_message(draft["tenant_user_id"], lead["id"], con)
    if route["status"] != "persisted":
        detail = {
            "changed": "assigned MailHub tenant (user %s) is no longer usable; "
                       "needs a fresh approval" % route.get("was"),
            "assigned": "reached READY_TO_SEND without a tenant recorded at "
                        "approval time",
            "none": "no usable MailHub tenant configured",
        }[route["status"]]
        with P.writing(con):
            P.transition(con, lead["id"], "HUMAN_REVIEW", AGENT, detail,
                         expect="READY_TO_SEND")
        return "READY_TO_SEND -> HUMAN_REVIEW (%s)" % route["status"]
    tok = route["tenant"]["queue"]
    if not tok:
        with P.writing(con):
            P.transition(con, lead["id"], "HUMAN_REVIEW", AGENT,
                         "no queue credential for the assigned MailHub tenant",
                         expect="READY_TO_SEND")
        return "READY_TO_SEND -> HUMAN_REVIEW (no queue credential)"

    if not draft["mailhub_queue_id"]:
        res = mailhub("POST", "/api/v1/messages", {
            "to_email": lead["email"], "to_name": lead["contact_name"] or "",
            "subject": draft["subject"], "body_text": draft["body"],
            "idempotency_key": key,
            "meta": {"lead_id": lead["id"], "campaign_id": lead["campaign_id"],
                     "stage": stage},
        }, token=tok)
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
        _mirror(con, lead["id"], "queued",
                {"mailhub_message_id": res.get("id")})
        return "READY_TO_SEND: queued as MailHub #%s (%s)" % (
            res.get("id"), res.get("status"))

    # Already queued — ask MailHub whether the provider took it.
    status = mailhub("GET", "/api/v1/messages/%s" % draft["mailhub_queue_id"],
                     token=tok)
    st = status.get("status")
    # The identity the prospect saw. MailHub records it on the message at
    # dispatch; it is the professional alias, never the transport mailbox.
    sender_shown = ((status.get("from_name") or "").strip() + " <" + status["from_email"] + ">"
                    ).strip() if status.get("from_email") else None
    if st in ("sent", "simulated"):
        with P.writing(con):
            con.execute(
                "UPDATE messages SET status=?, provider_message_id=?,"
                "       provider_thread_id=?, mailhub_account_id=?,"
                "       from_email=COALESCE(?, from_email),"
                "       sent_at=?, dry_run=?, updated_at=datetime('now') "
                " WHERE id=?",
                (st, status.get("provider_message_id"),
                 status.get("provider_thread_id"), status.get("account_id"),
                 sender_shown,
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
        # Only here, where the provider has confirmed. A queued message is
        # not a sent one and the mirror must not claim otherwise.
        _mirror(con, lead["id"], "sent",
                {"sender": sender_shown, "provider_message_id": status.get("provider_message_id"),
                 "provider_thread_id": status.get("provider_thread_id")})
        return "READY_TO_SEND -> SENT (%s, provider %s, as %s)" % (
            st, status.get("provider_message_id"),
            sender_shown or "sender not recorded")
    if st in ("failed", "dead", "needs_review", "cancelled"):
        with P.writing(con):
            P.transition(con, lead["id"], "ERROR", AGENT,
                         "MailHub reported %s: %s"
                         % (st, (status.get("error") or "")[:160]),
                         expect="READY_TO_SEND")
        _mirror(con, lead["id"], "send_failed",
                {"error": (status.get("error") or st)[:300]})
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


def _status_from_any_tenant(queue_id: str, preferred: Optional[int]):
    """Ask MailHub about one queued message, using a credential that can see it.

    A message is visible only to the tenant that queued it -- any other key
    gets a 404, not a 403. The tenant recorded on the message is tried first;
    the rest are tried only because a few early rows were queued before the
    tenant was recorded on them, and without that fallback those rows can
    never be resolved.
    """
    order = []
    if preferred is not None:
        t = tenants.by_user_id(int(preferred))
        if t and t.get("queue"):
            order.append(t)
    for t in tenants.load():
        if t.get("queue") and t not in order:
            order.append(t)
    for t in order:
        res = mailhub("GET", "/api/v1/messages/%s" % queue_id, token=t["queue"])
        if res and not res.get("error") and res.get("status"):
            return res, t
    return None, None


def reconcile_queued(con, limit: int = 50) -> List[str]:
    """Correct messages the provider confirmed but Hermes still calls queued.

    A send is recorded when the orchestrator polls the message it queued. If
    the lead leaves READY_TO_SEND first -- a reply arriving in the same two
    minutes moves it to REPLIED or HUMAN_REVIEW -- nothing polls that message
    again and it stays 'queued' for ever, so the agency under-counts its own
    sends and Supabase never learns the email went out.

    This asks MailHub for the truth and writes it down. It reads only; it can
    never send, re-send, or change a subject, body or approval. It touches
    only rows still marked queued, so running it twice is the same as running
    it once, and a message already recorded as sent is never revisited.

    The lead's state is advanced only when it is still waiting to send. A lead
    that has moved on moved on for a reason, and correcting the message it
    already sent is not grounds for dragging it backwards.
    """
    log: List[str] = []
    rows = con.execute(
        "SELECT id, lead_id, campaign_id, mailhub_queue_id, tenant_user_id,"
        "       followup_stage"
        "  FROM messages WHERE status='queued' AND mailhub_queue_id IS NOT NULL"
        " ORDER BY updated_at LIMIT ?", (limit,)).fetchall()
    for row in rows:
        status, tenant = _status_from_any_tenant(row["mailhub_queue_id"],
                                                 row["tenant_user_id"])
        if not status:
            log.append("[%s] MailHub #%s not visible to any tenant credential"
                       % (row["lead_id"], row["mailhub_queue_id"]))
            continue
        st = status.get("status")
        if st not in ("sent", "simulated"):
            continue                      # still pending, or already failed
        if not status.get("provider_message_id") and st == "sent":
            continue                      # sent without acknowledgement: leave it

        sender_shown = ((status.get("from_name") or "").strip() + " <"
                        + status["from_email"] + ">").strip() \
            if status.get("from_email") else None
        lead = con.execute("SELECT * FROM leads WHERE id=?",
                           (row["lead_id"],)).fetchone()
        state = lead["state"] if lead else None

        with P.writing(con):
            con.execute(
                "UPDATE messages SET status=?, provider_message_id=?,"
                "       provider_thread_id=?, mailhub_account_id=?,"
                "       from_email=COALESCE(?, from_email),"
                "       tenant_user_id=COALESCE(tenant_user_id, ?),"
                "       sent_at=?, dry_run=?, updated_at=datetime('now')"
                " WHERE id=? AND status='queued'",
                (st, status.get("provider_message_id"),
                 status.get("provider_thread_id"), status.get("account_id"),
                 sender_shown, tenant["user_id"] if tenant else None,
                 status.get("sent_at"), 1 if st == "simulated" else 0,
                 row["id"]))
            if state == "READY_TO_SEND":
                P.transition(con, row["lead_id"], "SENT", AGENT,
                             "reconciled: provider confirmed %s"
                             % (status.get("provider_message_id") or st),
                             expect="READY_TO_SEND")
        _mirror(con, row["lead_id"], "sent",
                {"sender": sender_shown,
                 "provider_message_id": status.get("provider_message_id"),
                 "provider_thread_id": status.get("provider_thread_id")})
        log.append("[%s] reconciled MailHub #%s -> %s (provider %s)%s"
                   % (row["lead_id"], row["mailhub_queue_id"], st,
                      status.get("provider_message_id"),
                      "" if state == "READY_TO_SEND"
                      else "; lead left READY_TO_SEND (%s), state untouched" % state))
    return log


AGENT_PROFILES = ("nova", "aria", "sentinel", "echo", "leo")

# How long a task is left alone after it last stopped, before it is offered
# again. Long enough that a provider having a bad minute is not hammered,
# short enough that a recovered one is picked up while the day is still young.
BLOCKED_COOLDOWN_MINUTES = int(os.getenv("AGENCY_BLOCKED_COOLDOWN_MIN", "15"))
BLOCKED_PER_TICK = int(os.getenv("AGENCY_BLOCKED_PER_TICK", "20"))


def _kanban_json(*args: str) -> Any:
    env = {**os.environ, "HOME": os.getenv("HERMES_HOME", "/opt/data"),
           "HERMES_HOME": os.getenv("HERMES_HOME", "/opt/data")}
    try:
        out = subprocess.run([HERMES, "kanban", *args], capture_output=True,
                             text=True, timeout=120, env=env)
        return json.loads((out.stdout or "").strip() or "null")
    except Exception:
        return None


def _epoch(value: Any) -> Optional[float]:
    """Seconds since the epoch, from whichever shape the board reports.

    Kanban returns these as integers, not ISO strings. Comparing them as text
    against a formatted timestamp is not a comparison at all -- "1788466776"
    sorts before "2026-.." on its first character -- so every task looked
    equally stale and the cooldown did nothing.
    """
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).replace("Z", "+00:00")
        when = datetime.datetime.fromisoformat(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return when.timestamp()
    except Exception:
        return None


# A running task older than this is worth mentioning. It is not a fault by
# itself -- during the 2026-09-04 provider outage eight ARIA workers ran for
# ten hours, alive and heartbeating the whole time, and reclaiming them would
# have thrown away work that later completed -- but it is the first thing to
# look at when the pipeline is quiet.
RUNNING_WARN_MINUTES = int(os.getenv("AGENCY_RUNNING_WARN_MIN", "45"))


def _kanban_db_path() -> str:
    return os.path.join(os.getenv("HERMES_HOME", "/opt/data"), "kanban.db")


def _pid_alive(pid) -> Optional[bool]:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True                      # exists, owned by someone else
    except (TypeError, ValueError):
        return None                      # no pid recorded


def board_health() -> Dict[str, Any]:
    """Counts worth alerting on, read straight from Kanban's own store.

    `kanban list --json` does not expose worker_pid or last_heartbeat_at, and
    those two are the only real evidence of whether a running task is alive.
    Reading the database read-only gets them without asking Kanban to reason
    about staleness on our behalf -- deciding that is Kanban's job, and it
    already does it well. This only reports.
    """
    out: Dict[str, Any] = {"running": 0, "blocked": 0, "ready": 0,
                           "running_over_warn": 0, "zombie_suspects": 0,
                           "oldest_running_min": 0, "oldest_blocked_min": 0}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % _kanban_db_path(), uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return out
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    try:
        rows = con.execute(
            "SELECT id, status, assignee, started_at, worker_pid,"
            "       last_heartbeat_at FROM tasks"
            " WHERE status IN ('running','blocked','ready')").fetchall()
    except sqlite3.Error:
        con.close()
        return out
    suspects = []
    for r in rows:
        st = r["status"]
        out[st] = out.get(st, 0) + 1
        started = r["started_at"] or 0
        age = (now - int(started)) // 60 if started else 0
        if st == "running":
            out["oldest_running_min"] = max(out["oldest_running_min"], age)
            if age >= RUNNING_WARN_MINUTES:
                out["running_over_warn"] += 1
            # A suspect is a running task with no live worker AND no recent
            # heartbeat. Kanban reclaims these itself; if one persists across
            # ticks, that is the thing to escalate.
            hb = r["last_heartbeat_at"]
            hb_age = (now - int(hb)) if hb else None
            if _pid_alive(r["worker_pid"]) is False and (
                    hb_age is None or hb_age > 60 * 60):
                suspects.append(r["id"])
        elif st == "blocked":
            out["oldest_blocked_min"] = max(out["oldest_blocked_min"], age)
    con.close()
    out["zombie_suspects"] = len(suspects)
    out["zombie_suspect_ids"] = suspects[:10]
    return out


# Which task key belongs to the stage a lead is waiting in. A lead that has
# just reached QA_PENDING still carries finished research and copy tasks, so
# asking whether ALL its tasks are terminal says yes about work that is not
# the work it is waiting for.
STAGE_TASK_PREFIX = {"RESEARCHING": "research", "COPY_PENDING": "copy",
                     "QA_PENDING": "qa"}


def _task_statuses_by_lead(lead_ids) -> Dict[str, List[tuple]]:
    """(stage, status) for each lead's tasks, in one pass over the board."""
    out: Dict[str, List[tuple]] = {}
    if not lead_ids:
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % _kanban_db_path(), uri=True)
    except sqlite3.Error:
        return out
    try:
        for key, status in con.execute(
                "SELECT idempotency_key, status FROM tasks"
                " WHERE idempotency_key LIKE 'agency:%'"):
            # agency:<lead>:gen:<n>:<stage>[:rescue:<n>]
            parts = (key or "").split(":")
            if len(parts) >= 5:
                out.setdefault(parts[1], []).append(
                    (parts[4], (status or "").lower()))
    except sqlite3.Error:
        pass
    con.close()
    return out


def stranded_leads(con, limit: int = 400) -> List[str]:
    """Leads whose stage output is missing AND whose every task has stopped.

    Both halves are needed, and getting that wrong is easy: a lead sitting in
    QA_PENDING with a draft and no verdict is the NORMAL waiting state, not a
    fault, and counting those made this metric report sixty stranded leads on
    a pipeline that was draining perfectly. What makes a lead stranded is that
    nothing is left to produce the output -- every task it has is terminal --
    which is the condition dispatch() re-offers on the next tick.

    Reported, not repaired. A count that does not fall over successive ticks
    means the rescue budget is spent and a person should look.
    """
    out = []
    try:
        rows = con.execute(
            "SELECT id, state, followup_stage FROM leads"
            " WHERE state IN ('COPY_PENDING','QA_PENDING','RESEARCHING')"
            " LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return out
    tasks = _task_statuses_by_lead([r["id"] for r in rows])
    for r in rows:
        lid, state = r["id"], r["state"]
        if state == "RESEARCHING":
            missing = not P.load_research(con, lid)
        else:
            draft = P.load_draft(con, lid, r["followup_stage"] or 0)
            missing = (not draft) if state == "COPY_PENDING"                 else (not draft or not draft["qa_status"])
        if not missing:
            continue
        prefix = STAGE_TASK_PREFIX.get(state)
        seen = [st for stage, st in (tasks.get(lid) or [])
                if prefix and stage.startswith(prefix)]
        # No task for THIS stage means it has not been dispatched yet, which is
        # the tick's ordinary backlog, not a strand.
        if seen and all(st in TERMINAL_TASK_STATUSES for st in seen):
            out.append(lid)
    return out


def reap_blocked(limit: int = BLOCKED_PER_TICK) -> List[str]:
    """Offer blocked agent tasks back to the board.

    Kanban blocks a task once its retries are spent. That is right for a task
    that cannot succeed, and wrong for one that failed because the model
    endpoint was refusing requests: the work is fine and the world has moved
    on. Nothing retried those, and because dispatch is idempotent the
    orchestrator kept receiving the same blocked task and creating no new
    work, so the pipeline stayed frozen after the provider recovered. One
    provider outage of ninety minutes cost six hours of stillness and 251
    stranded tasks.

    A task is left alone for a cooldown after it last stopped, so a provider
    having a bad minute is retried patiently rather than hammered, and only a
    bounded number are offered per tick. If the endpoint is still refusing,
    they simply block again and are tried once more later; when it recovers,
    the queue drains on its own.
    """
    tasks = _kanban_json("list", "--json")
    if not tasks:
        return []
    rows = tasks.get("tasks", tasks) if isinstance(tasks, dict) else tasks
    cutoff = (datetime.datetime.now(datetime.timezone.utc).timestamp()
              - BLOCKED_COOLDOWN_MINUTES * 60)
    stale = []
    for t in rows:
        if not isinstance(t, dict) or t.get("status") != "blocked":
            continue
        if t.get("assignee") not in AGENT_PROFILES:
            continue
        when = _epoch(t.get("completed_at") or t.get("started_at")
                      or t.get("created_at"))
        if when is not None and when > cutoff:
            continue                    # still cooling down
        stale.append((when if when is not None else 0.0, t))
    if not stale:
        return []
    stale.sort(key=lambda pair: pair[0])
    stale = [t for _, t in stale]
    log = []
    for t in stale[:limit]:
        _kanban_json("unblock", t["id"], "--reason",
                     "retried by the orchestrator: blocked after its retries "
                     "were spent, which is usually the model endpoint refusing "
                     "requests rather than anything wrong with the task")
        log.append("unblocked %s (%s) %s" % (t["id"], t.get("assignee"),
                                             (t.get("title") or "")[:48]))
    if len(stale) > limit:
        log.append("%d more blocked task(s) left for the next tick"
                   % (len(stale) - limit))
    return log


def tick(limit: int = 5, only: Optional[str] = None) -> List[str]:
    """One orchestration pass. Safe to run repeatedly and concurrently."""
    log: List[str] = []
    worker = "maya-%d" % os.getpid()
    with P.connect() as con:
        # Before anything else: a message the provider confirmed while its lead
        # was moving elsewhere is still marked queued. Cheap -- it reads only
        # rows in that state, and there are normally none.
        try:
            log.extend(reconcile_queued(con))
        except Exception as exc:
            log.append("reconcile: %s: %s" % (type(exc).__name__, exc))
        # A stage only moves when its agent task runs. One blocked task is one
        # lead that will never advance on its own, however healthy everything
        # else looks, so they are offered back before any state is examined.
        try:
            log.extend(reap_blocked())
        except Exception as exc:
            log.append("reap: %s: %s" % (type(exc).__name__, exc))
        # Say what the board looks like every tick. A stall used to be visible
        # only by noticing that nothing had happened for hours.
        try:
            h = board_health()
            if h["running_over_warn"] or h["blocked"] or h["zombie_suspects"]:
                log.append(
                    "board: running=%d (>%dm: %d) blocked=%d ready=%d "
                    "oldest_running=%dm oldest_blocked=%dm zombie_suspects=%d"
                    % (h["running"], RUNNING_WARN_MINUTES,
                       h["running_over_warn"], h["blocked"], h["ready"],
                       h["oldest_running_min"], h["oldest_blocked_min"],
                       h["zombie_suspects"]))
        except Exception as exc:
            log.append("board_health: %s: %s" % (type(exc).__name__, exc))
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
