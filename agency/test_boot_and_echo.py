#!/usr/bin/env python3
"""Boot durability, the echo-followups cron, and stale-automation detection.

The boot checks read the Dockerfile and bootstrap.sh as text. That is the right
level: the failure was not a bug inside a running process, it was the container
never running its own CMD, and the only durable record of that decision is
those two files.
"""
import datetime
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import followups as F      # noqa: E402
import pipeline as P       # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []
_SEQ = [0]


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-58s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-58s %s" % (name, detail))


def fresh_db(tmp):
    _SEQ[0] += 1
    path = os.path.join(tmp, "be%d.db" % _SEQ[0])
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


def main():
    tmp = tempfile.mkdtemp()
    print("=" * 72)
    print("BOOT DURABILITY, ECHO CRON, AUTOMATION HEALTH")
    print("=" * 72)

    # ------------------------------------------------------------- 15.1, 15.2
    print("\n--- The container runs its own CMD (15.1, 15.2) ---")
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    bootstrap = (REPO / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")

    check("bootstrap hands off to the dispatcher that execs the CMD",
          "entrypoint-dispatch.sh" in bootstrap)
    check("  and NOT to the shim that returns without exec'ing it",
          "exec /opt/hermes/docker/entrypoint.sh" not in bootstrap)
    check("  via exec, so PID 1 is preserved for the dispatcher's own check",
          "exec /opt/hermes/docker/entrypoint-dispatch.sh" in bootstrap)

    entry = [l for l in dockerfile.splitlines() if l.startswith("ENTRYPOINT")]
    check("the entrypoint does not route the CMD through the tini shim",
          entry and "/usr/bin/tini" not in entry[0],
          entry[0] if entry else "no ENTRYPOINT line")
    check("  bootstrap.sh is the entrypoint directly",
          'ENTRYPOINT ["/opt/render-tools/bootstrap.sh"]' in dockerfile)
    check("  and the CMD is still the gateway",
          'CMD ["gateway", "run"]' in dockerfile)

    # ------------------------------------------------------------- 15.3, 15.4
    print("\n--- Cron installation is idempotent (15.3, 15.4) ---")
    spec = importlib.util.spec_from_file_location(
        "install_agency_crons", HERE / "scripts" / "install_agency_crons.py")
    inst = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inst)

    jobs_path = os.path.join(tmp, "jobs.json")
    inst.JOBS = jobs_path
    json.dump([{"name": "maya-orchestrator", "id": "aaa"}],
              open(jobs_path, "w"))
    sys.argv = ["x", "--check"]
    check("--check reports both jobs missing and changes nothing",
          inst.main() == 1 and len(json.load(open(jobs_path))) == 1)

    jobs = json.load(open(jobs_path))
    for name, jid, script in (("leo-inbound", "f84e", "leo_inbound_tick.py"),
                              ("echo-followups", "d802", "echo_followups.py")):
        jobs.append({"name": name, "id": jid, "schedule_display": "every 2m",
                     "script": script, "no_agent": True, "deliver": "local"})
    json.dump(jobs, open(jobs_path, "w"))

    check("with both present the installer is a no-op", inst.main() == 0)
    check("  and re-running still creates nothing", inst.main() == 0)
    names = [j["name"] for j in json.load(open(jobs_path))]
    check("  exactly one of each after three runs",
          names.count("leo-inbound") == 1 and names.count("echo-followups") == 1,
          str(sorted(names)))

    jobs.append({"name": "echo-followups", "id": "dupe"})
    json.dump(jobs, open(jobs_path, "w"))
    check("a duplicate is reported rather than tolerated", inst.main() == 1)

    # ------------------------------------------------- 15.5, 15.6, 15.7, 15.8
    print("\n--- ECHO dispatches only what is genuinely due (15.5-15.9) ---")
    import echo_tick

    db = fresh_db(tmp)
    P.DB = db
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("INSERT INTO campaigns (id,name,status) VALUES ('c1','c','active')")
    # due() compares scheduled_for against SQLite's datetime('now') as strings,
    # so the separator matters: "T" sorts after " ", and an ISO-with-T
    # timestamp for today would never compare as due. A far-past date would
    # hide that, because the year decides the comparison before the separator
    # is reached -- so use the same formatter production uses.
    past = F._fmt(datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(minutes=5))         if hasattr(F, "_fmt") else (datetime.datetime.now(datetime.timezone.utc)
                                    - datetime.timedelta(minutes=5)
                                    ).strftime("%Y-%m-%d %H:%M:%S")
    # LP lives in its own campaign so it can be paused before its first tick.
    # In c1 it would simply be dispatched by the tick below and the pause test
    # would be asserting against an already-dispatched follow-up.
    con.execute("INSERT INTO campaigns (id,name,status) VALUES ('c2','c','paused')")
    for lid in ("LD", "LR"):
        con.execute("INSERT INTO leads (id,campaign_id,email,state)"
                    " VALUES (?, 'c1', ?, 'FOLLOWUP_WAITING')",
                    (lid, "%s@example.invalid" % lid))
    con.execute("INSERT INTO leads (id,campaign_id,email,state)"
                " VALUES ('LP','c2','lp@example.invalid','FOLLOWUP_WAITING')")
    con.commit()
    with P.writing(con):
        for lid in ("LD", "LR"):
            F.schedule(con, lid, "c1", 1, past)
        F.schedule(con, "LP", "c2", 1, past)

    # A future follow-up must not be picked up at all.
    con.execute("INSERT INTO leads (id,campaign_id,email,state)"
                " VALUES ('LF','c1','f@example.invalid','FOLLOWUP_WAITING')")
    con.commit()
    future = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    with P.writing(con):
        F.schedule(con, "LF", "c1", 1, future)

    # LR has already replied -> must never be chased.
    with P.writing(con):
        P.transition(con, "LR", "REPLIED", "test", "prospect replied")
        F.cancel_all(con, "LR", "reply received", agent="inbound")

    first_tick = echo_tick.tick(limit=20)
    out = first_tick
    states = {r["id"]: r["state"] for r in
              con.execute("SELECT id, state FROM leads")}
    check("a due follow-up is moved to FOLLOWUP_PENDING",
          states["LD"] == "FOLLOWUP_PENDING", states["LD"])
    check("  exactly once — a second tick does not redispatch",
          not any("LD" in l and "due" in l for l in echo_tick.tick(limit=20)),
          "already dispatched")
    check("a lead that replied is never chased",
          states["LR"] == "REPLIED", states["LR"])
    check("  and its follow-up stays cancelled, never dispatched",
          con.execute("SELECT status FROM followups WHERE lead_id='LR'"
                      ).fetchone()[0] == "cancelled")
    check("a follow-up that is not due yet is untouched",
          states["LF"] == "FOLLOWUP_WAITING", states["LF"])
    check("  ECHO does not send; it makes a lead eligible for the pipeline",
          "FOLLOWUP_PENDING" in str(out) and "queue" not in str(out).lower())

    # ------------------------------------------------------------ 15.8, 15.9
    print("\n--- A paused campaign holds, and resuming releases (15.8, 15.9) ---")
    before_attempts = con.execute(
        "SELECT attempts FROM followups WHERE lead_id='LP'").fetchone()[0]
    out = echo_tick.tick(limit=20)
    row = con.execute("SELECT status, attempts FROM followups"
                      " WHERE lead_id='LP'").fetchone()
    check("a paused campaign holds the follow-up", row["status"] == "scheduled",
          row["status"])
    check("  without burning an attempt (the hold is reversible)",
          row["attempts"] == before_attempts,
          "%s -> %s" % (before_attempts, row["attempts"]))
    # The hold is announced once, when the reason first applies -- not on every
    # tick. A job that repeated itself every two minutes would train you to
    # stop reading it, which is how a real hold goes unnoticed.
    check("  and said so when the hold first applied",
          any("hold" in l and "LP" in l for l in first_tick),
          str([l for l in first_tick if "LP" in l]))
    check("  but does not repeat itself every tick",
          not any("hold" in l and "LP" in l for l in out), str(out)[:60])
    check("  the lead has not moved", con.execute(
        "SELECT state FROM leads WHERE id='LP'").fetchone()[0]
        == "FOLLOWUP_WAITING")

    con.execute("UPDATE campaigns SET status='active' WHERE id='c2'")
    con.commit()
    echo_tick.tick(limit=20)
    check("resuming the campaign makes it eligible again", con.execute(
        "SELECT state FROM leads WHERE id='LP'").fetchone()[0]
        == "FOLLOWUP_PENDING")

    # --------------------------------------------------------- 15.10 - 15.14
    print("\n--- Recovery after a stalled scheduler (15.10-15.14) ---")
    # Eighteen hours of missed ticks must not become eighteen hours of work
    # released at once.
    db2 = fresh_db(tmp)
    P.DB = db2
    con2 = sqlite3.connect(db2)
    con2.row_factory = sqlite3.Row
    con2.execute("INSERT INTO campaigns (id,name,status) VALUES ('c1','c','active')")
    for i in range(30):
        con2.execute("INSERT INTO leads (id,campaign_id,email,state)"
                     " VALUES (?, 'c1', ?, 'FOLLOWUP_WAITING')",
                     ("B%02d" % i, "b%02d@example.invalid" % i))
    con2.commit()
    with P.writing(con2):
        for i in range(30):
            F.schedule(con2, "B%02d" % i, "c1", 1, past)

    moved = echo_tick.tick(limit=20)
    n_pending = con2.execute("SELECT count(*) FROM leads"
                             " WHERE state='FOLLOWUP_PENDING'").fetchone()[0]
    check("a backlog is drained in bounded batches, not all at once",
          n_pending <= 20, "%d released on the first tick" % n_pending)
    check("  and the rest stay scheduled for the next tick",
          con2.execute("SELECT count(*) FROM followups WHERE status='scheduled'"
                       ).fetchone()[0] >= 10)
    check("  no lead is released twice",
          len({l.split()[1] for l in moved if l.startswith("due")}) == len(
              [l for l in moved if l.startswith("due")]))

    print("\n--- Scheduled timestamps are comparable with SQLite's clock ---")
    # This is the trap: due() is a string comparison, so a follow-up written
    # with an ISO "T" separator is never due on the same day it was scheduled.
    con.execute("INSERT INTO leads (id,campaign_id,email,state)"
                " VALUES ('LT','c1','t@example.invalid','FOLLOWUP_WAITING')")
    con.commit()
    stamped = F.next_due(con, "c1", 1)
    check("next_due produces a space-separated timestamp",
          stamped is None or "T" not in stamped, str(stamped))
    sqlite_now = con.execute("SELECT datetime('now')").fetchone()[0]
    check("  matching the format SQLite's datetime('now') returns",
          "T" not in sqlite_now, sqlite_now)
    overdue = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    with P.writing(con):
        F.schedule(con, "LT", "c1", 1, overdue)
    check("  a follow-up one minute overdue TODAY is seen as due",
          any(r["lead_id"] == "LT" for r in F.due(con, 50)),
          "same-day comparison, where the separator actually decides it")

    # ------------------------------------------------------------------ 15.15
    print("\n--- ORBIT notices a stalled scheduler (15.15) ---")
    import orbit
    now = datetime.datetime(2026, 9, 2, 8, 0, tzinfo=datetime.timezone.utc)
    store = os.path.join(tmp, "cron.json")
    orbit.CRON_JOBS_PATH = store

    fresh = (now - datetime.timedelta(minutes=2)).isoformat()
    stale = (now - datetime.timedelta(hours=18)).isoformat()
    json.dump([
        {"name": "maya-orchestrator", "id": "a", "schedule_display": "every 2m",
         "last_run_at": fresh, "last_status": "ok"},
        {"name": "leo-inbound", "id": "b", "schedule_display": "every 2m",
         "last_run_at": fresh, "last_status": "ok"},
        {"name": "echo-followups", "id": "c", "schedule_display": "every 2m",
         "last_run_at": stale, "last_status": "ok"},
        {"name": "supabase-lead-sync", "id": "d", "schedule_display": "every 2m",
         "last_run_at": None, "last_status": None},
    ], open(store, "w"))

    h = orbit.automation_health(now=now)
    check("a job that ran two minutes ago is healthy",
          not [j for j in h["jobs"] if j["name"] == "maya-orchestrator"][0]["stale"])
    check("a job silent for eighteen hours is reported stale",
          "echo-followups" in h["stale"], str(h["stale"]))
    check("a job that has NEVER run is called out separately",
          "supabase-lead-sync" in h["never_ran"], str(h["never_ran"]))
    check("  and the age is stated in minutes, not guessed",
          [j for j in h["jobs"] if j["name"] == "echo-followups"][0]["age_minutes"]
          == 18 * 60)

    json.dump([{"name": "leo-inbound", "id": "x", "last_run_at": fresh},
               {"name": "leo-inbound", "id": "y", "last_run_at": fresh}],
              open(store, "w"))
    check("duplicate job names are reported",
          orbit.automation_health(now=now)["duplicates"] == ["leo-inbound"])

    orbit.CRON_JOBS_PATH = os.path.join(tmp, "does-not-exist.json")
    check("an unreadable cron store is an error, not silent health",
          orbit.automation_health(now=now)["error"] is not None)

    print()
    print("=" * 72)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:")
        for f in FAILURES:
            print("  " + f)
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
