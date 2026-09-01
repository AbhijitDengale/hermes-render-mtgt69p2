#!/usr/bin/env python3
"""Regression tests for three bugs found by the live Phase D/E proofs.

Each of these shipped, reached production, and was caught only by running the
real pipeline. They stay pinned here so they cannot come back.

    A. save_draft is stage-aware
    B. a provider-confirmed message is immutable
    C. a lead resting after a send can always reach a human

Throwaway SQLite. Never /opt/data.

    python3 test_regressions.py
"""

import os
import pathlib
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

import followups as F     # noqa: E402
import lead_ingest as li  # noqa: E402
import pipeline as P      # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-60s %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh_db(tmp) -> str:
    path = os.path.join(tmp, "reg.db")
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


def main() -> int:
    tmp = tempfile.mkdtemp()
    db = fresh_db(tmp)
    os.environ["AGENCY_DB"] = db

    con0 = li.connect(db)
    with con0:
        con0.execute("INSERT OR REPLACE INTO campaigns (id,name,status,"
                     "followup_schedule) VALUES ('C-R','r','active','[\"2m\"]')")
        lead = li.ingest_one(con0, {"email": "reg@example.com",
                                    "business_name": "Reg Ltd"},
                             default_campaign="C-R")["lead_id"]
    con0.close()

    print("\n--- A. save_draft is stage-aware ---")
    with P.connect(db) as con:
        with P.writing(con):
            m0 = P.save_draft(con, lead, "C-R", 0, "Original subject",
                              "Original body.")
            m1 = P.save_draft(con, lead, "C-R", 1, "Follow-up subject",
                              "Follow-up body.")
        check("stage 0 and stage 1 are DIFFERENT rows", m0 != m1,
              "%s vs %s" % (m0, m1))
        d0, d1 = P.load_draft(con, lead, 0), P.load_draft(con, lead, 1)
        check("stage 0 keeps its own subject", d0["subject"] == "Original subject")
        check("stage 1 has its own subject", d1["subject"] == "Follow-up subject")
        check("their content hashes differ",
              d0["content_hash"] != d1["content_hash"])
        n = con.execute("SELECT COUNT(*) c FROM messages WHERE lead_id=?",
                        (lead,)).fetchone()["c"]
        check("two message rows exist, not one overwritten", n == 2, "%d" % n)

    print("\n--- B. a provider-confirmed message is immutable ---")
    with P.connect(db) as con:
        # Make stage 0 look like real sent mail.
        with P.writing(con):
            con.execute(
                "UPDATE messages SET status='sent', qa_status='approved',"
                "       approval_id='A-1', provider_message_id='PROV-123',"
                "       provider_thread_id='THREAD-123', mailhub_queue_id='42' "
                " WHERE id=?", (m0,))
        before = P.load_draft(con, lead, 0)

        for label, subj, body in (
                ("same text", "Original subject", "Original body."),
                ("changed subject", "HIJACKED subject", "Original body."),
                ("changed body", "Original subject", "HIJACKED body.")):
            try:
                with P.writing(con):
                    P.save_draft(con, lead, "C-R", 0, subj, body)
                check("overwriting a SENT message (%s) is refused" % label,
                      False, "IT WAS ALLOWED")
            except P.TransitionError as exc:
                check("overwriting a SENT message (%s) is refused" % label,
                      "already sent" in str(exc) or "cannot be rewritten" in str(exc))

        after = P.load_draft(con, lead, 0)
        for field in ("subject", "body", "qa_status", "approval_id",
                      "provider_message_id", "provider_thread_id",
                      "content_hash", "status"):
            check("sent message %-20s is unchanged" % field,
                  before[field] == after[field],
                  "%r -> %r" % (before[field], after[field]))

        # A queued-but-not-yet-sent message is equally off limits: it is
        # already in MailHub's hands.
        with P.writing(con):
            con.execute("UPDATE messages SET status='queued',"
                        " provider_message_id=NULL WHERE id=?", (m1,))
        try:
            with P.writing(con):
                P.save_draft(con, lead, "C-R", 1, "x", "y")
            check("overwriting a QUEUED message is refused", False, "ALLOWED")
        except P.TransitionError:
            check("overwriting a QUEUED message is refused", True)

        # But a brand-new stage must still work.
        with P.writing(con):
            m2 = P.save_draft(con, lead, "C-R", 2, "Stage two", "Body two.")
        check("creating a NEW stage still works", P.load_draft(con, lead, 2)
              is not None, m2)

    print("\n--- C. a lead resting after a send can reach a human ---")
    with P.connect(db) as con:
        for src in ("SENT", "FOLLOWUP_WAITING", "FOLLOWUP_PENDING"):
            check("%-18s -> HUMAN_REVIEW is legal" % src,
                  P.is_legal(con, src, "HUMAN_REVIEW"))
        check("HUMAN_REVIEW -> FOLLOWUP_WAITING (a human can resume it)",
              P.is_legal(con, "HUMAN_REVIEW", "FOLLOWUP_WAITING"))

    print("\n--- OOO blocking, both cases ---")
    # Create the lead on its own connection and close it: leaving a second
    # handle open across a BEGIN IMMEDIATE deadlocks SQLite.
    con1 = li.connect(db)
    try:
        with con1:
            lead2 = li.ingest_one(con1, {"email": "ooo@example.com",
                                         "business_name": "OOO Ltd"},
                                  default_campaign="C-R")["lead_id"]
    finally:
        con1.close()

    with P.connect(db) as con:
        prev = "NEW"
        for nxt in ("RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE",
                    "COPY_PENDING", "COPY_READY", "QA_PENDING",
                    "READY_TO_SEND", "SENT", "FOLLOWUP_WAITING"):
            with P.writing(con):
                P.transition(con, lead2, nxt, "test", "walk", expect=prev)
            prev = nxt

        # Vague OOO: escalate, and stay blocked.
        with P.writing(con):
            P.transition(con, lead2, "HUMAN_REVIEW", "leo",
                         "out of office, no parseable return date")
        row = {"lead_state": P.get_lead(con, lead2)["state"], "ooo_until": None}
        check("vague OOO reaches HUMAN_REVIEW", row["lead_state"] == "HUMAN_REVIEW")
        check("and blocked_reason is non-null",
              F.blocked_reason(row) is not None, F.blocked_reason(row) or "")

        # Dated OOO: held, blocked before the date, eligible after.
        soon = (datetime.now(timezone.utc) + timedelta(days=4)
                ).strftime("%Y-%m-%d %H:%M:%S")
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)
                ).strftime("%Y-%m-%d %H:%M:%S")
        check("blocked while ooo_until is in the FUTURE",
              F.blocked_reason({"lead_state": "FOLLOWUP_WAITING",
                                "ooo_until": soon}) is not None)
        check("eligible once ooo_until has PASSED",
              F.blocked_reason({"lead_state": "FOLLOWUP_WAITING",
                                "ooo_until": past}) is None)

        with P.writing(con):
            F.schedule(con, lead2, "C-R", 1, past)
            F.hold_for_ooo(con, lead2, soon, 1, "C-R")
        r = con.execute("SELECT scheduled_for, status FROM followups "
                        " WHERE lead_id=? AND stage=1", (lead2,)).fetchone()
        check("hold_for_ooo pushes the follow-up to the return date",
              r["scheduled_for"] == soon and r["status"] == "scheduled",
              "%s / %s" % (r["scheduled_for"], r["status"]))
        check("ooo_until is stored on the lead",
              P.get_lead(con, lead2)["ooo_until"] == soon)
        due_now = [d["lead_id"] for d in F.due(con)]
        check("ECHO sees NO due follow-up while the person is away",
              lead2 not in due_now, str(due_now))

    print("\n" + "=" * 76)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
