#!/usr/bin/env python3
"""Poll every active tenant's inbox and hand what it finds to LEO.

Deterministic: no model decides anything here. The tick fetches, dedupes,
cancels follow-ups and dispatches classification work; whether a reply means
"interested" or "unsubscribe" is LEO's job, and it happens after the
protective half is already done.

Runs under the LEO profile. The profile's .env is loaded explicitly rather
than relying on the cron runner's environment, because the credential set is
the whole point: LEO holds read+suppress per tenant and no queue key, so a
tick that accidentally ran with MAYA's environment would be reading inboxes
with a credential that can also send.

Output is deliberately quiet. An empty stdout means "nothing happened", which
is the normal case and keeps the delivery channel silent; anything printed is
something a person would want to see.
"""
import os
import pathlib
import sys

LEO_PROFILE = os.getenv("LEO_PROFILE_DIR", "/opt/data/profiles/leo")
AGENCY = os.getenv("AGENCY_DIR", "/opt/data/agency")
sys.path.insert(0, AGENCY)


def load_env(path: str) -> bool:
    """Load a profile .env without overriding anything already set."""
    if not os.path.exists(path):
        return False
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
    return True


def main() -> int:
    env_path = os.path.join(LEO_PROFILE, ".env")
    if not load_env(env_path):
        print("leo-inbound: no LEO profile env at %s — refusing to poll with "
              "whatever credentials happen to be in scope" % env_path)
        return 1

    import pipeline as P
    import tenants
    import inbound_processor as IP

    pool = tenants.load()
    if not pool:
        print("leo-inbound: no MailHub tenant configured")
        return 1

    without = [t["name"] for t in pool if not t.get("leo")]
    if without:
        # Worth saying out loud: a tenant with no LEO credential has an inbox
        # nobody is reading, and its prospects can reply into silence.
        print("leo-inbound: no LEO credential for %s — those inboxes are NOT "
              "being read" % ", ".join(without))

    lines = IP.poll(limit=int(os.getenv("LEO_INBOUND_LIMIT", "25")))

    # Errors are always worth reporting; routine per-reply lines only when
    # something actually happened. Nothing to say means nothing printed.
    problems = [l for l in lines if "error" in l.lower() or "MailHub" in l]
    handled = [l for l in lines if l not in problems]

    if handled:
        print("leo-inbound: handled %d repl%s"
              % (len(handled), "y" if len(handled) == 1 else "ies"))
        for line in handled:
            print("  " + line)
    if problems:
        print("leo-inbound: %d tenant problem(s)" % len(problems))
        for line in problems:
            print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
