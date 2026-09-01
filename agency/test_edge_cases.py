#!/usr/bin/env python3
"""The two edge cases the V1 audit turned up.

A. A campaign marked paused went on sending, because nothing consulted
   campaigns.status. A pause has to be reversible, so a held follow-up stays
   scheduled and its attempt count is untouched.

B. A deleted-and-re-ingested lead could never be worked again: Kanban
   idempotency keys were derived from the deterministic lead id, so the new
   lifecycle matched the COMPLETED task from the old one.

    python3 test_edge_cases.py
"""

import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

import followups as F     # noqa: E402
import lead_ingest as li  # noqa: E402
import orchestrator as O  # noqa: E402
import pipeline as P      # noqa: E402

PASS, FAIL = [], []
_SEQ = [0]


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-60s %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh(tmp):
    _SEQ[0] += 1
    path = os.path.join(tmp, "e%d.db" % _SEQ[0])
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


WALK = ["RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE", "COPY_PENDING",
        "COPY_READY", "QA_PENDING", "READY_TO_SEND", "SENT"]


def seed_sent(con, lead, camp, status="active"):
    with P.writing(con):
        con.execute("INSERT OR IGNORE INTO campaigns (id,name,status,"
                    "followup_schedule) VALUES (?,?,?, ?)",
                    (camp, camp, status, '["2m"]'))
        con.execute("INSERT INTO leads (id,campaign_id,email,business_name,"
                    "state) VALUES (?,?, 'a@b.c','Co','NEW')", (lead, camp))
        prev = "NEW"
        for nxt in WALK:
            P.transition(con, lead, nxt, "t", "seed", expect=prev)
            prev = nxt
        F.schedule(con, lead, camp, 1, "2000-01-01 00:00:00")
        P.transition(con, lead, "FOLLOWUP_WAITING", "t", "s", expect="SENT")


def main():
    tmp = tempfile.mkdtemp()

    # ---------------------------------------------------------------- A ---
    print("--- A. A paused campaign blocks ECHO ---")
    db = fresh(tmp)
    with P.connect(db) as con:
        seed_sent(con, "L-ACTIVE", "C-ON", status="active")
        seed_sent(con, "L-PAUSED", "C-OFF", status="paused")

        due = {r["lead_id"]: r for r in F.due(con)}
        check("both follow-ups are due by time", len(due) == 2, str(list(due)))

        check("the active campaign's follow-up is NOT blocked",
              F.blocked_reason(due["L-ACTIVE"]) is None,
              str(F.blocked_reason(due["L-ACTIVE"])))
        reason = F.blocked_reason(due["L-PAUSED"])
        check("the paused campaign's follow-up IS blocked", bool(reason), reason)
        check("  and the reason names the campaign and its status",
              "C-OFF" in (reason or "") and "paused" in (reason or ""), reason)
        check("is_paused agrees", F.is_paused(due["L-PAUSED"])
              and not F.is_paused(due["L-ACTIVE"]))

        # Run the real tick, not a reimplementation of it.
        os.environ["AGENCY_DB"] = db
        import importlib
        import echo_tick
        importlib.reload(echo_tick)
        P.DB = db
        lines = echo_tick.tick()
        held = [l for l in lines if l.startswith("hold")]
        moved = [l for l in lines if l.startswith("due")]
        check("ECHO holds the paused one", len(held) == 1, str(held))
        check("  and dispatches the active one", len(moved) == 1, str(moved))

        row = con.execute("SELECT status, attempts, last_blocked_reason,"
                          "       last_blocked_at FROM followups"
                          " WHERE lead_id='L-PAUSED'").fetchone()
        check("the held follow-up is STILL scheduled (reversible)",
              row["status"] == "scheduled", row["status"])
        check("  attempts was NOT incremented", row["attempts"] == 0,
              "attempts=%d" % row["attempts"])
        check("  the block reason is recorded on the row",
              "paused" in (row["last_blocked_reason"] or ""),
              row["last_blocked_reason"])
        check("  with a timestamp", bool(row["last_blocked_at"]))
        check("  and the lead did not move",
              P.get_lead(con, "L-PAUSED")["state"] == "FOLLOWUP_WAITING")

        ev = con.execute("SELECT COUNT(*) c FROM events WHERE lead_id='L-PAUSED'"
                         "   AND event_type='followup.blocked'").fetchone()["c"]
        check("  one auditable event was written", ev == 1, "%d event(s)" % ev)

        # Ten more ticks must not add ten more events or ten more attempts.
        for _ in range(10):
            echo_tick.tick()
        row2 = con.execute("SELECT attempts FROM followups"
                           " WHERE lead_id='L-PAUSED'").fetchone()
        ev2 = con.execute("SELECT COUNT(*) c FROM events WHERE lead_id='L-PAUSED'"
                          "   AND event_type='followup.blocked'").fetchone()["c"]
        check("ten more ticks add no attempts", row2["attempts"] == 0,
              "attempts=%d" % row2["attempts"])
        check("  and no repeated events", ev2 == 1, "%d event(s)" % ev2)

        # Resuming the campaign must bring it straight back.
        with P.writing(con):
            con.execute("UPDATE campaigns SET status='active' WHERE id='C-OFF'")
        again = {r["lead_id"]: r for r in F.due(con)}
        check("resuming the campaign unblocks the follow-up",
              "L-PAUSED" in again
              and F.blocked_reason(again["L-PAUSED"]) is None)
        lines = echo_tick.tick()
        check("  and ECHO then dispatches it",
              any(l.startswith("due") and "L-PAUSED" in l for l in lines),
              str(lines))

    print("\n--- A2. draft and archived count as not running ---")
    for status in ("draft", "archived", "paused"):
        db = fresh(tmp)
        with P.connect(db) as con:
            seed_sent(con, "L-X", "C-X", status=status)
            r = F.due(con)[0]
            check("campaign status %-9s blocks the follow-up" % status,
                  bool(F.blocked_reason(r)), F.blocked_reason(r))
    db = fresh(tmp)
    with P.connect(db) as con:
        seed_sent(con, "L-Y", "C-Y", status="active")
        r = F.due(con)[0]
        check("campaign status active    does not block",
              F.blocked_reason(r) is None)
        # A follow-up with no campaign attached must not silently halt: the
        # LEFT JOIN yields NULL and COALESCE has to read that as 'active',
        # or a bookkeeping gap would quietly stop live outreach.
        with P.writing(con):
            con.execute("UPDATE followups SET campaign_id=NULL"
                        " WHERE lead_id='L-Y'")
        r = F.due(con)[0]
        check("an unattached campaign is treated as active, not paused",
              F.blocked_reason(r) is None, str(F.blocked_reason(r)))
        check("  and is_paused agrees", not F.is_paused(r))

    print()
    print("--- A3. A paused campaign also stops MAYA, not just ECHO ---")
    db = fresh(tmp)
    with P.connect(db) as con:
        # Two leads at the very start of the pipeline, one campaign running.
        for lead, camp, status in (("L-GO", "C-GO", "active"),
                                   ("L-NO", "C-NO", "paused")):
            with P.writing(con):
                con.execute("INSERT OR IGNORE INTO campaigns (id,name,status,"
                            "followup_schedule) VALUES (?,?,?,?)",
                            (camp, camp, status, '["2m"]'))
                con.execute("INSERT INTO leads (id,campaign_id,email,"
                            "business_name,state) VALUES (?,?, 'a@b.c','Co',"
                            "'NEW')", (lead, camp))
        got = {r["id"] for r in P.eligible(con, "NEW", 10)}
        check("MAYA sees the running campaign's lead", "L-GO" in got, str(got))
        check("  and NOT the paused campaign's lead", "L-NO" not in got, str(got))

        # It must hold at every stage, not only at intake.
        for state in ("RESEARCH_PENDING", "COPY_PENDING", "QA_PENDING",
                      "READY_TO_SEND", "FOLLOWUP_PENDING"):
            with P.writing(con):
                con.execute("UPDATE leads SET state=? WHERE id IN ('L-GO','L-NO')",
                            (state,))
            got = {r["id"] for r in P.eligible(con, state, 10)}
            check("  %-17s paused lead excluded" % state, "L-NO" not in got,
                  str(got))

        with P.writing(con):
            con.execute("UPDATE campaigns SET status='active' WHERE id='C-NO'")
        got = {r["id"] for r in P.eligible(con, "FOLLOWUP_PENDING", 10)}
        check("resuming the campaign makes MAYA see it again",
              "L-NO" in got, str(got))


    print()
    print("--- A4. Importing into a new campaign must not silently stall ---")
    db = fresh(tmp)
    con = li.connect(db)
    try:
        with con:
            r = li.ingest_one(con, {"email": "new@example.com",
                                    "business_name": "Brand New"},
                              default_campaign="C-BRAND-NEW")
            status = con.execute("SELECT status FROM campaigns WHERE id=?",
                                 ("C-BRAND-NEW",)).fetchone()[0]
    finally:
        con.close()
    check("an auto-created campaign is active, not draft",
          status == "active", status)
    with P.connect(db) as pcon:
        got = {x["id"] for x in P.eligible(pcon, "NEW", 10)}
        check("  so MAYA picks the imported lead up",
              r["lead_id"] in got, str(got))
        # A campaign somebody deliberately left as draft still holds.
        with P.writing(pcon):
            pcon.execute("UPDATE campaigns SET status='draft'"
                         " WHERE id='C-BRAND-NEW'")
        got = {x["id"] for x in P.eligible(pcon, "NEW", 10)}
        check("  while a deliberately drafted campaign still holds",
              r["lead_id"] not in got, str(got))


    # ---------------------------------------------------------------- B ---
    print("\n--- B. A re-ingested lead gets a fresh task generation ---")
    db = fresh(tmp)
    con = li.connect(db)
    try:
        with con:
            con.execute("INSERT OR IGNORE INTO campaigns (id,name,status,"
                        "followup_schedule) VALUES ('C-G','C-G','active',?)",
                        ('["2m"]',))
            first = li.ingest_one(con, {"email": "gen@example.com",
                                        "business_name": "Gen Co"},
                                  default_campaign="C-G")
        lead_id = first["lead_id"]
        check("first ingestion creates the lead", first["status"] == "created")
        with con:
            g1 = li.generation_of(con, lead_id)
        check("  at generation 1", g1 == 1, "gen=%d" % g1)

        with con:
            dup = li.ingest_one(con, {"email": "gen@example.com",
                                      "business_name": "Gen Co"},
                                default_campaign="C-G")
            g_dup = li.generation_of(con, lead_id)
        check("re-ingesting a LIVE lead is a duplicate",
              dup["status"] == "duplicate", str(dup))
        check("  and does NOT bump the generation", g_dup == 1, "gen=%d" % g_dup)

        # Delete the lead the way the test harnesses do, then re-ingest.
        with con:
            con.execute("DELETE FROM events WHERE lead_id=?", (lead_id,))
            con.execute("DELETE FROM leads WHERE id=?", (lead_id,))
            second = li.ingest_one(con, {"email": "gen@example.com",
                                         "business_name": "Gen Co"},
                                   default_campaign="C-G")
            g2 = li.generation_of(con, second["lead_id"])
        check("re-ingesting after deletion creates it again",
              second["status"] == "created")
        check("  with the SAME deterministic lead id",
              second["lead_id"] == lead_id, second["lead_id"])
        check("  but generation 2", g2 == 2, "gen=%d" % g2)
        check("  because the counter outlives the lead row",
              con.execute("SELECT generation FROM lead_generations"
                          " WHERE lead_id=?", (lead_id,)).fetchone()[0] == 2)
    finally:
        con.close()

    print("\n--- B2. What the Kanban key actually becomes ---")
    keys = []

    def fake_dispatch(lead_id, profile, title, body, stage_key, generation=1):
        keys.append("agency:%s:gen:%d:%s" % (lead_id, generation, stage_key))
        return {"id": "t_fake", "ok": True}

    real, O.dispatch = O.dispatch, fake_dispatch
    try:
        with P.connect(db) as pcon:
            with P.writing(pcon):
                P.transition(pcon, lead_id, "RESEARCH_PENDING", "t", "seed",
                             expect="NEW")
            lead = P.get_lead(pcon, lead_id)
            O.dispatch_research(pcon, lead)
            gen2_key = keys[-1]
            # Same lead, rewound to its first lifecycle.
            with P.writing(pcon):
                pcon.execute("UPDATE leads SET lifecycle_generation=1,"
                             " state='RESEARCH_PENDING' WHERE id=?", (lead_id,))
            lead = P.get_lead(pcon, lead_id)
            O.dispatch_research(pcon, lead)
            gen1_key = keys[-1]
    finally:
        O.dispatch = real

    check("the key carries the generation", ":gen:2:" in gen2_key, gen2_key)
    check("two lifecycles produce DIFFERENT keys", gen1_key != gen2_key,
          "%s vs %s" % (gen1_key[-24:], gen2_key[-24:]))
    check("  so an old completed task cannot block the new lifecycle",
          gen1_key.replace(":gen:1:", "") == gen2_key.replace(":gen:2:", ""),
          "identical apart from the generation")

    print("\n--- B3. Within one lifecycle the key is still idempotent ---")
    keys.clear()
    real, O.dispatch = O.dispatch, fake_dispatch
    try:
        with P.connect(db) as pcon:
            with P.writing(pcon):
                pcon.execute("UPDATE leads SET lifecycle_generation=7,"
                             " state='RESEARCH_PENDING' WHERE id=?", (lead_id,))
            for _ in range(5):
                # A retry is a tick that finds the lead still RESEARCH_PENDING
                # — it lost a race, or the process restarted mid-stage. Each
                # one must land on the same task, not spawn another worker.
                with P.writing(pcon):
                    pcon.execute("UPDATE leads SET state='RESEARCH_PENDING'"
                                 " WHERE id=?", (lead_id,))
                lead = P.get_lead(pcon, lead_id)
                O.dispatch_research(pcon, lead)
    finally:
        O.dispatch = real
    check("five retries in one lifecycle produce one key",
          len(set(keys)) == 1, "%d distinct of %d" % (len(set(keys)), len(keys)))
    check("  and it is the generation-7 key", ":gen:7:" in keys[0], keys[0])

    print("\n--- B4. Concurrent ingests cannot collide on a generation ---")
    db2 = fresh(tmp)
    con = li.connect(db2)
    try:
        with con:
            con.execute("INSERT OR IGNORE INTO campaigns (id,name,status,"
                        "followup_schedule) VALUES ('C-C','C-C','active',?)",
                        ('["2m"]',))
        seen = set()
        for _ in range(6):
            with con:
                seen.add(li.bump_generation(con, "L-RACE"))
        check("six bumps give six distinct generations",
              seen == {1, 2, 3, 4, 5, 6}, str(sorted(seen)))
    finally:
        con.close()

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
