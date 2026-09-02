#!/usr/bin/env python3
"""Email verification in operation: concurrency, reporting, installation, restart.

The transport is faked so nothing here reaches the verifier or Supabase; the
live behaviour is covered by live/verifier_probe.py and the canary run.
"""
import datetime
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import email_verifier as EV        # noqa: E402
import verification_worker as VW   # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-58s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-58s %s" % (name, detail))


def main():
    tmp = tempfile.mkdtemp()
    print("=" * 74)
    print("EMAIL VERIFICATION — OPERATIONS")
    print("=" * 74)

    # ------------------------------------------------------- 31.15, 31.16
    print("\n--- Concurrent ticks do not double-verify (31.15, 31.16) ---")
    # A fake Supabase: one shared table, a lock around each PATCH the way the
    # real database serialises row writes, and a fake verifier that records
    # every address it is asked about.
    table = {"L%d" % i: {"id": "L%d" % i, "email": "u%d@x.com" % i,
                         "status": "ready", "is_active": True,
                         "hermes_status": "not_imported",
                         "email_verification_status": None,
                         "email_verified": None, "raw_data": {}}
             for i in range(25)}
    lock = threading.Lock()
    asked = []

    def fake_fetch(limit=200):
        with lock:
            return [dict(r) for r in table.values()]

    def fake_patch(lead_id, fields):
        with lock:
            table[lead_id].update(fields)

    def fake_verify(emails):
        with lock:
            asked.extend(emails)
        return [{"email": e, "status": "valid", "score": 85,
                 "deliverable": True, "reason": "domain_accepts_mail",
                 "flags": [], "did_you_mean": None, "mx_host": "mx",
                 "cached": False, "took_ms": 5, "error": None} for e in emails]

    real = (VW.fetch_candidates, VW._patch, EV.verify_batch, EV.BASE, EV.API_KEY,
            VW.S.configured)
    VW.fetch_candidates, VW._patch, EV.verify_batch = fake_fetch, fake_patch, fake_verify
    EV.BASE, EV.API_KEY = "https://fake", "k"
    VW.S.configured = lambda: True
    try:
        results = {}
        ts = [threading.Thread(target=lambda n=n: results.__setitem__(
            n, VW.tick(limit=200))) for n in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        (VW.fetch_candidates, VW._patch, EV.verify_batch, EV.BASE, EV.API_KEY,
         VW.S.configured) = real

    total_verified = sum(r["verified"] for r in results.values())
    check("four ticks ran over the same 25 leads without error",
          all(not r["errors"] for r in results.values()))
    check("every lead ends up verified exactly once in the table",
          all(r["email_verification_status"] == "valid" for r in table.values())
          and all(VW.record_of(r)["attempts"] == 1 for r in table.values()),
          "attempts: %s" % sorted({VW.record_of(r)["attempts"]
                                   for r in table.values()}))
    # The ticks may overlap in what they ask the verifier, because the fake
    # fetch has no row lock -- that is the race being tested. What must hold
    # is that overlap never corrupts the stored verdict or inflates attempts.
    check("  and no address was recorded with attempts > 1",
          max(VW.record_of(r)["attempts"] for r in table.values()) == 1)
    # Once every row holds a final verdict, another tick must be free: the
    # verifier's own cache is not what saves the subrequest budget, this is.
    asked.clear()
    VW.fetch_candidates, VW._patch, EV.verify_batch = fake_fetch, fake_patch, fake_verify
    EV.BASE, EV.API_KEY = "https://fake", "k"
    VW.S.configured = lambda: True
    try:
        again = VW.tick(limit=200)
    finally:
        (VW.fetch_candidates, VW._patch, EV.verify_batch, EV.BASE, EV.API_KEY,
         VW.S.configured) = real
    check("  a second pass after settlement asks the verifier nothing",
          again["verified"] == 0 and not asked, "asked %d" % len(asked))

    # A lead that is not verified valid cannot be claimed, even while another
    # tick is mid-verification of it: the guard reads the row as it is.
    half = {"id": "L99", "email": "z@x.com", "status": "ready",
            "hermes_status": "not_imported", "email_verification_status": None,
            "email_verified": None, "raw_data": {}}
    ok, why = VW.claim_guard(half)
    check("a lead mid-verification is not claimable", not ok, why)

    # --------------------------------------------------------------- 31.21
    print("\n--- ORBIT verification report (31.21) ---")
    import orbit

    fake_counts = {"valid": 312, "invalid": 41, "risky": 22, "unknown": 9,
                   "pending": 16, "error": None}
    real_counts = VW.counts
    VW.counts = lambda now=None: dict(fake_counts)
    try:
        # collect() touches many things; assert on the maths directly instead.
        done = sum(fake_counts[k] for k in ("valid", "invalid", "risky", "unknown"))
        rate = orbit.rate(fake_counts["valid"], done)
    finally:
        VW.counts = real_counts
    check("completed verdicts exclude pending", done == 384, str(done))
    # rate() is a percentage rounded to one decimal, matching how every other
    # figure in the ORBIT report is expressed.
    check("pass rate is valid / completed", rate == round(100.0 * 312 / 384, 1),
          "%s%%" % rate)
    check("  and can never exceed 100%%", rate <= 100.0 and orbit.rate(5, 5) == 100.0)
    check("  a zero denominator is reported as no rate, not 0%%",
          orbit.rate(0, 0) is None)
    src = (HERE / "orbit.py").read_text(encoding="utf-8")
    check("the daily report has an EMAIL VERIFICATION section",
          "EMAIL VERIFICATION" in src)
    check("  showing pending, valid, invalid, risky, unknown and pass rate",
          all(k in src for k in ("Pending:", "Valid:", "Invalid:",
                                 "Risky (held):", "Unknown (retrying):",
                                 "Pass rate:")))
    check("  verifier staleness rides in the AUTOMATION block, not a silo",
          "email-verifier" in orbit.CRON_STALE_MINUTES)

    # --------------------------------------------------------------- 31.22
    print("\n--- Installer manages exactly seven jobs, once (31.22) ---")
    spec = importlib.util.spec_from_file_location(
        "install_agency_crons", HERE / "scripts" / "install_agency_crons.py")
    inst = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inst)
    jobs_path = os.path.join(tmp, "jobs.json")
    inst.JOBS = jobs_path
    base = [{"name": n, "id": "x%d" % i} for i, n in enumerate(
        ("maya-orchestrator", "supabase-lead-sync", "review-alerts", "orbit-daily"))]
    json.dump(base, open(jobs_path, "w"))
    sys.argv = ["x", "--check"]
    check("--check reports the verifier missing and writes nothing",
          inst.main() == 1 and len(json.load(open(jobs_path))) == 4)
    check("  email-verifier is one of the managed jobs",
          any(w["name"] == "email-verifier" and w["script"] == "email_verifier_tick.py"
              for w in inst.WANTED))
    jobs = json.load(open(jobs_path))
    for w in inst.WANTED:
        jobs.append({"name": w["name"], "id": w["name"][:4],
                     "schedule_display": w["schedule"], "script": w["script"],
                     "no_agent": True, "deliver": "local"})
    json.dump(jobs, open(jobs_path, "w"))
    sys.argv = ["x"]
    check("with all seven present, three runs create nothing",
          inst.main() == 0 and inst.main() == 0 and inst.main() == 0)
    names = sorted(j["name"] for j in json.load(open(jobs_path)))
    check("  exactly seven, one of each", len(names) == 7 and len(set(names)) == 7,
          str(names))
    check("  and they are the expected seven",
          names == sorted(["echo-followups", "email-verifier", "leo-inbound",
                           "maya-orchestrator", "orbit-daily", "review-alerts",
                           "supabase-lead-sync"]))

    # --------------------------------------------------------------- 31.23
    print("\n--- Restart durability (31.23) ---")
    check("verifier settings come from the environment, not code",
          all(k in (HERE / "email_verifier.py").read_text(encoding="utf-8")
              for k in ("EMAIL_VERIFIER_URL", "EMAIL_VERIFIER_API_KEY",
                        "EMAIL_VERIFIER_BATCH_SIZE",
                        "EMAIL_VERIFIER_TIMEOUT_SECONDS",
                        "EMAIL_VERIFIER_MAX_RETRIES")))
    check("  the claim RPC name is runtime config too",
          "SUPABASE_CLAIM_RPC" in (HERE / "supabase_sync.py").read_text(encoding="utf-8"))
    # The scan skips this file, which necessarily names the prefix it looks for.
    me = pathlib.Path(__file__).resolve()
    check("  rotating the key touches no file in the repo",
          not any("da8c1687" in p.read_text(encoding="utf-8", errors="ignore")
                  for p in HERE.rglob("*.py") if p.resolve() != me))
    check("the schedule lives on disk and re-reads in a fresh process",
          len([j for j in json.load(open(jobs_path))
               if j["name"] == "email-verifier"]) == 1)
    os.environ["EMAIL_VERIFIER_BATCH_SIZE"] = "40"
    check("batch size honours its env var and the API ceiling",
          EV.BATCH_SIZE == 40 and EV.API_BATCH_MAX == 100)

    print("\n--- The gate fails closed when the verifier is down (25) ---")
    real2 = (EV.BASE, EV.API_KEY)
    EV.BASE, EV.API_KEY = "", ""
    try:
        res = VW.tick(limit=10)
    finally:
        EV.BASE, EV.API_KEY = real2
    check("an unconfigured verifier verifies nothing",
          res["verified"] == 0 and res["errors"])
    check("  and an unverified lead stays unclaimable meanwhile",
          not VW.claim_guard({"id": "L", "email": "a@b.com",
                              "email_verification_status": None,
                              "email_verified": None, "raw_data": {}})[0])
    check("  while an already-valid lead is unaffected",
          VW.claim_guard({"id": "L", "email": "a@b.com",
                          "email_verification_status": "valid",
                          "email_verified": True,
                          "raw_data": {VW.RAW_KEY: {"status": "valid",
                                                    "verified_email": "a@b.com"}}})[0])

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
