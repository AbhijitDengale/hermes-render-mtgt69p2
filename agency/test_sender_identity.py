#!/usr/bin/env python3
"""Sender identity on the agency side.

The prospect must see the professional Send-As identity, never the Gmail
mailbox. MailHub enforces that at dispatch; this suite pins what the agency
does around it: the freelancing line is part of the text SENTINEL approves,
the orchestrator records the identity MailHub confirmed on the message row,
and ORBIT reports who each email was sent as.

Runs against a temporary database only. No network.
"""
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-64s %s" % ("PASS" if ok else "FAIL", name, detail))


_SEQ = [0]


def fresh_db(tmp) -> str:
    _SEQ[0] += 1
    path = os.path.join(tmp, "si%d.db" % _SEQ[0])
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


TO_READY = ["RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE", "COPY_PENDING",
            "COPY_READY", "QA_PENDING", "READY_TO_SEND"]

LISA = {"from_email": "demon@socialnexa.cv", "from_name": "Lisa Chen"}


def tenant_env():
    for k in list(os.environ):
        if k.startswith("MAILHUB_TENANT_") or k == "MAILHUB_API_TOKEN":
            del os.environ[k]
    os.environ["MAILHUB_TENANT_1_NAME"] = "t1"
    os.environ["MAILHUB_TENANT_1_USER_ID"] = "2"
    os.environ["MAILHUB_TENANT_1_QUEUE_TOKEN"] = "q1"
    os.environ["MAILHUB_TENANT_1_APPROVE_TOKEN"] = "a1"
    os.environ["MAILHUB_TENANT_1_LEO_TOKEN"] = "l1"


def new_lead(con, li, P, n, upto):
    con.execute("INSERT OR IGNORE INTO campaigns (id, name, status, followup_schedule)"
                " VALUES ('C-1', 'C-1', 'active', '[\"2m\"]')")
    r = li.ingest_one(con, {"email": "prospect%d@example.com" % n,
                            "business_name": "Prospect %d" % n,
                            "niche": "real estate", "country": "AE"},
                      default_campaign="C-1")
    lead = r["lead_id"]
    prev = "NEW"
    for nxt in upto:
        with P.writing(con):
            P.transition(con, lead, nxt, "seed", "fixture", expect=prev)
        prev = nxt
    return lead


def main() -> int:
    tmp = tempfile.mkdtemp()
    tenant_env()
    db = fresh_db(tmp)
    os.environ["AGENCY_DB"] = db
    import importlib
    import lead_ingest as li     # noqa: E402
    import pipeline as P         # noqa: E402
    import tenants               # noqa: E402
    import orchestrator as O     # noqa: E402
    import orbit                 # noqa: E402
    import agency_mcp as M       # noqa: E402
    for mod in (tenants, O, orbit, M):
        importlib.reload(mod)

    print("\n--- 1. The freelancing line is part of the text SENTINEL approves ---")
    out = M.ensure_freelance_line("Hi there,\n\nYour site is down.")
    check("a body that does not say it gets the line appended",
          out.endswith(M.FREELANCE_LINE) and "freelanc" in out.lower())
    check("appending twice changes nothing (idempotent)",
          M.ensure_freelance_line(out) == out)
    own = "Hi,\n\nWe're a two-person freelance studio. Interested?"
    check("ARIA's own wording is kept, nothing appended",
          M.ensure_freelance_line(own) == own)
    check("trailing whitespace is trimmed before the line is added",
          M.ensure_freelance_line("Hello   \n\n") == "Hello" + "\n\n" + M.FREELANCE_LINE)

    with P.connect(db) as con:
        lead = new_lead(con, li, P, 1, TO_READY[:4])       # COPY_PENDING: ARIA's turn
        res = M.t_save_draft({"lead_id": lead, "subject": "Quick question",
                              "body": "Hi,\n\nNoticed your site is offline."})
        draft = P.load_draft(con, lead, 0)
        check("save_draft stores the body WITH the line",
              bool(draft) and draft["body"].endswith(M.FREELANCE_LINE),
              str(res)[:100])
        check("and the stored content hash covers that final text",
              bool(draft) and draft["content_hash"]
              == P.content_hash("Quick question", draft["body"]))

    print("\n--- 2. The orchestrator records the identity MailHub confirmed ---")
    calls = []

    def fake_mailhub(method, path, body=None, token=None, _resp={}):
        calls.append((method, path, token))
        return dict(_resp)

    with P.connect(db) as con:
        con.execute("INSERT INTO tenant_health (tenant_name, user_id, queue_ok,"
                    " approve_ok, leo_ok, mailbox_ok, daily_limit, sent_today,"
                    " health, mailbox_email) VALUES ('t1', 2, 1, 1, 1, 1, 70, 0,"
                    " 'healthy', 'minhulisa@gmail.com')")
        con.commit()

        def ready_lead(n):
            lead = new_lead(con, li, P, n, TO_READY)
            with P.writing(con):
                P.save_draft(con, lead, "C-1", 0, "S%d" % n, "B%d" % n)
                P.record_qa(con, lead, 0, "approved", approval_id="AP-%d" % n)
                con.execute("UPDATE messages SET tenant_user_id=2, mailhub_queue_id=?,"
                            " status='queued' WHERE id=?",
                            (str(70 + n), P.message_id(lead, 0)))
            return dict(con.execute("SELECT * FROM leads WHERE id=?", (lead,)).fetchone())

        real_mailhub = O.mailhub
        try:
            lead_a = ready_lead(2)
            O.mailhub = lambda m, p, body=None, token=None: (calls.append((m, p, token)) or {
                "status": "sent", "provider_message_id": "PM-72",
                "provider_thread_id": "T-72", "account_id": "acct_x",
                "sent_at": "2026-09-02T10:00:00Z", **LISA})
            out = O.queue_and_send(con, lead_a)
            row = con.execute("SELECT status, from_email, provider_message_id FROM messages"
                              " WHERE id=?", (P.message_id(lead_a["id"], 0),)).fetchone()
            state = con.execute("SELECT state FROM leads WHERE id=?",
                                (lead_a["id"],)).fetchone()["state"]
            check("status was asked through the tenant's own queue token",
                  calls and calls[-1] == ("GET", "/api/v1/messages/72", "q1"), str(calls[-1:]))
            check("the lead is SENT on the provider's confirmation", state == "SENT", state)
            check("the message row records the professional sender",
                  row["from_email"] == "Lisa Chen <demon@socialnexa.cv>", str(dict(row)))
            check("  and the provider id", row["provider_message_id"] == "PM-72")
            check("the run summary names the sender, not the mailbox",
                  "as Lisa Chen <demon@socialnexa.cv>" in (out or "") and "gmail" not in (out or ""),
                  out)

            lead_b = ready_lead(3)
            O.mailhub = lambda m, p, body=None, token=None: {
                "status": "sent", "provider_message_id": "PM-73",
                "provider_thread_id": "T-73", "account_id": "acct_x",
                "sent_at": "2026-09-02T10:01:00Z"}          # an older MailHub: no identity
            out = O.queue_and_send(con, lead_b)
            row = con.execute("SELECT status, from_email FROM messages WHERE id=?",
                              (P.message_id(lead_b["id"], 0),)).fetchone()
            check("without an identity from MailHub the row stays honest (NULL)",
                  row["status"] == "sent" and row["from_email"] is None, str(dict(row)))
            check("  and the summary says the sender was not recorded",
                  "sender not recorded" in (out or ""), out)
        finally:
            O.mailhub = real_mailhub

    print("\n--- 3. ORBIT reports who each email was sent as ---")
    m = orbit.collect(db)
    txt = orbit.report(m)
    check("the OUTREACH section breaks sends down by sender",
          "Sent as Lisa Chen <demon@socialnexa.cv>: 1" in txt, txt[txt.find("Sent as"):][:80])
    check("  and does not hide sends with no recorded sender",
          "Sent as (sender not recorded): 1" in txt)
    m["senders"] = [
        {"email": "minhulisa@gmail.com", "enabled": True, "health": "warming",
         "sent_today": 0, "effective_daily_limit": 70, "identity_status": "verified",
         "from_email": "demon@socialnexa.cv", "from_name": "Lisa Chen"},
        {"email": "abhiden98@gmail.com", "enabled": False, "health": "warming",
         "identity_status": "missing"},
    ]
    m["senders_error"] = None
    txt = orbit.report(m)
    check("each active mailbox line names the identity it sends as",
          "minhulisa@gmail.com" in txt and "as Lisa Chen <demon@socialnexa.cv>" in txt)
    check("a mailbox with no verified identity is flagged as held",
          "NO VERIFIED SENDER IDENTITY (missing) - sends held" in txt)

    print("\n" + "=" * 70)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
