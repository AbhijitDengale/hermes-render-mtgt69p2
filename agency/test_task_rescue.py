#!/usr/bin/env python3
"""A stage whose task finished without doing it must be offered again.

Kanban resolves an idempotency key to whatever task already holds it, whatever
state that task is in. So an agent that finishes its turn without calling
save_draft -- answering conversationally, or completing after a reclaim having
written nothing -- leaves a key that resolves to a finished task: every later
tick re-issues it, receives that task, spawns no worker, and the lead waits for
ever. Verified against the live board on 2026-09-04 by re-issuing the key for
L-fcc2be1364e512e7, which returned t_8adaf392 with status=done and created
nothing.

This closes that hole. It is a latent one: no lead was actually lost to it on
the day. Two things that LOOKED like it were measurement errors of mine, and
both are encoded below so they cannot be mistaken again.

  * Eight ARIA tasks sat in `running` for ten hours during the provider
    outage. They were healthy -- live workers, heartbeats every sixty seconds,
    crashing and being retried against a dead endpoint -- and all eight
    completed once the provider returned. Kanban's own detect_crashed_workers
    correctly left them alone, and a reaper judging them by started_at would
    have destroyed live work mid-generation.

  * A "stranded" count built from missing output alone reported sixty leads on
    a pipeline draining perfectly at five per stage per tick. A lead waiting
    its turn looks identical on that test. Strandedness needs the second half:
    the task for its CURRENT stage has stopped, so nothing is left to produce
    the output.

Nothing here spawns an agent, calls Kanban, or touches the network.
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_TMP = tempfile.mkdtemp(prefix="rescue-")
os.environ["AGENCY_DB"] = os.path.join(_TMP, "rescue.db")
os.environ["HERMES_HOME"] = _TMP

import pipeline as P        # noqa: E402
import orchestrator as O    # noqa: E402

P.DB = os.environ["AGENCY_DB"]

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-64s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-64s %s" % (name, detail))


def fresh_db():
    if os.path.exists(P.DB):
        os.remove(P.DB)
    con = sqlite3.connect(P.DB)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()


class FakeKanban:
    """Kanban's idempotency: one task per key, returned whatever its state."""

    def __init__(self, seed=None):
        self.tasks = dict(seed or {})     # key -> {"id","status"}
        self.created = []

    def __call__(self, key, profile, title, body):
        if key not in self.tasks:
            tid = "t_%02d" % (len(self.tasks) + 1)
            self.tasks[key] = {"id": tid, "status": "ready",
                               "assignee": profile}
            self.created.append(key)
        return dict(self.tasks[key])


def main() -> int:
    print("=" * 78)
    print("DEAD-END TASK RESCUE")
    print("=" * 78)
    real = O._create_task
    try:
        print("\n--- 1-2. A live task is never disturbed ---")
        for status in ("ready", "running", "in_progress"):
            fk = FakeKanban({"agency:L1:gen:1:copy:0": {"id": "t_live",
                                                        "status": status}})
            O._create_task = fk
            res = O.dispatch("L1", "aria", "t", "b", "copy:0", 1)
            check("1. a %-11s task is returned as-is, nothing created" % status,
                  res["id"] == "t_live" and fk.created == [], str(fk.created))
        fk = FakeKanban({"agency:L1:gen:1:copy:0": {"id": "t_live",
                                                    "status": "running"}})
        O._create_task = fk
        O.dispatch("L1", "aria", "t", "b", "copy:0", 1)
        check("2. a ten-hour running task with a live worker is not re-offered",
              fk.created == [],
              "the 8 ARIA tasks of 2026-09-04 were healthy, not zombies")

        print("\n--- 3-6. A finished task with no output is offered again ---")
        for status in ("done", "archived", "cancelled", "completed"):
            fk = FakeKanban({"agency:L1:gen:1:copy:0": {"id": "t_dead",
                                                        "status": status}})
            O._create_task = fk
            res = O.dispatch("L1", "aria", "t", "b", "copy:0", 1)
            check("3. a %-9s task yields a NEW task for the lead" % status,
                  res["id"] != "t_dead" and len(fk.created) == 1,
                  "new key %s" % (fk.created[0] if fk.created else "-"))

        fk = FakeKanban({"agency:L9:gen:1:research": {"id": "t_d",
                                                      "status": "done"}})
        O._create_task = fk
        O.dispatch("L9", "nova", "t", "b", "research", 1)
        check("4. a crashed NOVA stage becomes runnable again",
              fk.created == ["agency:L9:gen:1:research:rescue:1"],
              str(fk.created))
        fk = FakeKanban({"agency:L9:gen:1:qa:0": {"id": "t_d",
                                                  "status": "done"}})
        O._create_task = fk
        O.dispatch("L9", "sentinel", "t", "b", "qa:0", 1)
        check("5. a crashed SENTINEL stage becomes runnable again",
              len(fk.created) == 1, str(fk.created))
        check("6. and the rescue key derives from the original, not a new one",
              fk.created[0].startswith("agency:L9:gen:1:qa:0:"), fk.created[0])

        print("\n--- 7. Rescue is bounded ---")
        seeded = {"agency:L1:gen:1:copy:0": {"id": "t0", "status": "done"}}
        for i in range(1, 9):
            seeded["agency:L1:gen:1:copy:0:rescue:%d" % i] = {
                "id": "t%d" % i, "status": "done"}
        fk = FakeKanban(seeded)
        O._create_task = fk
        O.dispatch("L1", "aria", "t", "b", "copy:0", 1)
        check("7. a stage that keeps dying stops after MAX_TASK_RESCUES",
              fk.created == [] and O.MAX_TASK_RESCUES == 3,
              "%d attempts, no runaway task creation" % O.MAX_TASK_RESCUES)

        print("\n--- 8. The key is stable within one attempt ---")
        fk = FakeKanban()
        O._create_task = fk
        a = O.dispatch("L2", "aria", "t", "b", "copy:0", 1)
        b = O.dispatch("L2", "aria", "t", "b", "copy:0", 1)
        check("8. two ticks for the same live stage collapse onto one task",
              a["id"] == b["id"] and len(fk.created) == 1, str(fk.created))
        c = O.dispatch("L2", "aria", "t", "b", "copy:0", 2)
        check("   a new lifecycle generation gets its own task",
              c["id"] != a["id"], "%s vs %s" % (a["id"], c["id"]))
    finally:
        O._create_task = real

    print("\n--- 9-11. A rescue cannot duplicate work ---")
    fresh_db()
    with P.connect(P.DB) as con:
        with P.writing(con):
            con.execute("INSERT INTO campaigns (id, name, status) "
                        "VALUES ('C1','c','active')")
            con.execute("INSERT INTO leads (id, campaign_id, email,"
                        " business_name, state) VALUES"
                        " ('L-x','C1','a@b.com','Co','COPY_PENDING')")
        mid = P.save_draft(con, "L-x", "C1", 0, "s1", "b1")
        again = P.save_draft(con, "L-x", "C1", 0, "s2", "b2")
        n = con.execute("SELECT COUNT(*) c FROM messages WHERE lead_id='L-x'"
                        ).fetchone()["c"]
        check("9. a re-run replaces the draft rather than adding one",
              mid == again and n == 1, "%d message row(s)" % n)

        with P.writing(con):
            con.execute("UPDATE messages SET qa_status='approved',"
                        " approval_id='A1' WHERE id=?", (mid,))
        d = P.load_draft(con, "L-x", 0)
        check("   the verdict lives on the same row, so it cannot be doubled",
              d["qa_status"] == "approved" and d["approval_id"] == "A1")

        with P.writing(con):
            con.execute("UPDATE messages SET status='queued',"
                        " mailhub_queue_id='q-1' WHERE id=?", (mid,))
        try:
            P.save_draft(con, "L-x", "C1", 0, "s3", "b3")
            refused = False
        except P.TransitionError:
            refused = True
        check("10. a rescued agent cannot rewrite a message already queued",
              refused, "save_draft refuses once it is queued or sent")
        q = con.execute("SELECT mailhub_queue_id, status FROM messages"
                        " WHERE id=?", (mid,)).fetchone()
        check("11. and the queue id it already holds is untouched",
              q["mailhub_queue_id"] == "q-1" and q["status"] == "queued",
              "no second MailHub row can be created for this stage")

        print("\n--- 12. Completed work is repaired, never re-run ---")
        # dispatch_copy transitions on an existing draft instead of dispatching,
        # so a task that DID its work is reconciled rather than repeated.
        src = (HERE / "orchestrator.py").read_text(encoding="utf-8")
        body = src[src.index("def dispatch_copy"):src.index("def to_qa")]
        check("12. dispatch_copy returns on an existing draft before dispatching",
              body.index("load_draft") < body.index("dispatch("),
              "existing copy is promoted, not regenerated")
        qbody = src[src.index("def dispatch_qa"):src.index("def rewrite")]
        check("    dispatch_qa promotes an existing verdict before dispatching",
              qbody.index("qa_status") < qbody.index("dispatch("))

        print("\n--- 13. Ownership is never touched by task recovery ---")
        rescue = src[src.index("def dispatch("):src.index("def brief(")]
        check("13. dispatch changes no lead, tenant or sender state",
              not any(w in rescue for w in ("transition(", "tenant_user_id",
                                            "sender_account_id", "UPDATE ",
                                            "mailhub(")),
              "it only creates or reads kanban tasks")

        print("\n--- 14. Stranded leads are counted, not silently repaired ---")
        with P.writing(con):
            con.execute("INSERT INTO leads (id, campaign_id, email,"
                        " business_name, state) VALUES"
                        " ('L-y','C1','c@d.com','Co2','COPY_PENDING')")
        real_tasks = O._task_statuses_by_lead
        try:
            # Missing output alone is not a strand. A lead waiting its turn in
            # a backlog looks identical on that test alone, and counting those
            # reported sixty strands on a pipeline that was draining perfectly.
            O._task_statuses_by_lead = lambda ids: {}
            check("14. a lead with no task on the board yet is backlog",
                  "L-y" not in O.stranded_leads(con),
                  "the stage simply has not been reached")
            O._task_statuses_by_lead = lambda ids: {"L-y": [("copy:0", "running")]}
            check("    a lead whose task is still running is not stranded",
                  "L-y" not in O.stranded_leads(con))
            O._task_statuses_by_lead = lambda ids: {"L-y": [("copy:0", "done")]}
            check("    output missing AND every task stopped is a strand",
                  "L-y" in O.stranded_leads(con))
            O._task_statuses_by_lead = lambda ids: {
                "L-x": [("copy:0", "done")], "L-y": [("copy:0", "done")]}
            check("    a lead that HAS its output never counts",
                  "L-x" not in O.stranded_leads(con),
                  "L-x holds a queued message from the checks above")
            O._task_statuses_by_lead = lambda ids: {
                "L-y": [("research", "done")]}
            check("    a finished task from an earlier stage does not count",
                  "L-y" not in O.stranded_leads(con),
                  "a lead newly in QA_PENDING still carries a done copy task")
        finally:
            O._task_statuses_by_lead = real_tasks

    print("\n--- 15. Board health reads real liveness evidence ---")
    h = O.board_health()
    check("15. board_health returns counts even with no kanban database",
          isinstance(h, dict) and h["running"] == 0 and h["blocked"] == 0,
          str({k: v for k, v in h.items() if not isinstance(v, list)}))
    check("    a dead pid is reported false and a missing one is unknown",
          O._pid_alive(999999) is False and O._pid_alive(None) is None)
    check("    our own pid is alive",  O._pid_alive(os.getpid()) is True)

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
