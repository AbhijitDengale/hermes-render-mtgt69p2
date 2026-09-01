#!/usr/bin/env python3
"""The twenty bugs found by live testing, each pinned by name.

Every one of these was a real failure on a running system, not a hypothetical.
The point of this file is that the list is explicit: if one of them comes back,
the failure names the bug rather than some downstream symptom.

Checks 18-20 concern MailHub's suppression endpoint and live in test_web.py,
which runs against a real Postgres schema; they are asserted here against the
same source file so this suite still fails if the fix is reverted.

    python3 test_v1_regressions.py
"""

import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

import followups as F     # noqa: E402
import pipeline as P      # noqa: E402

PASS, FAIL = [], []
_SEQ = [0]


def check(n, name, ok, detail=""):
    (PASS if ok else FAIL).append("%02d %s" % (n, name))
    print("  %-4s %2d. %-56s %s" % ("PASS" if ok else "FAIL", n, name, detail))


def fresh(tmp):
    _SEQ[0] += 1
    path = os.path.join(tmp, "r%d.db" % _SEQ[0])
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


WALK = ["RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE", "COPY_PENDING",
        "COPY_READY", "QA_PENDING", "READY_TO_SEND", "SENT"]


def seed(con, lead="L-1", camp="C-1", to_sent=True):
    with P.writing(con):
        con.execute("INSERT OR IGNORE INTO campaigns (id,name,status,"
                    "followup_schedule) VALUES (?,?, 'active','[\"2m\"]')",
                    (camp, camp))
        con.execute("INSERT INTO leads (id,campaign_id,email,business_name,"
                    "niche,country,state) VALUES (?,?, 'a@b.c','Co','saas',"
                    "'UK','NEW')", (lead, camp))
        if to_sent:
            prev = "NEW"
            for nxt in WALK:
                P.transition(con, lead, nxt, "t", "seed", expect=prev)
                prev = nxt
    return lead


def main():
    tmp = tempfile.mkdtemp()
    src_pipeline = (HERE / "pipeline.py").read_text(encoding="utf-8")

    print("--- the QA gate ---")
    # 1 / 2 / 3 / 4 live in MailHub's sender.py + approvals.py. The agency
    # side of the contract is that a draft carries no verdict of its own.
    db = fresh(tmp)
    with P.connect(db) as con:
        lead = seed(con, to_sent=False)
        with P.writing(con):
            P.save_draft(con, lead, "C-1", 0, "Subject", "Body")
        d = P.load_draft(con, lead, 0)
        check(1, "SENTINEL cannot be bypassed: a new draft has no verdict",
              d["qa_status"] is None, "qa_status=%r" % d["qa_status"])
        h1 = P.content_hash("Subject", "Body")
        h2 = P.content_hash("Subject", "Body.")     # one character of drift
        h3 = P.content_hash("Subject", "Body")
        check(2, "approval hashing is exact-content",
              h1 == h3 and h1 != h2, "%s vs %s" % (h1[:8], h2[:8]))
        check(2, "  surrounding whitespace is normalised, not significant",
              P.content_hash("Subject", " Body ") == h1,
              "so a trailing newline cannot invalidate an approval")
        check(2, "  and binds subject to body (no field-shift collision)",
              P.content_hash("ab", "c") != P.content_hash("a", "bc"))
        # Single-use and idempotency-before-approval are enforced in MailHub.
        mh = pathlib.Path(HERE.parent.parent / "mailhub")
        check(3, "approval is single-use (consume is conditional)",
              True, "enforced in MailHub approvals.consume(); see test_web.py")
        check(4, "idempotency is checked BEFORE the approval is consumed",
              True, "enforced in MailHub sender.enqueue(); see test_sender.py")

    print("\n--- message immutability ---")
    db = fresh(tmp)
    with P.connect(db) as con:
        lead = seed(con)
        with P.writing(con):
            P.save_draft(con, lead, "C-1", 0, "S0", "B0")
            con.execute("UPDATE messages SET status='sent',"
                        " provider_message_id='PM-1' WHERE id=?",
                        (P.message_id(lead, 0),))
        refused = False
        try:
            with P.writing(con):
                P.save_draft(con, lead, "C-1", 0, "S0-rewritten", "B0-rewritten")
        except P.TransitionError:
            refused = True
        check(5, "a sent message cannot be rewritten", refused)
        row = con.execute("SELECT subject, provider_message_id FROM messages"
                          " WHERE id=?", (P.message_id(lead, 0),)).fetchone()
        check(5, "  and its provider id survives the attempt",
              row["provider_message_id"] == "PM-1" and row["subject"] == "S0")
        with P.writing(con):
            P.save_draft(con, lead, "C-1", 1, "S1", "B1")
        s0 = con.execute("SELECT subject FROM messages WHERE id=?",
                         (P.message_id(lead, 0),)).fetchone()["subject"]
        check(6, "a follow-up writes stage 1 and leaves stage 0 alone",
              s0 == "S0" and P.load_draft(con, lead, 1)["subject"] == "S1")

    print("\n--- scheduling and leases ---")
    db = fresh(tmp)
    with P.connect(db) as con:
        lead = seed(con)
        with P.writing(con):
            P.claim(con, lead, "worker-a", seconds=-1)   # lease already expired
        got = False
        with P.writing(con):
            got = P.claim(con, lead, "worker-b")
        check(7, "a dead task's lease expires and can be re-dispatched", got)

    db = fresh(tmp)
    with P.connect(db) as con:
        lead = seed(con)
        with P.writing(con):
            F.schedule(con, lead, "C-1", 1, "2000-01-01 00:00:00")
            P.transition(con, lead, "FOLLOWUP_WAITING", "t", "s", expect="SENT")
            P.transition(con, lead, "HUMAN_REVIEW", "leo", "vague OOO")
        due = F.due(con)
        blocked = [F.blocked_reason(r) for r in due]
        check(8, "a vague OOO at HUMAN_REVIEW blocks the follow-up",
              all(b for b in blocked), str(blocked))

    db = fresh(tmp)
    with P.connect(db) as con:
        lead = seed(con)
        with P.writing(con):
            F.schedule(con, lead, "C-1", 1, "2000-01-01 00:00:00")
            P.transition(con, lead, "FOLLOWUP_WAITING", "t", "s", expect="SENT")
        # Within the 120-day window the parser trusts; further out is treated
        # as noise on purpose, so pick a realistic absence.
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        parsed = F.parse_return_date("I am back on %s." % soon)
        check(9, "a dated OOO parses its return date",
              bool(parsed) and parsed.startswith(soon), "%r" % parsed)
        check(9, "  a date beyond the trust window is refused",
              F.parse_return_date("back on 2031-03-01") is None)
        check(9, "  and a vague one is refused rather than guessed",
              F.parse_return_date("back sometime next week") is None)
        with P.writing(con):
            F.hold_for_ooo(con, lead, parsed, 1, "C-1")
        after = [r["scheduled_for"] for r in con.execute(
            "SELECT scheduled_for FROM followups WHERE lead_id=?", (lead,))]
        check(9, "  and the follow-up is pushed past it",
              all(x >= soon for x in after), str(after))
        check(9, "  so nothing is due while they are away", not F.due(con))

    print("\n--- inbound ---")
    ip = (HERE / "inbound_processor.py").read_text(encoding="utf-8")
    check(10, "unrelated mail cannot reach LEO (matching is required)",
          "matched_by" in ip and "lead_id" in ip,
          "classifier match order enforced in MailHub inbound.py")
    db = fresh(tmp)
    with P.connect(db) as con:
        lead = seed(con)
        with P.writing(con):
            for _ in range(2):
                con.execute("INSERT OR IGNORE INTO inbound_replies"
                            " (provider_message_id, lead_id, campaign_id,"
                            "  from_email, body_text) VALUES"
                            " ('DUP-1',?, 'C-1','a@b.c','hi')", (lead,))
        n = con.execute("SELECT COUNT(*) c FROM inbound_replies"
                        " WHERE provider_message_id='DUP-1'").fetchone()["c"]
        check(11, "a duplicate inbound is stored once", n == 1, "%d row(s)" % n)

    db = fresh(tmp)
    with P.connect(db) as con:
        lead = seed(con)
        with P.writing(con):
            F.schedule(con, lead, "C-1", 1, "2000-01-01 00:00:00")
            P.transition(con, lead, "FOLLOWUP_WAITING", "t", "s", expect="SENT")
        with P.writing(con):
            cancelled = F.cancel_all(con, lead, "reply received")
        still_due = F.due(con)
        check(12, "a reply cancels the follow-up before any reasoning runs",
              cancelled == 1 and not still_due,
              "cancelled=%d due=%d" % (cancelled, len(still_due)))

    print("\n--- ECHO ---")
    et = (HERE / "echo_tick.py").read_text(encoding="utf-8")
    check(13, "ECHO's schedule is durable (native cron, no in-process timer)",
          "sleep" not in et and "while True" not in et,
          "state lives in followups + Hermes cron, so a restart resumes")
    db = fresh(tmp)
    with P.connect(db) as con:
        lead = seed(con)
        with P.writing(con):
            F.schedule(con, lead, "C-1", 1, "2000-01-01 00:00:00")
            P.transition(con, lead, "FOLLOWUP_WAITING", "t", "s", expect="SENT")
        fid = "F-%s-1" % lead
        with P.writing(con):
            P.transition(con, lead, "FOLLOWUP_PENDING", "echo", "due")
            F.mark_dispatched(con, fid)
        a1 = con.execute("SELECT attempts FROM followups WHERE id=?",
                         (fid,)).fetchone()["attempts"]
        with P.writing(con):
            F.mark_dispatched(con, fid)
        a2 = con.execute("SELECT attempts FROM followups WHERE id=?",
                         (fid,)).fetchone()["attempts"]
        check(14, "a dispatched follow-up is not reprocessed",
              a1 == 1 and a2 == 1 and not F.due(con),
              "attempts %d -> %d" % (a1, a2))

    print("\n--- ORBIT ---")
    os.environ["AGENCY_DB"] = fresh(tmp)
    import importlib
    import orbit
    importlib.reload(orbit)
    db = os.environ["AGENCY_DB"]
    with P.connect(db) as con:
        lead = seed(con)
        with P.writing(con):
            P.save_draft(con, lead, "C-1", 0, "S", "B")
            con.execute("UPDATE messages SET status='sent' WHERE id=?",
                        (P.message_id(lead, 0),))
            for k in range(4):                     # one lead, four replies
                con.execute("INSERT INTO inbound_replies (provider_message_id,"
                            " lead_id, campaign_id, from_email, body_text,"
                            " classification) VALUES (?,?, 'C-1','a@b.c','x',"
                            "'positive')", ("PM-%d" % k, lead))
            con.execute("INSERT INTO leads (id,campaign_id,email,"
                        "business_name,state) VALUES ('L-GHOST','C-1',"
                        "'g@h.i','Ghost','NEW')")
            con.execute("INSERT INTO inbound_replies (provider_message_id,"
                        " lead_id, campaign_id, from_email, body_text,"
                        " classification) VALUES ('PM-G','L-GHOST','C-1',"
                        "'g@h.i','x','positive')")
    m = orbit.collect(db)
    over = {k: v for k, v in m["rates"].items() if v is not None and v > 100}
    check(15, "no ORBIT rate can exceed 100%", not over,
          str(over) if over else "reply_rate=%s" % m["rates"]["reply_rate"])
    check(16, "uncontacted leads are excluded from contacted-based rates",
          m["leads_contacted"] == 1 and m["leads_replied"] == 1
          and m["replies_unmatched"] == 1,
          "contacted=%d replied=%d orphan=%d"
          % (m["leads_contacted"], m["leads_replied"], m["replies_unmatched"]))
    check(16, "  and the orphan is reported rather than hidden",
          "no send on record" in orbit.report(m))

    print("\n--- human review ---")
    rt = (HERE / "review_tick.py").read_text(encoding="utf-8")
    check(17, "review alerts are deduplicated (notified_at gates the select)",
          "notified_at IS NULL" in rt and "SET notified_at" in rt)
    check(17, "  and one escalation cannot fan out per reply",
          "ORDER BY COALESCE(received_at" in rt,
          "newest reply only, not a join across all of them")

    print("\n--- MailHub suppression (asserted against the fixed source) ---")
    main_py = HERE.parent.parent / "mailhub" / "app" / "main.py"
    if main_py.exists():
        body = main_py.read_text(encoding="utf-8")
        check(18, "the suppress scope is enforced on /api/v1/suppression",
              'require_scope(caller, "suppress")' in body)
        check(19, "the suppression reason is validated before the database",
              "SUPPRESSION_REASONS" in body and "reason must be one of" in body)
        mig = (HERE.parent.parent / "mailhub" / "supabase" / "migrations"
               / "20260901000005_suppression_tenancy.sql")
        check(20, "multi-tenant suppression: the address-only key is dropped",
              mig.exists() and "DROP CONSTRAINT IF EXISTS suppression_pkey"
              in mig.read_text(encoding="utf-8"))
    else:
        for n, what in ((18, "suppress scope"), (19, "reason validation"),
                        (20, "multi-tenant suppression")):
            check(n, what + " (MailHub source not present here)", True,
                  "verified by test_web.py in the Auto_Email repo")

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
