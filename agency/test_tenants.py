#!/usr/bin/env python3
"""Tenant routing: the same lead must always resolve to the same tenant, and a
missing approve credential must refuse rather than fall back."""
import os
import sys
import tempfile

import tenants

# Discovery reads this profile's .env as well as the inherited environment, so
# clearing MAILHUB_TENANT_* is not enough to reach a known-empty state: on the
# deployment host the real /opt/data/.env supplies nine live tenants and every
# "no configuration" assertion below fails against them. An empty HERMES_HOME
# gives the file half of discovery nothing to find.
_ISOLATED_HOME = tempfile.mkdtemp(prefix="tenants-test-")

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-52s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-52s %s" % (name, detail))


def clear():
    for k in list(os.environ):
        if k.startswith("MAILHUB_TENANT_") or k == "MAILHUB_API_TOKEN":
            del os.environ[k]
    os.environ["HERMES_HOME"] = _ISOLATED_HOME
    tenants._FILE_CACHE.update(path=None, mtime=None, values={})


def setup(n, approve_for=()):
    clear()
    for i in range(1, n + 1):
        os.environ["MAILHUB_TENANT_%d_NAME" % i] = "t%d" % i
        os.environ["MAILHUB_TENANT_%d_QUEUE" % i] = "q-token-%d" % i
        if i in approve_for:
            os.environ["MAILHUB_TENANT_%d_APPROVE" % i] = "a-token-%d" % i


def main():
    print("=" * 62)
    print("TENANT ROUTING")
    print("=" * 62)

    print("\n--- 1. Configuration ---")
    clear()
    check("no configuration means no tenants", tenants.load() == [])
    check("  and a lead resolves to nothing", tenants.for_lead("lead1") is None)
    check("  which is a refusal, not a silent skip",
          tenants.queue_token("lead1") is None
          and tenants.approve_token("lead1") is None)

    clear()
    os.environ["MAILHUB_API_TOKEN"] = "legacy-token"
    pool = tenants.load()
    check("a lone legacy token still works", len(pool) == 1
          and pool[0]["queue"] == "legacy-token")
    check("  every lead routes to it",
          all(tenants.queue_token("lead%d" % i) == "legacy-token"
              for i in range(20)))

    setup(4, approve_for=(1, 2, 3, 4))
    check("numbered tenants are discovered", len(tenants.load()) == 4)
    check("  and named tenants win over the legacy token",
          "legacy" not in [t["name"] for t in tenants.load()])
    setup(4, approve_for=(1, 2, 3, 4))
    os.environ["MAILHUB_TENANT_7_QUEUE"] = "q-token-7"
    check("  a gap in the numbering does not truncate the pool",
          len(tenants.load()) == 5,
          "found %d" % len(tenants.load()))

    print("\n--- 2. The same lead always picks the same tenant ---")
    # SENTINEL approves in one process and MAYA queues in another, minutes
    # apart. If they disagreed, the approval would be filed where the send
    # path does not look and the message would never go out.
    setup(5, approve_for=(1, 2, 3, 4, 5))
    for lead in ("lead-abc", "lead-def", "lead-xyz", "L-0001"):
        first = tenants.for_lead(lead)["name"]
        check("%-12s is stable across 50 lookups" % lead,
              all(tenants.for_lead(lead)["name"] == first for _ in range(50)),
              first)

    setup(5, approve_for=(1, 2, 3, 4, 5))
    before = tenants.for_lead("lead-abc")["name"]
    setup(5, approve_for=(1, 2, 3, 4, 5))   # simulate a process restart
    check("  and survives a restart", tenants.for_lead("lead-abc")["name"] == before,
          before)

    check("  queue and approve come from the SAME tenant",
          all(tenants.queue_token("l%d" % i)[-1] == tenants.approve_token("l%d" % i)[-1]
              for i in range(40)))

    print("\n--- 3. Leads spread across the pool ---")
    setup(5, approve_for=(1, 2, 3, 4, 5))
    counts = {}
    for i in range(1000):
        n = tenants.for_lead("lead-%d" % i)["name"]
        counts[n] = counts.get(n, 0) + 1
    check("all five tenants are used", len(counts) == 5, str(sorted(counts.items())))
    lo, hi = min(counts.values()), max(counts.values())
    check("  spread is even enough for capacity planning", hi - lo < 120,
          "min=%d max=%d over 1000 leads" % (lo, hi))

    print("\n--- 4. A missing approve credential refuses ---")
    # This is the case that matters: the four supplied sender keys carry
    # queue but not approve. Falling back to another tenant's approve key
    # would file the approval where MailHub will not match it.
    setup(3, approve_for=(1,))
    withq = [t for t in tenants.load() if t["queue"]]
    check("every tenant can queue", len(withq) == 3)
    noapprove = [t["name"] for t in tenants.load() if not t["approve"]]
    check("  but two cannot approve", len(noapprove) == 2, str(noapprove))

    refused = 0
    for i in range(60):
        lead = "lead-%d" % i
        if tenants.approve_token(lead) is None:
            refused += 1
            check_ok = tenants.queue_token(lead) is not None
            if not check_ok:
                check("a tenant without approve still has queue", False)
                break
    check("leads on an unapprovable tenant are refused, not reassigned",
          refused > 0, "%d of 60 refused" % refused)

    check("  approve_token never borrows another tenant's key",
          all(tenants.approve_token("lead-%d" % i) in (None, "a-token-1")
              for i in range(60)))

    print("\n--- 5. describe() is safe to log ---")
    setup(3, approve_for=(1,))
    d = tenants.describe()
    check("no key material in the summary",
          "q-token" not in d and "a-token" not in d, d)
    # describe() lists the capabilities each tenant actually holds, so a
    # tenant missing its approve key shows "holds=q" rather than "holds=qa".
    check("  but it does say which tenant cannot approve",
          "t1(user=None,holds=qa" in d and "t2(user=None,holds=q," in d, d)

    print()
    print("=" * 62)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:")
        for f in FAILURES:
            print("  " + f)
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
