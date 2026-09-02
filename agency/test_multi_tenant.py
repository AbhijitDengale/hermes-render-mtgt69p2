#!/usr/bin/env python3
"""Multi-tenant sending: routing, readiness, inbound and capacity.

The MailHub-side guarantees these depend on -- content_hash matching and
single-use approvals -- are exercised against the live API by
live/multi_tenant_approval.py; this file covers everything the agency decides
for itself.
"""
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import followups as F          # noqa: E402
import pipeline as P           # noqa: E402
import tenants                 # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []
_SEQ = [0]


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-56s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-56s %s" % (name, detail))


def fresh_db(tmp):
    _SEQ[0] += 1
    path = os.path.join(tmp, "mt%d.db" % _SEQ[0])
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


def clear_env():
    for k in list(os.environ):
        if k.startswith("MAILHUB_TENANT_") or k == "MAILHUB_API_TOKEN":
            del os.environ[k]


def set_tenants(n, approve=(), leo=(), queue=None):
    """Configure n tenants; `approve`/`leo` list which indexes get those keys."""
    clear_env()
    queue = range(1, n + 1) if queue is None else queue
    for i in range(1, n + 1):
        os.environ["MAILHUB_TENANT_%d_NAME" % i] = "t%d" % i
        os.environ["MAILHUB_TENANT_%d_USER_ID" % i] = str(i + 1)
        if i in queue:
            os.environ["MAILHUB_TENANT_%d_QUEUE_TOKEN" % i] = "q%d" % i
        if i in approve:
            os.environ["MAILHUB_TENANT_%d_APPROVE_TOKEN" % i] = "a%d" % i
        if i in leo:
            os.environ["MAILHUB_TENANT_%d_LEO_TOKEN" % i] = "l%d" % i


def mark_healthy(con, names, queue=1, approve=1, leo=1, mailbox=1,
                 limit=70, sent=0, health="healthy"):
    for i, name in enumerate(names, start=1):
        con.execute(
            "INSERT INTO tenant_health (tenant_name, user_id, queue_ok,"
            " approve_ok, leo_ok, mailbox_ok, daily_limit, sent_today, health,"
            " mailbox_email) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(tenant_name) DO UPDATE SET queue_ok=excluded.queue_ok,"
            " approve_ok=excluded.approve_ok, leo_ok=excluded.leo_ok,"
            " mailbox_ok=excluded.mailbox_ok, daily_limit=excluded.daily_limit,"
            " sent_today=excluded.sent_today, health=excluded.health",
            (name, i + 1, queue, approve, leo, mailbox, limit, sent, health,
             "%s@example.test" % name))
    con.commit()


def main():
    tmp = tempfile.mkdtemp()
    print("=" * 70)
    print("MULTI-TENANT SENDING")
    print("=" * 70)

    # ---------------------------------------------------------------- 5, 6
    print("\n--- Readiness gates routing (19.5, 19.6) ---")
    db = fresh_db(tmp)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    set_tenants(5, approve=(1, 2, 3, 4, 5), leo=(1, 2, 3, 4, 5))
    mark_healthy(con, ["t1", "t2", "t3", "t4", "t5"])
    check("all five tenants ready", len(tenants.ready(con)) == 5)

    con.execute("UPDATE tenant_health SET approve_ok=0 WHERE tenant_name='t3'")
    con.commit()
    names = [t["name"] for t in tenants.ready(con)]
    check("a tenant with no approve credential leaves routing",
          "t3" not in names and len(names) == 4, str(names))

    con.execute("UPDATE tenant_health SET approve_ok=1, leo_ok=0"
                " WHERE tenant_name='t3'")
    con.execute("UPDATE tenant_health SET leo_ok=0 WHERE tenant_name='t4'")
    con.commit()
    names = [t["name"] for t in tenants.ready(con)]
    check("a tenant with no LEO credential leaves routing",
          "t3" not in names and "t4" not in names, str(names))
    check("  and it is reported as unavailable with a reason",
          any(u["name"] == "t4" and "leo_ok" in u["missing"]
              for u in tenants.unavailable(con)))

    # ---------------------------------------------------------------- 7, 8
    print("\n--- Sender state gates routing (19.7, 19.8) ---")
    mark_healthy(con, ["t1", "t2", "t3", "t4", "t5"])
    con.execute("UPDATE tenant_health SET mailbox_ok=0 WHERE tenant_name='t2'")
    con.commit()
    check("a disabled sender's tenant is excluded",
          "t2" not in [t["name"] for t in tenants.ready(con)])

    mark_healthy(con, ["t1", "t2", "t3", "t4", "t5"])
    con.execute("UPDATE tenant_health SET mailbox_ok=0, health='disabled'"
                " WHERE tenant_name='t5'")
    con.commit()
    check("an unhealthy sender's tenant is excluded",
          "t5" not in [t["name"] for t in tenants.ready(con)])

    check("an unchecked tenant is never ready (fails closed)",
          not tenants.ready(sqlite3.connect(fresh_db(tmp))))

    # ---------------------------------------------------------------- 9, 10
    print("\n--- Distribution (19.9, 19.10) ---")
    mark_healthy(con, ["t1", "t2", "t3", "t4", "t5"])
    counts = {}
    for i in range(1000):
        counts.setdefault(tenants.for_lead("lead-%d" % i, con)["name"], 0)
        counts[tenants.for_lead("lead-%d" % i, con)["name"]] += 1
    check("five tenants all receive leads", len(counts) == 5,
          str(sorted(counts.items())))
    lo, hi = min(counts.values()), max(counts.values())
    check("  spread is even enough for capacity planning", hi - lo < 120,
          "min=%d max=%d" % (lo, hi))
    first = tenants.for_lead("lead-abc", con)["name"]
    check("the same lead always maps to the same tenant",
          all(tenants.for_lead("lead-abc", con)["name"] == first
              for _ in range(200)), first)

    # ---------------------------------------------------------------- 1, 2
    print("\n--- Approval and queue agree on one tenant (19.1, 19.2) ---")
    t = tenants.for_lead("lead-xyz", con)
    r = tenants.for_message(t["user_id"], "lead-xyz", con)
    check("a persisted tenant is honoured at queue time",
          r["status"] == "persisted" and r["tenant"]["user_id"] == t["user_id"])
    check("  and the approve and queue tokens are that tenant's",
          r["tenant"]["approve"].lstrip("a") == r["tenant"]["queue"].lstrip("q"))

    other = [x for x in tenants.load() if x["user_id"] != t["user_id"]][0]
    con.execute("UPDATE tenant_health SET mailbox_ok=0 WHERE tenant_name=?",
                (other["name"],))
    con.commit()
    r2 = tenants.for_message(other["user_id"], "lead-xyz", con)
    check("a lead pinned to a now-unusable tenant is REFUSED, not rerouted",
          r2["status"] == "changed" and r2["tenant"] is None, str(r2["status"]))

    mark_healthy(con, ["t1", "t2", "t3", "t4", "t5"])
    r3 = tenants.for_message(999, "lead-xyz", con)
    check("a lead pinned to an unknown tenant is refused",
          r3["status"] == "changed")
    r4 = tenants.for_message(None, "lead-new", con)
    check("an unpinned lead is assigned, and reports that it was",
          r4["status"] == "assigned" and r4["tenant"] is not None)

    # ------------------------------------------------------------- 11 - 16
    print("\n--- Inbound is per tenant (19.11-19.15, 19.16) ---")
    import inbound_processor as IP

    P.DB = db
    con.execute("INSERT INTO campaigns (id, name, status) VALUES ('c1','c','active')")
    for i, (lead, tenant_uid) in enumerate(
            [("L2", 2), ("L3", 3), ("L4", 4), ("L5", 5), ("L6", 6)]):
        con.execute("INSERT INTO leads (id, campaign_id, email, state)"
                    " VALUES (?,?,?,?)",
                    (lead, "c1", "p%d@example.test" % i, "SENT"))
    con.commit()

    seen = []
    for lead, uid, msgid, bounce in (("L2", 2, "m-a", 0), ("L3", 3, "m-b", 0),
                                     ("L4", 4, "m-c", 0), ("L5", 5, "m-d", 0),
                                     ("L6", 6, "m-e", 0)):
        ev = {"provider_message_id": msgid, "lead_id": lead, "campaign_id": "c1",
              "from": "p@example.test", "subject": "re", "body_text": "hi",
              "tenant_user_id": uid, "is_bounce": bounce}
        with P.writing(con):
            seen.append(IP.record(con, ev))
    check("a reply from each of the five tenants is recorded",
          all(x is not None for x in seen), str(seen))

    rows = con.execute("SELECT tenant_user_id, provider_message_id"
                       " FROM inbound_replies ORDER BY id").fetchall()
    check("  each is stamped with the tenant it arrived in",
          [r["tenant_user_id"] for r in rows] == [2, 3, 4, 5, 6])

    with P.writing(con):
        again = IP.record(con, {"provider_message_id": "m-a", "lead_id": "L2",
                                "tenant_user_id": 2})
    check("the same reply polled twice is recorded once", again is None)

    with P.writing(con):
        cross = IP.record(con, {"provider_message_id": "m-a", "lead_id": "L3",
                                "tenant_user_id": 3, "from": "x@example.test"})
    check("  but the same id in a DIFFERENT tenant is a separate reply",
          cross is not None,
          "tenant-scoped dedupe, not global" if cross else "wrongly dropped")

    n = con.execute("SELECT count(*) FROM inbound_replies").fetchone()[0]
    check("  six rows total, no cross-tenant collapse", n == 6, str(n))

    # ------------------------------------------------------------------ 17
    print("\n--- Follow-ups are cancelled before LEO reasons (19.17) ---")
    con.execute("INSERT INTO leads (id, campaign_id, email, state)"
                " VALUES ('LC','c1','c@example.test','SENT')")
    con.commit()
    with P.writing(con):
        F.schedule(con, "LC", "c1", 1, "2020-01-01T00:00:00")
        F.schedule(con, "LC", "c1", 2, "2020-01-02T00:00:00")
    pending = con.execute("SELECT count(*) FROM followups WHERE lead_id='LC'"
                          " AND status='scheduled'").fetchone()[0]
    check("two follow-ups are due", pending == 2, str(pending))

    order = []
    real_dispatch = IP.dispatch_leo

    def spy(lead_id, reply_id):
        left = con.execute("SELECT count(*) FROM followups WHERE lead_id=?"
                           " AND status='scheduled'", (lead_id,)).fetchone()[0]
        order.append(("leo_called", left))
        return None

    IP.dispatch_leo = spy
    try:
        IP.process_one(con, {"provider_message_id": "m-late", "lead_id": "LC",
                             "campaign_id": "c1", "from": "c@example.test",
                             "subject": "re", "body_text": "interested",
                             "tenant_user_id": 3})
    finally:
        IP.dispatch_leo = real_dispatch
    check("LEO is called only after follow-ups are already cancelled",
          order and order[0][1] == 0,
          "scheduled remaining when LEO ran: %s"
          % (order[0][1] if order else "LEO never ran"))
    left = con.execute("SELECT count(*) FROM followups WHERE lead_id='LC'"
                       " AND status='scheduled'").fetchone()[0]
    check("  and none are left to send", left == 0, str(left))

    # ------------------------------------------------------------------ 18
    print("\n--- Bounce (19.18) ---")
    con.execute("INSERT INTO leads (id, campaign_id, email, state)"
                " VALUES ('LB','c1','b@example.test','SENT')")
    con.commit()
    with P.writing(con):
        F.schedule(con, "LB", "c1", 1, "2020-01-01T00:00:00")
    IP.dispatch_leo = lambda *a, **k: None
    try:
        IP.process_one(con, {"provider_message_id": "m-bounce", "lead_id": "LB",
                             "campaign_id": "c1", "from": "mailer@example.test",
                             "subject": "failed", "body_text": "550",
                             "tenant_user_id": 4, "is_bounce": True})
    finally:
        IP.dispatch_leo = real_dispatch
    st = con.execute("SELECT state FROM leads WHERE id='LB'").fetchone()[0]
    check("a hard bounce moves the lead to BOUNCED", st == "BOUNCED", st)
    left = con.execute("SELECT count(*) FROM followups WHERE lead_id='LB'"
                       " AND status='scheduled'").fetchone()[0]
    check("  and cancels its follow-ups", left == 0, str(left))
    bt = con.execute("SELECT tenant_user_id FROM inbound_replies"
                     " WHERE provider_message_id='m-bounce'").fetchone()[0]
    check("  and is attributed to the tenant it bounced in", bt == 4, str(bt))

    # ------------------------------------------------------------------ 19
    print("\n--- Suppression stays inside its tenant (19.19) ---")
    # MailHub keys suppression on (owner_user_id, email); the agency's job is
    # to hand it the right tenant's credential, which is what is checked here.
    set_tenants(5, approve=(1, 2, 3, 4, 5), leo=(1, 2, 3, 4, 5))
    t5 = tenants.by_user_id(6)
    check("the reply's tenant selects its own LEO credential",
          t5 is not None and t5["leo"] == "l5", (t5 or {}).get("leo"))
    check("  and that credential is not a queue credential",
          t5["leo"] != t5["queue"])
    set_tenants(5, approve=(1, 2, 3, 4, 5), leo=(1, 2, 4, 5))
    check("a tenant with no LEO key yields no suppress credential",
          tenants.by_user_id(4)["leo"] is None)

    # ------------------------------------------------------------------ 20
    print("\n--- ORBIT capacity maths (19.20) ---")
    import orbit
    db2 = fresh_db(tmp)
    c2 = sqlite3.connect(db2)
    c2.row_factory = sqlite3.Row
    mark_healthy(c2, ["t1", "t2", "t3", "t4", "t5"], limit=70, sent=0)
    m = orbit.collect(db2)
    check("configured capacity is 5 x 70", m["capacity_configured"] == 350,
          str(m.get("capacity_configured")))
    check("  usable capacity matches when all are ready",
          m["capacity_usable"] == 350, str(m.get("capacity_usable")))

    c2.execute("UPDATE tenant_health SET sent_today=70 WHERE tenant_name='t1'")
    c2.commit()
    m = orbit.collect(db2)
    check("a sender at its cap contributes no usable capacity",
          m["capacity_usable"] == 280, str(m.get("capacity_usable")))
    check("  but stays in configured capacity", m["capacity_configured"] == 350)

    c2.execute("UPDATE tenant_health SET mailbox_ok=0, health='disabled'"
                " WHERE tenant_name='t2'")
    c2.commit()
    m = orbit.collect(db2)
    check("a disabled sender leaves BOTH configured and usable capacity",
          m["capacity_configured"] == 280 and m["capacity_usable"] == 210,
          "configured=%s usable=%s" % (m["capacity_configured"], m["capacity_usable"]))

    c2.execute("UPDATE tenant_health SET approve_ok=0 WHERE tenant_name='t3'")
    c2.commit()
    m = orbit.collect(db2)
    check("an incomplete tenant is capacity on paper only",
          m["capacity_configured"] == 280 and m["capacity_usable"] == 140,
          "configured=%s usable=%s" % (m["capacity_configured"], m["capacity_usable"]))
    # t1 is capped for today but still complete: readiness is about whether a
    # tenant can carry a lead end to end, not about how much room is left in
    # it. Only t2 (disabled) and t3 (no approve key) are actually unusable.
    check("  and the ready count drops with it", m["tenants_ready"] == 3,
          str(m.get("tenants_ready")))
    check("  a tenant at its cap is still READY, just out of room today",
          any(t["tenant_name"] == "t1" and t["ready"] and t["remaining"] == 0
              for t in m["tenants"]))

    print()
    print("=" * 70)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:")
        for f in FAILURES:
            print("  " + f)
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
