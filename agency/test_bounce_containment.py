#!/usr/bin/env python3
"""A recipient we could not reach is not the same as a recipient who is wrong.

On 2026-09-04 twenty-five delivery reports came back from Google's own daemon
saying it had refused to transmit our message. They were filed as unknown
delivery failures, and twenty-four of the recipients were marked BOUNCED for
it -- twenty-four live mailboxes recorded as dead because our provider was
unhappy with the account writing to them, not because anything was wrong with
the address. Meanwhile the twelve genuine `550 5.4.1 Recipient address
rejected` reports were the actual cause: guessed `info@` addresses released on
domain-level evidence, whose failure rate is what set Google off in the first
place. 291 role addresses produced 24 failures; 93 named addresses produced 0.

So the two must never share a code path. A hard bounce suppresses one exact
address for ever. A sender-side block suppresses nothing, because nothing was
learned about the recipient at all.

No network, no Gmail, no MailHub, no live database.
"""
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_TMP = tempfile.mkdtemp(prefix="contain-")
os.environ["AGENCY_DB"] = os.path.join(_TMP, "contain.db")
os.environ["HERMES_HOME"] = _TMP

import delivery_status as DS      # noqa: E402
import pipeline as P              # noqa: E402
import role_account_policy as RP  # noqa: E402
import tenants                    # noqa: E402

P.DB = os.environ["AGENCY_DB"]

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-66s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-66s %s" % (name, detail))


DAEMON = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
SUBJ = "Delivery Status Notification (Failure)"

# Verbatim from a real report received 2026-09-04.
GOOGLE_BLOCK = """** Message blocked **

Your message to info@example.com has been blocked. See technical details
below for more information.

Learn more here: https://support.google.com/mail/answer/69585

The response was:

Message rejected. For more information, go to
https://support.google.com/mail/answer/69585
"""


def fresh_db():
    if os.path.exists(P.DB):
        os.remove(P.DB)
    con = sqlite3.connect(P.DB)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()


def main() -> int:
    print("=" * 78)
    print("BOUNCE CONTAINMENT: recipient failure vs sender-side block")
    print("=" * 78)

    print("\n--- 1-3. The two failures are told apart ---")
    v = DS.classify(DAEMON, SUBJ,
                    "550 5.1.1 <a@b.com>: Recipient address rejected: "
                    "User unknown in virtual mailbox table", "a@b.com")
    check("1. 550 5.1.1 is a recipient hard bounce",
          v["status"] == DS.HARD_BOUNCE and v["permanent"], v["status"])
    v = DS.classify(DAEMON, SUBJ, "smtp; 550 NoSuchUser", "a@b.com")
    check("2. NoSuchUser is a recipient hard bounce",
          v["status"] == DS.HARD_BOUNCE, v["status"])
    v = DS.classify(DAEMON, SUBJ, GOOGLE_BLOCK, "info@example.com")
    check("3. Google refusing to transmit is a sender policy block",
          v["status"] == DS.SENDER_POLICY_BLOCK, v["status"])
    check("   and it is not permanent, because nothing was learned",
          v["permanent"] is False)
    v2 = DS.classify(DAEMON, SUBJ,
                     "550 5.1.1 The email account that you tried to reach "
                     "does not exist. Message blocked.", "a@b.com")
    check("   a report naming the mailbox invalid still wins over the wording",
          v2["status"] == DS.HARD_BOUNCE,
          "'message blocked' does not override 5.1.1")

    print("\n--- 4-6. What each one is allowed to do ---")
    block = DS.classify(DAEMON, SUBJ, GOOGLE_BLOCK, "info@example.com")
    hard = DS.classify(DAEMON, SUBJ,
                       "550 5.1.1 <a@b.com>: Recipient address rejected",
                       "a@b.com")
    check("4. a sender policy block never suppresses the recipient",
          DS.may_suppress(block) is False)
    check("   and its advice says so in words",
          any("not suppress" in a.lower() for a in DS.recommended_actions(block)),
          DS.recommended_actions(block)[0])
    check("5. a confirmed hard bounce does suppress that exact recipient",
          DS.may_suppress(hard) is True and hard["recipient"] == "a@b.com",
          str(hard["recipient"]))
    check("6. suppression is one address, never the domain",
          "@" in (hard["recipient"] or "")
          and not any("domain" in a.lower() and "suppress" in a.lower()
                      for a in DS.recommended_actions(hard)),
          "no action suppresses a domain")

    print("\n--- 7-8. Follow-ups ---")
    check("7. a hard bounce's first instruction is to cancel follow-ups",
          "cancel" in DS.recommended_actions(hard)[0].lower(),
          DS.recommended_actions(hard)[0])
    check("8. a sender block holds the follow-up rather than cancelling it",
          any("hold" in a.lower() for a in DS.recommended_actions(block)),
          "the person keeps their place in the sequence")

    print("\n--- 9-12. The role-account gate ---")
    def rec(**kw):
        r = {"status": "risky", "decision": "hold", "deliverable": True,
             "score": 55, "reason": "role_account", "flags": ["role_account"],
             "did_you_mean": None, "mx_host": "mx.company.com",
             "verified_email": "info@company.com", "attempts": 1}
        r.update(kw)
        return r

    v = RP.evaluate("info@company.com", rec())
    check("9. a role address with only domain evidence is held",
          v["status"] == "risky" and not v["eligible"]
          and v["reason"] == RP.REASON_NO_MAILBOX_PROOF, v["reason"])
    v = RP.evaluate("info@company.com",
                    rec(verification_level="mailbox", mailbox_status="valid"))
    check("10. a mailbox-confirmed role address may become valid",
          v["status"] == "valid" and v["eligible"], v["status"])
    v = RP.evaluate("info@company.com",
                    rec(verification_level="mailbox", mailbox_status="catch_all"))
    check("11. a catch-all domain stays risky",
          v["status"] == "risky" and v["reason"] == RP.REASON_CATCH_ALL,
          "accepting every local part proves nothing about this one")
    v = RP.evaluate("nadim@company.com", rec(verified_email="nadim@company.com"))
    check("12. a named address is untouched by the whole policy",
          v["status"] is None and v["tier"] == RP.NOT_ROLE,
          "93 named sends bounced 0; they were never the problem")

    print("\n--- 13. A hold does not rewrite history ---")
    fresh_db()
    with P.connect(P.DB) as con:
        with P.writing(con):
            con.execute("INSERT INTO campaigns (id, name, status)"
                        " VALUES ('C1','c','active')")
            for lid, email, state in (
                    ("L-unsent", "info@a.com", "READY_TO_SEND"),
                    ("L-sent", "info@b.com", "FOLLOWUP_WAITING"),
                    ("L-reply", "info@c.com", "POSITIVE")):
                con.execute(
                    "INSERT INTO leads (id, campaign_id, email, business_name,"
                    " state) VALUES (?,?,?,?,?)",
                    (lid, "C1", email, "Co", state))
        with P.writing(con):
            P.hold(con, "L-unsent", "role_account_quality_pause")
        row = con.execute("SELECT state, hold_reason FROM leads WHERE id=?",
                          ("L-unsent",)).fetchone()
        check("13. a held lead keeps the state it was already in",
              row["state"] == "READY_TO_SEND"
              and row["hold_reason"] == "role_account_quality_pause",
              "the hold is a column, not a transition")
        check("   and a held lead is not offered for work",
              not any(r["id"] == "L-unsent"
                      for r in P.eligible(con, "READY_TO_SEND", 10)))
        with P.writing(con):
            P.release_hold(con, "L-unsent", "role_account_quality_pause")
        check("   releasing it puts it straight back in the queue",
              any(r["id"] == "L-unsent"
                  for r in P.eligible(con, "READY_TO_SEND", 10)),
              "fully reversible")
        for lid, state in (("L-sent", "FOLLOWUP_WAITING"), ("L-reply", "POSITIVE")):
            row = con.execute("SELECT state FROM leads WHERE id=?",
                              (lid,)).fetchone()
            check("   a %-16s lead was never touched" % state,
                  row["state"] == state)

        print("\n--- 14-15. Pausing a mailbox ---")
        with P.writing(con):
            for uid, name in ((2, "t2"), (9, "t9")):
                con.execute(
                    "INSERT INTO tenant_health (tenant_name, user_id, queue_ok,"
                    " approve_ok, leo_ok, mailbox_ok) VALUES (?,?,1,1,1,1)",
                    (name, uid))
        pool = [{"index": 1, "name": "t2", "user_id": 2},
                {"index": 2, "name": "t9", "user_id": 9}]
        check("14. both mailboxes are usable before any pause",
              {t["user_id"] for t in tenants.ready(con, pool)} == {2, 9})
        with P.writing(con):
            con.execute("UPDATE tenant_health SET paused_until="
                        "datetime('now','+72 hours'), paused_reason='rate',"
                        "paused_at=datetime('now') WHERE user_id=9")
        check("   a paused mailbox is not offered new work",
              {t["user_id"] for t in tenants.ready(con, pool)} == {2},
              "t9 stands down")
        check("   the pause records why and until when",
              tenants.is_paused(con, 9)["paused_reason"] == "rate"
              and tenants.is_paused(con, 9)["paused_until"])
        check("15. the mailboxes that were fine are unaffected",
              tenants.is_paused(con, 2) is None,
              "t2 keeps sending")
        with P.writing(con):
            con.execute("UPDATE tenant_health SET paused_until=NULL"
                        " WHERE user_id=9")
        check("   and lifting the pause restores it exactly",
              {t["user_id"] for t in tenants.ready(con, pool)} == {2, 9},
              "no credential was touched, so there is nothing to rebuild")
        with P.writing(con):
            con.execute("UPDATE tenant_health SET paused_until="
                        "datetime('now','-1 hours') WHERE user_id=9")
        check("   an expired pause lifts itself",
              tenants.is_paused(con, 9) is None)

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
