#!/usr/bin/env python3
"""Phase F live tests, run on the box against the real agency.db.

Uses its own campaign so nothing here touches leads from earlier phases. The
escalations it raises go through the same review_tick the cron job runs, so
what is exercised is the deployed path, not a copy of it.
"""
import os
import sys

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

import followups as F     # noqa: E402
import pipeline as P      # noqa: E402
import review             # noqa: E402
import review_tick        # noqa: E402

CAMP = "C-PHASE-F"
SCHEDULE = '["2m"]'
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-60s %s" % ("PASS" if ok else "FAIL", name, detail))


WALK = ["RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE", "COPY_PENDING",
        "COPY_READY", "QA_PENDING", "READY_TO_SEND", "SENT"]


def make_lead(con, key, email, company):
    lead = "L-PF-" + key
    with P.writing(con):
        con.execute("INSERT OR IGNORE INTO campaigns (id, name, status,"
                    " followup_schedule) VALUES (?,?, 'active', ?)",
                    (CAMP, CAMP, SCHEDULE))
        con.execute("INSERT OR REPLACE INTO leads (id, campaign_id, email,"
                    " business_name, niche, country, state) VALUES "
                    "(?,?,?,?, 'saas','UK','NEW')", (lead, CAMP, email, company))
        prev = "NEW"
        for nxt in WALK:
            P.transition(con, lead, nxt, "phasef", "fixture", expect=prev)
            prev = nxt
        P.save_draft(con, lead, CAMP, 0, "Subject " + key, "Body " + key)
        con.execute("UPDATE messages SET status='sent' WHERE id=?",
                    (P.message_id(lead, 0),))
        F.schedule(con, lead, CAMP, 1, "2030-01-01 00:00:00")
        P.transition(con, lead, "FOLLOWUP_WAITING", "phasef", "scheduled")
    return lead


def escalate(con, esc_id, lead, reason, cls, body, conf=0.95):
    with P.writing(con):
        P.transition(con, lead, "HUMAN_REVIEW", "leo", reason)
        con.execute(
            "INSERT INTO inbound_replies (provider_message_id, lead_id,"
            " campaign_id, from_email, subject, body_text, classification,"
            " confidence, requires_human, classified_at) VALUES "
            "(?,?,?,?,?,?,?,?,1,datetime('now'))",
            ("PF-" + esc_id, lead, CAMP,
             "reply-" + esc_id.lower() + "@example.com",
             "Re: Subject", body, cls, conf))
        con.execute(
            "INSERT OR REPLACE INTO human_escalations (id, lead_id, campaign_id,"
            " raised_by, reason, status, reply_summary, recommended_action,"
            " draft_response) VALUES (?,?,?, 'leo', ?, 'open', ?, ?, ?)",
            (esc_id, lead, CAMP, reason, "summary of " + reason,
             "suggested handling", "draft reply for " + esc_id))


CASES = [
    ("PRICE", "f-price@example.com", "Pricing Co", "PF-1", "pricing_question",
     "pricing_question", "What does this cost?"),
    ("CONTR", "f-contract@example.com", "Contract Co", "PF-2",
     "contract_request", "contract_request",
     "Send the contract and we will sign."),
    ("OOO", "f-ooo@example.com", "Away Co", "PF-3", "ambiguous_ooo",
     "out_of_office", "I am away, back sometime later."),
    ("DUP", "f-dup@example.com", "Dup Co", "PF-4", "pricing_question",
     "pricing_question", "Also asking about price."),
    ("APPR", "f-appr@example.com", "Approve Co", "PF-5", "pricing_question",
     "pricing_question", "Interested, what next?"),
    ("REJ", "f-rej@example.com", "Reject Co", "PF-6", "pricing_question",
     "pricing_question", "Tell me more."),
    ("CLOSE", "f-close@example.com", "Close Co", "PF-7", "pricing_question",
     "pricing_question", "Not now."),
    ("DNC", "f-dnc@example.com", "DNC Co", "PF-8", "complaint",
     "unsubscribe", "Remove me and never write again."),
    ("EDIT", "f-edit@example.com", "Edit Co", "PF-9", "pricing_question",
     "pricing_question", "Can you clarify pricing?"),
]


def main():
    with P.connect() as con:
        print("--- setup ---")
        # Sent messages are immutable by design, so a re-run cannot reuse
        # the previous run's rows. Clear this campaign's fixtures instead.
        with P.writing(con):
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM leads WHERE campaign_id=?", (CAMP,))]
            for t in ("messages", "followups", "inbound_replies", "events"):
                con.execute("DELETE FROM %s WHERE campaign_id=?" % t, (CAMP,))
            con.execute("DELETE FROM human_escalations WHERE campaign_id=?",
                        (CAMP,))
            for lid in ids:
                con.execute("DELETE FROM events WHERE lead_id=?", (lid,))
            con.execute("DELETE FROM leads WHERE campaign_id=?", (CAMP,))
            con.execute("DELETE FROM audit_logs WHERE subject_id LIKE 'PF-_'")
        print("  cleared %d lead(s) from a previous run" % len(ids))
        leads = {}
        for key, email, company, esc, reason, cls, body in CASES:
            lead = make_lead(con, key, email, company)
            escalate(con, esc, lead, reason, cls, body)
            leads[key] = (lead, esc)
        print("  %d leads driven to HUMAN_REVIEW with open escalations"
              % len(CASES))

        print("\n--- 1-3. Each escalation reaches the alert ---")
        text = "\n".join(review_tick.alerts())
        check("1. pricing question is alerted",
              "PF-1" in text and "pricing" in text)
        check("2. contract request is alerted",
              "PF-2" in text and "contract" in text)
        check("3. vague out-of-office is alerted", "PF-3" in text)
        check("the alert names the company", "Pricing Co" in text)
        check("the alert quotes the reply", "What does this cost?" in text)
        check("the alert offers the commands", "review approve PF-1" in text)
        check("the alert carries LEO's confidence", "confidence 0.95" in text)

        print()
        print("--- 4. No escalation is ever announced twice ---")
        # Each tick announces at most REVIEW_ALERTS_PER_TICK of them, so
        # the property is not "the next tick is silent" but "no id ever
        # appears twice".
        def announced(blob):
            return {"PF-%d" % i for i in range(1, 10)
                    if ("PF-%d" % i) in blob}

        seen, overlap, ticks = announced(text), set(), 1
        while ticks < 8:
            blob = "".join(review_tick.alerts())
            if not blob.strip():
                break
            ticks += 1
            got = announced(blob)
            overlap |= (got & seen)
            seen |= got
        check("4. no escalation was announced twice", not overlap,
              ("repeated: %s" % sorted(overlap)) if overlap else
              ("%d tick(s), %d announced once each" % (ticks, len(seen))))
        check("   all nine were announced", len(seen) == 9, str(sorted(seen)))
        check("   a further tick is silent",
              not "".join(review_tick.alerts()).strip())
        check("   and every one was marked notified",
              con.execute("SELECT COUNT(*) c FROM human_escalations"
                          " WHERE id LIKE 'PF-_' AND notified_at IS NULL"
                          ).fetchone()["c"] == 0)

        print("\n--- 5-8. Human decisions ---")
        r = review.act("PF-5", "approve", "live-test", note="go ahead")
        check("5. approve is recorded", r.get("action") == "approve", str(r)[:60])
        check("   and it does NOT send by itself",
              "SENTINEL" in (r.get("note") or ""))

        r = review.act("PF-6", "reject", "live-test", note="not a fit")
        check("6. reject is recorded", r.get("action") == "reject")
        check("   and says nothing will be sent",
              "nothing will be sent" in (r.get("note") or ""))

        r = review.act("PF-7", "close", "live-test")
        check("7. close cancels the pending follow-up",
              r.get("followups_cancelled") == 1,
              str(r.get("followups_cancelled")))
        check("   and moves the lead to CLOSED",
              r.get("lead_state") == "CLOSED", str(r.get("lead_state")))

        r = review.act("PF-8", "dnc", "live-test")
        check("8. dnc moves the lead to UNSUBSCRIBED",
              r.get("lead_state") == "UNSUBSCRIBED", str(r.get("lead_state")))
        check("   and cancels the follow-up", r.get("followups_cancelled") == 1)
        sup = r.get("suppression") or {}
        check("   and reaches MailHub's suppression list",
              sup.get("suppressed") == "f-dnc@example.com", str(sup)[:110])

        print("\n--- 9. Edited text needs a fresh SENTINEL approval ---")
        r = review.act("PF-9", "edit", "live-test",
                       text="Here is the pricing you asked for: it starts at X.")
        check("9. the edit is saved as a NEW draft", r.get("stage") == 1,
              str(r)[:70])
        d = P.load_draft(con, leads["EDIT"][0], 1)
        check("   the new draft exists", d is not None)
        check("   it carries NO QA verdict",
              d is not None and d["qa_status"] is None,
              "qa_status=%r" % (d["qa_status"] if d else "?"))
        check("   and it is not marked sent",
              d is not None and d["status"] not in ("sent", "queued",
                                                    "simulated"),
              "status=%r" % (d["status"] if d else "?"))

        print("\n--- 10. Bad input is refused ---")
        check("10. an unknown action is refused",
              "unknown action" in (review.act("PF-1", "banana", "live-test")
                                   .get("error") or ""))
        check("    an unknown escalation id is refused",
              "no such" in (review.act("PF-NOPE", "approve", "live-test")
                            .get("error") or ""))
        check("    edit without text is refused",
              "needs --text" in (review.act("PF-1", "edit", "live-test")
                                 .get("error") or ""))
        check("    acting twice on one escalation is refused",
              review.act("PF-5", "approve", "live-test").get("no_change") is True)
        check("    a CLOSED lead cannot be resumed",
              review.act("PF-7", "resume", "live-test").get("error") is not None)

        print("\n--- audit trail ---")
        n = con.execute("SELECT COUNT(*) c FROM audit_logs"
                        " WHERE subject_id LIKE 'PF-_'").fetchone()["c"]
        check("every decision left an audit row", n >= 5, "%d rows" % n)
        for row in con.execute("SELECT actor, action, subject_id, detail"
                               "  FROM audit_logs WHERE subject_id LIKE 'PF-_'"
                               " ORDER BY id"):
            print("    %-11s %-24s %-6s %s"
                  % (row["actor"], row["action"], row["subject_id"],
                     (row["detail"] or "")[:44]))

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
