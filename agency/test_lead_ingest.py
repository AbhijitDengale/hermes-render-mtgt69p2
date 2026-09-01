#!/usr/bin/env python3
"""Tests for Phase B lead ingestion.

Runs against a THROWAWAY SQLite file built from schema.sql, never
/opt/data/agency.db — the suite inserts junk and asserts on counts.

    python3 test_lead_ingest.py
"""

import csv
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

import lead_ingest as li  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-56s %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh_db(tmp) -> str:
    path = os.path.join(tmp, "test_agency.db")
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    # Every migration, in order. Pinning a subset meant a fixture could drift
    # behind the real schema and a suite would fail on a table the running
    # system has had for weeks.
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


def write_csv(tmp, name, rows, headers):
    path = os.path.join(tmp, name)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def main() -> int:
    tmp = tempfile.mkdtemp()
    db = fresh_db(tmp)

    print("\n--- 1. Normalisation ---")
    check("email is lowercased and trimmed",
          li.normalize_email("  Alice@Example.COM ") == "alice@example.com")
    check("angle brackets are stripped",
          li.normalize_email("<Bob@Example.com>") == "bob@example.com")
    for raw, want in (
            ("example.com", "https://example.com"),
            ("Example.COM/", "https://example.com"),
            ("http://Example.com", "https://example.com"),
            ("https://example.com/path/", "https://example.com/path"),
            ("https://example.com:443", "https://example.com"),
            ("  ", None)):
        got = li.normalize_website(raw)
        check("website %-28r -> %s" % (raw, want), got == want, "got %r" % got)

    print("\n--- 2. Valid CSV import ---")
    headers = ["business_name", "contact_name", "email", "phone", "website",
               "city", "state", "country", "niche", "source", "notes"]
    rows = [
        {"business_name": "Ghost Foundation", "contact_name": "Jane Doe",
         "email": "Jane@Ghost.ORG", "phone": "+1 555 0100",
         "website": "ghost.org", "city": "Singapore", "state": "SG",
         "country": "Singapore", "niche": "publishing", "source": "list-a",
         "notes": "non-profit"},
        {"business_name": "Basecamp", "contact_name": "", "email": "hi@basecamp.com",
         "phone": "", "website": "https://Basecamp.com/", "city": "Chicago",
         "state": "IL", "country": "USA", "niche": "saas", "source": "",
         "notes": ""},
    ]
    path = write_csv(tmp, "leads.csv", rows, headers)
    out = li.ingest_csv(path, default_campaign="C-TEST", db_path=db)
    check("both rows created", out["created"] == 2, str(out))
    check("nothing rejected", out["rejected"] == 0, str(out["rejected_detail"]))

    con = li.connect(db)
    lead = con.execute(
        "SELECT * FROM leads WHERE email='jane@ghost.org'").fetchone()
    check("state initialised to NEW", lead["state"] == "NEW", lead["state"])
    check("email stored normalised", lead["email"] == "jane@ghost.org")
    check("website stored normalised", lead["website"] == "https://ghost.org",
          lead["website"])
    check("CSV 'state' column mapped to region, not the workflow state",
          lead["region"] == "SG" and lead["state"] == "NEW")
    check("campaign applied", lead["campaign_id"] == "C-TEST")
    check("created_at and updated_at set",
          bool(lead["created_at"]) and bool(lead["updated_at"]))
    check("optional fields kept", lead["notes"] == "non-profit")
    check("second row's missing optional fields are NULL, not empty strings",
          con.execute("SELECT contact_name FROM leads WHERE email='hi@basecamp.com'"
                      ).fetchone()["contact_name"] is None)

    print("\n--- 3. Re-importing the SAME file changes nothing ---")
    before = con.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    out2 = li.ingest_csv(path, default_campaign="C-TEST", db_path=db)
    after = con.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    check("re-import creates 0", out2["created"] == 0, str(out2))
    check("re-import reports 2 duplicates", out2["duplicate"] == 2)
    check("row count unchanged", before == after, "%d -> %d" % (before, after))

    print("\n--- 4. Duplicate detection ---")
    with con:
        r = li.ingest_one(con, {"email": "JANE@ghost.org",
                                "business_name": "Ghost (again)"},
                          default_campaign="C-TEST")
    check("same email, different casing -> duplicate", r["status"] == "duplicate",
          str(r))
    with con:
        r = li.ingest_one(con, {"email": "jane@ghost.org",
                                "business_name": "Ghost", "id": "MY-OWN-ID"},
                          default_campaign="C-TEST")
    check("caller-supplied id cannot bypass the unique index",
          r["status"] == "duplicate", str(r))
    with con:
        r = li.ingest_one(con, {"email": "jane@ghost.org",
                                "business_name": "Ghost"},
                          default_campaign="C-OTHER")
    check("the same person IS allowed in a different campaign",
          r["status"] == "created", str(r))

    print("\n--- 5. Validation ---")
    for bad, why in (("not-an-email", "no @"),
                     ("a@b", "no dot in domain"),
                     ("a b@example.com", "space"),
                     ("a@@example.com", "double @"),
                     ("", "empty")):
        with con:
            r = li.ingest_one(con, {"email": bad, "business_name": "X"},
                              default_campaign="C-TEST")
        check("rejects %-18s (%s)" % (repr(bad), why),
              r["status"] == "rejected", r.get("reason", ""))
    with con:
        r = li.ingest_one(con, {"email": "ok@example.com"},
                          default_campaign="C-TEST")
    check("rejects a missing required field (business_name)",
          r["status"] == "rejected" and "business_name" in r["reason"],
          r.get("reason", ""))
    with con:
        r = li.ingest_one(con, {"business_name": "No Email Ltd"},
                          default_campaign="C-TEST")
    check("rejects a missing email", r["status"] == "rejected",
          r.get("reason", ""))
    with con:
        r = li.ingest_one(con, {"email": "minimal@example.com",
                                "business_name": "Minimal Ltd"},
                          default_campaign="C-TEST")
    check("accepts a lead with ONLY the required fields",
          r["status"] == "created", str(r))

    print("\n--- 6. A malformed row does not abort the import ---")
    mixed = write_csv(tmp, "mixed.csv",
                      [{"business_name": "Good Co", "email": "good@example.com"},
                       {"business_name": "Bad Co", "email": "nope"},
                       {"business_name": "", "email": "noname@example.com"},
                       {"business_name": "Also Good", "email": "also@example.com"}],
                      ["business_name", "email"])
    out3 = li.ingest_csv(mixed, default_campaign="C-MIX", db_path=db)
    check("good rows still imported around the bad ones",
          out3["created"] == 2 and out3["rejected"] == 2, str(out3))

    print("\n--- 7. Ingestion is logged ---")
    ev = con.execute("SELECT COUNT(*) c FROM events "
                     "WHERE event_type='lead.ingested'").fetchone()["c"]
    au = con.execute("SELECT COUNT(*) c FROM audit_logs "
                     "WHERE action='lead.created'").fetchone()["c"]
    leads = con.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    check("one event per created lead", ev == leads, "%d events, %d leads" % (ev, leads))
    check("one audit row per created lead", au == leads)
    check("the event records the NEW transition",
          con.execute("SELECT to_state FROM events WHERE event_type='lead.ingested' "
                      "LIMIT 1").fetchone()["to_state"] == "NEW")

    print("\n--- 8. Nothing downstream was touched ---")
    for table in ("messages", "send_jobs", "followups", "agent_tasks",
                  "agent_runs", "human_escalations", "email_threads"):
        n = con.execute("SELECT COUNT(*) c FROM %s" % table).fetchone()["c"]
        check("%-18s untouched" % table, n == 0, "%d rows" % n)
    states = [r["state"] for r in con.execute("SELECT DISTINCT state FROM leads")]
    check("every lead is still NEW — no pipeline started",
          states == ["NEW"], str(states))

    print("\n--- 9. Sample row ---")
    s = con.execute("SELECT id, business_name, email, website, region, country,"
                    " niche, campaign_id, source, state, created_at "
                    "FROM leads WHERE email='jane@ghost.org'").fetchone()
    for k in s.keys():
        print("    %-14s %s" % (k, s[k]))
    con.close()

    print("\n" + "=" * 70)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
