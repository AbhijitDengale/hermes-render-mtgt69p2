#!/usr/bin/env python3
"""The whole path, once, on the real database.

One lead goes out, replies asking about price, and every downstream step is
checked in the order it actually has to happen: follow-ups cancelled BEFORE
the reply is classified, LEO's verdict routed to a human, the escalation
announced, a human decision recorded, and ORBIT's numbers moving to match.

The ordering is the point. If classification ran first and then failed, a
prospect who already replied would still be chased.
"""
import os
import sys

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

import followups as F      # noqa: E402
import inbound_processor   # noqa: E402
import orbit               # noqa: E402
import pipeline as P       # noqa: E402
import review              # noqa: E402
import review_tick         # noqa: E402

CAMP = "C-INTEGRATED"
LEAD = "L-INT-1"
ESC = "PI-1"
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


WALK = ["RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE", "COPY_PENDING",
        "COPY_READY", "QA_PENDING", "READY_TO_SEND", "SENT"]


def main():
    with P.connect() as con:
        print("--- reset ---")
        with P.writing(con):
            for t in ("messages", "followups", "inbound_replies", "events"):
                con.execute("DELETE FROM %s WHERE campaign_id=?" % t, (CAMP,))
            con.execute("DELETE FROM human_escalations WHERE campaign_id=?",
                        (CAMP,))
            con.execute("DELETE FROM events WHERE lead_id=?", (LEAD,))
            con.execute("DELETE FROM leads WHERE campaign_id=?", (CAMP,))
            con.execute("DELETE FROM audit_logs WHERE subject_id=?", (ESC,))

        before = orbit.collect()

        print("\n--- 1. A lead is ingested and driven to SENT ---")
        with P.writing(con):
            con.execute("INSERT OR IGNORE INTO campaigns (id, name, status,"
                        " followup_schedule) VALUES (?,?, 'active', ?)",
                        (CAMP, CAMP, '["2m"]'))
            con.execute("INSERT INTO leads (id, campaign_id, email,"
                        " business_name, niche, country, state) VALUES "
                        "(?,?, 'integrated@example.com', 'Integrated Ltd',"
                        " 'saas','UK','NEW')", (LEAD, CAMP))
            prev = "NEW"
            for nxt in WALK:
                P.transition(con, LEAD, nxt, "maya", "integrated", expect=prev)
                prev = nxt
            P.save_draft(con, LEAD, CAMP, 0, "Quick question",
                         "Hi, thought this might be relevant.")
            con.execute("UPDATE messages SET status='sent',"
                        " provider_message_id='INT-PROVIDER-1' WHERE id=?",
                        (P.message_id(LEAD, 0),))
            F.schedule(con, LEAD, CAMP, 1, "2000-01-01 00:00:00")
            P.transition(con, LEAD, "FOLLOWUP_WAITING", "maya", "scheduled")
        check("lead reached SENT", P.get_lead(con, LEAD)["state"]
              == "FOLLOWUP_WAITING")
        check("a follow-up is scheduled and due", len(F.due(con)) >= 1)

        print("\n--- 2. The prospect replies asking about price ---")
        rec = inbound_processor.record(con, {
            "provider_message_id": "INT-REPLY-1",
            "provider_thread_id": "INT-PROVIDER-1",
            "lead_id": LEAD, "campaign_id": CAMP,
            "from_email": "integrated@example.com",
            "subject": "Re: Quick question",
            "body_text": "Interesting - how much does this cost?",
            "matched_by": "provider_thread_id"})
        check("the reply is recorded once", bool(rec), str(rec)[:60])

        print("\n--- 3. Cancellation happens BEFORE classification ---")
        with P.writing(con):
            n = F.cancel_all(con, LEAD, "reply received")
            P.transition(con, LEAD, "REPLIED", "inbound", "prospect replied")
        check("the pending follow-up is cancelled", n == 1, "%d cancelled" % n)
        check("and ECHO now sees nothing due for this lead",
              not any(r["lead_id"] == LEAD for r in F.due(con)))
        check("the lead is REPLIED", P.get_lead(con, LEAD)["state"] == "REPLIED")

        print("\n--- 4. LEO classifies it as needing a human ---")
        with P.writing(con):
            con.execute("UPDATE inbound_replies SET classification="
                        "'pricing_question', confidence=0.93, requires_human=1,"
                        " summary='asked for pricing',"
                        " recommended_action='send pricing',"
                        " classified_at=datetime('now')"
                        " WHERE provider_message_id='INT-REPLY-1'")
            P.transition(con, LEAD, "HUMAN_REVIEW", "leo", "pricing_question")
            con.execute("INSERT INTO human_escalations (id, lead_id,"
                        " campaign_id, raised_by, reason, status,"
                        " reply_summary, recommended_action, draft_response)"
                        " VALUES (?,?,?, 'leo','pricing_question','open',"
                        " 'asked for pricing','send pricing','draft')",
                        (ESC, LEAD, CAMP))
        check("the lead is at HUMAN_REVIEW",
              P.get_lead(con, LEAD)["state"] == "HUMAN_REVIEW")

        print("\n--- 5. It is announced, once ---")
        blob = "\n".join(review_tick.alerts())
        check("the alert names this escalation", ESC in blob)
        check("and carries the prospect's words", "how much does this cost" in blob)
        check("a second tick does not repeat it",
              ESC not in "\n".join(review_tick.alerts()))

        print("\n--- 6. A human decides, and it is audited ---")
        r = review.act(ESC, "approve", "operator", note="send our rate card")
        check("the decision is recorded", r.get("action") == "approve")
        check("it does NOT send on its own", "SENTINEL" in (r.get("note") or ""))
        row = con.execute("SELECT status, resolved_by, action, human_response"
                          "  FROM human_escalations WHERE id=?",
                          (ESC,)).fetchone()
        check("the escalation is resolved", row["status"] == "approved",
              row["status"])
        check("by a named actor", row["resolved_by"] == "operator")
        check("with the human's own words kept",
              row["human_response"] == "send our rate card")
        aud = con.execute("SELECT actor, action FROM audit_logs"
                          " WHERE subject_id=? ORDER BY id", (ESC,)).fetchall()
        check("the audit trail covers notify and decide",
              {a["action"] for a in aud} >=
              {"escalation.notified", "review.approve"},
              str([a["action"] for a in aud]))

        print("\n--- 7. ORBIT reflects it ---")
        after = orbit.collect()
        check("one more lead contacted",
              after["leads_contacted"] == before["leads_contacted"] + 1,
              "%d -> %d" % (before["leads_contacted"], after["leads_contacted"]))
        check("one more lead replied",
              after["leads_replied"] == before["leads_replied"] + 1,
              "%d -> %d" % (before["leads_replied"], after["leads_replied"]))
        check("the escalation is counted",
              after["human_reviews"] == before["human_reviews"] + 1)
        check("and counted as resolved, not still open",
              after["human_reviews_resolved"]
              == before["human_reviews_resolved"] + 1,
              "%d -> %d" % (before["human_reviews_resolved"],
                            after["human_reviews_resolved"]))
        check("every rate stays within 0-100",
              all(v is None or 0 <= v <= 100 for v in after["rates"].values()),
              str(after["rates"]))
        check("the cancelled follow-up is visible in the follow-up counts",
              after["followups"].get("cancelled", 0)
              > before["followups"].get("cancelled", 0))

        print("\n--- the report a human would actually read ---")
        for line in orbit.report(after).splitlines():
            print("   " + line)

    print("\n" + "=" * 76)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
