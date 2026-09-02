#!/usr/bin/env python3
"""Check the MailHub credentials this profile holds and record the result.

Run from each profile that holds a MailHub credential. Whichever kind it holds
is the column it fills in tenant_health; between them the profiles complete the
picture the tenant router reads. Safe to run as often as you like -- every probe
is a read or a deliberately malformed write that MailHub rejects before it
touches anything.

    HERMES_HOME=/opt/data/profiles/sentinel python3 tenant_health_tick.py
"""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def load_profile_env() -> str:
    """Read the profile's .env into os.environ without overriding what is set.

    The cron runner exports these already; reading the file too means the
    script behaves the same when invoked by hand for a spot check.
    """
    home = os.getenv("HERMES_HOME", "/opt/data")
    path = os.path.join(home, ".env")
    if not os.path.exists(path):
        return "(no %s)" % path
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
    return path


def main() -> int:
    src = load_profile_env()
    import pipeline as P
    import tenant_health
    import tenants

    print("profile env: %s" % src)
    results = []
    with P.connect() as con:
        with P.writing(con):
            results = tenant_health.check_all(con)
        print(tenants.describe(con))
        ready = [t["name"] for t in tenants.ready(con)]
    for r in results:
        caps = r.get("caps") or {}
        held = ",".join(k for k, v in sorted(caps.items()) if v) or "-"
        flags = " ".join("%s=%s" % (k, r[k]) for k in
                         ("queue_ok", "approve_ok", "leo_ok", "mailbox_ok")
                         if k in r)
        print("  %-22s %-34s %s" % (r["tenant"], "capabilities: " + held, flags))
    print("ready: %s" % (", ".join(ready) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
