#!/usr/bin/env python3
"""ECHO-style cron entrypoint for email verification.

Deterministic: selects leads needing a verdict, verifies them in batches
against the REST API, and writes the result back. No LLM decides whether an
address is deliverable.

Silent when nothing needed verifying — a job that speaks every tick trains you
to stop reading it.
"""
import os
import sys

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

import verification_worker as VW  # noqa: E402

res = VW.tick(limit=int(os.getenv("EMAIL_VERIFIER_SCAN_LIMIT", "200")))

parked = res.get("no_email_parked", 0)
restored = res.get("no_email_restored", 0)
if res["verified"] or res["unusable"] or res["errors"] or parked or restored:
    print("email-verifier: %d verified of %d considered in %dms"
          % (res["verified"], res["considered"], res["took_ms"]))
    print("  eligible=%d reject=%d hold=%d retry=%d unusable=%d"
          % (res["eligible"], res["reject"], res["hold"], res["retry"],
             res["unusable"]))
    if parked or restored:
        # Holding a lead out of the claim is a decision about it; say so.
        print("  no-email: %d held for manual contact, %d restored (address added)"
              % (parked, restored))
    for e in res["errors"][:8]:
        print("  error: %s" % e)
