#!/usr/bin/env python3
"""Which role addresses outreach may go to.

A verifier reporting `role_account` states a fact about the local part, not a
defect. For B2B outreach to small businesses the published `info@` is often
the only address there is, and treating it as a fault held 431 leads with
valid syntax, valid MX and a domain that accepts mail out of the pipeline.

These tests pin the policy that replaces that, and -- more importantly -- pin
everything the policy is NOT allowed to loosen.

Pure: no network, no database, nothing sent.
"""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import email_verifier as EV        # noqa: E402
import role_account_policy as RAP  # noqa: E402
import verification_worker as VW   # noqa: E402

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


def clean(email="info@company.com", **kw):
    """A role address that is clean on every axis INCLUDING the mailbox.

    Mailbox evidence is part of "clean" as of 2026-09-04. Before that, this
    fixture stopped at the domain -- deliverable, an MX host -- and the policy
    released addresses on that basis; 291 such sends produced 24 failures
    against 0 for named addresses. A domain answering says nothing about
    whether `info@` exists behind it, so the fixture now carries the answer
    to the question that actually matters. `domain_only()` keeps the old
    shape for the tests that assert it is no longer enough.
    """
    rec = {"status": "risky", "decision": "hold", "deliverable": True,
           "score": 55, "reason": "role_account", "flags": ["role_account"],
           "did_you_mean": None, "mx_host": "mx.company.com",
           "verified_email": email, "attempts": 1,
           "verification_level": "mailbox", "mailbox_status": "valid"}
    rec.update(kw)
    return rec


def domain_only(email="info@company.com", **kw):
    """What every record written before 2026-09-04 carries: the domain was
    checked and the mailbox was not."""
    rec = clean(email, **kw)
    rec.pop("verification_level", None)
    rec.pop("mailbox_status", None)
    return rec


def verdict(email, rec=None, **history):
    return RAP.evaluate(email, rec if rec is not None else clean(email), **history)


def main() -> int:
    print("=" * 78)
    print("ROLE-ACCOUNT POLICY")
    print("=" * 78)

    print("\n--- 1. Approved B2B role accounts become valid ---")
    for addr in ("info@company.com", "hello@company.com", "contact@company.com",
                 "sales@company.com", "enquiries@company.com",
                 "office@company.com", "inquiries@company.com",
                 "business@company.com", "team@company.com"):
        v = verdict(addr)
        check("%-26s -> valid" % addr,
              v["status"] == "valid" and v["reason"] == "role_account_allowed"
              and v["eligible"], "%s / %s" % (v["status"], v["reason"]))
    v = verdict("info.dubai@company.com")
    check("a suffixed role address is the same mailbox",
          v["status"] == "valid", "%s" % v["status"])
    check("  but a named person is not treated as a role account",
          RAP.tier("sales-director@company.com") == RAP.NOT_ROLE
          and RAP.tier("john.smith@company.com") == RAP.NOT_ROLE)
    check("  and 'information@' is not silently matched as 'info'",
          RAP.tier("information@company.com") == RAP.NOT_ROLE,
          RAP.tier("information@company.com"))

    print("\n--- 2. Internal functions stay held ---")
    for addr in ("admin@company.com", "support@company.com", "billing@company.com",
                 "accounts@company.com", "finance@company.com",
                 "security@company.com", "webmaster@company.com"):
        v = verdict(addr)
        check("%-26s -> risky" % addr,
              v["status"] == "risky" and not v["eligible"]
              and v["reason"] == "role_account_restricted", v["status"])

    print("\n--- 3. Automated and recruitment addresses are refused ---")
    for addr in ("abuse@company.com", "postmaster@company.com",
                 "mailer-daemon@company.com", "noreply@company.com",
                 "no-reply@company.com", "jobs@company.com",
                 "careers@company.com", "recruitment@company.com",
                 "hr@company.com"):
        v = verdict(addr)
        check("%-26s -> rejected" % addr,
              v["status"] == "invalid" and not v["eligible"]
              and v["reason"] == "role_account_excluded", v["status"])
    check("  none of them is ever eligible, whatever the evidence says",
          all(not verdict(a, clean(a, score=100))["eligible"]
              for a in ("abuse@x.com", "jobs@x.com", "postmaster@x.com")))

    print("\n--- 4. A role account on a free provider stays under review ---")
    v = verdict("info.business@gmail.com")
    check("info.business@gmail.com -> risky, not promoted",
          v["status"] == "risky" and not v["eligible"]
          and v["reason"] == "role_account_free_provider", v["reason"])
    check("  the reason names the free provider, not a made-up defect",
          "free provider" in " ".join(v["blockers"]).lower(), str(v["blockers"]))
    for d in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"):
        check("  info@%-14s stays risky" % d,
              verdict("info@" + d)["status"] == "risky")
    check("  but the same local part on a company domain is fine",
          verdict("info@realcompany.ae")["status"] == "valid")

    print("\n--- 5. Every other verification rule still outranks this ---")
    cases = [
        ("a disposable domain", clean(flags=["role_account", "disposable"])),
        ("a typo suggestion", clean(did_you_mean="info@company.com")),
        ("a scraper artifact", clean(flags=["role_account", "scraper_artifact"])),
        ("a suspicious local part", clean(flags=["role_account", "suspicious_local_part"])),
        ("a catch-all domain", clean(flags=["role_account", "catch_all"])),
        ("no MX host", clean(mx_host=None)),
        ("a domain that does not accept mail", clean(deliverable=False)),
    ]
    for label, rec in cases:
        v = RAP.evaluate("info@company.com", rec)
        check("%-32s blocks promotion" % label,
              not v["eligible"] and v["status"] == "risky", str(v["blockers"])[:46])
    v = RAP.evaluate("-info@company.com", clean("-info@company.com"))
    check("a malformed local part is never promoted",
          not v["eligible"], str(v["blockers"])[:60])
    check("  and syntax is judged, not assumed",
          RAP.technical_checks(clean(), "info@@company.com")["syntax_valid"] is False)

    print("\n--- 6. History is decisive and comes first ---")
    check("a previously bounced address is never re-promoted",
          not verdict("info@company.com", bounced=True)["eligible"],
          str(verdict("info@company.com", bounced=True)["blockers"]))
    check("  nor a suppressed one",
          not verdict("info@company.com", suppressed=True)["eligible"])
    check("  nor one that unsubscribed",
          not verdict("info@company.com", unsubscribed=True)["eligible"])
    check("  and the blocker says which it was",
          "previously bounced" in verdict("info@x.com", bounced=True)["blockers"])

    print("\n--- 7. Scope: only role_account is reinterpreted ---")
    check("a row whose sole objection is role_account is in scope",
          RAP.sole_reason_is_role(clean()))
    check("  role_account + free_provider is still in scope",
          RAP.sole_reason_is_role(clean(flags=["role_account", "free_provider"])))
    for extra in ("disposable", "catch_all", "scraper_artifact", "typo"):
        check("  role_account + %-16s is OUT of scope" % extra,
              not RAP.sole_reason_is_role(clean(flags=["role_account", extra])))
    check("  a different reason entirely is out of scope",
          not RAP.sole_reason_is_role(clean(reason="disposable", flags=[])))
    check("  a non-role address is left to the verifier",
          verdict("john@company.com")["status"] is None)

    print("\n--- 8. The categories used to report before writing ---")
    check("A: approved role only",
          RAP.categorise("info@company.com", clean()) == "A")
    check("B: role on a free provider",
          RAP.categorise("info@gmail.com", clean("info@gmail.com")) == "B")
    check("C: restricted role", RAP.categorise("admin@company.com", clean()) == "C")
    check("C: excluded role", RAP.categorise("jobs@company.com", clean()) == "C")
    check("D: carries another risk flag",
          RAP.categorise("info@company.com",
                         clean(flags=["role_account", "disposable"])) == "D")
    check("D: clean role account that has bounced",
          RAP.categorise("info@company.com", clean(), bounced=True) == "D")

    print("\n--- 9. Audit history is kept, not overwritten ---")
    rec = clean()
    v = verdict("info@company.com")
    entry = RAP.audit_entry(rec, v, "2026-09-03T17:00:00+00:00")
    check("it records what the verdict was before",
          entry["previous_verification_status"] == "risky"
          and entry["previous_reason"] == "role_account")
    check("  and what it became, and why",
          entry["new_verification_status"] == "valid"
          and entry["new_reason"] == "role_account_allowed")
    check("  with the policy version and the time",
          entry["policy_version"] == RAP.POLICY_VERSION
          and entry["re_evaluated_at"].startswith("2026-09-03"))
    check("  the verifier's own evidence is not modified",
          rec["status"] == "risky" and rec["reason"] == "role_account"
          and rec["score"] == 55 and rec["mx_host"] == "mx.company.com")

    print("\n--- 10. The worker writes the promotion through correctly ---")
    lead = {"id": "L1", "email": "info@company.com", "raw_data": {}}
    # Mailbox-confirmed, because domain-level evidence no longer releases a
    # role address -- see 10b. A verifier result without these two fields is
    # the pre-2026-09-04 shape and is held instead.
    result = {"email": "info@company.com", "status": "risky", "score": 55,
              "reason": "role_account", "flags": ["role_account"],
              "deliverable": True, "mx_host": "mx.company.com",
              "verification_level": "mailbox", "mailbox_status": "valid",
              "did_you_mean": None, "cached": False, "took_ms": 5, "error": None}
    out = VW.apply_result(lead, result)
    check("an approved role account is written as valid and eligible",
          out["fields"]["email_verification_status"] == "valid"
          and out["fields"]["email_verified"] is True
          and out["decision"] == "eligible", str(out["decision"]))
    check("  the stored reason says why it was allowed",
          out["record"]["reason"] == "role_account_allowed")
    check("  the audit block travels with it",
          out["record"]["policy"]["previous_verification_status"] == "risky"
          and out["record"]["policy"]["policy_version"] == RAP.POLICY_VERSION)
    check("  and status/hermes_status are NOT touched by a promotion",
          "status" not in out["fields"] and "hermes_status" not in out["fields"],
          str(sorted(out["fields"])))

    restricted = dict(result, email="admin@company.com")
    out = VW.apply_result({"id": "L2", "email": "admin@company.com", "raw_data": {}},
                          restricted)
    check("a restricted role account is still held",
          out["fields"]["email_verification_status"] == "risky"
          and out["decision"] == "hold" and out["fields"]["status"] == "hold")
    excluded = dict(result, email="jobs@company.com")
    out = VW.apply_result({"id": "L3", "email": "jobs@company.com", "raw_data": {}},
                          excluded)
    check("an excluded role account is rejected",
          out["fields"]["email_verification_status"] == "invalid"
          and out["decision"] == "reject" and out["fields"]["status"] == "rejected")

    print(chr(10) + "--- 10b. Domain-level evidence is no longer enough ---")
    v = RAP.evaluate("info@company.com", domain_only())
    check("a role address checked only at the domain is held",
          v["status"] == "risky" and v["reason"] == RAP.REASON_NO_MAILBOX_PROOF
          and not v["eligible"], "%s / %s" % (v["status"], v["reason"]))
    check("  and it says why, so the hold can be lifted deliberately",
          any("mailbox" in b for b in v["blockers"]), str(v["blockers"]))
    v = RAP.evaluate("info@company.com", clean())
    check("a mailbox-confirmed role address may become valid",
          v["status"] == "valid" and v["eligible"], v["status"])
    v = RAP.evaluate("info@company.com", clean(mailbox_status="catch_all"))
    check("a catch-all domain stays risky: acceptance proves nothing",
          v["status"] == "risky" and v["reason"] == RAP.REASON_CATCH_ALL,
          "%s / %s" % (v["status"], v["reason"]))
    v = RAP.evaluate("info@company.com", clean(mailbox_status="invalid"))
    check("a mailbox proven absent is rejected outright",
          v["status"] == "invalid" and not v["eligible"], v["status"])
    v = RAP.evaluate("info@company.com", clean(mailbox_status="unknown"))
    check("an inconclusive mailbox check holds rather than releases",
          v["status"] == "risky" and not v["eligible"], v["status"])
    v = RAP.evaluate("info@gmail.com", clean("info@gmail.com"))
    check("  a free provider is still refused even when mailbox-confirmed",
          v["status"] == "risky" and v["reason"] == RAP.REASON_FREE, v["reason"])
    v = RAP.evaluate("nadim@company.com", clean("nadim@company.com"))
    check("a named address is untouched by any of this",
          v["status"] is None and v["tier"] == RAP.NOT_ROLE,
          "policy has nothing to say about non-role addresses")

    print("\n--- 11. Nothing unrelated moved ---")
    check("the admission map is unchanged",
          EV.ADMISSION == {"valid": "eligible", "invalid": "reject",
                           "risky": "hold", "unknown": "retry"})
    check("the unknown retry ladder is unchanged",
          EV.RETRY_BACKOFF_MINUTES == [15, 60, 6 * 60, 24 * 60, 72 * 60])
    for status, expect in (("valid", "eligible"), ("invalid", "reject"),
                           ("unknown", "retry")):
        r = dict(result, status=status, reason="mx_ok", flags=[])
        out = VW.apply_result({"id": "L", "email": "a@b.com", "raw_data": {}},
                              dict(r, email="a@b.com"))
        check("  a %-8s verdict is still %s" % (status, expect),
              out["decision"] == expect, out["decision"])
    src = (HERE / "role_account_policy.py").read_text(encoding="utf-8")
    check("the policy module performs no I/O at all",
          not any(w in src for w in ("urllib", "sqlite3", "requests", "open(")))
    check("the claim contract is untouched",
          VW.claimable_filter() == ("status=eq.ready&hermes_status=eq.not_imported"
                                    "&email_verification_status=eq.valid"
                                    "&email_verified=is.true"))
    check("  so a promoted lead becomes claimable by the same rule as any other",
          verdict("info@company.com")["status"] == "valid")

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
