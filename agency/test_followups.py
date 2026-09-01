#!/usr/bin/env python3
"""Tests for Phase D/E — reply handling, cancellation order, follow-up gating.

Throwaway SQLite built from schema + both migrations. Never /opt/data.

    python3 test_followups.py
"""

import os
import pathlib
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

import followups as F   # noqa: E402
import lead_ingest as li  # noqa: E402
import pipeline as P    # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh_db(tmp) -> str:
    path = os.path.join(tmp, "de.db")
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    # Every migration, in order. Pinning a subset meant a fixture could drift
    # behind the real schema and a suite would fail on a table the running
    # system has had for weeks.
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


def make_lead(db, email, campaign="C-T", schedule='["2m","4m","6m"]'):
    con = li.connect(db)
    try:
        with con:
            con.execute(
                "INSERT OR REPLACE INTO campaigns (id, name, status, followup_schedule)"
                " VALUES (?,?, 'active', ?)", (campaign, campaign, schedule))
            r = li.ingest_one(con, {"email": email, "business_name": "Co",
                                    "website": "example.com"},
                              default_campaign=campaign)
    finally:
        con.close()
    return r["lead_id"]


def walk_to_sent(db, lead):
    """Drive a lead to SENT through legal transitions only."""
    path = ["RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE",
            "COPY_PENDING", "COPY_READY", "QA_PENDING", "READY_TO_SEND", "SENT"]
    with P.connect(db) as con:
        prev = "NEW"
        for nxt in path:
            with P.writing(con):
                P.transition(con, lead, nxt, "test", "walk", expect=prev)
            prev = nxt


def main() -> int:
    tmp = tempfile.mkdtemp()
    db = fresh_db(tmp)
    os.environ["AGENCY_DB"] = db

    print("\n--- 1. Campaign-configurable schedules (never global) ---")
    with P.connect(db) as con:
        con.execute("INSERT OR REPLACE INTO campaigns (id,name,status,followup_schedule)"
                    " VALUES ('C-DAYS','d','active','[3,7,12]')")
        con.execute("INSERT OR REPLACE INTO campaigns (id,name,status,followup_schedule)"
                    " VALUES ('C-MINS','m','active','[\"2m\",\"4m\",\"6m\"]')")
        con.execute("INSERT OR REPLACE INTO campaigns (id,name,status,followup_schedule)"
                    " VALUES ('C-DEF','x','active','[]')")
        days = F.campaign_schedule(con, "C-DAYS")
        mins = F.campaign_schedule(con, "C-MINS")
        dflt = F.campaign_schedule(con, "C-DEF")
        check("day schedule parses to seconds", days == [3*86400, 7*86400, 12*86400],
              str(days))
        check("minute schedule for test campaigns", mins == [120, 240, 360], str(mins))
        check("empty schedule falls back to the default", dflt == [3*86400, 7*86400, 12*86400])
        check("a stage beyond the schedule has no due date",
              F.next_due(con, "C-MINS", 4) is None)

    print("\n--- 2. Every terminal state blocks a follow-up ---")
    for state in sorted(F.TERMINAL):
        row = {"lead_state": state, "ooo_until": None}
        check("blocked when lead is %-14s" % state,
              F.blocked_reason(row) is not None, F.blocked_reason(row) or "")
    for state in ("SENT", "FOLLOWUP_WAITING", "FOLLOWUP_PENDING"):
        check("allowed when lead is %-14s" % state,
              F.blocked_reason({"lead_state": state, "ooo_until": None}) is None)
    check("mid-pipeline states are ambiguous -> blocked",
          F.blocked_reason({"lead_state": "QA_PENDING", "ooo_until": None}) is not None)
    future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    check("an active out-of-office blocks the follow-up",
          F.blocked_reason({"lead_state": "SENT", "ooo_until": future}) is not None)

    print("\n--- 3. Reply cancels the follow-up (MANDATORY TEST 1) ---")
    lead = make_lead(db, "reply@example.com", "C-MINS")
    walk_to_sent(db, lead)
    with P.connect(db) as con:
        due_at = F.next_due(con, "C-MINS", 1)
        with P.writing(con):
            F.schedule(con, lead, "C-MINS", 1, due_at)
            P.transition(con, lead, "FOLLOWUP_WAITING", "maya", "scheduled",
                         expect="SENT")
        n_before = con.execute("SELECT COUNT(*) c FROM followups WHERE lead_id=?"
                               " AND status='scheduled'", (lead,)).fetchone()["c"]
        check("a follow-up is scheduled", n_before == 1)

        # The reply arrives: cancel FIRST, then move the lead.
        with P.writing(con):
            cancelled = F.cancel_all(con, lead, "reply received", "inbound")
            P.transition(con, lead, "REPLIED", "inbound", "reply")
        check("the reply cancels the scheduled follow-up", cancelled == 1)
        row = con.execute("SELECT status, cancel_reason, cancelled_at FROM followups"
                          " WHERE lead_id=?", (lead,)).fetchone()
        check("the follow-up is marked cancelled with a reason and a time",
              row["status"] == "cancelled" and row["cancel_reason"]
              and row["cancelled_at"])
        check("the lead is REPLIED",
              P.get_lead(con, lead)["state"] == "REPLIED")

        # Now the due-time arrives. Nothing must go out.
        con.execute("UPDATE followups SET scheduled_for=datetime('now','-1 minute')"
                    " WHERE lead_id=?", (lead,))
        check("ZERO follow-ups are due after the reply",
              len([d for d in F.due(con) if d["lead_id"] == lead]) == 0)

    print("\n--- 4. No reply -> the follow-up IS due (MANDATORY TEST 2) ---")
    lead2 = make_lead(db, "noreply@example.com", "C-MINS")
    walk_to_sent(db, lead2)
    with P.connect(db) as con:
        with P.writing(con):
            F.schedule(con, lead2, "C-MINS", 1, F.next_due(con, "C-MINS", 1))
            P.transition(con, lead2, "FOLLOWUP_WAITING", "maya", "scheduled",
                         expect="SENT")
        con.execute("UPDATE followups SET scheduled_for=datetime('now','-1 minute')"
                    " WHERE lead_id=?", (lead2,))
        rows = [d for d in F.due(con) if d["lead_id"] == lead2]
        check("the follow-up becomes due", len(rows) == 1)
        check("and is not blocked", F.blocked_reason(rows[0]) is None,
              F.blocked_reason(rows[0]) or "clear")
        with P.writing(con):
            P.transition(con, lead2, "FOLLOWUP_PENDING", "echo", "due",
                         expect="FOLLOWUP_WAITING")
            F.touch(con, rows[0]["id"])
        check("FOLLOWUP_WAITING -> FOLLOWUP_PENDING is legal",
              P.get_lead(con, lead2)["state"] == "FOLLOWUP_PENDING")
        check("FOLLOWUP_PENDING can go to QA (follow-ups are re-approved)",
              P.is_legal(con, "FOLLOWUP_PENDING", "QA_PENDING"))
        check("attempts recorded exactly once",
              con.execute("SELECT attempts FROM followups WHERE id=?",
                          (rows[0]["id"],)).fetchone()["attempts"] == 1)

    print("\n--- 5. One schedule per (lead, stage) ---")
    with P.connect(db) as con:
        with P.writing(con):
            F.schedule(con, lead2, "C-MINS", 1, F.next_due(con, "C-MINS", 1))
        n = con.execute("SELECT COUNT(*) c FROM followups WHERE lead_id=? AND stage=1",
                        (lead2,)).fetchone()["c"]
        check("scheduling the same stage twice does NOT stack", n == 1, "%d rows" % n)

    print("\n--- 6. Classification routing (TESTS 3-6) ---")
    sys.path.insert(0, str(HERE))
    os.environ["AGENCY_ROLE"] = "leo"
    import importlib
    import agency_mcp
    importlib.reload(agency_mcp)
    m = agency_mcp
    for cls, want in (("unsubscribe", "UNSUBSCRIBED"), ("negative", "NEGATIVE"),
                      ("positive", "POSITIVE"), ("interested", "POSITIVE"),
                      ("meeting_request", "MEETING_STAGE"),
                      ("pricing_question", "HUMAN_REVIEW"),
                      ("proposal_request", "HUMAN_REVIEW"),
                      ("contract_request", "HUMAN_REVIEW")):
        check("%-18s maps to %s" % (cls, want),
              m.CLASS_TO_STATE.get(cls) == want, str(m.CLASS_TO_STATE.get(cls)))
    for cls in ("pricing_question", "proposal_request", "contract_request",
                "objection", "referral", "wrong_person", "unclear"):
        check("%-18s ALWAYS needs a human" % cls, cls in m.ALWAYS_HUMAN)
    check("REPLIED can reach every classification target",
          all(P.is_legal(sqlite3.connect(db), "REPLIED", s)
              for s in ("UNSUBSCRIBED", "NEGATIVE", "POSITIVE",
                        "MEETING_STAGE", "HUMAN_REVIEW")))

    print("\n--- 7. Out-of-office date parsing (MANDATORY TEST 7) ---")
    soon = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
    check("an ISO return date is parsed",
          F.parse_return_date("I am away, back on %s." % soon) is not None)
    check("'until <date>' is parsed",
          F.parse_return_date("OOO until %s" % soon) is not None)
    for vague in ("I'm out of office, back Monday.",
                  "Away until the 3rd.", "On leave, returning shortly.", ""):
        check("vague OOO %-34r is NOT guessed" % vague[:34],
              F.parse_return_date(vague) is None)
    past = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    check("a return date in the past is rejected",
          F.parse_return_date("back on %s" % past) is None)
    far = (datetime.now(timezone.utc) + timedelta(days=400)).strftime("%Y-%m-%d")
    check("an implausibly distant date is rejected",
          F.parse_return_date("back on %s" % far) is None)

    print("\n--- 8. Duplicate inbound (MANDATORY TEST 8) ---")
    with P.connect(db) as con:
        ev = {"provider_message_id": "PMID-1", "lead_id": lead,
              "campaign_id": "C-MINS", "from": "p@example.com",
              "subject": "Re: hi", "body_text": "thanks"}
        import inbound_processor as IP
        with P.writing(con):
            first = IP.record(con, ev)
        with P.writing(con):
            second = IP.record(con, ev)
        check("the first delivery is recorded", first is not None)
        check("a REDELIVERY of the same provider id is ignored", second is None)
        n = con.execute("SELECT COUNT(*) c FROM inbound_replies "
                        " WHERE provider_message_id='PMID-1'").fetchone()["c"]
        check("exactly one row exists", n == 1, "%d" % n)

    print("\n--- 9. Cancellation is idempotent and safe to repeat ---")
    with P.connect(db) as con:
        with P.writing(con):
            again = F.cancel_all(con, lead, "second call")
        check("cancelling an already-cancelled lead affects 0 rows", again == 0)

    print("\n" + "=" * 74)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
