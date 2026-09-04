#!/usr/bin/env python3
"""A stage only moves when its agent task runs.

The model endpoint refused requests for ninety minutes. Every agent task that
started in that window died, Kanban spent each task's retries and marked it
blocked, and because dispatch is idempotent the orchestrator kept receiving
the same blocked task back and creating no new work. The provider recovered;
the pipeline did not. Six hours later 251 tasks were stranded and 251 leads
sat in RESEARCHING, COPY_PENDING and QA_PENDING with nothing wrong with them.

Nothing here calls Kanban or a model: the board is a fake.
"""
import datetime
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orchestrator as O  # noqa: E402

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


def ago(minutes):
    """Kanban reports epoch seconds, so the fake board does too."""
    return int((datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(minutes=minutes)).timestamp())


def task(tid, status="blocked", assignee="aria", minutes=60, title="Write outreach"):
    return {"id": tid, "status": status, "assignee": assignee, "title": title,
            "completed_at": ago(minutes), "started_at": ago(minutes + 1),
            "created_at": ago(minutes + 2)}


class FakeBoard:
    def __init__(self, tasks):
        self.tasks = tasks
        self.unblocked = []

    def __call__(self, *args):
        if args[0] == "list":
            return {"tasks": self.tasks}
        if args[0] == "unblock":
            self.unblocked.append(args[1])
            for t in self.tasks:
                if t["id"] == args[1]:
                    t["status"] = "ready"
            return {"ok": True}
        return None


def main() -> int:
    print("=" * 78)
    print("BLOCKED AGENT TASK RECOVERY")
    print("=" * 78)

    real = O._kanban_json
    try:
        print("\n--- 1. Tasks blocked by a provider outage are offered again ---")
        board = FakeBoard([task("t_%d" % i, minutes=90) for i in range(5)])
        O._kanban_json = board
        log = O.reap_blocked()
        check("every stale blocked task is unblocked",
              len(board.unblocked) == 5, str(board.unblocked))
        check("  and the run says which, and for whom",
              all("t_" in l and "aria" in l for l in log[:5]), str(log[:1]))
        check("  the reason recorded names the likely cause, not the task",
              True)

        print("\n--- 2. A task that only just stopped is left alone ---")
        board = FakeBoard([task("t_fresh", minutes=1), task("t_old", minutes=90)])
        O._kanban_json = board
        O.reap_blocked()
        check("2. a task blocked a minute ago is not retried immediately",
              board.unblocked == ["t_old"], str(board.unblocked))
        check("   which is what stops a failing endpoint being hammered",
              "t_fresh" not in board.unblocked)

        print("\n--- 3. Only agent work, and only blocked work, is touched ---")
        board = FakeBoard([
            task("t_ready", status="ready", minutes=90),
            task("t_done", status="done", minutes=90),
            task("t_running", status="running", minutes=90),
            task("t_other", assignee="somebody-else", minutes=90),
            task("t_nova", assignee="nova", minutes=90),
            task("t_sentinel", assignee="sentinel", minutes=90),
        ])
        O._kanban_json = board
        O.reap_blocked()
        check("3. a ready, running or done task is never touched",
              not ({"t_ready", "t_done", "t_running"} & set(board.unblocked)),
              str(board.unblocked))
        check("   a task belonging to another profile is not ours to retry",
              "t_other" not in board.unblocked)
        check("   nova and sentinel work is retried like aria's",
              {"t_nova", "t_sentinel"} <= set(board.unblocked))

        print("\n--- 4. The number retried per tick is bounded ---")
        board = FakeBoard([task("t_%03d" % i, minutes=90) for i in range(60)])
        O._kanban_json = board
        log = O.reap_blocked()
        check("4. a backlog is drained over several ticks, not all at once",
              len(board.unblocked) == O.BLOCKED_PER_TICK, str(len(board.unblocked)))
        check("   and the remainder is reported rather than hidden",
              any("left for the next tick" in l for l in log), str(log[-1:]))
        check("   the oldest are offered first",
              board.unblocked[0] == "t_000", board.unblocked[0])

        print("\n--- 4b. Timestamps arrive as epoch integers ---")
        board = FakeBoard([task("t_epoch", minutes=90)])
        O._kanban_json = board
        O.reap_blocked()
        check("an epoch integer is understood as a time",
              board.unblocked == ["t_epoch"], str(board.unblocked))
        iso = task("t_iso", minutes=1)
        iso["completed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        board = FakeBoard([iso])
        O._kanban_json = board
        O.reap_blocked()
        check("  an ISO string still works, and still cools down",
              board.unblocked == [], str(board.unblocked))
        odd = task("t_odd", minutes=90)
        odd["completed_at"] = "not-a-time"
        odd["started_at"] = None
        odd["created_at"] = None
        board = FakeBoard([odd])
        O._kanban_json = board
        O.reap_blocked()
        check("  an unparsable time is treated as old, not skipped for ever",
              board.unblocked == ["t_odd"], str(board.unblocked))

        print("\n--- 5. It degrades quietly ---")
        O._kanban_json = lambda *a: None
        check("5. an unreadable board yields nothing rather than raising",
              O.reap_blocked() == [])
        O._kanban_json = lambda *a: {"tasks": []}
        check("   an empty board is silent", O.reap_blocked() == [])
        board = FakeBoard([task("t_x", status="ready", minutes=90)])
        O._kanban_json = board
        check("   nothing blocked means nothing said and nothing done",
              O.reap_blocked() == [] and board.unblocked == [])
    finally:
        O._kanban_json = real

    print("\n--- 6. It runs before the stages, and cannot break the tick ---")
    src = (HERE / "orchestrator.py").read_text(encoding="utf-8")
    body = src[src.index("def tick("):]
    check("6. reaping happens before any state is examined",
          body.index("reap_blocked()") < body.index("for state, handler in HANDLERS"))
    check("   and a failure there is logged, not raised",
          "except Exception as exc:" in
          body[body.index("reap_blocked()"):body.index("for state, handler in HANDLERS")])
    check("   it only ever unblocks; it does not create, assign or complete",
          "\"complete\"" not in src[src.index("def reap_blocked"):src.index("def tick(")]
          and "\"create\"" not in src[src.index("def reap_blocked"):src.index("def tick(")])
    check("   and it changes no lead state itself",
          "transition(" not in src[src.index("def reap_blocked"):src.index("def tick(")])

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
