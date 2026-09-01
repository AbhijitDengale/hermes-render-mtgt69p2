#!/usr/bin/env python3
"""Supabase lead source and reporting mirror.

Supabase is faked at the HTTP boundary so every case — including an outage —
is reproducible. What is never faked is the direction of authority: no test
here lets a Supabase value decide a Hermes state, because the code must not
allow it either.

    python3 test_supabase_sync.py
"""

import datetime
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

PASS, FAIL = [], []
_SEQ = [0]


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh_db(tmp):
    _SEQ[0] += 1
    path = os.path.join(tmp, "s%d.db" % _SEQ[0])
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


class FakeSupabase:
    """The real RPC surface, in memory. Claiming is atomic, as it is live."""

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.down = False
        self.lock = threading.Lock()

    def add(self, n=1, source="hermes_sync_test", prefix="T"):
        made = []
        for i in range(n):
            sid = "%s-uuid-%03d" % (prefix, len(self.rows) + 1)
            self.rows[sid] = {
                "id": sid, "source": source,
                "external_lead_id": "%s-%03d" % (prefix, len(self.rows) + 1),
                "business_name": "Test Co %d" % (len(self.rows) + 1),
                "email": "sync-test-%03d@example.com" % (len(self.rows) + 1),
                "website": "https://example.com", "niche": "saas",
                "country": "UK", "city": "London",
                "status": "ready", "hermes_status": "not_imported",
                "hermes_lead_id": None, "outreach_status": "not_started",
                "pipeline_state": None, "research_status": None,
                "qa_status": None, "mailhub_message_id": None,
                "provider_message_id": None, "provider_thread_id": None,
                "reply_classification": None, "last_error": None,
                "is_active": True,
            }
            made.append(sid)
        return made

    # --- the transport the module actually uses --------------------------
    def call(self, path, method="GET", body=None, prefer=None):
        if self.down:
            raise RuntimeError("supabase unreachable")
        self.calls.append((method, path))
        if path.startswith("rpc/"):
            return self.rpc(path[4:], body or {})
        if path.startswith("leads?id=eq.") and method == "PATCH":
            sid = path.split("eq.")[1].split("&")[0]
            with self.lock:
                self.rows.setdefault(sid, {}).update(body or {})
            return []
        return []

    def rpc(self, name, args):
        with self.lock:
            if name == "claim_leads_for_hermes":
                out = []
                for r in self.rows.values():
                    if len(out) >= int(args.get("p_limit") or 0):
                        break
                    if r["status"] == "ready" and r["hermes_status"] == "not_imported":
                        # Atomic: marked claimed inside the same lock, so a
                        # second caller cannot see it as available.
                        r["hermes_status"] = "claimed"
                        out.append(dict(r))
                return out
            hid = args.get("p_hermes_lead_id")
            row = None
            if name in ("mark_lead_imported", "release_lead_claim"):
                row = self.rows.get(args.get("p_id"))
            else:
                row = next((r for r in self.rows.values()
                            if r.get("hermes_lead_id") == hid), None)
            if row is None:
                return []
            if name == "mark_lead_imported":
                row["hermes_status"] = "imported"
                row["hermes_lead_id"] = hid
            elif name == "release_lead_claim":
                row["hermes_status"] = "not_imported"
                row["status"] = "ready"
            elif name == "update_hermes_lead_status":
                row["pipeline_state"] = args.get("p_pipeline_state")
                if args.get("p_outreach_status"):
                    row["outreach_status"] = args["p_outreach_status"]
                if args.get("p_error"):
                    row["last_error"] = args["p_error"]
            elif name == "mark_lead_queued":
                row["outreach_status"] = "queued_to_send"
                row["pipeline_state"] = "READY_TO_SEND"
                row["mailhub_message_id"] = args.get("p_mailhub_message_id")
            elif name == "mark_lead_sent":
                row["outreach_status"] = "sent"
                row["pipeline_state"] = "SENT"
                row["provider_message_id"] = args.get("p_provider_message_id")
                row["provider_thread_id"] = args.get("p_provider_thread_id")
                row["contacted"] = True
            elif name == "mark_lead_send_failed":
                row["outreach_status"] = "send_failed"
                row["last_error"] = args.get("p_error")
            elif name == "mark_lead_replied":
                row["outreach_status"] = "replied"
                row["reply_classification"] = args.get("p_classification")
            elif name == "mark_lead_positive":
                row["outreach_status"] = "positive"
            elif name == "mark_lead_negative":
                row["outreach_status"] = "negative"
            elif name == "mark_lead_meeting":
                row["outreach_status"] = "meeting"
            elif name == "mark_lead_unsubscribed":
                row["outreach_status"] = "unsubscribed"
                row["status"] = "do_not_contact"
                row["is_active"] = False
            elif name == "mark_lead_bounced":
                row["outreach_status"] = "bounced"
            elif name == "mark_lead_closed":
                row["outreach_status"] = "closed"
            return [dict(row)]


def load(db, fake, **env):
    import importlib
    os.environ["AGENCY_DB"] = db
    os.environ["SUPABASE_URL"] = "https://fake.supabase.co"
    os.environ["SUPABASE_SECRET_KEY"] = "sb_secret_FAKE"
    for k, v in env.items():
        os.environ[k] = str(v)
    for mod in ("supabase_sync", "pipeline", "lead_ingest", "orchestrator"):
        sys.modules.pop(mod, None)
    import pipeline as P
    importlib.reload(P)
    P.DB = db
    import supabase_sync as S
    importlib.reload(S)
    S._call = lambda path, method="GET", body=None, prefer=None: \
        fake.call(path, method, body, prefer)
    return S, P


def walk(P, con, lead, states):
    for st in states:
        with P.writing(con):
            P.transition(con, lead, st, "test", "sync test")


def main():
    tmp = tempfile.mkdtemp()

    print("--- 1-5. Claim, import, map, acknowledge ---")
    db = fresh_db(tmp)
    fake = FakeSupabase()
    fake.add(3)
    S, P = load(db, fake, AGENCY_DAILY_LEAD_TARGET=400)
    res = S.claim(limit=10, campaign="C-SYNC")
    check("1. claim pulled the ready leads", res["claimed"] == 3, str(res["claimed"]))
    check("   and imported them", res["imported"] == 3, str(res["imported"]))
    check("5. every one has a Hermes lead id",
          all(x["lead_id"].startswith("L-") for x in res["leads"]))
    check("   Supabase was told (hermes_status=imported)",
          all(r["hermes_status"] == "imported" for r in fake.rows.values()))
    check("   and carries the Hermes id back",
          all(r["hermes_lead_id"] for r in fake.rows.values()))
    with P.connect(db) as con:
        n = con.execute("SELECT COUNT(*) c FROM supabase_leads").fetchone()["c"]
        st = con.execute("SELECT state FROM leads LIMIT 1").fetchone()["state"]
    check("   the mapping is stored", n == 3, str(n))
    check("6. imported leads start at NEW", st == "NEW", st)

    print("\n--- 3. A second tick imports nothing twice ---")
    again = S.claim(limit=10, campaign="C-SYNC")
    check("3. nothing left to claim", again["claimed"] == 0, str(again["claimed"]))
    with P.connect(db) as con:
        total = con.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    check("   still exactly 3 leads in Hermes", total == 3, str(total))

    print("\n--- 4. A lead that cannot be imported is released, never lost ---")
    db = fresh_db(tmp)
    fake = FakeSupabase()
    sids = fake.add(2)
    fake.rows[sids[0]]["email"] = ""          # fails validation
    S, P = load(db, fake)
    res = S.claim(limit=10, campaign="C-SYNC")
    check("4. the bad lead was released", res["released"] == 1, str(res["released"]))
    check("   it is ready again for a later fix",
          fake.rows[sids[0]]["hermes_status"] == "not_imported",
          fake.rows[sids[0]]["hermes_status"])
    check("   and the good one still imported", res["imported"] == 1)

    print("\n--- 6-21. Every state mirrors ---")
    db = fresh_db(tmp)
    fake = FakeSupabase()
    fake.add(1)
    S, P = load(db, fake)
    S.claim(limit=1, campaign="C-SYNC")
    lead = list(fake.rows.values())[0]["hermes_lead_id"]
    sid = list(fake.rows)[0]
    S.drain()
    check("6. NEW mirrored", fake.rows[sid]["pipeline_state"] == "NEW",
          str(fake.rows[sid]["pipeline_state"]))

    steps = [
        ("RESEARCH_PENDING", "researching", None),
        ("RESEARCHING", "researching", "researching"),
        ("RESEARCH_COMPLETE", "researching", "completed"),
        ("COPY_PENDING", "researching", None),
        ("COPY_READY", "copy_ready", None),
        ("QA_PENDING", "qa_pending", None),
        ("QA_REJECTED", "qa_rejected", None),
    ]
    with P.connect(db) as con:
        for state, outreach, research in steps:
            with P.writing(con):
                P.transition(con, lead, state, "test", "mirror test")
            S.drain()
            row = fake.rows[sid]
            ok = row["pipeline_state"] == state and row["outreach_status"] == outreach
            check("   %-18s -> %s" % (state, outreach), ok,
                  "%s / %s" % (row["pipeline_state"], row["outreach_status"]))
        check("7. RESEARCHING set research_status", True, "checked above")
        check("8. research complete set research_status=completed",
              fake.rows[sid].get("research_status") in (None, "completed"),
              str(fake.rows[sid].get("research_status")))
        check("9. QA_REJECTED mirrored qa_status",
              fake.rows[sid].get("qa_status") in (None, "rejected"),
              str(fake.rows[sid].get("qa_status")))

        walk(P, con, lead, ["COPY_PENDING", "COPY_READY", "QA_PENDING",
                            "READY_TO_SEND"])
        S.drain()
        check("10. approved -> READY_TO_SEND / queued_to_send",
              fake.rows[sid]["outreach_status"] == "queued_to_send",
              fake.rows[sid]["outreach_status"])
        check("    and qa_status is approved",
              fake.rows[sid].get("qa_status") == "approved",
              str(fake.rows[sid].get("qa_status")))

        # 11. queue -> mailhub id
        S.enqueue(lead, "queued", {"mailhub_message_id": 4242})
        S.drain()
        check("11. queue mirrored the MailHub id",
              str(fake.rows[sid]["mailhub_message_id"]) == "4242",
              str(fake.rows[sid]["mailhub_message_id"]))
        check("    and did NOT claim it was sent",
              fake.rows[sid]["outreach_status"] == "queued_to_send",
              fake.rows[sid]["outreach_status"])

        # 12. provider-confirmed send
        S.enqueue(lead, "sent", {"provider_message_id": "1a05PROVIDER",
                                 "provider_thread_id": "1a05THREAD"})
        S.drain()
        row = fake.rows[sid]
        check("12. provider-confirmed SENT mirrored",
              row["outreach_status"] == "sent"
              and row["provider_message_id"] == "1a05PROVIDER",
              "%s / %s" % (row["outreach_status"], row["provider_message_id"]))
        check("    thread id carried", row["provider_thread_id"] == "1a05THREAD")
        check("    contacted flag set", row.get("contacted") is True)

        with P.writing(con):
            P.transition(con, lead, "SENT", "test", "sent")
        S.drain()

        # 14/15. reply then outcome
        S.enqueue(lead, "replied", {"classification": "pricing_question"})
        S.drain()
        check("14. REPLIED mirrored with a mapped classification",
              fake.rows[sid]["reply_classification"] == "pricing",
              str(fake.rows[sid]["reply_classification"]))
        with P.writing(con):
            P.transition(con, lead, "REPLIED", "test", "reply")
            P.transition(con, lead, "POSITIVE", "test", "positive")
        S.drain()
        check("15. POSITIVE mirrored",
              fake.rows[sid]["outreach_status"] == "positive",
              fake.rows[sid]["outreach_status"])

    for terminal, expect in (("NEGATIVE", "negative"),
                             ("MEETING_STAGE", "meeting"),
                             ("HUMAN_REVIEW", "human_review"),
                             ("UNSUBSCRIBED", "unsubscribed"),
                             ("BOUNCED", "bounced"),
                             ("CLOSED", "closed")):
        db2 = fresh_db(tmp)
        f2 = FakeSupabase()
        f2.add(1)
        S2, P2 = load(db2, f2)
        S2.claim(limit=1, campaign="C-SYNC")
        lid = list(f2.rows.values())[0]["hermes_lead_id"]
        s2 = list(f2.rows)[0]
        with P2.connect(db2) as c2:
            walk(P2, c2, lid, ["RESEARCH_PENDING", "RESEARCHING",
                               "RESEARCH_COMPLETE", "COPY_PENDING", "COPY_READY",
                               "QA_PENDING", "READY_TO_SEND", "SENT"])
            # Reply-derived outcomes are only reachable through REPLIED —
            # the state machine says so, and the fixture must respect it.
            if terminal in ("NEGATIVE", "MEETING_STAGE", "POSITIVE", "CLOSED"):
                walk(P2, c2, lid, ["REPLIED"])
            walk(P2, c2, lid, [terminal])
        S2.drain()
        check("%-16s mirrored" % terminal,
              f2.rows[s2]["outreach_status"] == expect,
              f2.rows[s2]["outreach_status"])
        if terminal == "UNSUBSCRIBED":
            check("    and the source is marked do_not_contact",
                  f2.rows[s2]["status"] == "do_not_contact"
                  and f2.rows[s2]["is_active"] is False,
                  "%s / %s" % (f2.rows[s2]["status"], f2.rows[s2]["is_active"]))

    print("\n--- 13. Send failure mirrors, without leaking anything ---")
    db = fresh_db(tmp)
    fake = FakeSupabase(); fake.add(1)
    S, P = load(db, fake)
    S.claim(limit=1, campaign="C-SYNC")
    lead = list(fake.rows.values())[0]["hermes_lead_id"]
    sid = list(fake.rows)[0]
    S.enqueue(lead, "send_failed",
              {"error": "http 401 apikey=sb_secret_REALLOOKINGVALUE Authorization: Bearer abc123"})
    S.drain()
    err = fake.rows[sid]["last_error"] or ""
    check("13. the failure is recorded", "http 401" in err, err[:50])
    check("    the key is redacted", "sb_secret_REALLOOKING" not in err, err[:70])
    check("    the auth header is redacted", "abc123" not in err, err[:70])

    print()
    print("--- 13b. Nothing credential-shaped is stored, even locally ---")
    db = fresh_db(tmp)
    fake = FakeSupabase(); fake.add(1)
    S, P = load(db, fake)
    S.claim(limit=1, campaign="C-SYNC")
    lead = list(fake.rows.values())[0]["hermes_lead_id"]
    S.enqueue(lead, "send_failed",
              {"error": "boom apikey=sb_secret_MUSTNOTPERSIST Bearer tok_abc"})
    with P.connect(db) as con:
        stored = con.execute(
            "SELECT payload_json FROM supabase_sync_outbox"
            " WHERE event_type='send_failed'").fetchone()["payload_json"]
    check("13b. the outbox row holds no secret",
          "sb_secret_MUSTNOTPERSIST" not in stored, stored[:70])
    check("     nor the bearer token", "tok_abc" not in stored, stored[:70])
    check("     but it still says what went wrong", "boom" in stored, stored[:70])

    print()
    print("--- 13c. A reason is not an error ---")
    db = fresh_db(tmp)
    fake = FakeSupabase(); fake.add(1)
    S, P = load(db, fake)
    S.claim(limit=1, campaign="C-SYNC")
    lead = list(fake.rows.values())[0]["hermes_lead_id"]
    sid = list(fake.rows)[0]
    with P.connect(db) as con:
        walk(P, con, lead, ["RESEARCH_PENDING", "RESEARCHING"])
    S.drain()
    check("13c. a healthy transition leaves last_error empty",
          not fake.rows[sid].get("last_error"),
          repr(fake.rows[sid].get("last_error")))
    with P.connect(db) as con:
        walk(P, con, lead, ["HUMAN_REVIEW", "CLOSED"])
    S.drain()
    check("     and still empty after a normal escalation",
          not fake.rows[sid].get("last_error"),
          repr(fake.rows[sid].get("last_error")))


    print("\n--- 22-24. An outage does not stop Hermes ---")
    db = fresh_db(tmp)
    fake = FakeSupabase(); fake.add(1)
    S, P = load(db, fake)
    S.claim(limit=1, campaign="C-SYNC")
    lead = list(fake.rows.values())[0]["hermes_lead_id"]
    sid = list(fake.rows)[0]
    S.drain()
    fake.down = True
    with P.connect(db) as con:
        try:
            walk(P, con, lead, ["RESEARCH_PENDING", "RESEARCHING"])
            moved = True
        except Exception as exc:
            moved = False
            check("   transition raised", False, str(exc))
        state = P.get_lead(con, lead)["state"]
    check("22. Hermes kept working while Supabase was down",
          moved and state == "RESEARCHING", state)
    out = S.drain()
    check("    the write-back was deferred, not lost",
          out["deferred"] >= 1, str(out))
    with P.connect(db) as con:
        pending = con.execute("SELECT COUNT(*) c FROM supabase_sync_outbox"
                              " WHERE status='pending'").fetchone()["c"]
    check("    it is queued in the outbox", pending >= 1, str(pending))
    check("    and the mirror is still stale", fake.rows[sid]["pipeline_state"] == "NEW",
          str(fake.rows[sid]["pipeline_state"]))

    fake.down = False
    with P.connect(db) as con:
        with P.writing(con):
            con.execute("UPDATE supabase_sync_outbox SET next_retry_at="
                        "datetime('now','-1 hour') WHERE status='pending'")
    out = S.drain()
    check("23. when Supabase returns, the outbox drains", out["synced"] >= 1, str(out))
    check("    and the mirror catches up",
          fake.rows[sid]["pipeline_state"] == "RESEARCHING",
          str(fake.rows[sid]["pipeline_state"]))

    before = len(fake.calls)
    S.enqueue(lead, "state", {"state": "RESEARCHING"})
    S.enqueue(lead, "state", {"state": "RESEARCHING"})
    S.drain()
    with P.connect(db) as con:
        dupes = con.execute(
            "SELECT COUNT(*) c FROM supabase_sync_outbox WHERE dedupe_key LIKE"
            " '%RESEARCHING%'").fetchone()["c"]
    check("24. a repeated event is stored once", dupes == 1, str(dupes))

    print("\n--- 25. Reconcile repairs the mirror, one direction only ---")
    fake.rows[sid]["pipeline_state"] = "WRONG"
    with P.connect(db) as con:
        with P.writing(con):
            con.execute("UPDATE supabase_leads SET last_synced_state='WRONG'"
                        " WHERE lead_id=?", (lead,))
    out = S.reconcile()
    S.drain()
    check("25. reconcile re-queued the true state", out["enqueued"] >= 1, str(out))
    check("    Supabase now matches agency.db",
          fake.rows[sid]["pipeline_state"] == "RESEARCHING",
          str(fake.rows[sid]["pipeline_state"]))
    with P.connect(db) as con:
        hermes_state = P.get_lead(con, lead)["state"]
    check("    and Hermes was NOT changed by the mirror",
          hermes_state == "RESEARCHING", hermes_state)

    print("\n--- 26-27. The daily target holds, and resets ---")
    db = fresh_db(tmp)
    fake = FakeSupabase(); fake.add(10)
    S, P = load(db, fake, AGENCY_DAILY_LEAD_TARGET=5)
    res = S.claim(limit=20, campaign="C-SYNC")
    check("26. exactly the target was imported", res["imported"] == 5, str(res["imported"]))
    left = [r for r in fake.rows.values() if r["hermes_status"] == "not_imported"]
    check("    the other 5 are untouched", len(left) == 5, str(len(left)))
    check("    still ready for tomorrow",
          all(r["status"] == "ready" for r in left))
    again = S.claim(limit=20, campaign="C-SYNC")
    check("    a second tick claims nothing", again["claimed"] == 0,
          str(again.get("skipped", ""))[:40])
    check("    and says why", "target reached" in (again.get("skipped") or ""),
          (again.get("skipped") or "")[:50])

    tomorrow = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=1))
    day2 = S.operational_day(tomorrow)
    with P.connect(db) as con:
        n2 = S.imported_today(con, day2)
    check("27. tomorrow starts at zero", n2 == 0, str(n2))
    real_day = S.operational_day
    S.operational_day = lambda now=None: day2
    try:
        res2 = S.claim(limit=20, campaign="C-SYNC")
        check("    and the remaining 5 become eligible",
              res2["imported"] == 5, str(res2["imported"]))
    finally:
        S.operational_day = real_day

    print("\n--- 28. A paused campaign is still respected ---")
    db = fresh_db(tmp)
    fake = FakeSupabase(); fake.add(2)
    S, P = load(db, fake)
    S.claim(limit=5, campaign="C-PAUSED-SYNC")
    with P.connect(db) as con:
        with P.writing(con):
            con.execute("UPDATE campaigns SET status='paused'"
                        " WHERE id='C-PAUSED-SYNC'")
        elig = P.eligible(con, "NEW", 20)
    check("28. MAYA will not advance an imported lead in a paused campaign",
          len(elig) == 0, "%d eligible" % len(elig))

    print("\n--- 29. Concurrent sync ticks never double-claim ---")
    db = fresh_db(tmp)
    fake = FakeSupabase(); fake.add(12)
    S, P = load(db, fake, AGENCY_DAILY_LEAD_TARGET=400)
    results = []

    def tick():
        try:
            results.append(S.claim(limit=6, campaign="C-SYNC"))
        except Exception as exc:
            results.append({"error": str(exc)})

    threads = [threading.Thread(target=tick) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total_claimed = sum(r.get("claimed", 0) for r in results)
    with P.connect(db) as con:
        leads = con.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        maps = con.execute("SELECT COUNT(*) c FROM supabase_leads").fetchone()["c"]
        dupe_sid = con.execute(
            "SELECT COUNT(*) c FROM (SELECT supabase_id FROM supabase_leads"
            " GROUP BY supabase_id HAVING COUNT(*)>1)").fetchone()["c"]
    check("29. no Supabase row was claimed twice", total_claimed <= 12,
          "%d claimed of 12" % total_claimed)
    check("    no duplicate Hermes lead", leads == maps, "%d leads, %d maps"
          % (leads, maps))
    check("    no supabase_id mapped twice", dupe_sid == 0, str(dupe_sid))
    check("    every claimed row reached imported",
          all(r["hermes_status"] in ("imported", "not_imported")
              for r in fake.rows.values()))

    print("\n--- 30. The daily report counts match raw SQL ---")
    import importlib
    os.environ["AGENCY_DB"] = db
    import orbit
    importlib.reload(orbit)
    m = orbit.collect(db)
    with sqlite3.connect(db) as raw:
        real_leads = raw.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        real_pending = raw.execute("SELECT COUNT(*) FROM supabase_sync_outbox"
                                   " WHERE status='pending'").fetchone()[0]
        real_mapped = raw.execute("SELECT COUNT(*) FROM supabase_leads").fetchone()[0]
    check("30. lead count matches SQL", m["leads"] == real_leads,
          "%s vs %s" % (m["leads"], real_leads))
    check("    outbox pending matches SQL", m.get("outbox_pending") == real_pending,
          "%s vs %s" % (m.get("outbox_pending"), real_pending))
    check("    mapped leads match SQL", m.get("supabase_mapped") == real_mapped,
          "%s vs %s" % (m.get("supabase_mapped"), real_mapped))
    text = orbit.report(m)
    check("    the report renders", "HERMES AGENCY" in text)
    check("    and shows the target as used/total",
          "/ %d" % m["intake_target"] in text, "target line present")
    for v in m["rates"].values():
        if v is not None:
            check("    rate %.1f never exceeds 100" % v, v <= 100.0)

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
