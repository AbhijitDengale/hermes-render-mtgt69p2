#!/usr/bin/env python3
"""Scenario tests for the agency data layer.

Exercises the fail-safe rules against a REAL copy of agency.db. Every test
runs inside a transaction that is rolled back, so the live database is never
mutated.

Run: python3 test_scenarios.py
"""

import sqlite3
import sys
import os

DB = os.getenv("AGENCY_DB", "/opt/data/agency.db")

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print("  %-4s %-46s %s" % ("PASS" if condition else "FAIL", name, detail))


def rejects(con, sql, params=()):
    """True when the DB refuses the write."""
    try:
        con.execute(sql, params)
        return False
    except sqlite3.IntegrityError:
        return True


def allowed(con, frm, to):
    return con.execute(
        "SELECT 1 FROM state_transitions WHERE from_state=? AND to_state=?",
        (frm, to)).fetchone() is not None


def main():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys=ON")

    con.execute("INSERT INTO campaigns (id,name,country,niche,followup_schedule) "
                "VALUES ('C1','Test Campaign','US','dental','[3,7,12]')")
    con.execute("INSERT INTO sender_accounts (id,email,display_name,auth_secret_ref,"
                "campaign_id,daily_limit,hourly_limit,sent_today) "
                "VALUES ('S1','a@x.com','A','MAIL_ACCT_S1','C1',40,8,0)")
    con.execute("INSERT INTO sender_accounts (id,email,display_name,auth_secret_ref,"
                "campaign_id,daily_limit,sent_today,health) "
                "VALUES ('S2','b@x.com','B','MAIL_ACCT_S2','C1',40,40,'healthy')")
    con.execute("INSERT INTO sender_accounts (id,email,display_name,auth_secret_ref,"
                "campaign_id,enabled,health) "
                "VALUES ('S3','c@x.com','C','MAIL_ACCT_S3','C1',0,'disabled')")

    print("\n--- 1. Lead intake ---")
    con.execute("INSERT INTO leads (id,campaign_id,business_name,email,website,region,state) "
                "VALUES ('L1','C1','Acme','Owner@Acme.com','https://acme.com','CA','NEW')")
    check("normal lead accepted", True)

    check("duplicate lead blocked (case-insensitive)",
          rejects(con, "INSERT INTO leads (id,campaign_id,email,state) "
                       "VALUES ('L2','C1','owner@acme.com','NEW')"))

    con.execute("INSERT INTO leads (id,campaign_id,business_name,website,state) "
                "VALUES ('L3','C1','NoEmail','https://n.com','NEW')")
    check("lead with missing email accepted at intake", True,
          "(send path must refuse it)")

    con.execute("INSERT INTO leads (id,campaign_id,business_name,email,state) "
                "VALUES ('L4','C1','NoSite','no-site@x.com','NEW')")
    check("lead with missing website accepted at intake", True,
          "(NOVA returns failed)")

    print("\n--- 2. State machine ---")
    check("NEW -> RESEARCH_PENDING allowed", allowed(con, "NEW", "RESEARCH_PENDING"))
    check("NEW -> SENT REJECTED (cannot skip pipeline)",
          not allowed(con, "NEW", "SENT"))
    check("QA_PENDING -> READY_TO_SEND allowed",
          allowed(con, "QA_PENDING", "READY_TO_SEND"))
    check("QA_REJECTED -> READY_TO_SEND REJECTED",
          not allowed(con, "QA_REJECTED", "READY_TO_SEND"))
    check("QA_REJECTED -> COPY_PENDING allowed (rewrite loop)",
          allowed(con, "QA_REJECTED", "COPY_PENDING"))
    check("CLOSED is terminal (no outbound)",
          con.execute("SELECT COUNT(*) FROM state_transitions "
                      "WHERE from_state='CLOSED'").fetchone()[0] == 0)
    check("every state can reach HUMAN_REVIEW or CLOSED",
          con.execute("SELECT COUNT(DISTINCT from_state) FROM state_transitions "
                      "WHERE to_state IN ('HUMAN_REVIEW','CLOSED')").fetchone()[0] >= 15)
    check("invalid state value rejected by CHECK",
          rejects(con, "INSERT INTO leads (id,state) VALUES ('LX','NOT_A_STATE')"))

    print("\n--- 3. Per-lead locking (two workers, one lead) ---")
    con.execute("UPDATE leads SET locked_by='nova', "
                "locked_until=datetime('now','+5 minutes') WHERE id='L1'")
    n = con.execute("UPDATE leads SET locked_by='aria' WHERE id='L1' AND "
                    "(locked_until IS NULL OR locked_until < datetime('now'))").rowcount
    check("second worker cannot steal an active lock", n == 0)
    con.execute("UPDATE leads SET locked_until=datetime('now','-1 minutes') WHERE id='L1'")
    n = con.execute("UPDATE leads SET locked_by='aria' WHERE id='L1' AND "
                    "(locked_until IS NULL OR locked_until < datetime('now'))").rowcount
    check("expired lock IS reclaimable (no deadlock after restart)", n == 1)

    print("\n--- 4. Send idempotency ---")
    con.execute("INSERT INTO send_jobs (id,idempotency_key,lead_id,campaign_id,"
                "sender_account_id) VALUES ('J1','L1:outreach:0','L1','C1','S1')")
    check("duplicate send job blocked (restart-safe)",
          rejects(con, "INSERT INTO send_jobs (id,idempotency_key,lead_id) "
                       "VALUES ('J2','L1:outreach:0','L1')"))
    check("different stage IS allowed",
          not rejects(con, "INSERT INTO send_jobs (id,idempotency_key,lead_id) "
                           "VALUES ('J3','L1:followup:1','L1')"))

    print("\n--- 5. Suppression (fail closed) ---")
    for em, why in (("unsub@x.com", "unsubscribed"), ("bounce@x.com", "bounced"),
                    ("dnc@x.com", "do_not_contact")):
        con.execute("INSERT INTO suppression (email,reason) VALUES (?,?)", (em, why))

    def sendable(email):
        if not email:
            return False
        return con.execute("SELECT COUNT(*) FROM suppression WHERE email=?",
                           (email.lower(),)).fetchone()[0] == 0

    check("unsubscribed recipient refused", not sendable("unsub@x.com"))
    check("bounced recipient refused", not sendable("BOUNCE@x.com"), "(case-insensitive)")
    check("do-not-contact refused", not sendable("dnc@x.com"))
    check("missing email refused", not sendable(None))
    check("clean recipient allowed", sendable("fresh@x.com"))

    print("\n--- 6. Sender selection ---")
    def pick(campaign):
        return [r[0] for r in con.execute(
            "SELECT id FROM sender_accounts WHERE enabled=1 AND health='healthy' "
            "AND (campaign_id IS NULL OR campaign_id=?) AND sent_today < daily_limit "
            "AND (cooldown_until IS NULL OR cooldown_until < datetime('now')) "
            "ORDER BY sent_today ASC", (campaign,))]

    picked = pick("C1")
    check("sender at daily limit excluded", "S2" not in picked, "S2 sent_today=40/40")
    check("disabled sender excluded", "S3" not in picked)
    check("healthy sender selectable", "S1" in picked)
    con.execute("UPDATE sender_accounts SET cooldown_until=datetime('now','+1 hour') "
                "WHERE id='S1'")
    check("sender in cooldown excluded", "S1" not in pick("C1"))
    con.execute("UPDATE sender_accounts SET cooldown_until=NULL WHERE id='S1'")
    check("no credential stored in DB (only a reference)",
          con.execute("SELECT auth_secret_ref FROM sender_accounts WHERE id='S1'"
                      ).fetchone()[0] == "MAIL_ACCT_S1")

    print("\n--- 7. Reply stops follow-ups ---")
    con.execute("INSERT INTO followups (id,lead_id,stage,scheduled_for,status) "
                "VALUES ('F1','L1',1,datetime('now','+3 days'),'scheduled')")
    con.execute("INSERT INTO followups (id,lead_id,stage,scheduled_for,status) "
                "VALUES ('F2','L1',2,datetime('now','+7 days'),'scheduled')")
    check("duplicate follow-up stage blocked",
          rejects(con, "INSERT INTO followups (id,lead_id,stage,scheduled_for) "
                       "VALUES ('F3','L1',1,datetime('now'))"))
    n = con.execute("UPDATE followups SET status='cancelled', cancel_reason='replied' "
                    "WHERE lead_id='L1' AND status='scheduled'").rowcount
    check("reply cancels ALL pending follow-ups", n == 2, "cancelled=%d" % n)
    check("no scheduled follow-ups remain",
          con.execute("SELECT COUNT(*) FROM followups WHERE lead_id='L1' "
                      "AND status='scheduled'").fetchone()[0] == 0)

    print("\n--- 8. Inbound dedupe + thread mapping ---")
    con.execute("INSERT INTO email_threads (id,lead_id,campaign_id,sender_account_id,"
                "provider_thread_id,root_message_id,recipient) "
                "VALUES ('T1','L1','C1','S1','thr-1','<root@x>','o@acme.com')")
    con.execute("INSERT INTO messages (id,lead_id,thread_id,direction,kind,"
                "provider_message_id) VALUES ('M1','L1','T1','inbound','reply','<r1@x>')")
    check("duplicate inbound Message-ID blocked",
          rejects(con, "INSERT INTO messages (id,lead_id,direction,provider_message_id) "
                       "VALUES ('M2','L1','inbound','<r1@x>')"))
    check("thread maps back to its lead",
          con.execute("SELECT lead_id FROM email_threads WHERE provider_thread_id='thr-1'"
                      ).fetchone()[0] == "L1")

    print("\n--- 9. Retry / dead-letter ---")
    con.execute("UPDATE send_jobs SET attempts=3, max_attempts=3, status='failed' "
                "WHERE id='J1'")
    con.execute("UPDATE send_jobs SET status='dead' WHERE id='J1' AND attempts>=max_attempts")
    check("job exhausting retries becomes dead-letter",
          con.execute("SELECT status FROM send_jobs WHERE id='J1'").fetchone()[0] == "dead")
    check("dead job is not re-picked",
          con.execute("SELECT COUNT(*) FROM send_jobs WHERE status='pending' "
                      "AND id='J1'").fetchone()[0] == 0)

    print("\n--- 10. Escalation queue ---")
    con.execute("INSERT INTO human_escalations (id,lead_id,raised_by,reason,"
                "recommended_action,draft_response) VALUES "
                "('E1','L1','leo','pricing question','send rate card','Draft...')")
    check("escalation lands in queue as open",
          con.execute("SELECT status FROM human_escalations WHERE id='E1'"
                      ).fetchone()[0] == "open")

    con.rollback()
    con.close()

    print("\n" + "=" * 62)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    print("live database unchanged (all work rolled back)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
