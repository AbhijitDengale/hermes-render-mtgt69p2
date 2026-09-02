#!/usr/bin/env python3
"""The email verification gate: policy, persistence, and admission.

Fixtures mirror what the live service actually returned during the probe
(live/verifier_probe.py), including the cases that surprised us -- gmial.com
came back `unknown` with a typo suggestion rather than `risky`, and
admin@example.com came back `invalid` because that domain publishes a null MX.
Policy is asserted against those real shapes rather than against the shapes we
assumed before looking.
"""
import datetime
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import email_verifier as EV        # noqa: E402
import verification_worker as VW   # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []
NOW = datetime.datetime(2026, 9, 2, 12, 0, tzinfo=datetime.timezone.utc)


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-58s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-58s %s" % (name, detail))


def result(email, status, **kw):
    base = {"email": email, "status": status, "deliverable": None,
            "score": None, "reason": None, "flags": [], "did_you_mean": None,
            "mx_host": None, "cached": False, "took_ms": 10, "error": None}
    base.update(kw)
    return base


def lead(email, **kw):
    d = {"id": "L1", "email": email, "status": "ready", "is_active": True,
         "hermes_status": "not_imported", "email_verification_status": None,
         "email_verified": None, "raw_data": {}}
    d.update(kw)
    return d


def verified_lead(email, status="valid", verified_email=None, **kw):
    rec = {"status": status, "decision": EV.ADMISSION[status],
           "verified_email": EV.normalise(verified_email or email),
           "attempts": 1, "next_retry_at": None}
    return lead(email, email_verification_status=status,
                email_verified=(status == "valid"),
                raw_data={VW.RAW_KEY: rec}, **kw)


def main():
    print("=" * 74)
    print("EMAIL VERIFICATION GATE")
    print("=" * 74)

    # ------------------------------------------------------------------ 1-5
    print("\n--- Status maps to one admission decision (31.1-31.5) ---")
    for status, expect in (("valid", "eligible"), ("invalid", "reject"),
                           ("risky", "hold"), ("unknown", "retry")):
        check("%-8s -> %s" % (status, expect),
              EV.admission(result("a@b.com", status)) == expect)
    check("an unrecognised status is treated as unknown, never as sendable",
          EV.admission({"status": "totally-new"}) == "retry")
    check("  and normalise_result rewrites it rather than passing it through",
          EV.normalise_result({"status": "totally-new"}, "a@b.com")["status"]
          == "unknown")

    # unknown must never silently become invalid
    r = result("a@b.com", "unknown", reason="dns_error:server_failure")
    applied = VW.apply_result(lead("a@b.com"), r, NOW)
    check("unknown never becomes invalid",
          applied["record"]["status"] == "unknown"
          and applied["fields"]["email_verification_status"] == "unknown")
    check("  and the lead is not rejected", "status" not in applied["fields"],
          str(applied["fields"].get("status")))
    check("  it is scheduled for another attempt",
          applied["record"]["next_retry_at"] is not None)

    print("\n--- Retry ladder, then a human (31.14) ---")
    check("backoff is 15m, 1h, 6h, 24h, 72h",
          [EV.next_retry_minutes(i) for i in range(1, 6)]
          == [15, 60, 360, 1440, 4320])
    check("  the ladder ends rather than repeating",
          EV.next_retry_minutes(6) is None)
    exhausted_lead = lead("a@b.com", raw_data={VW.RAW_KEY: {
        "status": "unknown", "verified_email": "a@b.com", "attempts": 5}})
    applied = VW.apply_result(exhausted_lead, r, NOW)
    check("an exhausted unknown is held for a human",
          applied["fields"]["status"] == "hold")
    check("  and is STILL unknown, not invalid",
          applied["fields"]["email_verification_status"] == "unknown",
          applied["fields"]["email_verification_status"])
    check("  with no further retry scheduled",
          applied["record"]["next_retry_at"] is None
          and applied["record"].get("exhausted"))

    # ------------------------------------------------------------------ 6
    print("\n--- A typo suggestion is evidence, not an edit (31.6) ---")
    # Exactly what the live service returned for gmial.com.
    typo = result("someone@gmial.com", "unknown", score=50,
                  reason="dns_error:server_failure", flags=["possible_typo"],
                  did_you_mean="someone@gmail.com")
    l = lead("someone@gmial.com")
    applied = VW.apply_result(l, typo, NOW)
    check("the suggestion is stored",
          applied["record"]["did_you_mean"] == "someone@gmail.com")
    check("  the lead's own address is NOT rewritten",
          "email" not in applied["fields"], str(applied["fields"].keys()))
    check("  and the verified address is the one actually checked",
          applied["record"]["verified_email"] == "someone@gmial.com")

    # ------------------------------------------------------------------ 7
    print("\n--- A changed address invalidates its old verdict (31.7) ---")
    ok, why = VW.claim_guard(verified_lead("john@company.com"))
    check("a verified-valid lead is claimable", ok, why)

    moved = verified_lead("john2@company.com", verified_email="john@company.com")
    ok, why = VW.claim_guard(moved)
    check("the same verdict does NOT authorise a different address", not ok, why)
    check("  and the worker treats it as needing verification",
          VW.due_for_verification(moved, NOW))
    check("  a re-verified address starts its retry ladder fresh",
          VW.apply_result(moved, result("john2@company.com", "unknown"),
                          NOW)["record"]["attempts"] == 1)

    cased = verified_lead("John@Company.com", verified_email="john@company.com")
    ok, why = VW.claim_guard(cased)
    check("  but a change of case alone does not invalidate it", ok, why)

    # ------------------------------------------------------------- 8, 9, 10
    print("\n--- Batching (31.8, 31.9, 31.10) ---")
    check("default batch size is 40", EV.BATCH_SIZE == 40, str(EV.BATCH_SIZE))
    check("  the API maximum is respected", EV.API_BATCH_MAX == 100)
    emails = ["u%d@x.com" % i for i in range(95)]
    chunks = list(EV.batches(emails))
    check("95 addresses split into 40/40/15",
          [len(c) for c in chunks] == [40, 40, 15], str([len(c) for c in chunks]))
    check("  duplicates are collapsed within a tick",
          len(list(EV.batches(["a@x.com", "A@X.com", "b@x.com"]))[0]) == 2)
    over = ["u%d@x.com" % i for i in range(101)]
    try:
        EV.verify_batch(over)
        check("a batch over the API maximum is refused", False, "no error")
    except ValueError:
        check("a batch over the API maximum is refused", True)

    # ------------------------------------------------------- 11, 12, 13, 10
    print("\n--- Transport failures never produce a verdict (31.10-31.13) ---")
    real = EV._call
    for label, fake in (
            ("timeout", lambda p, b=None, t=None: (0, {"error": "timed out"}, 30)),
            ("401", lambda p, b=None, t=None: (401, {"error": "unauthorized"}, 5)),
            ("500", lambda p, b=None, t=None: (500, {"error": "boom"}, 5))):
        EV._call = fake
        try:
            res = EV.verify_batch(["a@x.com", "b@x.com"])
        finally:
            EV._call = real
        check("a %s marks every address unknown, never invalid" % label,
              all(r["status"] == "unknown" for r in res),
              str([r["status"] for r in res]))
        check("  and both addresses are accounted for", len(res) == 2)

    # A partial response: one address answered, one silently dropped.
    EV._call = lambda p, b=None, t=None: (
        200, {"results": [{"email": "a@x.com", "status": "valid"}]}, 9)
    try:
        res = EV.verify_batch(["a@x.com", "b@x.com"])
    finally:
        EV._call = real
    got = {r["email"]: r["status"] for r in res}
    check("a partially-answered batch keeps the answer it got",
          got.get("a@x.com") == "valid", str(got))
    check("  and marks only the MISSING one unknown",
          got.get("b@x.com") == "unknown", str(got))

    print("\n--- Credentials never reach a stored message (32) ---")
    msg = ("HTTPError 401: {'headers': {'x-api-key': 'sk-supersecret-123', "
           "'authorization': 'Bearer abc.def'}}")
    scrubbed = EV.scrub(msg)
    check("api keys are redacted from errors",
          "sk-supersecret-123" not in scrubbed and "abc.def" not in scrubbed,
          scrubbed[:56])
    if EV.API_KEY:
        check("  the configured key itself is redacted",
              EV.API_KEY not in EV.scrub("leaked " + EV.API_KEY))

    # ------------------------------------------------------------ 17, 18, 19
    print("\n--- Only valid leads are claimable (31.17-31.19) ---")
    for status in ("invalid", "risky", "unknown", "pending"):
        l = verified_lead("a@b.com", status=status) if status in EV.STATUSES \
            else lead("a@b.com", email_verification_status="pending")
        ok, why = VW.claim_guard(l)
        check("%-8s is NOT claimable" % status, not ok, why)
    ok, _ = VW.claim_guard(lead("a@b.com"))
    check("an unverified lead is NOT claimable", not ok)
    check("the claim filter demands a valid verdict",
          "email_verification_status=eq.valid" in VW.claimable_filter()
          and "email_verified=is.true" in VW.claimable_filter())

    print("\n--- A settled verdict is not re-checked every tick (31.17) ---")
    check("a valid lead is not re-verified",
          not VW.due_for_verification(verified_lead("a@b.com"), NOW))
    check("  nor an invalid one",
          not VW.due_for_verification(verified_lead("a@b.com", "invalid"), NOW))
    waiting = lead("a@b.com", raw_data={VW.RAW_KEY: {
        "status": "unknown", "verified_email": "a@b.com", "attempts": 1,
        "next_retry_at": "2026-09-02T18:00:00+00:00"}})
    check("an unknown waiting on its backoff is left alone",
          not VW.due_for_verification(waiting, NOW))
    check("  and is picked up once the backoff has passed",
          VW.due_for_verification(
              waiting, NOW + datetime.timedelta(hours=7)))
    check("a never-verified lead is due immediately",
          VW.due_for_verification(lead("a@b.com"), NOW))

    print("\n--- Leads the worker must not touch (31.12) ---")
    for st in ("rejected", "unsubscribed", "archived", "duplicate"):
        check("%-13s is skipped" % st,
              not VW.due_for_verification(lead("a@b.com", status=st), NOW))
    check("an inactive lead is skipped",
          not VW.due_for_verification(lead("a@b.com", is_active=False), NOW))

    print("\n--- Structurally impossible addresses (real service shapes) ---")
    res = VW.reject_unusable(lead(""), NOW)
    check("an empty address is rejected without an API call",
          res["decision"] == "reject" and res["record"]["reason"] == "missing_email")
    check("  and it is recorded as invalid",
          res["fields"]["email_verification_status"] == "invalid")

    # Shapes copied from the live probe.
    live_cases = [
        ("not-an-email", "invalid", "missing_at_sign", "reject"),
        ("someone@example.invalid", "invalid", "nxdomain", "reject"),
        ("admin@example.com", "invalid", "null_mx", "reject"),
        ("hermes.verifier.probe@gmail.com", "valid", "domain_accepts_mail",
         "eligible"),
    ]
    for email, status, reason, expect in live_cases:
        r = result(email, status, reason=reason,
                   deliverable=(status == "valid"),
                   score=85 if status == "valid" else 0)
        check("live shape %-34s -> %s" % (email[:34], expect),
              EV.admission(r) == expect)

    print("\n--- Rejection is not suppression (9) ---")
    applied = VW.apply_result(lead("a@b.com"),
                              result("a@b.com", "invalid", reason="nxdomain"),
                              NOW)
    check("an invalid address sets the lead to rejected",
          applied["fields"]["status"] == "rejected")
    check("  but writes nothing resembling do_not_contact",
          "do_not_contact" not in json.dumps(applied["fields"]).lower())
    applied = VW.apply_result(lead("a@b.com"),
                              result("a@b.com", "risky", flags=["disposable"]),
                              NOW)
    check("a risky address is held, not rejected",
          applied["fields"]["status"] == "hold")
    check("  and its flags are kept for a later policy",
          applied["record"]["flags"] == ["disposable"])

    print("\n--- A valid verdict does not reopen a moved-on lead ---")
    applied = VW.apply_result(lead("a@b.com", status="sent"),
                              result("a@b.com", "valid", score=85), NOW)
    check("verification does not rewrite the lead's own status",
          "status" not in applied["fields"], str(applied["fields"].get("status")))
    check("  it only records the verdict",
          applied["fields"]["email_verified"] is True)

    print()
    print("=" * 74)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:")
        for f in FAILURES:
            print("  " + f)
    print("=" * 74)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
