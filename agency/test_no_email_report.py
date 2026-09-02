#!/usr/bin/env python3
"""Leads without an email address: excluded from the email pipeline, reported
in full to a person every evening.

Everything runs against fakes. No Supabase, no verifier, no Discord.
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import email_verifier as EV        # noqa: E402
import no_email_report as NE       # noqa: E402
import supabase_sync as S          # noqa: E402
import verification_worker as VW   # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-60s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-60s %s" % (name, detail))


def fresh_db(tmp):
    path = os.path.join(tmp, "ne.db")
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


def lead(i, email, **kw):
    d = {"id": "L%03d" % i, "email": email, "status": "ready", "is_active": True,
         "hermes_status": "not_imported", "email_verification_status": None,
         "email_verified": None, "raw_data": {}, "business_name": "Biz %d" % i,
         "city": "Dubai", "country": "AE", "source": "csv_import",
         "phone": None, "whatsapp": None, "instagram_url": None,
         "facebook_url": None, "website": None, "google_maps_url": None}
    d.update(kw)
    return d


class FakeSupabase:
    """Just enough of PostgREST for the worker and the report."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.patches = []

    def call(self, path, method="GET", body=None, prefer=None):
        if method == "PATCH" and path.startswith("leads?id=eq."):
            sid = path.split("eq.")[1].split("&")[0]
            self.rows[sid].update(body or {})
            self.patches.append((sid, dict(body or {})))
            return []
        if method == "GET" and path.startswith("leads?"):
            out = list(self.rows.values())
            if "or=(email.is.null,email.eq.)" in path:
                out = [r for r in out if r.get("email") in (None, "")]
            if "&status=eq.hold" in path:
                out = [r for r in out if r.get("status") == "hold"]
            if "&status=eq.ready" in path:
                out = [r for r in out if r.get("status") == "ready"]
            if "hermes_status=eq.not_imported" in path:
                out = [r for r in out if r.get("hermes_status") == "not_imported"]
            if "email=not.is.null" in path:
                out = [r for r in out if r.get("email") is not None]
            if "email=neq." in path:
                out = [r for r in out if r.get("email") != ""]
            if "email_verification_status=is.null" in path:
                out = [r for r in out if r.get("email_verification_status") is None]
            for st in ("valid", "invalid", "risky", "unknown"):
                if "email_verification_status=eq.%s" % st in path:
                    out = [r for r in out if r.get("email_verification_status") == st]
            return [dict(r) for r in out]
        return []


def main():
    tmp = tempfile.mkdtemp()
    print("=" * 76)
    print("NO-EMAIL LEADS")
    print("=" * 76)

    # ----------------------------------------------------------- 16.1-16.3
    print("\n--- Missing addresses never reach the verifier (16.1-16.3) ---")
    rows = [lead(1, None, whatsapp="+971 55 000 0001"),
            lead(2, "", phone="+971 55 000 0002"),
            lead(3, "   ", instagram_url="https://instagram.com/three"),
            lead(4, "real@example.com", facebook_url="https://fb.com/four"),
            lead(5, "role@example.com", website="https://five.example")]
    fake = FakeSupabase(rows)
    real_call, real_verify, real_base, real_key, real_cfg = (
        S._call, EV.verify_batch, EV.BASE, EV.API_KEY, S.configured)
    asked = []
    S._call = fake.call
    EV.verify_batch = lambda emails: (asked.extend(emails) or [
        {"email": e, "status": "valid", "score": 85, "deliverable": True,
         "reason": "domain_accepts_mail", "flags": [], "did_you_mean": None,
         "mx_host": "mx", "cached": False, "took_ms": 5, "error": None}
        for e in emails])
    EV.BASE, EV.API_KEY = "https://fake", "k"
    S.configured = lambda: True
    try:
        res = VW.tick(limit=50)
    finally:
        S._call, EV.verify_batch, EV.BASE, EV.API_KEY, S.configured = (
            real_call, real_verify, real_base, real_key, real_cfg)
    check("NULL email is never sent to the verifier", "L001" not in str(asked)
          and None not in asked)
    check("empty-string email is never sent", "" not in asked)
    check("whitespace-only email is never sent", "   " not in asked and "" not in asked)
    check("  the two real addresses were", sorted(asked) == ["real@example.com",
                                                             "role@example.com"],
          str(asked))
    check("no-email leads are held out of the claim, not rejected",
          all(fake.rows[i]["status"] == "hold" for i in ("L001", "L002", "L003"))
          and all(fake.rows[i]["hermes_status"] == "not_imported" for i in ("L001", "L002", "L003")))
    check("  classified NO_EMAIL, with the original status remembered",
          all((fake.rows[i]["raw_data"].get("no_email") or {}).get("classification") == "NO_EMAIL"
              and (fake.rows[i]["raw_data"].get("no_email") or {}).get("prev_status") == "ready"
              for i in ("L001", "L002", "L003")))
    check("  and NOT marked invalid",
          all(fake.rows[i]["email_verification_status"] is None
              for i in ("L001", "L002", "L003")))
    check("  with first_seen_at stamped in metadata",
          all((fake.rows[i]["raw_data"].get("no_email") or {}).get("first_seen_at")
              for i in ("L001", "L002", "L003")))
    check("  tick reports what it parked", res.get("no_email_parked") == 3,
          str(res.get("no_email_parked")))

    # ----------------------------------------------------------- 16.4, 16.5
    print("\n--- Never into Hermes, never against the target (16.4, 16.5) ---")
    for i in ("L001", "L002", "L003"):
        ok, why = VW.claim_guard(fake.rows[i])
        check("%s cannot be claimed" % i, not ok and why.startswith("NO_EMAIL"), why)
    check("held leads are invisible to a claim on status=ready",
          not any(r["status"] == "ready" and r["email"] in (None, "", "   ")
                  for r in fake.rows.values()))
    check("the daily target counts imports only (parking is not an import)",
          "imported" not in json.dumps([p[1] for p in fake.patches]))

    # -------------------------------------------------------- 16.11, 16.12
    print("\n--- An address added later re-enters the funnel (16.11-16.13) ---")
    fake.rows["L001"]["email"] = "Owner@One.example"
    S._call = fake.call
    EV.BASE, EV.API_KEY = "https://fake", "k"
    S.configured = lambda: True
    EV.verify_batch = lambda emails: (asked.clear() or asked.extend(emails) or [
        {"email": e, "status": "valid", "score": 85, "deliverable": True,
         "reason": "domain_accepts_mail", "flags": [], "did_you_mean": None,
         "mx_host": "mx", "cached": False, "took_ms": 5, "error": None}
        for e in emails])
    try:
        res = VW.tick(limit=50)
    finally:
        S._call, EV.verify_batch, EV.BASE, EV.API_KEY, S.configured = (
            real_call, real_verify, real_base, real_key, real_cfg)
    check("the lead is restored to the status it had (ready)",
          fake.rows["L001"]["status"] == "ready", fake.rows["L001"]["status"])
    check("  it left the no-email group",
          not NE._is_no_email(fake.rows["L001"]))
    check("  and was verified on the same pass, only then admissible",
          fake.rows["L001"].get("email_verification_status") == "valid"
          and VW.claim_guard(fake.rows["L001"])[0])
    check("  the restored lead's old no-email marker is gone",
          "no_email" not in (fake.rows["L001"].get("raw_data") or {}))

    # ------------------------------------------------------ 17.1-17.9 (part 2)
    print("\n--- The report: who is in it, what it says (17.1-17.9) ---")
    S._call = fake.call
    try:
        listed = NE.fetch_no_email_leads()
    finally:
        S._call = real_call
    ids = [l["id"] for l in listed]
    check("NULL email included", "L002" in ids or "L003" in ids)   # L001 now has one
    check("blank email included", "L002" in ids)
    check("whitespace-only (parked) included", "L003" in ids)
    check("a lead WITH an email is excluded", "L004" not in ids and "L005" not in ids
          and "L001" not in ids, str(ids))
    check("every no-email lead appears exactly once", sorted(ids) == ["L002", "L003"])

    card2 = NE.render_card(1, fake.rows["L002"])
    card3 = NE.render_card(2, fake.rows["L003"])
    check("phone-only lead says Call / SMS", "Call / SMS" in card2 and "+971 55 000 0002" in card2)
    check("Instagram-only lead says Instagram DM", "Instagram DM" in card3)
    wa = NE.render_card(3, lead(9, None, whatsapp="+971 50 1", phone="+971 50 2"))
    check("WhatsApp lead says WhatsApp preferred (over phone)", "WhatsApp preferred" in wa)
    fb = NE.render_card(4, lead(10, None, facebook_url="https://fb.com/x"))
    check("Facebook-only lead says Facebook message", "Facebook message" in fb)
    web = NE.render_card(5, lead(11, None, website="https://x.example"))
    check("website-only lead says Website contact form", "Website contact form" in web)
    gm = NE.render_card(6, lead(12, None, google_maps_url="https://maps.google/x"))
    check("maps-only lead says Google Maps", "Google Maps" in gm)
    none = NE.render_card(7, lead(13, None))
    check("a lead with no channel says so, inventing nothing",
          "No contact details on record" in none)
    check("every card carries the manual-contact footer",
          all(NE.FOOTER in c for c in (card2, card3, wa, fb, web, gm, none)))
    full = NE.render_card(1, lead(20, None, business_name="Prestige", niche="Luxury RE",
                                  area_locality="Marina", city="Dubai", country="AE",
                                  phone="+971 4", owner_name="Sam", main_services="Sales",
                                  main_opportunity="Video tours", priority="hot",
                                  score=98, rating=4.9, review_count=120,
                                  recommended_offer="Website build",
                                  external_lead_id="ext-1"))
    check("full business/contact data is included",
          all(k in full for k in ("Prestige", "Luxury RE", "Marina", "Dubai", "AE",
                                  "+971 4", "Sam", "Sales", "Video tours", "hot",
                                  "98", "4.9", "120", "Website build", "ext-1", "L020")))
    check("empty fields are omitted, never shown as invented values",
          "Not available" not in card2 and "None" not in card2 and "WhatsApp:" not in card2)

    # ----------------------------------------------------------------- 17.10
    print("\n--- Pagination respects Discord (17.10, 17.11, 16.8, 16.9) ---")
    many = [lead(100 + i, None, phone="+971 5%d" % i, main_services="x" * 300,
                 main_opportunity="y" * 300) for i in range(60)]
    cards = [NE.render_card(i, l) for i, l in enumerate(many, 1)]
    pages = NE.paginate(cards)
    check("every message is under the 2,000-char hard limit",
          all(len("**NO EMAIL LEADS — PART 99/99**\n\n" + p) < NE.DISCORD_HARD_LIMIT
              for p in pages), "max %d" % max(len(p) for p in pages))
    check("  and around the 1,800 target", max(len(p) for p in pages) <= NE.PART_TARGET)
    check("every lead appears exactly once across all pages",
          all(sum(p.count("LEAD %d\n" % i) for p in pages) == 1 for i in range(1, 61)))
    check("no card is cut mid-way", all(p.count("--------------------------------") % 2 == 0
                                        for p in pages), "odd separators = split card")
    huge = NE.render_card(1, lead(200, None, main_services="z" * 2500))
    hp = NE.paginate([huge])
    check("a single oversized lead is split cleanly with a continuation marker",
          len(hp) > 1 and any("(cont.)" in p for p in hp[1:]))

    # --------------------------------------------------------- 16.10, 16.14
    print("\n--- New vs still-missing, and no metadata corruption (16.10, 16.14) ---")
    old = lead(300, None, raw_data={"no_email": {"first_seen_at": "2026-09-01T10:00:00"}})
    new = lead(301, None, raw_data={"no_email": {"first_seen_at": "2026-09-02T10:00:00"}})
    s = NE.summary([old, new], "2026-09-02")
    check("a lead first seen today is 'new'", s["new_today"] == 1)
    check("  one seen yesterday is 'still missing'", s["still_missing"] == 1)
    check("  and the two add up to the total", s["new_today"] + s["still_missing"] == s["total"] == 2)
    unstamped = lead(302, None)
    s2 = NE.summary([old, new, unstamped], "2026-09-02")
    check("a lead never stamped counts as NEW, not as still-missing",
          s2["new_today"] == 2 and s2["still_missing"] == 1, str(s2))
    stamp_fake = FakeSupabase([old, new])
    S._call = stamp_fake.call
    try:
        n1 = NE.mark_reported([old, new], "2026-09-02")
        for l in (old, new):
            l["raw_data"] = stamp_fake.rows[l["id"]]["raw_data"]
        n2 = NE.mark_reported([old, new], "2026-09-02")
    finally:
        S._call = real_call
    check("reporting stamps last_reported_at once per day", n1 == 2 and n2 == 0, "%d then %d" % (n1, n2))
    check("  and keeps the original first_seen_at",
          stamp_fake.rows["L300"]["raw_data"]["no_email"]["first_seen_at"] == "2026-09-01T10:00:00")
    check("  touching nothing else on the lead",
          all(set(p[1].keys()) == {"raw_data"} for p in stamp_fake.patches))

    # ------------------------------------------------------ 16.15-16.17
    print("\n--- Country / city / source counts (16.15-16.17) ---")
    mix = [lead(400, None, country="AE", city="Dubai", source="csv_import"),
           lead(401, None, country="AE", city="Dubai", source="csv_import"),
           lead(402, None, country="IN", city="Pune", source="manual"),
           lead(403, None, country=None, city=None, source=None)]
    s = NE.summary(mix, "2026-09-02")
    check("country counts", s["by_country"] == {"AE": 2, "IN": 1, "unknown": 1}, str(s["by_country"]))
    check("city counts", s["by_city"] == {"Dubai": 2, "Pune": 1, "unknown": 1}, str(s["by_city"]))
    check("source counts", s["by_source"] == {"csv_import": 2, "manual": 1, "unknown": 1}, str(s["by_source"]))
    txt = NE.summary_text(s)
    check("the summary states the real total and the owner action",
          "Total no-email leads:       4" in txt and "OWNER" in txt.upper())

    # ---------------------------------------------------- 17.12, 17.13
    print("\n--- Delivery resumes, never duplicates (17.12, 17.13) ---")
    db = fresh_db(tmp)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    built = NE.build("2026-09-02", leads=many[:20])
    calls = []
    fail_at = {"n": 3}

    def flaky_send(method, path, body=None, files=None):
        calls.append(body.get("content", "")[:40] if body else "file")
        if len(calls) == fail_at["n"]:
            return 500, {"error": "discord hiccup"}
        return 200, {"id": "msg%d" % len(calls)}

    r1 = NE.post_all(con, built, channel="chan", send=flaky_send)
    check("a mid-way failure stops the run and is recorded",
          r1["failed"] == 1 and r1["sent"] == 2, str(r1))
    delivered = con.execute("SELECT count(*) FROM report_deliveries WHERE delivered_at IS NOT NULL").fetchone()[0]
    check("  the ledger holds exactly the parts that landed", delivered == 2, str(delivered))
    calls.clear(); fail_at["n"] = -1
    r2 = NE.post_all(con, built, channel="chan", send=flaky_send)
    check("a rerun resumes at the first undelivered part", r2["skipped"] == 2 and r2["failed"] == 0, str(r2))
    check("  and sends the remainder exactly once",
          r2["sent"] == r2["parts"] - 2, "%d of %d" % (r2["sent"], r2["parts"]))
    calls.clear()
    r3 = NE.post_all(con, built, channel="chan", send=flaky_send)
    check("a third run on the same day sends nothing", r3["sent"] == 0 and not calls,
          str(r3))
    total_rows = con.execute("SELECT count(*) FROM report_deliveries").fetchone()[0]
    check("  one ledger row per part, no duplicates", total_rows == r2["parts"], str(total_rows))
    check("the CSV names every lead", built["csv"].decode().count("\n") - 1 == 20)
    check("  with the agreed columns",
          built["csv"].decode().splitlines()[0] == ",".join(NE.CSV_COLUMNS))

    # ------------------------------------------------------------ 17.15
    print("\n--- Never into MailHub (17.15) ---")
    for l in (lead(500, None), lead(501, ""), lead(502, "  ")):
        ok, why = VW.claim_guard(l)
        check("%r cannot be claimed into Hermes, hence never MailHub" % (l["email"],),
              not ok, why)

    print()
    print("=" * 76)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:")
        for f in FAILURES:
            print("  " + f)
    print("=" * 76)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
