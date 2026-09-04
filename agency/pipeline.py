#!/usr/bin/env python3
"""The lead state machine — the only sanctioned way a lead changes state.

Every stage transition in the agency goes through `transition()`. Nothing else
may write `leads.state`. That matters because the states encode real-world
consequences: READY_TO_SEND means a human-reviewed message is cleared to leave
the building, and SENT means it actually did.

Three guarantees, all enforced by the database rather than by convention:

**Legality.** A transition must exist in `state_transitions`. An agent that
decides a lead should jump from NEW straight to READY_TO_SEND is refused, so
skipping research or QA is not merely discouraged — it is impossible.

**Atomicity.** The state change is a compare-and-swap: `UPDATE ... WHERE id = ?
AND state = ?`. Two workers reading the same lead both see COPY_PENDING, both
try to advance it, and exactly one gets `rowcount == 1`. The loser is told it
lost instead of silently double-advancing the lead — which is how a prospect
gets two emails.

**Auditability.** Every accepted transition appends an `events` row. The state
column tells you where a lead is; the events table tells you how it got there,
which agent moved it, and why.

Locks are leases with an expiry, not flags. A Render restart mid-stage must not
strand a lead forever, so a lock that is never released simply ages out.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

DB = os.getenv("AGENCY_DB", "/opt/data/agency.db")

# How long a worker may hold a lead before the lease expires. Long enough for a
# slow model call, short enough that a crashed worker does not block a lead for
# an appreciable time.
LEASE_SECONDS = int(os.getenv("AGENCY_LEASE_SECONDS", "900"))


class TransitionError(RuntimeError):
    """Raised when a state change is refused. Always fail closed."""


@contextmanager
def connect(path: str = None) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(path or DB, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    try:
        yield con
    finally:
        con.close()


@contextmanager
def writing(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction.

    BEGIN IMMEDIATE takes the write lock up front. With the default deferred
    mode, two writers can both begin, both read, and only collide at COMMIT —
    at which point one has already made decisions on stale data.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


# --- state machine ----------------------------------------------------------

def is_legal(con: sqlite3.Connection, from_state: str, to_state: str) -> bool:
    return con.execute(
        "SELECT 1 FROM state_transitions WHERE from_state=? AND to_state=?",
        (from_state, to_state)).fetchone() is not None


def get_lead(con: sqlite3.Connection, lead_id: str) -> Optional[sqlite3.Row]:
    return con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()


def transition(con: sqlite3.Connection, lead_id: str, to_state: str,
               agent: str, reason: str = "",
               expect: Optional[str] = None,
               detail: Optional[str] = None) -> Dict[str, Any]:
    """Move a lead to `to_state`. Refuses anything it cannot prove is safe.

    `expect` asserts the state the caller believed the lead was in. Supplying it
    turns the update into a compare-and-swap against that exact state, which is
    what makes concurrent advancement impossible rather than merely unlikely.

    Must be called inside `writing()`.
    """
    lead = get_lead(con, lead_id)
    if lead is None:
        raise TransitionError("no such lead: %s" % lead_id)

    current = lead["state"]
    if expect is not None and current != expect:
        raise TransitionError(
            "lead %s is in %s, caller expected %s" % (lead_id, current, expect))

    if current == to_state:
        return {"changed": False, "state": current, "reason": "already there"}

    if not is_legal(con, current, to_state):
        raise TransitionError(
            "illegal transition %s -> %s for lead %s" % (current, to_state, lead_id))

    n = con.execute(
        "UPDATE leads SET state=?, state_reason=?, "
        "       state_changed_at=datetime('now'), updated_at=datetime('now') "
        " WHERE id=? AND state=?",
        (to_state, (reason or "")[:500], lead_id, current)).rowcount
    if n != 1:
        # Someone else moved it between our read and our write. Losing this
        # race is normal; acting anyway would double-advance the lead.
        raise TransitionError(
            "lead %s changed underneath us (expected %s)" % (lead_id, current))

    con.execute(
        "INSERT INTO events (lead_id, campaign_id, agent, event_type,"
        "                    from_state, to_state, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (lead_id, lead["campaign_id"], agent, "state.changed", current,
         to_state, detail or reason or None))
    _mirror(con, lead_id, to_state, reason)
    return {"changed": True, "from": current, "state": to_state}


def _mirror(con: sqlite3.Connection, lead_id: str, to_state: str,
            reason: str = None) -> None:
    """Queue this state change for the Supabase mirror, if the lead came from it.

    Deliberately swallows everything. The transition has already been written
    and the event already recorded; a mirror that cannot be queued must not
    turn a real state change into an exception, and must never be a reason to
    roll back a message that has already left. Leads that did not come from
    Supabase queue nothing.

    Imported lazily so pipeline.py does not depend on the sync module — the
    state machine has to work on a box where Supabase was never configured.
    """
    try:
        row = con.execute("SELECT 1 FROM supabase_leads WHERE lead_id=?",
                          (lead_id,)).fetchone()
        if not row:
            return
        import supabase_sync
        # The reason travels only for states that actually represent a
        # failure; for everything else it is a normal explanatory note and
        # belongs in events, not in the mirror's error column.
        payload = {"state": to_state}
        if to_state in ("ERROR", "BOUNCED") and reason:
            payload["error"] = reason
        supabase_sync.enqueue(lead_id, "state", payload, con)
    except Exception:
        pass


# --- leasing ----------------------------------------------------------------

def claim(con: sqlite3.Connection, lead_id: str, worker: str,
          seconds: int = LEASE_SECONDS) -> bool:
    """Take the lease on a lead, or return False.

    One statement, so two workers cannot both succeed. An expired lease is
    reclaimable — the previous holder is assumed dead, not merely slow, which
    is why the lease is generous.
    """
    return con.execute(
        "UPDATE leads SET locked_by=?, locked_until=datetime('now', ?), "
        "       updated_at=datetime('now') "
        " WHERE id=? AND (locked_until IS NULL OR locked_until < datetime('now'))",
        (worker, "+%d seconds" % seconds, lead_id)).rowcount == 1


def release(con: sqlite3.Connection, lead_id: str, worker: str) -> None:
    """Drop our own lease. Scoped to the holder so a late worker cannot free
    a lead another worker has since taken."""
    con.execute(
        "UPDATE leads SET locked_by=NULL, locked_until=NULL, "
        "       updated_at=datetime('now') WHERE id=? AND locked_by=?",
        (lead_id, worker))


# --- queries the orchestrator needs ------------------------------------------

def eligible(con: sqlite3.Connection, state: str,
             limit: int = 10) -> List[sqlite3.Row]:
    """Unlocked leads sitting in `state` whose campaign is running, oldest first.

    The campaign check lives here rather than in each handler so that pausing a
    campaign stops everything under it — research, copy, QA, queueing and
    follow-ups alike — instead of stopping whichever stage somebody remembered
    to guard. Only 'active' runs; draft, paused and archived do not. A lead
    whose campaign row is missing is treated as running, because a bookkeeping
    gap must not silently halt live outreach.

    One thing a pause does NOT do: recall a message already handed to MailHub.
    MailHub owns its queue and will deliver what it has accepted. Pausing stops
    the agency creating anything new; to stop mail already in flight, disable
    the mailbox in MailHub.
    """
    return con.execute(
        "SELECT l.* FROM leads l "
        "  LEFT JOIN campaigns c ON c.id = l.campaign_id "
        " WHERE l.state=? "
        "   AND (l.locked_until IS NULL OR l.locked_until < datetime('now')) "
        "   AND COALESCE(c.status, 'active') = 'active' "
        # A hold stops a lead being worked without moving it. Withdrawing a
        # batch by changing state would rewrite where each lead had actually
        # got to and could not be undone; clearing this column can.
        "   AND l.hold_reason IS NULL "
        " ORDER BY l.updated_at ASC LIMIT ?", (state, limit)).fetchall()


def hold(con: sqlite3.Connection, lead_id: str, reason: str) -> bool:
    """Make a lead ineligible for work without moving it. Reversible."""
    cur = con.execute(
        "UPDATE leads SET hold_reason=?, held_at=datetime('now'),"
        "       updated_at=datetime('now')"
        " WHERE id=? AND hold_reason IS NULL", (reason, lead_id))
    return cur.rowcount > 0


def release_hold(con: sqlite3.Connection, lead_id: str,
                 reason: Optional[str] = None) -> bool:
    """Let a held lead be worked again. `reason` restricts it to one batch."""
    sql = ("UPDATE leads SET hold_reason=NULL, held_at=NULL,"
           "       updated_at=datetime('now') WHERE id=? AND hold_reason IS NOT NULL")
    args: List[Any] = [lead_id]
    if reason:
        sql += " AND hold_reason=?"
        args.append(reason)
    return con.execute(sql, args).rowcount > 0


def held(con: sqlite3.Connection, reason: Optional[str] = None) -> Dict[str, int]:
    """How many leads are held, by reason and state, for reporting."""
    sql = ("SELECT hold_reason, state, COUNT(*) n FROM leads"
           " WHERE hold_reason IS NOT NULL")
    args: List[Any] = []
    if reason:
        sql += " AND hold_reason=?"
        args.append(reason)
    sql += " GROUP BY hold_reason, state"
    return {"%s/%s" % (r["hold_reason"], r["state"]): r["n"]
            for r in con.execute(sql, args)}


def counts(con: sqlite3.Connection) -> Dict[str, int]:
    return {r["state"]: r["n"] for r in con.execute(
        "SELECT state, COUNT(*) n FROM leads GROUP BY state ORDER BY state")}


def timeline(con: sqlite3.Connection, lead_id: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in con.execute(
        "SELECT created_at, agent, event_type, from_state, to_state, detail "
        "  FROM events WHERE lead_id=? ORDER BY id", (lead_id,))]


# --- stage payload persistence ----------------------------------------------
# Agents write their output through these rather than touching leads directly,
# so the state machine stays the only path that moves a lead.

def save_research(con: sqlite3.Connection, lead_id: str,
                  research: Dict[str, Any]) -> None:
    conf = research.get("confidence")
    if conf is None:
        obs = research.get("verified_observations") or []
        conf = max([o.get("confidence", 0) or 0 for o in obs], default=0.0)
    con.execute(
        "UPDATE leads SET research_json=?, research_confidence=?, "
        "       updated_at=datetime('now') WHERE id=?",
        (json.dumps(research), float(conf), lead_id))


def load_research(con: sqlite3.Connection, lead_id: str) -> Dict[str, Any]:
    row = con.execute("SELECT research_json FROM leads WHERE id=?",
                      (lead_id,)).fetchone()
    if not row or not row["research_json"]:
        return {}
    try:
        return json.loads(row["research_json"])
    except Exception:
        return {}


def content_hash(subject: str, body: str) -> str:
    """The same hash MailHub computes. Both sides must agree byte for byte, or
    the approval will not match and the send is refused."""
    import hashlib
    payload = (subject or "").strip() + "\x00" + (body or "").strip()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def message_id(lead_id: str, stage: int) -> str:
    """Deterministic, so a rewrite after a QA rejection replaces the draft
    rather than leaving two rows with nothing to say which was approved."""
    return "M-%s-%d" % (lead_id, stage)


def save_draft(con: sqlite3.Connection, lead_id: str, campaign_id: str,
               stage: int, subject: str, body: str,
               claims_used: Optional[List[Dict[str, Any]]] = None) -> str:
    """Store or replace the draft for one (lead, stage).

    Writing a draft always clears any previous QA verdict: text that has
    changed has not been reviewed, and leaving a stale `approved` on it would
    be exactly the bypass the gate exists to prevent.

    A message that has already been transmitted is NOT a draft and cannot be
    overwritten. It is the record of what a real person received, and the
    provider ids on it would otherwise end up attached to text that was never
    sent. Live testing caught exactly that: a follow-up written against the
    wrong stage silently replaced the original outreach.
    """
    mid = message_id(lead_id, stage)
    existing = con.execute(
        "SELECT status, provider_message_id FROM messages WHERE id=?",
        (mid,)).fetchone()
    if existing and (existing["status"] in ("sent", "simulated", "queued")
                     or existing["provider_message_id"]):
        raise TransitionError(
            "message %s is already %s and cannot be rewritten — a follow-up "
            "belongs to a new stage" % (mid, existing["status"]))
    con.execute(
        "INSERT INTO messages (id, lead_id, campaign_id, direction, kind,"
        "                      followup_stage, subject, body, claims_used,"
        "                      content_hash, status, qa_status, qa_issues,"
        "                      approval_id, dry_run, updated_at) "
        "VALUES (?,?,?, 'outbound', ?, ?,?,?,?,?, 'draft', NULL, NULL, NULL, 1,"
        "        datetime('now')) "
        "ON CONFLICT (id) DO UPDATE SET "
        "  subject=excluded.subject, body=excluded.body,"
        "  claims_used=excluded.claims_used, content_hash=excluded.content_hash,"
        "  status='draft', qa_status=NULL, qa_issues=NULL, approval_id=NULL,"
        "  updated_at=datetime('now')",
        (mid, lead_id, campaign_id, "outreach" if stage == 0 else "followup",
         stage, subject, body, json.dumps(claims_used or []),
         content_hash(subject, body)))
    return mid


def load_draft(con: sqlite3.Connection, lead_id: str,
               stage: int = 0) -> Optional[Dict[str, Any]]:
    row = con.execute("SELECT * FROM messages WHERE id=?",
                      (message_id(lead_id, stage),)).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["claims_used"] = json.loads(out.get("claims_used") or "[]")
    except Exception:
        out["claims_used"] = []
    return out


def record_qa(con: sqlite3.Connection, lead_id: str, stage: int, status: str,
              issues: Optional[List[str]] = None,
              approval_id: Optional[str] = None) -> None:
    """Attach SENTINEL's verdict to the exact draft it reviewed.

    Guarded on the content hash: if the text changed between review and this
    write, the update matches nothing and the verdict is not recorded.
    """
    draft = load_draft(con, lead_id, stage)
    if not draft:
        raise TransitionError("no draft for %s stage %d" % (lead_id, stage))
    n = con.execute(
        "UPDATE messages SET qa_status=?, qa_issues=?, approval_id=?,"
        "       updated_at=datetime('now') WHERE id=? AND content_hash=?",
        (status, json.dumps(issues or []), approval_id,
         draft["id"], draft["content_hash"])).rowcount
    if n != 1:
        raise TransitionError("draft changed while QA was running")
