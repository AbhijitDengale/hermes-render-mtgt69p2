#!/usr/bin/env python3
"""Tests for Phase F (human review) and Phase G (ORBIT metrics).

Throwaway SQLite, seeded with known data so every number can be checked
against a value worked out by hand rather than against whatever the code
happened to produce.

    python3 test_orbit_review.py
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
import pipeline as P      # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


_SEQ = [0]


def fresh_db(tmp) -> str:
    _SEQ[0] += 1
    path = os.path.join(tmp, "og%d.db" % _SEQ[0])
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


WALK = ["RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE", "COPY_PENDING",
        "COPY_READY", "QA_PENDING", "READY_TO_SEND", "SENT"]


def seed(db, campaign, niche, country, n, final_state=None):
    """n leads driven to SENT, optionally moved on to a terminal state."""
    ids = []
    con = li.connect(db)
    try:
        with con:
            con.execute("INSERT OR IGNORE INTO campaigns (id,name,status,"
                        "followup_schedule) VALUES (?,?, 'active','[\"2m\"]')",
                        (campaign, campaign))
            for i in range(n):
                r = li.ingest_one(con, {
                    "email": "%s-%d@example.com" % (campaign.lower(), i),
                    "business_name": "%s %d" % (campaign, i),
                    "niche": niche, "country": country}, default_campaign=campaign)
                ids.append(r["lead_id"])
    finally:
        con.close()

    with P.connect(db) as con:
        for j, lead in enumerate(ids):
            prev = "NEW"
            for nxt in WALK:
                with P.writing(con):
                    P.transition(con, lead, nxt, "seed", "fixture", expect=prev)
                prev = nxt
            with P.writing(con):
                P.save_draft(con, lead, campaign, 0, "S%d" % j, "B%d" % j)
                con.execute("UPDATE messages SET status='sent' WHERE id=?",
                            (P.message_id(lead, 0),))
            if final_state:
                # Outcomes that follow a reply are only reachable through
                # REPLIED — the state machine is what says so, not the fixture.
                with P.writing(con):
                    if final_state in ("POSITIVE", "NEGATIVE", "MEETING_STAGE"):
                        P.transition(con, lead, "REPLIED", "seed", "fixture")
                    P.transition(con, lead, final_state, "seed", "fixture")
    return ids


def main() -> int:
    tmp = tempfile.mkdtemp()

    print("\n--- 1. Zero data: no invented denominators ---")
    empty = fresh_db(tmp)
    os.environ["AGENCY_DB"] = empty
    os.environ["ORBIT_MIN_SAMPLE"] = "20"
    import importlib
    import orbit
    importlib.reload(orbit)
    m = orbit.collect(empty)
    check("leads = 0", m["leads"] == 0)
    check("outbound = 0", m["outbound_total"] == 0)
    for k, v in m["rates"].items():
        check("%-22s is None, not 0.0" % k, v is None, repr(v))
    check("pct(None) renders as n/a", orbit.pct(None) == "n/a")
    check("best() on no data says insufficient",
          orbit.best([]) == "insufficient data")
    txt = orbit.report(m)
    check("a zero-data report still renders", "HERMES AGENCY" in txt)
    check("  and every rate reads n/a rather than a fabricated zero",
          txt.count("n/a") >= 4, "%d n/a" % txt.count("n/a"))
    check("  it says plainly that the sample is too small",
          "not meaningful" in txt)
    check("  and it does not invent a research summary",
          "no research runs recorded yet" in txt)

    print("\n--- 2. Division by zero is impossible ---")
    check("rate(0,0) is None", orbit.rate(0, 0) is None)
    check("rate(5,0) is None", orbit.rate(5, 0) is None)
    check("rate(1,4) == 25.0", orbit.rate(1, 4) == 25.0)

    print("\n--- 3. Known data gives exact numbers ---")
    db = fresh_db(tmp)
    os.environ["AGENCY_DB"] = db
    importlib.reload(orbit)
    a = seed(db, "C-ALPHA", "saas", "UK", 4, "POSITIVE")
    b = seed(db, "C-BETA", "retail", "US", 3, "NEGATIVE")
    c = seed(db, "C-GAMMA", "saas", "UK", 2, "UNSUBSCRIBED")
    d = seed(db, "C-DELTA", "media", "SG", 1, "MEETING_STAGE")
    seed(db, "C-EPS", "media", "SG", 2)          # left at SENT

    m = orbit.collect(db)
    check("12 leads counted", m["leads"] == 12, str(m["leads"]))
    check("12 initial sends", m["initial_sent"] == 12, str(m["initial_sent"]))
    check("0 follow-ups yet", m["followups_sent"] == 0)
    check("4 POSITIVE", m["by_state"].get("POSITIVE") == 4, str(m["by_state"]))
    check("3 NEGATIVE", m["by_state"].get("NEGATIVE") == 3)
    check("2 UNSUBSCRIBED counted", m["unsubscribes"] == 2)
    check("1 MEETING_STAGE counted", m["meetings"] == 1)
    check("2 still at SENT", m["by_state"].get("SENT") == 2)
    check("bounce rate is 0.0, not None (denominator exists)",
          m["rates"]["bounce_rate"] == 0.0, str(m["rates"]["bounce_rate"]))
    check("unsub rate = 2/12 = 16.7%",
          m["rates"]["unsubscribe_rate"] == 16.7,
          str(m["rates"]["unsubscribe_rate"]))
    check("meeting rate = 1/12 = 8.3%",
          m["rates"]["meeting_rate"] == 8.3, str(m["rates"]["meeting_rate"]))
    check("followup_reply_rate is None (no follow-ups sent)",
          m["rates"]["followup_reply_rate"] is None)

    print("\n--- 4. Breakdowns ---")
    camps = {r["key"]: r for r in m["by_campaign"]}
    check("5 campaigns broken out", len(camps) == 5, str(sorted(camps)))
    check("C-ALPHA has 4 leads, 4 replied", camps["C-ALPHA"]["leads"] == 4
          and camps["C-ALPHA"]["replied"] == 4)
    check("C-EPS has 2 leads, 0 replied", camps["C-EPS"]["leads"] == 2
          and camps["C-EPS"]["replied"] == 0)
    niches = {r["key"]: r for r in m["by_niche"]}
    check("saas groups both campaigns (4+2=6)",
          niches["saas"]["leads"] == 6, str(niches["saas"]))
    countries = {r["key"]: r for r in m["by_country"]}
    check("UK = 6, US = 3, SG = 3",
          countries["UK"]["leads"] == 6 and countries["US"]["leads"] == 3
          and countries["SG"]["leads"] == 3)

    print("\n--- 5. Small samples refuse to name a winner ---")
    check("best campaign is refused below the floor",
          orbit.best(m["by_campaign"]) == "insufficient data",
          orbit.best(m["by_campaign"]))
    big = [{"key": "BIG", "leads": 50, "reply_rate": 12.0},
           {"key": "SMALL", "leads": 2, "reply_rate": 100.0}]
    check("a 2-lead 100% campaign does NOT beat a 50-lead 12% one",
          orbit.best(big) == "BIG (12.0% of 50)", orbit.best(big))
    check("rates carry an insufficient-data note under the floor",
          "insufficient data" in orbit.pct(16.7, 12))
    check("and not above it", "insufficient" not in orbit.pct(16.7, 40))

    print("\n--- 6. Follow-ups and escalations feed the metrics ---")
    with P.connect(db) as con:
        lead = a[0]
        with P.writing(con):
            P.save_draft(con, lead, "C-ALPHA", 1, "F1", "Follow body")
            con.execute("UPDATE messages SET status='sent' WHERE id=?",
                        (P.message_id(lead, 1),))
            con.execute(
                "INSERT INTO human_escalations (id, lead_id, campaign_id,"
                " raised_by, reason, status) VALUES "
                "('H-1',?, 'C-ALPHA','leo','pricing_question','open')", (lead,))
            con.execute(
                "INSERT INTO human_escalations (id, lead_id, campaign_id,"
                " raised_by, reason, status) VALUES "
                "('H-2',?, 'C-ALPHA','leo','contract_request','resolved')", (lead,))
    m2 = orbit.collect(db)
    check("follow-up send counted", m2["followups_sent"] == 1)
    check("outbound total now 13", m2["outbound_total"] == 13)
    check("2 escalations, 1 open, 1 resolved",
          m2["human_reviews"] == 2 and m2["human_reviews_open"] == 1
          and m2["human_reviews_resolved"] == 1)
    check("by_stage separates initial from follow-up",
          m2["by_stage"].get(0) == 12 and m2["by_stage"].get(1) == 1,
          str(m2["by_stage"]))

    print()
    print("--- 6b. No rate can exceed 100% (the '400% reply rate' bug) ---")
    # One lead, emailed twice, replying four times. Counting raw messages
    # against raw replies is what produced a 400% reply rate on live data.
    dbr = fresh_db(tmp)
    noisy = seed(dbr, "C-NOISY", "saas", "UK", 1)[0]
    with P.connect(dbr) as con:
        with P.writing(con):
            P.save_draft(con, noisy, "C-NOISY", 1, "F", "B")
            con.execute("UPDATE messages SET status='sent' WHERE id=?",
                        (P.message_id(noisy, 1),))
            for k in range(4):
                con.execute(
                    "INSERT INTO inbound_replies (provider_message_id, lead_id,"
                    "  campaign_id, from_email, subject, body_text,"
                    "  classification) VALUES (?,?, 'C-NOISY','a@b.c','s','b',"
                    "  'positive')", ("PM-%d" % k, noisy))
            # A reply from a lead we never emailed: drift, not a rate.
            con.execute(
                "INSERT INTO leads (id, campaign_id, email, business_name,"
                "  state) VALUES ('L-GHOST','C-NOISY','g@h.i','Ghost','NEW')")
            con.execute(
                "INSERT INTO inbound_replies (provider_message_id, lead_id,"
                "  campaign_id, from_email, subject, body_text, classification)"
                " VALUES ('PM-G','L-GHOST','C-NOISY','g@h.i','s','b','positive')")
    mn = orbit.collect(dbr)
    check("2 messages sent to 1 lead", mn["outbound_total"] == 2
          and mn["leads_contacted"] == 1, str(mn["leads_contacted"]))
    check("5 raw inbound recorded", mn["replies"] == 5, str(mn["replies"]))
    check("but only 1 lead actually replied", mn["leads_replied"] == 1)
    check("reply rate is 100%, NOT 400%", mn["rates"]["reply_rate"] == 100.0,
          str(mn["rates"]["reply_rate"]))
    for k, v in mn["rates"].items():
        if v is not None:
            check("%-22s never exceeds 100" % k, v <= 100.0, str(v))
    check("the orphan reply is surfaced, not folded into a rate",
          mn["replies_unmatched"] == 1, str(mn["replies_unmatched"]))
    check("and the report says so", "no send on record" in orbit.report(mn))


    print("\n--- 7. ORBIT is read-only ---")
    src = (HERE / "orbit.py").read_text(encoding="utf-8")
    for verb in ("UPDATE ", "DELETE ", "INSERT ", "DROP "):
        check("orbit.py issues no %-7s statement" % verb.strip(),
              verb not in src.upper().replace("P.WRITING", ""))
    before = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM leads").fetchone()[0]
    orbit.collect(db)
    after = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM leads").fetchone()[0]
    check("collecting metrics changes no rows", before == after)

    print("\n--- 8. ECHO bookkeeping: a dispatched follow-up is not re-counted ---")
    db2 = fresh_db(tmp)
    os.environ["AGENCY_DB"] = db2
    lead = seed(db2, "C-BK", "saas", "UK", 1)[0]
    with P.connect(db2) as con:
        with P.writing(con):
            F.schedule(con, lead, "C-BK", 1, "2000-01-01 00:00:00")
            P.transition(con, lead, "FOLLOWUP_WAITING", "t", "sched",
                         expect="SENT")
        fid = "F-%s-1" % lead
        check("the follow-up is due", len(F.due(con)) == 1)
        with P.writing(con):
            P.transition(con, lead, "FOLLOWUP_PENDING", "echo", "due")
            F.mark_dispatched(con, fid)
        row = con.execute("SELECT status, attempts, dispatched_at FROM followups"
                          " WHERE id=?", (fid,)).fetchone()
        check("status becomes 'dispatched'", row["status"] == "dispatched",
              row["status"])
        check("attempts incremented once", row["attempts"] == 1)
        check("dispatched_at recorded", bool(row["dispatched_at"]))
        check("a later tick sees NOTHING due", len(F.due(con)) == 0)
        with P.writing(con):
            F.mark_dispatched(con, fid)
        again = con.execute("SELECT attempts FROM followups WHERE id=?",
                            (fid,)).fetchone()["attempts"]
        check("re-dispatching does NOT increment attempts again", again == 1,
              "attempts=%d" % again)
        with P.writing(con):
            n = F.cancel_all(con, lead, "reply received")
        check("cancellation of a dispatched follow-up is NOT silently changed",
              n == 0, "%d (only 'scheduled' rows cancel)" % n)

    print("\n--- 9. Review actions ---")
    db3 = fresh_db(tmp)
    os.environ["AGENCY_DB"] = db3
    # review.act() opens its own connection from the module default, which was
    # resolved at import; point it at this fixture the same way the process
    # environment would.
    P.DB = db3
    import review
    importlib.reload(review)
    leads = seed(db3, "C-REV", "saas", "UK", 4)
    with P.connect(db3) as con:
        for i, lead in enumerate(leads):
            with P.writing(con):
                F.schedule(con, lead, "C-REV", 1, "2030-01-01 00:00:00")
                P.transition(con, lead, "FOLLOWUP_WAITING", "t", "s",
                             expect="SENT")
                P.transition(con, lead, "HUMAN_REVIEW", "leo", "pricing")
                con.execute(
                    "INSERT INTO human_escalations (id, lead_id, campaign_id,"
                    " raised_by, reason, status, draft_response) VALUES "
                    "(?,?, 'C-REV','leo','pricing_question','open', 'draft')",
                    ("H-%d" % i, lead))

    r = review.act("H-0", "approve", "tester", note="looks fine")
    check("approve resolves the escalation", r.get("action") == "approve")
    check("approve does NOT send — SENTINEL still required",
          "SENTINEL" in (r.get("note") or ""), r.get("note", "")[:50])
    r = review.act("H-0", "approve", "tester")
    check("acting twice on one escalation is refused", r.get("no_change") is True)

    r = review.act("H-1", "close", "tester")
    check("close cancels follow-ups", r.get("followups_cancelled") == 1, str(r))
    check("close moves the lead to CLOSED", r.get("lead_state") == "CLOSED",
          str(r.get("lead_state")))

    r = review.act("H-2", "dnc", "tester")
    check("dnc moves the lead to UNSUBSCRIBED",
          r.get("lead_state") == "UNSUBSCRIBED", str(r.get("lead_state")))
    check("dnc cancels follow-ups", r.get("followups_cancelled") == 1)

    r = review.act("H-3", "edit", "tester", text="Here is what I want said.")
    check("edit saves a NEW draft", r.get("stage") == 1, str(r))
    check("and says it needs a fresh SENTINEL approval",
          "SENTINEL" in (r.get("note") or ""))
    with P.connect(db3) as con:
        d = P.load_draft(con, leads[3], 1)
        check("the edited draft carries NO QA verdict",
              d is not None and d["qa_status"] is None)

    r = review.act("H-99", "approve", "tester")
    check("an unknown escalation id is refused", "no such" in (r.get("error") or ""))
    r = review.act("H-0", "banana", "tester")
    check("an invalid action is refused", "unknown action" in (r.get("error") or ""))

    with P.connect(db3) as con:
        n = con.execute("SELECT COUNT(*) c FROM audit_logs "
                        " WHERE action LIKE 'review.%'").fetchone()["c"]
        check("every review action is audited", n >= 4, "%d audit rows" % n)
        closed = con.execute("SELECT state FROM leads WHERE id=?",
                             (leads[1],)).fetchone()["state"]
        r2 = review.act("H-1", "resume", "tester")
        check("a CLOSED lead cannot be resumed back into outreach",
              r2.get("error") is not None or r2.get("lead_state") == closed,
              str(r2)[:70])

    print("\n" + "=" * 76)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
