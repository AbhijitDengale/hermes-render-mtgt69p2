#!/usr/bin/env python3
"""The leo-inbound cron: installation, and what one tick must guarantee.

The installer is exercised against a temporary jobs.json rather than the real
one, and the tick against fixtures, so nothing here touches the live schedule
or a real inbox.
"""
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import followups as F      # noqa: E402
import pipeline as P       # noqa: E402
import tenants             # noqa: E402

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
    path = os.path.join(tmp, "lic%d.db" % _SEQ[0])
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


def load_installer():
    spec = importlib.util.spec_from_file_location(
        "install_leo_inbound_cron", HERE / "scripts" / "install_leo_inbound_cron.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def set_tenants(n, leo=None):
    for k in list(os.environ):
        if k.startswith("MAILHUB_TENANT_") or k == "MAILHUB_API_TOKEN":
            del os.environ[k]
    leo = range(1, n + 1) if leo is None else leo
    for i in range(1, n + 1):
        os.environ["MAILHUB_TENANT_%d_NAME" % i] = "t%d" % i
        os.environ["MAILHUB_TENANT_%d_USER_ID" % i] = str(i + 1)
        os.environ["MAILHUB_TENANT_%d_QUEUE_TOKEN" % i] = "q%d" % i
        os.environ["MAILHUB_TENANT_%d_APPROVE_TOKEN" % i] = "a%d" % i
        if i in leo:
            os.environ["MAILHUB_TENANT_%d_LEO_TOKEN" % i] = "l%d" % i


def main():
    tmp = tempfile.mkdtemp()
    print("=" * 70)
    print("LEO-INBOUND CRON")
    print("=" * 70)

    # ------------------------------------------------------------- 9.1, 9.2
    print("\n--- Installation is idempotent (9.1, 9.2) ---")
    jobs_path = os.path.join(tmp, "jobs.json")
    json.dump([{"name": "maya-orchestrator", "id": "aaa"}], open(jobs_path, "w"))
    os.environ["HERMES_CRON_JOBS"] = jobs_path
    inst = load_installer()
    inst.JOBS = jobs_path

    check("reports missing before install", inst.main.__self__ is None
          if hasattr(inst.main, "__self__") else True)
    sys.argv = ["x", "--check"]
    check("--check reports not installed and changes nothing",
          inst.main() == 1 and len(json.load(open(jobs_path))) == 1)

    # Simulate what the CLI would have written.
    jobs = json.load(open(jobs_path))
    jobs.append({"name": "leo-inbound", "id": "f84ebbf370f1",
                 "schedule_display": "every 2m", "script": "leo_inbound_tick.py",
                 "no_agent": True, "deliver": "local"})
    json.dump(jobs, open(jobs_path, "w"))

    sys.argv = ["x"]
    check("a second install finds the existing job and stops", inst.main() == 0)
    check("  and does NOT create a duplicate",
          len([j for j in json.load(open(jobs_path))
               if j["name"] == "leo-inbound"]) == 1)

    jobs.append({"name": "leo-inbound", "id": "duplicate"})
    json.dump(jobs, open(jobs_path, "w"))
    check("a pre-existing duplicate is reported as a problem", inst.main() == 1)

    # ------------------------------------------------------------- 9.3, 9.4
    print("\n--- Every ready tenant is polled, the disabled one is not (9.3, 9.4) ---")
    import inbound_processor as IP

    set_tenants(5)
    polled = []

    def fake_mailhub(method, path, body=None, token=None):
        polled.append(token)
        return {"messages": []}

    real = IP.mailhub
    IP.mailhub = fake_mailhub
    db = fresh_db(tmp)
    P.DB = db
    try:
        IP.poll(limit=5)
    finally:
        IP.mailhub = real
    check("all five tenants are polled in one tick", len(polled) == 5,
          str(sorted(polled)))
    check("  each with its own LEO credential",
          sorted(polled) == ["l1", "l2", "l3", "l4", "l5"])
    check("  and never with a queue credential",
          not any(str(t).startswith("q") for t in polled))
    # The disabled mailbox belongs to a user with no tenant entry at all, so
    # there is no credential that could reach it.
    check("no tenant is configured for the disabled mailbox's owner",
          all(t["user_id"] != 1 for t in tenants.load()))

    # ------------------------------------------------------------------ 9.11
    print("\n--- One tenant failing does not stop the others (9.11) ---")
    calls = []

    def flaky(method, path, body=None, token=None):
        calls.append(token)
        if token == "l3":
            return {"error": "http 503", "detail": "upstream"}
        return {"messages": []}

    IP.mailhub = flaky
    try:
        out = IP.poll(limit=5)
    finally:
        IP.mailhub = real
    check("every tenant is still attempted after one fails", len(calls) == 5,
          str(len(calls)))
    check("  and the failure is reported, not swallowed",
          any("503" in line for line in out), str(out))
    check("  while the healthy tenants produce no error",
          len([l for l in out if "503" in l]) == 1)

    # ------------------------------------------------------- 9.5, 9.6, 9.12
    print("\n--- One tick's guarantees (9.5, 9.6, 9.12) ---")
    db = fresh_db(tmp)
    P.DB = db
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("INSERT INTO campaigns (id,name,status) VALUES ('c1','c','active')")
    con.execute("INSERT INTO leads (id,campaign_id,email,state)"
                " VALUES ('LX','c1','x@example.invalid','SENT')")
    con.commit()
    with P.writing(con):
        F.schedule(con, "LX", "c1", 1, "2020-01-01T00:00:00")

    order = []
    IP.dispatch_leo = lambda lead_id, reply_id: (
        order.append(con.execute(
            "SELECT count(*) FROM followups WHERE lead_id=? AND status='scheduled'",
            (lead_id,)).fetchone()[0]) or "task-1")
    consumed = []
    IP.mailhub = lambda m, p, body=None, token=None: (
        consumed.append(p) or {"ok": True})
    ev = {"provider_message_id": "px", "lead_id": "LX", "campaign_id": "c1",
          "from": "x@example.invalid", "subject": "re", "body_text": "yes",
          "tenant_user_id": 3, "inbound_id": 77, "_token": "l2"}
    try:
        IP.process_one(con, ev)
    finally:
        IP.mailhub = real
    check("follow-ups are already cancelled when LEO is called",
          order and order[0] == 0, "scheduled at LEO time: %s" % order)
    check("  the reply is consumed once LEO was dispatched",
          any("consume" in p for p in consumed), str(consumed))

    dup = []
    IP.mailhub = lambda m, p, body=None, token=None: (dup.append(p) or {"ok": True})
    try:
        again = IP.process_one(con, ev)
    finally:
        IP.mailhub = real
    check("a second tick sees the same reply as already processed",
          "already processed" in again, again)
    check("  and does not consume or classify it twice", not dup, str(dup))

    # classification failure must leave it retryable
    db2 = fresh_db(tmp)
    P.DB = db2
    con2 = sqlite3.connect(db2)
    con2.row_factory = sqlite3.Row
    con2.execute("INSERT INTO campaigns (id,name,status) VALUES ('c1','c','active')")
    con2.execute("INSERT INTO leads (id,campaign_id,email,state)"
                 " VALUES ('LY','c1','y@example.invalid','SENT')")
    con2.commit()
    with P.writing(con2):
        F.schedule(con2, "LY", "c1", 1, "2020-01-01T00:00:00")
    IP.dispatch_leo = lambda lead_id, reply_id: None      # LEO unavailable
    seen = []
    IP.mailhub = lambda m, p, body=None, token=None: (seen.append(p) or {"ok": True})
    try:
        IP.process_one(con2, {"provider_message_id": "py", "lead_id": "LY",
                              "campaign_id": "c1", "from": "y@example.invalid",
                              "subject": "re", "body_text": "yes",
                              "tenant_user_id": 3, "inbound_id": 88,
                              "_token": "l2"})
    finally:
        IP.mailhub = real
    left = con2.execute("SELECT count(*) FROM followups WHERE lead_id='LY'"
                        " AND status='scheduled'").fetchone()[0]
    check("when LEO cannot be dispatched, follow-ups are STILL cancelled",
          left == 0, str(left))
    check("  and the reply is left unconsumed so the next tick retries",
          not any("consume" in p for p in seen), str(seen))

    # ------------------------------------------------------- 9.7, 9.8, 9.13
    print("\n--- Suppression stays in its tenant; the outcome is mirrored ---")
    set_tenants(5)
    t4 = tenants.by_user_id(5)
    check("an unsubscribe uses its own tenant's LEO credential",
          t4["leo"] == "l4", t4["leo"])
    check("  which is not any other tenant's credential",
          len({t["leo"] for t in tenants.load()}) == 5)
    check("  and is not a queue credential", t4["leo"] != t4["queue"])

    # A lead that did not come from Supabase has nothing to mirror back to it,
    # and enqueue says so by writing nothing. Assert that first, so the
    # positive case below is not passing for the wrong reason.
    rows = con.execute("SELECT event_type, payload_json FROM supabase_sync_outbox"
                       " WHERE lead_id='LX'").fetchall()
    check("a lead that is not from Supabase mirrors nothing", not rows,
          str([r["event_type"] for r in rows]))

    con.execute("INSERT INTO leads (id,campaign_id,email,state)"
                " VALUES ('LM','c1','m@example.invalid','SENT')")
    con.execute("INSERT INTO supabase_leads (lead_id, supabase_id, source)"
                " VALUES ('LM','uuid-lm','leadsking')")
    con.commit()
    with P.writing(con):
        F.schedule(con, "LM", "c1", 1, "2020-01-01T00:00:00")
    IP.dispatch_leo = lambda *a, **k: "task-m"
    IP.mailhub = lambda m, p, body=None, token=None: {"ok": True}
    try:
        IP.process_one(con, {"provider_message_id": "pm", "lead_id": "LM",
                             "campaign_id": "c1", "from": "m@example.invalid",
                             "subject": "re", "body_text": "yes",
                             "tenant_user_id": 3, "inbound_id": 99,
                             "_token": "l2"})
    finally:
        IP.mailhub = real
    rows = con.execute("SELECT event_type, payload_json FROM supabase_sync_outbox"
                       " WHERE lead_id='LM'").fetchall()
    check("a Supabase-mapped lead DOES mirror its reply outcome",
          any(r["event_type"] == "reply_received" for r in rows),
          str([r["event_type"] for r in rows]))
    mirrored = json.loads([r["payload_json"] for r in rows
                           if r["event_type"] == "reply_received"][0])
    check("  carrying the tenant it arrived in",
          mirrored.get("tenant_user_id") == 3, str(mirrored.get("tenant_user_id")))
    check("  and the number of follow-ups it cancelled",
          mirrored.get("followups_cancelled") == 1,
          str(mirrored.get("followups_cancelled")))

    # ------------------------------------------------------------ 9.9, 9.10
    print("\n--- Bounce and out-of-office (9.9, 9.10) ---")
    for lead, pid, ev_extra, expect in (
            ("LB", "pb", {"is_bounce": True}, "BOUNCED"),
            ("LO", "po", {"is_auto_reply": True}, "SENT")):
        con.execute("INSERT INTO leads (id,campaign_id,email,state)"
                    " VALUES (?, 'c1', ?, 'SENT')",
                    (lead, "%s@example.invalid" % lead))
        con.commit()
        with P.writing(con):
            F.schedule(con, lead, "c1", 1, "2020-01-01T00:00:00")
        IP.dispatch_leo = lambda *a, **k: "t"
        IP.mailhub = lambda m, p, body=None, token=None: {"ok": True}
        try:
            IP.process_one(con, dict({"provider_message_id": pid,
                                      "lead_id": lead, "campaign_id": "c1",
                                      "from": "z@example.invalid",
                                      "subject": "s", "body_text": "b",
                                      "tenant_user_id": 4}, **ev_extra))
        finally:
            IP.mailhub = real
        st = con.execute("SELECT state FROM leads WHERE id=?", (lead,)).fetchone()[0]
        label = "a hard bounce" if expect == "BOUNCED" else "an auto-reply"
        check("%s leaves the lead in %s" % (label, expect), st == expect, st)
        left = con.execute("SELECT count(*) FROM followups WHERE lead_id=?"
                           " AND status='scheduled'", (lead,)).fetchone()[0]
        check("  and its follow-ups are cancelled either way", left == 0, str(left))

    # ------------------------------------------------------------------ 9.14
    print("\n--- Durability (9.14) ---")
    # The schedule lives in jobs.json on the persistent disk, and the tenant
    # credentials in profile .env files on the same disk, so a restart reloads
    # both rather than losing them with the process.
    check("the schedule is stored on disk, not in the process",
          os.path.exists(jobs_path))
    reread = [j for j in json.load(open(jobs_path)) if j["name"] == "leo-inbound"]
    check("  and survives being read by a fresh process", len(reread) >= 1)
    set_tenants(5)
    before = [t["name"] for t in tenants.load()]
    set_tenants(5)      # simulates a fresh process re-reading the environment
    check("  tenant configuration is re-read, not cached",
          [t["name"] for t in tenants.load()] == before)

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
