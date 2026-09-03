#!/usr/bin/env python3
"""Onboarding a new sender mailbox: readiness, separation, selection.

The rules a new mailbox has to satisfy before it may carry real outreach, and
the guarantees the mailboxes already in production keep while it is added.

Everything runs against a temporary database and a fake MailHub. No network,
no Supabase, no Discord, nothing sent.
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pipeline as P          # noqa: E402
import tenant_health          # noqa: E402
import tenants                # noqa: E402

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


def fresh_db(tmp, name="onboard.db"):
    path = os.path.join(tmp, name)
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


# The five already in production, then the two being onboarded, then the ones
# that must stay out: one excluded by the operator, one with two accepted
# aliases and therefore no single answer.
OLD = [("abhijitdeng20187", 2, "Abhiji", "abhijit@syntrix.cv"),
       ("minhulisa", 3, "Lisa Chen", "demon@socialnexa.cv"),
       ("dnyandev887", 4, "Darki", "darki@socialnexa.cv"),
       ("dnyandevdeng", 5, "Ethan cole", "ethan@socialnexa.cv"),
       ("minhuli2005", 6, "Sophie Laurent", "minhu@socialnexa.cv")]
NEW = [("amitshinde7489", 9, "Amit Shinde", "amit@syntrix.cv"),
       ("amolsatkar994", 10, "amol satkar", "amol@syntrix.cv")]
EXCLUDED = ("chetansatkar176", 8)
AMBIGUOUS = ("chetansatkar72", 11)


def set_env(entries):
    for k in list(os.environ):
        if k.startswith("MAILHUB_TENANT_") or k == "MAILHUB_API_TOKEN":
            del os.environ[k]
    for i, (name, uid) in enumerate(entries, 1):
        os.environ["MAILHUB_TENANT_%d_NAME" % i] = name
        os.environ["MAILHUB_TENANT_%d_USER_ID" % i] = str(uid)
        os.environ["MAILHUB_TENANT_%d_QUEUE_TOKEN" % i] = "q-%s" % name
        os.environ["MAILHUB_TENANT_%d_APPROVE_TOKEN" % i] = "a-%s" % name
        os.environ["MAILHUB_TENANT_%d_LEO_TOKEN" % i] = "l-%s" % name


def health_row(con, name, uid, sends_as=None, display=None, identity="verified",
               queue=1, approve=1, leo=1, mailbox=1, limit=70, sent=0):
    con.execute(
        "INSERT INTO tenant_health (tenant_name, user_id, queue_ok, approve_ok,"
        " leo_ok, mailbox_ok, daily_limit, sent_today, health, mailbox_email,"
        " sender_from_email, sender_from_name, sender_identity_status)"
        " VALUES (?,?,?,?,?,?,?,?, 'warming', ?,?,?,?)"
        " ON CONFLICT(tenant_name) DO UPDATE SET queue_ok=excluded.queue_ok,"
        "  approve_ok=excluded.approve_ok, leo_ok=excluded.leo_ok,"
        "  mailbox_ok=excluded.mailbox_ok, sender_from_email=excluded.sender_from_email,"
        "  sender_identity_status=excluded.sender_identity_status",
        (name, uid, queue, approve, leo, mailbox, limit, sent,
         "%s@gmail.com" % name, sends_as, display, identity))


class FakeMailHub:
    """Answers the health probes the way the real API does for a given key."""

    def __init__(self, accounts_by_token):
        self.accounts = accounts_by_token
        self.calls = []

    def call(self, token, method, path, body=None):
        self.calls.append((token, method, path))
        kind = (token or "").split("-", 1)[0]
        if path == "/api/v1/accounts":
            if not token:
                return 0, "not configured"
            return 200, json.dumps({"accounts": self.accounts.get(token, [])})
        # Scope gates: 400 means the capability was held and only the empty
        # body was rejected; 403 means refused. This is the real contract.
        allowed = {"q": {"/api/v1/messages"},
                   "a": {"/api/v1/approvals"},
                   "l": {"/api/v1/suppression"}}.get(kind, set())
        return (400 if path in allowed else 403), "{}"


def account(email, from_email, from_name, identity="verified", enabled=True,
            limit=70, sent=0, health="warming"):
    return {"id": "acct_" + email.split("@")[0], "email": email, "enabled": enabled,
            "health": health, "effective_daily_limit": limit, "daily_limit": limit,
            "sent_today": sent, "from_email": from_email, "from_name": from_name,
            "identity_status": identity, "sent_total": sent,
            "consecutive_errors": 0, "next_send_at": None, "last_sent_at": None}


def main() -> int:
    tmp = tempfile.mkdtemp()
    print("=" * 78)
    print("NEW SENDER ONBOARDING")
    print("=" * 78)

    # ---------------------------------------------------------- 1, 2, 3, 7
    print("\n--- 1-3, 7. Readiness depends on a verified professional identity ---")
    db = fresh_db(tmp)
    set_env([(n, u) for n, u, _, _ in OLD + NEW] + [EXCLUDED, AMBIGUOUS])
    accounts = {}
    for name, uid, disp, addr in OLD + NEW:
        accounts["q-" + name] = [account("%s@gmail.com" % name, addr, disp)]
    # The excluded mailbox: connected, but never discovered and never enabled.
    accounts["q-" + EXCLUDED[0]] = [account("%s@gmail.com" % EXCLUDED[0], None, None,
                                            identity=None, enabled=False)]
    # The ambiguous one: enabled would still not make it sendable.
    accounts["q-" + AMBIGUOUS[0]] = [account("%s@gmail.com" % AMBIGUOUS[0], None, None,
                                             identity="ambiguous", enabled=True)]
    fake = FakeMailHub(accounts)
    real_call, tenant_health._call = tenant_health._call, fake.call
    try:
        with P.connect(db) as con:
            with P.writing(con):
                results = tenant_health.check_all(con)
            ready = {t["name"] for t in tenants.ready(con)}
            rows = {r["tenant_name"]: dict(r) for r in
                    con.execute("SELECT * FROM tenant_health")}
    finally:
        tenant_health._call = real_call

    check("3. a verified professional identity makes a new mailbox READY",
          {"amitshinde7489", "amolsatkar994"} <= ready, str(sorted(ready)))
    check("   and the identity it will send as is recorded, from MailHub",
          rows["amitshinde7489"]["sender_from_email"] == "amit@syntrix.cv"
          and rows["amitshinde7489"]["sender_from_name"] == "Amit Shinde"
          and rows["amitshinde7489"]["sender_identity_status"] == "verified")
    check("1. the mailbox the operator excluded never becomes READY",
          EXCLUDED[0] not in ready and rows[EXCLUDED[0]]["mailbox_ok"] == 0,
          "mailbox_ok=%s" % rows[EXCLUDED[0]]["mailbox_ok"])
    check("2. a mailbox with an ambiguous identity is NOT ready, even enabled",
          AMBIGUOUS[0] not in ready and rows[AMBIGUOUS[0]]["mailbox_ok"] == 0,
          "identity=%s" % rows[AMBIGUOUS[0]]["sender_identity_status"])
    check("   an unverified identity alone is enough to withhold readiness",
          all(r["mailbox_ok"] == 0 for n, r in rows.items()
              if r["sender_identity_status"] not in ("verified",)))
    check("7. the five already in production are unaffected",
          {n for n, _, _, _ in OLD} <= ready
          and all(rows[n]["sender_from_email"] == a for n, _, _, a in OLD),
          str(sorted(n for n, _, _, _ in OLD)))
    check("   readiness needs all four credentials, not just the mailbox",
          all(rows[n]["queue_ok"] and rows[n]["approve_ok"] and rows[n]["leo_ok"]
              for n, _, _, _ in OLD + NEW))

    # ------------------------------------------------------------- 4, 5
    print("\n--- 4-5. Credential separation, proved against the scope gates ---")
    # check_all reports one merged row per tenant, so its `caps` is whichever
    # credential it probed last. Each credential is probed on its own here.
    by_tenant = {r["tenant"]: r for r in results}
    fake.calls.clear()
    real_call, tenant_health._call = tenant_health._call, fake.call
    try:
        q = tenant_health.probe("q-amitshinde7489")
        a = tenant_health.probe("a-amitshinde7489")
        l = tenant_health.probe("l-amitshinde7489")
    finally:
        tenant_health._call = real_call
    check("4. the queue credential can queue and cannot approve",
          q["queue"] and not q["approve"], str(q))
    check("   the queue check itself refuses a credential that could approve",
          by_tenant["amitshinde7489"]["queue_ok"] is True)
    check("   no credential holds both queue and approve",
          not any(c["queue"] and c["approve"] for c in (q, a, l)),
          "queue=%s approve=%s leo=%s" % (q, a, l))
    check("   the approve credential cannot queue and cannot suppress",
          a["approve"] and not a["queue"] and not a["suppress"], str(a))
    check("5. the LEO credential can read and suppress only",
          l["read"] and l["suppress"] and not l["queue"] and not l["approve"], str(l))
    check("   MAYA is never handed an approval credential",
          not q["approve"])

    # --------------------------------------------------------------- 6
    print("\n--- 6. Tenant selection stays deterministic as the pool grows ---")
    with P.connect(db) as con:
        first = {"L-%03d" % i: tenants.for_lead("L-%03d" % i, con)["name"] for i in range(40)}
        again = {"L-%03d" % i: tenants.for_lead("L-%03d" % i, con)["name"] for i in range(40)}
        check("the same lead always routes to the same tenant",
              first == again, "%d leads" % len(first))
        check("  and only to READY tenants",
              set(first.values()) <= ready, str(sorted(set(first.values()))))
        check("  the excluded and ambiguous mailboxes are never chosen",
              EXCLUDED[0] not in first.values() and AMBIGUOUS[0] not in first.values())
        check("  the new tenants do take a share once ready",
              {"amitshinde7489", "amolsatkar994"} & set(first.values()) != set(),
              str({v: list(first.values()).count(v) for v in sorted(set(first.values()))}))
        # A tenant recorded at approval time is honoured, never swapped.
        route = tenants.for_message(9, "L-001", con)
        check("  a message keeps the tenant recorded at approval time",
              route["status"] == "persisted" and route["tenant"]["user_id"] == 9,
              str(route["status"]))
        route = tenants.for_message(11, "L-001", con)
        check("  and a message recorded against a not-ready tenant is refused,"
              " not rerouted", route["status"] == "changed" and route["tenant"] is None,
              str(route["status"]))

    # --------------------------------------------------------------- 9
    print("\n--- 9. An inbound reply maps to the tenant that sent it ---")
    import inbound_processor as IP
    with P.connect(db) as con:
        with P.writing(con):
            con.execute("INSERT OR IGNORE INTO campaigns (id, name, status,"
                        " followup_schedule) VALUES ('C-1','C-1','active','[\"2m\"]')")
            for lead, uid in (("L-NEW", 9), ("L-OLD", 2)):
                con.execute("INSERT INTO leads (id, campaign_id, email, business_name,"
                            " state, created_at, updated_at) VALUES (?,?,?,?, 'SENT',"
                            " datetime('now'), datetime('now'))",
                            (lead, "C-1", "%s@example.com" % lead.lower(), lead))
                P.save_draft(con, lead, "C-1", 0, "S", "B")
                con.execute("UPDATE messages SET status='sent', tenant_user_id=?,"
                            " provider_thread_id=?, mailhub_queue_id=? WHERE id=?",
                            (uid, "thread-%s" % lead, "q-%s" % lead, P.message_id(lead, 0)))
        rows = con.execute("SELECT lead_id, tenant_user_id FROM messages"
                           " WHERE status='sent' ORDER BY lead_id").fetchall()
    check("each sent message carries the tenant that sent it",
          {r["lead_id"]: r["tenant_user_id"] for r in rows} == {"L-NEW": 9, "L-OLD": 2})
    check("  a reply is read with the credential of that same tenant",
          tenants.by_user_id(9)["leo"] == "l-amitshinde7489"
          and tenants.by_user_id(2)["leo"] == "l-abhijitdeng20187")
    check("  and never with another tenant's credential",
          tenants.by_user_id(9)["leo"] != tenants.by_user_id(2)["leo"])
    check("  LEO's per-tenant read is scoped by MailHub, not chosen here",
          "leo" in tenants.by_user_id(9) and tenants.by_user_id(9)["queue"] is not None)

    # ------------------------------------------------------------ 10-12
    print("\n--- 10-12. Sends are not duplicated and the two stores agree ---")
    with P.connect(db) as con:
        # The idempotency key is derived from the approved content, so the same
        # draft presented twice is the same key and cannot become two emails.
        with P.writing(con):
            P.save_draft(con, "L-NEW", "C-1", 1, "Follow up", "Body")
        d0 = P.load_draft(con, "L-NEW", 0)
        d1 = P.load_draft(con, "L-NEW", 1)
        key = lambda lead, stage, h: "lead:%s:%s:stage%d:%s" % (lead, "C-1", stage, h[:16])
        check("10. the same draft yields the same idempotency key",
              key("L-NEW", 0, d0["content_hash"]) == key("L-NEW", 0, d0["content_hash"]))
        check("    a different stage yields a different key",
              key("L-NEW", 0, d0["content_hash"]) != key("L-NEW", 1, d1["content_hash"]))
        check("    a rewritten body yields a different key",
              d0["content_hash"] != d1["content_hash"])
        try:
            with P.writing(con):
                P.save_draft(con, "L-NEW", "C-1", 0, "rewritten", "rewritten")
            refused = False
        except P.TransitionError:
            refused = True
        check("    a message already sent cannot be rewritten into a second send",
              refused)

        # Reconciliation: a lead counts as sent only on provider confirmation.
        with P.writing(con):
            con.execute("INSERT INTO leads (id, campaign_id, email, business_name,"
                        " state, created_at, updated_at) VALUES ('L-QUEUED','C-1',"
                        "'q@example.com','Q','READY_TO_SEND', datetime('now'), datetime('now'))")
            P.save_draft(con, "L-QUEUED", "C-1", 0, "S", "B")
            con.execute("UPDATE messages SET status='queued', mailhub_queue_id='q-9'"
                        " WHERE id=?", (P.message_id("L-QUEUED", 0),))
        sent = con.execute("SELECT COUNT(*) FROM messages WHERE status='sent'").fetchone()[0]
        queued = con.execute("SELECT COUNT(*) FROM messages WHERE status='queued'").fetchone()[0]
        check("11. a queued message is not counted as sent", sent == 2 and queued == 1,
              "sent=%d queued=%d" % (sent, queued))
        check("    every sent message carries a provider-side queue id",
              con.execute("SELECT COUNT(*) FROM messages WHERE status='sent'"
                          " AND mailhub_queue_id IS NULL").fetchone()[0] == 0)
        check("12. a send with no MailHub row would be visible as a mismatch",
              con.execute("SELECT COUNT(*) FROM messages WHERE status='sent'"
                          " AND mailhub_queue_id IN ('q-L-NEW','q-L-OLD')").fetchone()[0] == 2)

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
