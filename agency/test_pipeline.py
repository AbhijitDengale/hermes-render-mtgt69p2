#!/usr/bin/env python3
"""Tests for the lead state machine — legality, atomicity, concurrency.

Runs against a THROWAWAY SQLite file built from schema.sql plus the Phase C
migration. Never /opt/data/agency.db.

    python3 test_pipeline.py
"""

import os
import pathlib
import sqlite3
import sys
import tempfile
import threading

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

import lead_ingest as li          # noqa: E402
import pipeline as P              # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh_db(tmp) -> str:
    path = os.path.join(tmp, "pipe.db")
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    # The transition table is seeded separately from the DDL. Without it every
    # transition is "illegal", which is the state machine failing closed and
    # therefore exactly right — but it makes for a confusing test run.
    con.executescript(
        (HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    con.executescript(
        (HERE / "migrations" / "001_phase_c_pipeline.sql").read_text(encoding="utf-8"))
    con.close()
    return path


def make_lead(db, email="lead@example.com", campaign="C-T") -> str:
    con = li.connect(db)
    try:
        with con:
            r = li.ingest_one(con, {"email": email, "business_name": "Test Co",
                                    "website": "example.com"},
                              default_campaign=campaign)
    finally:
        con.close()
    return r["lead_id"]


def main() -> int:
    tmp = tempfile.mkdtemp()
    db = fresh_db(tmp)
    lead = make_lead(db)

    print("\n--- 1. Only legal transitions are allowed ---")
    with P.connect(db) as con:
        with P.writing(con):
            r = P.transition(con, lead, "RESEARCH_PENDING", "maya", "queued")
        check("NEW -> RESEARCH_PENDING accepted", r["changed"], str(r))

        for bad in ("SENT", "READY_TO_SEND", "COPY_READY"):
            try:
                with P.writing(con):
                    P.transition(con, lead, bad, "maya", "skip ahead")
                check("skipping to %s is refused" % bad, False, "IT WAS ALLOWED")
            except P.TransitionError as exc:
                check("skipping to %s is refused" % bad, "illegal" in str(exc))

        try:
            with P.writing(con):
                P.transition(con, lead, "RESEARCHING", "maya", "x",
                             expect="NEW")
            check("a wrong `expect` is refused", False, "IT WAS ALLOWED")
        except P.TransitionError as exc:
            check("a wrong `expect` is refused", "expected NEW" in str(exc))

        try:
            with P.writing(con):
                P.transition(con, "nope", "RESEARCHING", "maya", "x")
            check("unknown lead is refused", False, "IT WAS ALLOWED")
        except P.TransitionError as exc:
            check("unknown lead is refused", "no such lead" in str(exc))

        with P.writing(con):
            r = P.transition(con, lead, "RESEARCH_PENDING", "maya", "again")
        check("re-entering the same state is a no-op, not an error",
              r["changed"] is False)

        st = con.execute("SELECT state FROM leads WHERE id=?", (lead,)).fetchone()
        check("the lead never left RESEARCH_PENDING",
              st["state"] == "RESEARCH_PENDING", st["state"])

    print("\n--- 2. Every transition is audited ---")
    with P.connect(db) as con:
        tl = P.timeline(con, lead)
        moves = [e for e in tl if e["event_type"] == "state.changed"]
        check("one event per accepted change", len(moves) == 1, str(len(moves)))
        check("the event records from, to and agent",
              moves[0]["from_state"] == "NEW"
              and moves[0]["to_state"] == "RESEARCH_PENDING"
              and moves[0]["agent"] == "maya")
        check("refused transitions left NO event", len(moves) == 1)

    print("\n--- 3. Compare-and-swap blocks double advancement ---")
    lead2 = make_lead(db, "cas@example.com")
    winners, lock = [], threading.Lock()

    def advance():
        try:
            with P.connect(db) as con:
                with P.writing(con):
                    P.transition(con, lead2, "RESEARCH_PENDING", "w", "race",
                                 expect="NEW")
            with lock:
                winners.append(1)
        except Exception:
            pass

    ts = [threading.Thread(target=advance) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("12 concurrent advances -> exactly ONE wins",
          len(winners) == 1, "winners=%d" % len(winners))
    with P.connect(db) as con:
        n = con.execute("SELECT COUNT(*) c FROM events WHERE lead_id=? "
                        "AND event_type='state.changed'", (lead2,)).fetchone()["c"]
        check("and exactly ONE event was written", n == 1, "events=%d" % n)

    print("\n--- 4. Leases ---")
    lead3 = make_lead(db, "lease@example.com")
    with P.connect(db) as con:
        with P.writing(con):
            first = P.claim(con, lead3, "worker-a")
        with P.writing(con):
            second = P.claim(con, lead3, "worker-b")
        check("first worker takes the lease", first)
        check("second worker is refused while it is held", not second)
        with P.writing(con):
            P.release(con, lead3, "worker-b")
        with P.writing(con):
            third = P.claim(con, lead3, "worker-c")
        check("a worker cannot release someone else's lease", not third)
        with P.writing(con):
            P.release(con, lead3, "worker-a")
        with P.writing(con):
            fourth = P.claim(con, lead3, "worker-c")
        check("the holder can release, and the lease is then free", fourth)

        con.execute("UPDATE leads SET locked_until=datetime('now','-1 minute') "
                    "WHERE id=?", (lead3,))
        with P.writing(con):
            fifth = P.claim(con, lead3, "worker-d")
        check("an EXPIRED lease is reclaimable (a crash cannot strand a lead)",
              fifth)

    grabbed = []

    def grab():
        try:
            with P.connect(db) as con:
                with P.writing(con):
                    if P.claim(con, lead3, "t%d" % threading.get_ident()):
                        with lock:
                            grabbed.append(1)
        except Exception:
            pass

    with P.connect(db) as con:
        con.execute("UPDATE leads SET locked_by=NULL, locked_until=NULL "
                    "WHERE id=?", (lead3,))
    ts = [threading.Thread(target=grab) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("12 concurrent claims -> exactly ONE lease", len(grabbed) == 1,
          "grabbed=%d" % len(grabbed))

    print("\n--- 5. Drafts and QA verdicts ---")
    lead4 = make_lead(db, "draft@example.com")
    with P.connect(db) as con:
        with P.writing(con):
            mid = P.save_draft(con, lead4, "C-T", 0, "Subject A", "Body A",
                               claims_used=[{"claim": "c", "source_url": "u"}])
        d = P.load_draft(con, lead4, 0)
        check("draft stored", d and d["subject"] == "Subject A", mid)
        check("content hash matches MailHub's formula",
              d["content_hash"] == P.content_hash("Subject A", "Body A"))
        check("claims_used round-trips", d["claims_used"][0]["claim"] == "c")

        with P.writing(con):
            P.record_qa(con, lead4, 0, "approved", approval_id="A-1")
        d = P.load_draft(con, lead4, 0)
        check("QA verdict attaches to the draft",
              d["qa_status"] == "approved" and d["approval_id"] == "A-1")

        with P.writing(con):
            P.save_draft(con, lead4, "C-T", 0, "Subject B", "Body B")
        d = P.load_draft(con, lead4, 0)
        check("rewriting REPLACES the draft, not duplicates it",
              d["subject"] == "Subject B"
              and con.execute("SELECT COUNT(*) c FROM messages WHERE lead_id=?",
                              (lead4,)).fetchone()["c"] == 1)
        check("a rewrite CLEARS the previous approval",
              d["qa_status"] is None and d["approval_id"] is None)
        check("and the hash moved with the text",
              d["content_hash"] == P.content_hash("Subject B", "Body B"))

    print("\n--- 6. Eligibility and counts ---")
    with P.connect(db) as con:
        elig = P.eligible(con, "NEW", 10)
        check("eligible() finds unlocked NEW leads", len(elig) >= 1,
              "%d found" % len(elig))
        with P.writing(con):
            P.claim(con, elig[0]["id"], "busy")
        after = [r["id"] for r in P.eligible(con, "NEW", 10)]
        check("a locked lead is NOT eligible", elig[0]["id"] not in after)
        c = P.counts(con)
        check("counts() reports by state", c.get("NEW", 0) >= 1, str(c))

    print("\n--- 7. The full legal path exists end to end ---")
    lead5 = make_lead(db, "path@example.com")
    path = ["RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE",
            "COPY_PENDING", "COPY_READY", "QA_PENDING", "READY_TO_SEND", "SENT"]
    ok = True
    with P.connect(db) as con:
        prev = "NEW"
        for nxt in path:
            try:
                with P.writing(con):
                    P.transition(con, lead5, nxt, "test", "walk", expect=prev)
                prev = nxt
            except P.TransitionError as exc:
                check("NEW -> ... -> SENT walk (%s -> %s)" % (prev, nxt), False,
                      str(exc))
                ok = False
                break
    check("the whole NEW -> SENT path is legal", ok)
    with P.connect(db) as con:
        st = con.execute("SELECT state FROM leads WHERE id=?", (lead5,)).fetchone()
        check("lead finished in SENT", st["state"] == "SENT", st["state"])
        moves = [e for e in P.timeline(con, lead5)
                 if e["event_type"] == "state.changed"]
        check("all 8 hops are in the audit trail", len(moves) == 8,
              "%d events" % len(moves))
        check("the QA reject loop is legal too",
              P.is_legal(con, "QA_PENDING", "QA_REJECTED")
              and P.is_legal(con, "QA_REJECTED", "COPY_PENDING"))
        check("failure routes exist from every working state",
              all(P.is_legal(con, s, "HUMAN_REVIEW")
                  for s in ("RESEARCHING", "QA_PENDING", "READY_TO_SEND")))

    print("\n" + "=" * 72)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
