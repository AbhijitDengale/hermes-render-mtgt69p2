#!/usr/bin/env python3
"""The verifier worker's policy verdict, consumed for role accounts only.

The worker now returns `policy` alongside its old `status`. With
DEEP_PROVIDER=none it returns `hold` for every domain-only address -- role
and named alike -- because no mailbox-level check exists to release anything.

Obeying that everywhere would stop all outreach: named addresses bounced 0 of
93 while role accounts bounced 24 of 291, so the named half of the list is not
the problem and holding it would be a self-inflicted outage. So the worker's
policy is authoritative for role accounts and ignored for named ones, which
works because verification_worker consults the role policy only when
role_account is the whole of the verifier's objection.

That is a TEMPORARY compatibility rule for DEEP_PROVIDER=none. When a deep
provider is enabled, revisit it deliberately -- the tests below are written so
that changing it makes them fail loudly rather than quietly.

Pure. No network, no verifier, no database.
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import email_verifier as EV       # noqa: E402
import role_account_policy as RP  # noqa: E402
import verification_worker as VW  # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-66s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-66s %s" % (name, detail))


def result(email, **kw):
    """What the worker returns today for a domain-only address."""
    r = {"email": email, "status": "risky", "score": 55,
         "reason": "role_account", "flags": ["role_account"],
         "deliverable": True, "mx_host": "mx.company.com",
         "did_you_mean": None, "cached": False, "took_ms": 5, "error": None,
         "verification_level": "domain", "mailbox_status": None,
         "provider": "none", "policy": "hold",
         "policy_reason": "mailbox verification unavailable",
         "policy_version": "verifier-2026-09-04"}
    r.update(kw)
    return r


def applied(email, **kw):
    lead = {"id": "L1", "email": email, "raw_data": {}}
    return VW.apply_result(lead, result(email, **kw))


def main() -> int:
    print("=" * 78)
    print("VERIFIER POLICY: authoritative for role accounts, not for named")
    print("=" * 78)

    print("\n--- 1-2. The compatibility rule, which is the whole point ---")
    # A healthy named address: the OLD contract still reports status=valid,
    # and the NEW policy field says hold because no mailbox check exists.
    # Those two disagreeing is the whole situation this rule exists for.
    named = applied("john.smith@company.com", status="valid", reason="",
                    flags=[])
    check("1. a named domain-only address is still admitted",
          named["decision"] == "eligible"
          and named["fields"]["email_verification_status"] == "valid",
          "verifier says policy=hold; we do not obey it for named addresses")
    check("   and it is admitted on the OLD contract, not the new one",
          named["record"]["status"] == "valid"
          and named["record"].get("verification_policy") == "hold",
          "the policy is recorded, and deliberately not acted on")
    role = applied("info@company.com")
    check("2. a role domain-only address is held",
          role["decision"] == "hold"
          and role["record"]["reason"] == RP.REASON_VERIFIER_POLICY,
          role["record"]["reason"])

    print("\n--- 3-6. Each policy value, for a role account ---")
    for policy, want_decision, want_status in (
            ("release", "eligible", "valid"),
            ("hold", "hold", "risky"),
            ("retry", "retry", "unknown"),
            ("reject", "reject", "invalid")):
        out = applied("info@company.com", policy=policy,
                      verification_level="mailbox",
                      mailbox_status="valid" if policy == "release" else None)
        check("%s. role policy=%-8s -> %s"
              % ({"release": 3, "hold": 4, "retry": 5, "reject": 6}[policy],
                 policy, want_decision),
              out["decision"] == want_decision
              and out["record"]["status"] == want_status,
              "%s / %s" % (out["decision"], out["record"]["status"]))

    print("\n--- 7-8. What always wins ---")
    lead = {"id": "L1", "email": "info@company.com", "raw_data": {},
            "bounced_at": "2026-09-04T00:00:00Z"}
    out = VW.apply_result(lead, result("info@company.com", policy="release",
                                       verification_level="mailbox",
                                       mailbox_status="valid"))
    check("7. a confirmed hard bounce beats a provider 'valid'",
          out["decision"] != "eligible",
          "%s -- a dead address is not revived by a later release"
          % out["decision"])
    lead = {"id": "L1", "email": "info@company.com", "raw_data": {},
            "unsubscribed_at": "2026-09-04T00:00:00Z"}
    out = VW.apply_result(lead, result("info@company.com", policy="release",
                                       verification_level="mailbox",
                                       mailbox_status="valid"))
    check("   an unsubscribe beats it too", out["decision"] != "eligible",
          out["decision"])
    # A sender-side block is a fact about our provider, not the recipient, and
    # nothing in this module may turn it into a mailbox verdict.
    src = (HERE / "role_account_policy.py").read_text(encoding="utf-8")
    check("8. a sender policy block cannot become mailbox_status=invalid",
          "sender_policy_block" not in src and "SENDER_POLICY_BLOCK" not in src,
          "the role policy has no notion of a sending failure at all")

    print("\n--- 9. Evidence does not survive an address change ---")
    prev = {"verified_email": "old@company.com", "status": "valid",
            "verification_level": "mailbox", "mailbox_status": "valid",
            "attempts": 3}
    check("9. a verdict for a different address is stale",
          EV.stale(prev, "new@company.com") is True)
    check("   and the same address is not",
          EV.stale(prev, "old@company.com") is False)
    fresh = applied("info@company.com")
    check("   a re-verified record carries only the new evidence",
          fresh["record"]["verified_email"] == "info@company.com"
          and fresh["record"]["attempts"] == 1,
          "the retry ladder restarts for the new address")

    print("\n--- 10-11. Catch-all is never confirmation ---")
    out = applied("info@company.com", policy="hold",
                  verification_level="mailbox", mailbox_status="catch_all")
    check("10. a catch-all role account stays held",
          out["decision"] == "hold", out["decision"])
    # Without the verifier's policy field, the raw mailbox evidence decides.
    v = RP.evaluate("info@company.com",
                    {"status": "risky", "reason": "role_account",
                     "flags": ["role_account"], "deliverable": True,
                     "mx_host": "mx.company.com", "verified_email": "x",
                     "verification_level": "mailbox",
                     "mailbox_status": "catch_all"})
    check("    and does so on the raw evidence when no policy is given",
          v["status"] == "risky" and v["reason"] == RP.REASON_CATCH_ALL,
          v["reason"])
    named_catch = applied("john.smith@company.com", status="valid", reason="",
                          flags=[], verification_level="mailbox",
                          mailbox_status="catch_all")
    check("11. a catch-all NAMED address keeps today's behaviour",
          named_catch["decision"] == "eligible",
          "unchanged while DEEP_PROVIDER=none; revisit with a deep provider")

    print("\n--- 16. The new evidence is persisted, and collides with nothing ---")
    rec = applied("info@company.com")["record"]
    for field in ("verification_level", "mailbox_status", "verification_provider",
                  "verification_policy", "verification_policy_reason",
                  "verification_policy_version", "verified_email",
                  "last_attempt_at"):
        check("16. %-30s persisted" % field, field in rec, repr(rec.get(field)))
    check("    the role-policy audit block is NOT overwritten by it",
          isinstance(rec.get("policy"), (dict, type(None))),
          "`policy` stays the audit trail; the worker's verdict is "
          "`verification_policy`")

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
