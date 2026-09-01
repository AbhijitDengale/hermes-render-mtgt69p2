#!/usr/bin/env python3
"""Follow-up scheduling and cancellation.

agency.db owns follow-up workflow state; Hermes cron owns execution timing. The
cron job is a trigger, not the source of truth — so a lost job leaves the
schedule intact, and a job that fires twice finds the work already done.

The cancellation rule is the important one. When a reply arrives, follow-ups
are cancelled BEFORE anything reasons about the reply. Classification can fail,
time out, or be wrong; cancellation must not depend on it. The worst outcome in
this system is chasing someone who already answered.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# A lead in any of these has either answered, opted out, or needs a human.
# Nothing automated may be sent to it. Checked live at execution time, never
# cached: the whole point is that the state may have changed since scheduling.
TERMINAL = frozenset({
    "REPLIED", "POSITIVE", "NEGATIVE", "UNSUBSCRIBED", "BOUNCED",
    "HUMAN_REVIEW", "MEETING_STAGE", "CLOSED", "ERROR",
})

DEFAULT_SCHEDULE = [3, 7, 12]          # days after the previous send


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def campaign_schedule(con: sqlite3.Connection,
                      campaign_id: str) -> List[float]:
    """Per-campaign, never global. Accepts days as numbers, or a string with a
    unit ("2m", "4h") so a test campaign can run in minutes rather than days."""
    row = con.execute(
        "SELECT followup_schedule, max_followups FROM campaigns WHERE id=?",
        (campaign_id,)).fetchone()
    raw = (row["followup_schedule"] if row else None) or json.dumps(DEFAULT_SCHEDULE)
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = DEFAULT_SCHEDULE
    out: List[float] = []
    for item in parsed:
        if isinstance(item, (int, float)):
            out.append(float(item) * 86400)          # days
        elif isinstance(item, str):
            m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", item, re.I)
            if not m:
                continue
            n, unit = float(m.group(1)), (m.group(2) or "d").lower()
            out.append(n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])
    return out or [d * 86400 for d in DEFAULT_SCHEDULE]


def next_due(con: sqlite3.Connection, campaign_id: str, stage: int,
             frm: Optional[datetime] = None) -> Optional[str]:
    """When follow-up `stage` (1-based) is due, or None if the campaign is done."""
    sched = campaign_schedule(con, campaign_id)
    if stage < 1 or stage > len(sched):
        return None
    return _iso((frm or _now()) + timedelta(seconds=sched[stage - 1]))


def schedule(con: sqlite3.Connection, lead_id: str, campaign_id: str,
             stage: int, due_at: str,
             cron_job_id: Optional[str] = None) -> Optional[str]:
    """Record a scheduled follow-up. Idempotent per (lead, stage)."""
    fid = "F-%s-%d" % (lead_id, stage)
    con.execute(
        "INSERT INTO followups (id, lead_id, campaign_id, stage, scheduled_for,"
        "                       status, cron_job_id) "
        "VALUES (?,?,?,?,?, 'scheduled', ?) "
        "ON CONFLICT (lead_id, stage) DO NOTHING",
        (fid, lead_id, campaign_id, stage, due_at, cron_job_id))
    con.execute(
        "INSERT INTO events (lead_id, campaign_id, agent, event_type, detail) "
        "VALUES (?,?,?,?,?)",
        (lead_id, campaign_id, "echo", "followup.scheduled",
         "stage %d due %s" % (stage, due_at)))
    return fid


def cancel_all(con: sqlite3.Connection, lead_id: str, reason: str,
               agent: str = "echo") -> int:
    """Cancel every scheduled follow-up for a lead. Returns how many.

    Called before classification, never after. Safe to call repeatedly — only
    rows still in 'scheduled' are affected.
    """
    n = con.execute(
        "UPDATE followups SET status='cancelled', cancel_reason=?,"
        "       cancelled_at=datetime('now') "
        " WHERE lead_id=? AND status='scheduled'",
        (reason[:200], lead_id)).rowcount
    if n:
        con.execute(
            "INSERT INTO events (lead_id, agent, event_type, detail) "
            "VALUES (?,?,?,?)",
            (lead_id, agent, "followup.cancelled",
             "%d cancelled: %s" % (n, reason[:160])))
    return n


def due(con: sqlite3.Connection, limit: int = 20) -> List[sqlite3.Row]:
    """Scheduled follow-ups whose time has come."""
    # Only 'scheduled' is eligible. Once ECHO has handed a stage to the
    # pipeline it becomes 'dispatched' and stops appearing here, so later ticks
    # neither re-evaluate it nor inflate its attempt count.
    # The campaign's status comes along for the ride so blocked_reason can see
    # it. A campaign the operator has paused must not keep sending, and until
    # this join existed nothing anywhere consulted campaigns.status.
    return con.execute(
        "SELECT f.*, l.state AS lead_state, l.ooo_until, l.email AS lead_email,"
        "       COALESCE(c.status, 'active') AS campaign_status "
        "  FROM followups f "
        "  JOIN leads l ON l.id = f.lead_id "
        "  LEFT JOIN campaigns c ON c.id = f.campaign_id "
        " WHERE f.status='scheduled' AND f.scheduled_for <= datetime('now') "
        " ORDER BY f.scheduled_for LIMIT ?", (limit,)).fetchall()


# Only 'active' runs. draft, paused and archived all mean "not now", and a
# campaign row that has gone missing is treated as active so a bookkeeping
# gap cannot silently halt live outreach.
RUNNING = frozenset({"active"})


def _campaign_status(row: sqlite3.Row) -> str:
    try:
        return row["campaign_status"] or "active"
    except (IndexError, KeyError):
        return "active"


def is_paused(row: sqlite3.Row) -> bool:
    """True when the follow-up's campaign is not running.

    Distinguished from every other block because it is REVERSIBLE: the
    follow-up stays scheduled so resuming the campaign brings it back, and its
    attempt count is untouched — a paused day is not a failed delivery.
    """
    return _campaign_status(row) not in RUNNING


def mark_blocked(con: sqlite3.Connection, followup_id: str,
                 reason: str) -> bool:
    """Record why a follow-up was held, without consuming it.

    Status stays 'scheduled' and attempts is not incremented, so the follow-up
    returns the moment the block lifts. Returns True only when the reason is
    new, so ECHO can write one event per change instead of one every two
    minutes for as long as the campaign stays paused.
    """
    row = con.execute("SELECT last_blocked_reason FROM followups WHERE id=?",
                      (followup_id,)).fetchone()
    changed = row is None or (row["last_blocked_reason"] or "") != reason[:200]
    con.execute(
        "UPDATE followups SET last_blocked_reason=?,"
        "       last_blocked_at=datetime('now') WHERE id=?",
        (reason[:200], followup_id))
    return changed


def blocked_reason(row: sqlite3.Row) -> Optional[str]:
    """Why this follow-up must not be sent right now, or None.

    Evaluated against the lead's CURRENT state, read at execution time. A
    schedule made three days ago knows nothing about the reply that arrived
    yesterday.
    """
    # Checked before anything about the lead: if the campaign is not running,
    # nothing under it should send, whatever the individual lead is doing.
    if is_paused(row):
        return ("campaign %s is %s"
                % (row["campaign_id"], _campaign_status(row)))
    state = row["lead_state"]
    if state in TERMINAL:
        return "lead is %s" % state
    if state not in ("SENT", "FOLLOWUP_WAITING", "FOLLOWUP_PENDING"):
        # Anything else means the lead is mid-pipeline. Ambiguous is a stop.
        return "lead is in %s, which is not a follow-up-safe state" % state
    ooo = row["ooo_until"]
    if ooo and ooo > _iso(_now()):
        return "out of office until %s" % ooo
    return None



def mark_dispatched(con: sqlite3.Connection, followup_id: str) -> None:
    """ECHO has handed this stage to the pipeline.

    Recording it here is what stops the next tick counting the same handover
    again. The state machine already made a repeat transition impossible; this
    makes the bookkeeping honest about it.
    """
    con.execute(
        "UPDATE followups SET status='dispatched',"
        "       dispatched_at=datetime('now'),"
        "       last_execution_at=datetime('now'), attempts=attempts+1 "
        " WHERE id=? AND status='scheduled'", (followup_id,))


def mark_status(con: sqlite3.Connection, followup_id: str, status: str) -> None:
    """Advance a dispatched follow-up through the rest of its lifecycle."""
    con.execute(
        "UPDATE followups SET status=?, last_execution_at=datetime('now') "
        " WHERE id=?", (status, followup_id))


def mark_sent(con: sqlite3.Connection, followup_id: str,
              message_id: str) -> None:
    con.execute(
        "UPDATE followups SET status='sent', message_id=?,"
        "       last_execution_at=datetime('now'), attempts=attempts+1 "
        " WHERE id=?", (message_id, followup_id))


def mark_skipped(con: sqlite3.Connection, followup_id: str,
                 reason: str) -> None:
    con.execute(
        "UPDATE followups SET status='skipped', cancel_reason=?,"
        "       cancelled_at=datetime('now'),"
        "       last_execution_at=datetime('now'), attempts=attempts+1 "
        " WHERE id=?", (reason[:200], followup_id))


def touch(con: sqlite3.Connection, followup_id: str) -> None:
    con.execute(
        "UPDATE followups SET last_execution_at=datetime('now'),"
        "       attempts=attempts+1 WHERE id=?", (followup_id,))


# --- out-of-office ----------------------------------------------------------

_OOO_DATE = [
    re.compile(r"\b(?:back|return(?:ing)?|available)\s+(?:on|from)?\s*"
               r"(\d{4}-\d{2}-\d{2})", re.I),
    re.compile(r"\b(?:until|till|through)\s+(\d{4}-\d{2}-\d{2})", re.I),
]


def parse_return_date(text: str) -> Optional[str]:
    """A return date, only when unambiguous.

    Deliberately narrow: ISO dates only. Free-form dates ("back Monday", "the
    3rd") are guesses, and guessing wrong means emailing someone while they are
    still away — so an unparseable OOO becomes a human decision instead.
    """
    for rx in _OOO_DATE:
        m = rx.search(text or "")
        if m:
            try:
                d = datetime.strptime(m.group(1), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
            if _now() < d < _now() + timedelta(days=120):
                return _iso(d)
    return None


def hold_for_ooo(con: sqlite3.Connection, lead_id: str, until: str,
                 stage: int, campaign_id: str) -> None:
    """Push the pending follow-up out to the return date."""
    con.execute("UPDATE leads SET ooo_until=?, updated_at=datetime('now') "
                " WHERE id=?", (until, lead_id))
    con.execute(
        "UPDATE followups SET scheduled_for=?, status='scheduled',"
        "       cancel_reason=NULL, cancelled_at=NULL "
        " WHERE lead_id=? AND stage=?", (until, lead_id, stage))
    con.execute(
        "INSERT INTO events (lead_id, campaign_id, agent, event_type, detail) "
        "VALUES (?,?,?,?,?)",
        (lead_id, campaign_id, "echo", "followup.rescheduled",
         "out of office until %s" % until))
