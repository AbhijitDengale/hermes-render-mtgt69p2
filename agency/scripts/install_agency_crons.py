#!/usr/bin/env python3
"""Ensure the deterministic agency crons exist, exactly once each.

Idempotent by name: re-running finds what is already there and leaves it
alone, so this belongs in a bootstrap sequence. It reports what it found
either way -- "already installed" and "just installed" are different facts to
someone reading a deploy log.

Only the two script-driven jobs this agency owns are managed here. The jobs
that were installed by hand earlier (maya-orchestrator, supabase-lead-sync,
review-alerts, orbit-daily) are left untouched; this never removes anything.

    python3 install_agency_crons.py          # install whatever is missing
    python3 install_agency_crons.py --check  # report only, change nothing
"""
import json
import os
import subprocess
import sys

JOBS = os.getenv("HERMES_CRON_JOBS", "/opt/data/cron/jobs.json")
HERMES = os.getenv("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")

WANTED = [
    {"name": "leo-inbound", "script": "leo_inbound_tick.py",
     "schedule": "every 2m",
     "why": "polls every tenant's inbox and cancels follow-ups on reply"},
    {"name": "echo-followups", "script": "echo_followups.py",
     "schedule": "every 2m",
     "why": "dispatches follow-ups that are already scheduled and due"},
]


def existing():
    if not os.path.exists(JOBS):
        return []
    try:
        j = json.load(open(JOBS, encoding="utf-8"))
    except Exception:
        return []
    jobs = j if isinstance(j, list) else j.get("jobs", j)
    seq = list(jobs.values()) if isinstance(jobs, dict) else jobs
    return [x for x in seq if isinstance(x, dict)]


def install(spec):
    cmd = [HERMES, "cron", "create", spec["schedule"], "--name", spec["name"],
           "--script", spec["script"], "--no-agent", "--deliver", "local"]
    env = dict(os.environ, HOME=os.getenv("HERMES_HOME", "/opt/data"))
    res = subprocess.run(cmd, capture_output=True, text=True, env=env,
                         cwd=env["HOME"])
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
    return res.returncode


def main() -> int:
    check_only = "--check" in sys.argv
    rc = 0
    for spec in WANTED:
        mine = [j for j in existing() if j.get("name") == spec["name"]]

        if len(mine) > 1:
            # Two jobs of the same name would double every tick. The dedupe
            # inside each job would stop duplicate work, but it is still wrong
            # and a person should choose which to remove.
            print("PROBLEM: %d jobs named %s — remove the duplicates by id: %s"
                  % (len(mine), spec["name"],
                     ", ".join(j.get("id", "?") for j in mine)))
            rc = 1
            continue

        if mine:
            j = mine[0]
            print("  %-16s present   id=%-14s %-12s script=%s"
                  % (spec["name"], j.get("id"), j.get("schedule_display"),
                     j.get("script")))
            continue

        if check_only:
            print("  %-16s MISSING   (%s)" % (spec["name"], spec["why"]))
            rc = 1
            continue

        if install(spec) != 0:
            print("  %-16s FAILED to install" % spec["name"])
            rc = 1
            continue

        after = [j for j in existing() if j.get("name") == spec["name"]]
        if len(after) != 1:
            print("  %-16s PROBLEM: expected one after install, found %d"
                  % (spec["name"], len(after)))
            rc = 1
            continue
        j = after[0]
        print("  %-16s INSTALLED id=%-14s %-12s script=%s no_agent=%s deliver=%s"
              % (spec["name"], j.get("id"), j.get("schedule_display"),
                 j.get("script"), j.get("no_agent"), j.get("deliver")))

    names = [j.get("name") for j in existing()]
    dupes = sorted({n for n in names if names.count(n) > 1})
    print()
    print("  %d cron job(s): %s" % (len(names), ", ".join(sorted(names))))
    if dupes:
        print("  DUPLICATE NAMES: %s" % ", ".join(dupes))
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
