#!/usr/bin/env python3
"""Lead intake must keep making progress.

Two ways it stopped, both reproduced here against fakes:

  1. The verifier selected candidates with one generous scan ordered by
     updated_at. Every already-final row still matched, so once there were
     more final rows than the scan limit they filled the window permanently
     and leads that had never been checked, sitting just past the end of it,
     were never verified. The verifier ran every two minutes and verified
     nothing.

  2. With nothing verified, the claim took a window of unverified rows every
     tick and the guard released every one of them, writing to Supabase twice
     a minute and reporting "claimed 20" while importing zero.

No network, no Supabase, no verifier service.
"""
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sqlite3                     # noqa: E402
import tempfile                    # noqa: E402

_TMP = tempfile.mkdtemp()
os.environ["AGENCY_DB"] = os.path.join(_TMP, "intake.db")

import pipeline as P               # noqa: E402
import supabase_sync as S          # noqa: E402
import verification_worker as VW   # noqa: E402

# supabase_sync and pipeline read AGENCY_DB at import time, so both are pointed
# at the temp file explicitly rather than relying on import order.
P.DB = os.environ["AGENCY_DB"]
_con = sqlite3.connect(P.DB)
_con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
_con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
for _m in sorted((HERE / "migrations").glob("*.sql")):
    _con.executescript(_m.read_text(encoding="utf-8"))
_con.close()

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


def lead(i, status_verdict, day, email=None):
    """One Supabase row. `day` drives updated_at, which is what ordering used."""
    rec = None
    if status_verdict in ("valid", "invalid", "risky"):
        rec = {"verified_email": email or "l%03d@example.com" % i,
               "status": status_verdict, "attempts": 1,
               "checked_at": "2026-09-%02dT10:00:00+00:00" % day}
    return {"id": "L%04d" % i, "email": email or "l%03d@example.com" % i,
            "status": "ready", "is_active": True, "hermes_status": "not_imported",
            "email_verification_status": status_verdict,
            "email_verified": status_verdict == "valid",
            "raw_data": {"email_verification": rec} if rec else {},
            "updated_at": "2026-09-%02dT10:00:00+00:00" % day}


class FakeRest:
    """Enough PostgREST to answer the two candidate queries and count()."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def call(self, path, method="GET", body=None, prefer=None):
        self.queries.append(path)
        if not path.startswith("leads?"):
            return []
        out = list(self.rows)
        if "email=not.is.null" in path:
            out = [r for r in out if r.get("email")]
        if "or=(email_verification_status.is.null," in path:
            out = [r for r in out if r["email_verification_status"] in (None, "unknown")]
        if "email_verification_status=eq.valid" in path:
            out = [r for r in out if r["email_verification_status"] == "valid"]
        if "email_verified=is.true" in path:
            out = [r for r in out if r.get("email_verified")]
        if "status=eq.ready" in path:
            out = [r for r in out if r.get("status") == "ready"]
        if "hermes_status=eq.not_imported" in path:
            out = [r for r in out if r.get("hermes_status") == "not_imported"]
        if "order=updated_at.asc" in path:
            out.sort(key=lambda r: r["updated_at"])
        if "limit=" in path:
            n = int(path.split("limit=")[1].split("&")[0])
            out = out[:n]
        return [dict(r) for r in out]


def main() -> int:
    print("=" * 78)
    print("LEAD INTAKE STARVATION")
    print("=" * 78)

    # 260 rows already finished on 2 September, 30 never checked on the 3rd:
    # exactly the shape production was in, scaled down.
    final = [lead(i, "risky", 2) for i in range(260)]
    pending = [lead(1000 + i, None, 3) for i in range(30)]
    rows = final + pending
    fake = FakeRest(rows)
    real_call, S._call = S._call, fake.call
    try:
        print("\n--- 1. The verifier reaches rows that have never been checked ---")
        got = VW.fetch_candidates(200)
        ids = {r["id"] for r in got}
        due = [r for r in got if VW.due_for_verification(r)]
        check("the scan returns rows that still need a verdict",
              len(due) == 30, "%d due of %d fetched" % (len(due), len(got)))
        check("  every pending lead is in the window, not just the oldest rows",
              {r["id"] for r in pending} <= ids)
        check("  the window is still full, so final rows are still swept for"
              " a changed address", len(got) == 200, str(len(got)))
        check("  and no row is returned twice", len(ids) == len(got))

        # The old behaviour, for contrast: one generous ordered scan.
        old_window = fake.call("leads?select=id&is_active=eq.true"
                               "&email=not.is.null&email=neq."
                               "&order=updated_at.asc&limit=200")
        check("  the previous single-scan selection saw none of them",
              not ({r["id"] for r in pending} & {r["id"] for r in old_window}),
              "%d due in the old window"
              % len([r for r in old_window if VW.due_for_verification(r)]))

        print("\n--- 2. Pending rows keep priority as the finished pile grows ---")
        fake.rows = [lead(i, "risky", 2) for i in range(5000)] + pending
        got = VW.fetch_candidates(200)
        due = [r for r in got if VW.due_for_verification(r)]
        check("5000 finished rows still do not crowd out 30 pending ones",
              len(due) == 30, "%d due" % len(due))
        fake.rows = final
        check("with nothing pending the scan still sweeps for changed addresses",
              len(VW.fetch_candidates(200)) == 200)
        fake.rows = rows

        print("\n--- 3. A lead whose address changed after verification ---")
        moved = lead(9001, "valid", 2, email="old@example.com")
        moved["email"] = "new@example.com"          # edited after the verdict
        check("its stored verdict no longer counts",
              VW.due_for_verification(moved) is True)
        allowed, why = VW.claim_guard(moved)
        check("  and it is refused admission until re-verified",
              not allowed and "no longer matches" in why, why)

        print("\n--- 4. The claim does not churn when nothing is eligible ---")
        # _count goes straight to PostgREST for an exact number, so it is
        # answered from the same fake rows rather than the network.
        def fake_count(query):
            return len(fake.call(query + "&select=id&limit=100000"))

        calls = []
        real_rpc, S.rpc = S.rpc, lambda name, args: calls.append(name) or []
        real_count, S._count = S._count, fake_count
        try:
            res = S.claim(limit=20)
        finally:
            S.rpc = real_rpc
        check("no verified lead means the claim RPC is never called",
              calls == [], str(calls))
        check("  it reports why, with the real backlog size",
              res.get("eligible") == 0 and "none has a valid verification" in
              (res.get("skipped") or ""), (res.get("skipped") or "")[:70])
        check("  nothing is claimed, released or imported",
              res["claimed"] == 0 and res["released"] == 0 and res["imported"] == 0,
              str({k: res[k] for k in ("claimed", "released", "imported")}))

        # One verified lead is enough to make the claim run again.
        fake.rows = rows + [lead(9100, "valid", 3)]
        calls = []
        real_rpc, S.rpc = S.rpc, lambda name, args: calls.append(name) or []
        try:
            res = S.claim(limit=20)
        finally:
            S.rpc = real_rpc
            S._count = real_count
        check("one eligible lead is enough for the claim to run",
              res.get("eligible") == 1 and calls[:1] == [S.CLAIM_RPC],
              "eligible=%s calls=%s" % (res.get("eligible"), calls[:1]))
    finally:
        S._call = real_call

    print("\n--- 5. The daily target counts imports, not attempts ---")
    src = (HERE / "supabase_sync.py").read_text(encoding="utf-8")
    body = src[src.index("def claim("):]
    record_ctx = body[max(0, body.index("_record_import") - 400):body.index("_record_import")]
    check("_record_import is reached only on a created lead",
          'res["status"] == "created"' in record_ctx)
    check("  a released lead never touches the counter",
          "_record_import" not in body[:body.index('out["released"] += 1')])
    check("  the counter reads the intake ledger, not a count of claims",
          "SELECT imported FROM lead_intake_days" in src)

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
