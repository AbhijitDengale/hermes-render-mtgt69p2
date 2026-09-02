#!/usr/bin/env python3
"""Install the leo-inbound cron, exactly once.

Idempotent by name: re-running finds the existing job and leaves it alone, so
this is safe to put in a bootstrap sequence. It reports what it found either
way rather than staying silent, because "already installed" and "just
installed" are different facts to a person reading a deploy log.

    python3 install_leo_inbound_cron.py          # install if missing
    python3 install_leo_inbound_cron.py --check  # report only, change nothing
"""
import json
import os
import subprocess
import sys

JOBS = os.getenv("HERMES_CRON_JOBS", "/opt/data/cron/jobs.json")
HERMES = os.getenv("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
NAME = "leo-inbound"
SCRIPT = "leo_inbound_tick.py"
SCHEDULE = "every 2m"


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


def main() -> int:
    check_only = "--check" in sys.argv
    jobs = existing()
    mine = [j for j in jobs if j.get("name") == NAME]

    if len(mine) > 1:
        # Two jobs of the same name would poll every inbox twice a tick. The
        # dedupe would stop double classification, but it is still wrong and
        # a person should decide which to remove.
        print("PROBLEM: %d jobs named %s — remove the duplicates by id: %s"
              % (len(mine), NAME, ", ".join(j.get("id", "?") for j in mine)))
        return 1

    if mine:
        j = mine[0]
        print("%s already installed (id=%s, %s, script=%s, no_agent=%s) — "
              "leaving it alone" % (NAME, j.get("id"), j.get("schedule_display"),
                                    j.get("script"), j.get("no_agent")))
        return 0

    if check_only:
        print("%s is NOT installed" % NAME)
        return 1

    cmd = [HERMES, "cron", "create", SCHEDULE, "--name", NAME,
           "--script", SCRIPT, "--no-agent", "--deliver", "local"]
    env = dict(os.environ, HOME=os.getenv("HERMES_HOME", "/opt/data"))
    res = subprocess.run(cmd, capture_output=True, text=True, env=env,
                         cwd=env["HOME"])
    sys.stdout.write(res.stdout)
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        return res.returncode

    after = [j for j in existing() if j.get("name") == NAME]
    if len(after) != 1:
        print("PROBLEM: expected exactly one %s after install, found %d"
              % (NAME, len(after)))
        return 1
    j = after[0]
    print("installed %s id=%s schedule=%s script=%s no_agent=%s deliver=%s"
          % (NAME, j.get("id"), j.get("schedule_display"), j.get("script"),
             j.get("no_agent"), j.get("deliver")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
