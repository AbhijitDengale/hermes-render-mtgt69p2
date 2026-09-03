#!/usr/bin/env python3
"""ORBIT's daily report as Discord embed cards: presentation tests.

Everything runs against fixtures and fakes: a temporary agency.db, a fake cron
store, a fake Discord. No network. The numbers a card shows must be the numbers
orbit.collect() produced, and building cards must change nothing.
"""
import copy
import datetime
import json
import os
import pathlib
import re
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import lead_ingest as li        # noqa: E402
import no_email_report as NE    # noqa: E402
import orbit                    # noqa: E402
import orbit_embeds as OE       # noqa: E402
import pipeline as P            # noqa: E402

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


# --- fixtures -----------------------------------------------------------------

def fresh_db(tmp, name="oe.db"):
    path = os.path.join(tmp, name)
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


JOB_NAMES = ["maya-orchestrator", "supabase-lead-sync", "email-verifier",
             "echo-followups", "leo-inbound", "review-alerts", "orbit-daily"]


def jobs_file(tmp, stale=None, failed=None, name="jobs.json"):
    now = datetime.datetime.now(datetime.timezone.utc)
    jobs = []
    for job in JOB_NAMES:
        age = 45 if job == stale else (2 if job != "orbit-daily" else 60)
        jobs.append({"name": job, "id": "j-" + job, "schedule_display": "every 2m",
                     "deliver": "local", "last_status": "error" if job == failed else "ok",
                     "last_run_at": (now - datetime.timedelta(minutes=age)).isoformat(),
                     "repeat": {"completed": 5}})
    path = os.path.join(tmp, name)
    json.dump({"jobs": jobs}, open(path, "w", encoding="utf-8"))
    return path


def automation(tmp, **kw):
    path = jobs_file(tmp, **kw)
    real = orbit.CRON_JOBS_PATH
    orbit.CRON_JOBS_PATH = path
    try:
        return orbit.automation_health()
    finally:
        orbit.CRON_JOBS_PATH = real


TENANTS = [
    ("abhijitdeng20187", 2, "abhijitdeng20187@gmail.com", 15),
    ("minhulisa", 3, "minhulisa@gmail.com", 12),
    ("dnyandev887", 4, "dnyandev887@gmail.com", 10),
    ("dnyandevdeng", 5, "dnyandevdeng@gmail.com", 9),
    ("minhuli2005", 6, "minhuli2005@gmail.com", 9),
]
IDENTITIES = {"2": "Abhiji <abhijit@syntrix.cv>", "3": "Lisa Chen <demon@socialnexa.cv>",
              "4": "Darki <darki@socialnexa.cv>", "5": "Ethan Cole <ethan@socialnexa.cv>",
              "6": "Sophie Laurent <minhu@socialnexa.cv>"}
PROFESSIONAL = ["abhijit@syntrix.cv", "demon@socialnexa.cv", "darki@socialnexa.cv",
                "ethan@socialnexa.cv", "minhu@socialnexa.cv"]


def metrics(auto):
    """A metrics dict shaped exactly like orbit.collect() returns it."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tenants = []
    for name, uid, box, sent in TENANTS:
        tenants.append({"tenant_name": name, "user_id": uid, "mailbox_email": box,
                        "health": "warming", "daily_limit": 70, "sent_today": sent,
                        "mailbox_ok": 1, "queue_ok": 1, "approve_ok": 1, "leo_ok": 1,
                        "mailbox_checked_at": now, "ready": True, "remaining": 70 - sent})
    return {
        "leads": 58, "by_state": {"RESEARCHING": 3, "RESEARCH_PENDING": 2, "COPY_READY": 8,
                                  "READY_TO_SEND": 5, "QA_REJECTED": 0},
        "research_complete": 20, "research_failed": 2,
        "initial_sent": 14, "followups_sent": 3, "outbound_total": 17,
        "sent_as": [("Lisa Chen <demon@socialnexa.cv>", 9), ("Darki <darki@socialnexa.cv>", 5),
                    (None, 3)],
        "send_failures": 0, "queued": 4,
        "leads_contacted": 17, "leads_replied": 4, "replies": 12, "replies_unmatched": 8,
        "replies_classified": 10,
        "by_classification": {"positive": 2, "pricing_question": 1, "meeting_request": 2,
                              "negative": 1, "out_of_office": 1},
        "positive_replies": 2, "negative_replies": 1, "unsubscribes": 0, "bounces": 0,
        "meetings": 2, "human_reviews": 3, "human_reviews_open": 1, "human_reviews_resolved": 2,
        "followups": {}, "leads_followed_up": 3, "followup_replies": 1,
        "by_campaign": [], "by_country": [], "by_niche": [], "by_stage": {0: 14, 1: 3},
        "rates": {"reply_rate": 23.5, "positive_reply_rate": 11.8, "negative_reply_rate": 5.9,
                  "bounce_rate": 0.0, "unsubscribe_rate": 0.0, "meeting_rate": 5.9,
                  "followup_reply_rate": 33.3},
        "sample": 17, "min_sample": 20,
        "intake_day": "2026-09-02", "intake_today": 58, "intake_target": 400,
        "intake_remaining": 342, "outbox_pending": 4, "outbox_failed": 0,
        "supabase_mapped": 58, "supabase_ready": 267, "timezone": "Asia/Kolkata",
        "research": {}, "automation": auto,
        "no_email": {"total": 340, "new_today": 340, "still_missing": 0,
                     "by_country": {"AE": 340}, "by_city": {"Dubai": 300, "Abu Dhabi": 40},
                     "by_source": {"csv_import": 340}},
        "verification": {"valid": 58, "invalid": 9, "risky": 259, "unknown": 1, "pending": 4,
                         "error": None, "no_email": 340},
        "verification_completed": 327, "verification_pass_rate": 17.7,
        "tenants": tenants, "capacity_configured": 350, "capacity_usable": 295,
        "tenants_ready": 5,
        "senders": [{"email": "minhulisa@gmail.com", "health": "warming", "enabled": True,
                     "sent_today": 12, "effective_daily_limit": 70, "daily_limit": 70,
                     "sent_total": 40, "consecutive_errors": 0,
                     "from_email": "demon@socialnexa.cv", "from_name": "Lisa Chen",
                     "identity_status": "verified"}],
        "sender_warnings": ["dnyandev887@gmail.com has reached today's cap (70) — "
                            "allocation is the limit, not a fault"],
    }


def lead(i, **kw):
    d = {"id": "L%03d" % i, "email": None, "business_name": "Biz %d" % i,
         "business_type": "Real Estate", "niche": "real estate", "area_locality": "Dubai Marina",
         "city": "Dubai", "country": "AE", "priority": "hot", "score": 90 + i,
         "phone": None, "whatsapp": None, "instagram_url": None, "facebook_url": None,
         "website": None, "google_maps_url": None, "owner_name": None,
         "main_opportunity": None, "recommended_offer": None, "raw_data": {},
         "source": "csv_import"}
    d.update(kw)
    return d


def built_for(leads, day="2026-09-02"):
    return {"day": day, "leads": leads, "summary": NE.summary(leads, day),
            "csv": b"business_name,phone\nBiz 1,+971\n", "pages": [], "header": "",
            "summary_text": ""}


def all_text(embeds):
    return json.dumps(embeds, ensure_ascii=False)


def titles(messages):
    return [e["title"] for msg in messages for e in msg.get("embeds") or []]


EXEC_TITLES = ["📊 HERMES AGENCY — DAILY REPORT", "📥 LEADS", "✉️ OUTREACH", "💬 REPLIES",
               "📈 PERFORMANCE", "📬 SENDER CAPACITY", "✅ EMAIL VERIFICATION",
               "⚙️ AUTOMATION HEALTH", "🔄 PIPELINE HEALTH", "🚨 NEEDS ATTENTION",
               "📌 TODAY'S SUMMARY", "📞 NO EMAIL — MANUAL CONTACT REQUIRED"]


def main() -> int:
    tmp = tempfile.mkdtemp()
    os.environ["ORBIT_SENDER_IDENTITIES"] = json.dumps(IDENTITIES)
    empty_db = fresh_db(tmp, "empty.db")

    print("=" * 76)
    print("ORBIT DAILY REPORT — DISCORD EMBED CARDS")
    print("=" * 76)

    # ------------------------------------------------------------ healthy day
    print("\n--- 1-3. Embeds, main card, order ---")
    m = metrics(automation(tmp))
    ids = OE.sender_identities(db=empty_db, m=m)
    leads = [lead(1, whatsapp="+971 55 000 0001", phone="+971 4 000 0001",
                  website="https://one.example", owner_name="Amira",
                  main_opportunity="No booking flow on the site.",
                  recommended_offer="Landing page + WhatsApp booking"),
             lead(2, priority="warm", website="https://two.example"),
             lead(3, priority="cold", instagram_url="https://instagram.com/three"),
             lead(4, google_maps_url="https://maps.google.com/?cid=4"),
             lead(5)]
    built = built_for(leads)
    messages = OE.build_messages(m, built, ids)
    embeds = [e for msg in messages for e in msg.get("embeds") or []]
    plain = orbit.report(m)
    check("1. the report is embeds, not one plaintext message",
          all(msg.get("embeds") or msg.get("file") for msg in messages)
          and not any(msg.get("content", "").startswith("**HERMES") for msg in messages)
          and all(len(msg.get("content") or "") < 200 for msg in messages),
          "%d messages, %d embeds" % (len(messages), len(embeds)))
    check("   the plaintext report is never posted",
          not any(plain[:60] in (msg.get("content") or "") for msg in messages))
    head = embeds[0]
    names = [f["name"] for f in head.get("fields", [])]
    check("2. the main report card exists, first, with its fields",
          head["title"] == "📊 HERMES AGENCY — DAILY REPORT"
          and head.get("description") == "Daily Agency Operations Dashboard"
          and names == ["Date", "Timezone", "System Status", "Daily Target", "Sender Capacity"]
          and head["footer"]["text"] == "Generated automatically by ORBIT", str(names))
    vals = {f["name"]: f["value"] for f in head["fields"]}
    check("   header values come from the metrics",
          vals["Date"] == "02 Sep 2026" and vals["Timezone"] == "Asia/Kolkata"
          and vals["Daily Target"].startswith("58 / 400") and vals["Sender Capacity"] == "295 remaining",
          str(vals))
    t = titles(messages)
    check("3. section cards appear in the required order",
          t[:12] == EXEC_TITLES, " | ".join(t[:12]))
    check("   lead cards follow the no-email header, nothing else in between",
          all((e.get("footer") or {}).get("text") == OE.LEAD_FOOTER for e in embeds[12:]),
          str([e["title"] for e in embeds[12:15]]))
    check("   message grouping: header+leads+outreach+replies, then performance..",
          [len(msg.get("embeds") or []) for msg in messages[:3]] == [4, 4, 3])

    # ---------------------------------------------------------- 4-5. senders
    print("\n--- 4-5. Professional identities shown, transport hidden ---")
    senders = next(e for e in embeds if e["title"] == "📬 SENDER CAPACITY")
    sender_names = [f["name"] for f in senders["fields"]][3:]
    check("4. one field per professional sender, by display name",
          sender_names == ["Abhiji", "Lisa Chen", "Darki", "Ethan Cole", "Sophie Laurent"],
          str(sender_names))
    lisa = next(f for f in senders["fields"] if f["name"] == "Lisa Chen")["value"]
    check("   each shows address, sent/limit, remaining, status",
          lisa.splitlines()[0] == "demon@socialnexa.cv" and "Sent: 12 / 70" in lisa
          and "Remaining: 58" in lisa and "Status: ✅ Ready" in lisa, lisa.replace("\n", " | "))
    top = {f["name"]: f["value"] for f in senders["fields"][:3]}
    check("   capacity totals are the metrics' own",
          top == {"Active Senders": "5 / 5", "Configured Capacity": "350/day",
                  "Remaining Today": "295"}, str(top))
    exec_text = all_text(embeds[:12])
    check("5. no raw Gmail transport address anywhere in the executive cards",
          "@gmail.com" not in exec_text.lower() and "googlemail" not in exec_text.lower())
    check("   every professional address appears", all(p in exec_text for p in PROFESSIONAL))
    attention = next(e for e in embeds if e["title"] == "🚨 NEEDS ATTENTION")
    check("   a sender warning names the identity, not the mailbox",
          "darki@socialnexa.cv has reached today's cap" in attention["description"],
          attention["description"][:120])
    check("   an unknown Gmail address is neutralised",
          OE.hide_transport("someone@gmail.com is paused", ids) == "a Gmail mailbox is paused")
    outreach = next(e for e in embeds if e["title"] == "✉️ OUTREACH")
    sent_as = next(f for f in outreach["fields"] if f["name"] == "Sent As")["value"]
    check("   'Sent as' breakdown keeps the unrecorded sender visible",
          "Lisa Chen <demon@socialnexa.cv> — 9" in sent_as and "(sender not recorded) — 3" in sent_as,
          sent_as.replace("\n", " | "))

    # ------------------------------------------------ identity sources
    print("\n--- Identity sources ---")
    del os.environ["ORBIT_SENDER_IDENTITIES"]
    db = fresh_db(tmp, "ident.db")
    with P.connect(db) as con:
        con.execute("INSERT OR IGNORE INTO campaigns (id, name, status, followup_schedule)"
                    " VALUES ('C-1','C-1','active','[\"2m\"]')")
        r = li.ingest_one(con, {"email": "p@example.com", "business_name": "P",
                                "niche": "x", "country": "AE"}, default_campaign="C-1")
        prev = "NEW"
        for nxt in ["RESEARCH_PENDING", "RESEARCHING", "RESEARCH_COMPLETE", "COPY_PENDING",
                    "COPY_READY", "QA_PENDING", "READY_TO_SEND", "SENT"]:
            with P.writing(con):
                P.transition(con, r["lead_id"], nxt, "seed", "fixture", expect=prev)
            prev = nxt
        with P.writing(con):
            P.save_draft(con, r["lead_id"], "C-1", 0, "S", "B")
            con.execute("UPDATE messages SET status='sent', tenant_user_id=4,"
                        " from_email='Darki <darki@socialnexa.cv>', sent_at='2026-09-03T03:53:52Z'"
                        " WHERE id=?", (P.message_id(r["lead_id"], 0),))
    ids2 = OE.sender_identities(db=db, m=m)
    check("identity resolved from the sender recorded on a sent message",
          ids2["by_user"].get(4) == {"name": "Darki", "email": "darki@socialnexa.cv"},
          str(ids2["by_user"].get(4)))
    check("  and from MailHub's identity fields on ORBIT's own mailbox",
          ids2["by_user"].get(3) == {"name": "Lisa Chen", "email": "demon@socialnexa.cv"},
          str(ids2["by_user"].get(3)))
    check("  transport -> professional map built for hiding",
          ids2["by_transport"].get("dnyandev887@gmail.com") == "darki@socialnexa.cv")
    m_unknown = metrics(automation(tmp))
    senders2 = OE.card_senders(m_unknown, ids2)
    txt2 = all_text(senders2)
    check("  a tenant without a known identity is still never shown as Gmail",
          "@gmail.com" not in txt2 and "identity not yet recorded" in txt2)
    os.environ["ORBIT_SENDER_IDENTITIES"] = json.dumps(IDENTITIES)

    # ------------------------------------------------------- 6, 11-13. leads
    print("\n--- 6, 11-13. Lead cards ---")
    lead_embeds = embeds[12:]
    check("one card per lead", len(lead_embeds) == 5, str(len(lead_embeds)))
    check("6. no empty field in any lead card",
          all((f["value"] or "").strip() not in ("", "—") for e in lead_embeds for f in e["fields"]))
    c1 = lead_embeds[0]
    f1 = {f["name"]: f["value"] for f in c1["fields"]}
    check("   a hot lead: title with 🔥, only the fields the row has",
          c1["title"] == "🔥 Biz 1" and "Website" in f1 and "Instagram" not in f1
          and f1["Owner"] == "Amira" and f1["Priority"] == "Hot" and c1["color"] == OE.COLOR["hot"],
          str(sorted(f1)))
    check("   long fields are not inline", not next(f for f in c1["fields"] if f["name"] == "Main Opportunity")["inline"])
    check("11. every lead card carries the manual action and the footer",
          all(any(f["name"] == "Manual Contact" for f in e["fields"]) for e in lead_embeds)
          and all(e["footer"]["text"] == OE.LEAD_FOOTER for e in lead_embeds))
    check("12. WhatsApp wins when present, and every available channel is listed",
          f1["Manual Contact"] == "📱 WhatsApp Preferred"
          and f1["Channels"] == "📱 WhatsApp  ·  📞 Call / SMS  ·  🌐 Website Contact Form", str(f1.get("Channels")))
    f2 = {f["name"]: f["value"] for f in lead_embeds[1]["fields"]}
    check("    website-only lead suggests the website form, nothing else",
          f2["Manual Contact"] == "🌐 Website Contact Form" and "Channels" not in f2
          and "WhatsApp" not in all_text(lead_embeds[1]))
    f3 = {f["name"]: f["value"] for f in lead_embeds[2]["fields"]}
    f4 = {f["name"]: f["value"] for f in lead_embeds[3]["fields"]}
    f5 = {f["name"]: f["value"] for f in lead_embeds[4]["fields"]}
    check("    Instagram / Google Maps / nothing each get their own icon",
          f3["Manual Contact"] == "📸 Instagram DM" and f4["Manual Contact"] == "📍 Google Maps"
          and f5["Manual Contact"] == "❌ No contact details on record")
    dup = OE.lead_cards([leads[0], leads[0], dict(leads[0], id=None)])
    check("13. a lead appears once even if it is listed twice",
          len(dup) == 2 and dup[0]["title"] == "🔥 Biz 1", str(len(dup)))
    check("    no-email header card carries the totals and the owner action",
          {f["name"] for f in embeds[11]["fields"]} >= {"Total No-Email Leads", "New Today",
                                                       "Still Missing", "Owner Action"}
          and "Contact these businesses manually" in all_text(embeds[11]))

    # ---------------------------------------------------- 7-9. status colours
    print("\n--- 7-9. Healthy / warning / critical ---")
    check("7. a healthy day: header green, system ✅, automation green, no failed job",
          head["color"] == OE.COLOR["healthy"] and vals["System Status"] == "✅ Healthy"
          and next(e for e in embeds if e["title"] == "⚙️ AUTOMATION HEALTH")["color"] == OE.COLOR["healthy"])
    auto_card = next(e for e in embeds if e["title"] == "⚙️ AUTOMATION HEALTH")
    check("   every job reads ✅ Healthy with a friendly name, no cron syntax",
          all(f["value"] == "✅ Healthy" for f in auto_card["fields"])
          and {f["name"] for f in auto_card["fields"]} == {"MAYA Orchestrator", "Lead Sync",
                                                            "Email Verifier", "ECHO Follow-ups",
                                                            "LEO Inbound", "Review Alerts", "ORBIT Daily"}
          and "* * *" not in all_text(auto_card) and "every 2m" not in all_text(auto_card))
    mw = metrics(automation(tmp, stale="email-verifier"))
    cw = OE.build_report_cards(mw, ids)
    auto_w = {f["name"]: f["value"] for f in cw["automation"]["fields"]}
    check("8. a stale job makes a warning: header orange, ⚠️ Attention",
          cw["header"]["color"] == OE.COLOR["warning"]
          and "⚠️ Attention" in all_text(cw["header"]) and cw["automation"]["color"] == OE.COLOR["warning"])
    check("   the stale job says how long ago it ran",
          re.fullmatch(r"⚠️ Last run 4\dm ago", auto_w["Email Verifier"]) is not None, auto_w["Email Verifier"])
    check("   and the attention card is orange and names it",
          cw["attention"]["color"] == OE.COLOR["warning"] and "Email Verifier" in cw["attention"]["description"])
    mc = metrics(automation(tmp, failed="leo-inbound"))
    cc = OE.build_report_cards(mc, ids)
    auto_c = {f["name"]: f["value"] for f in cc["automation"]["fields"]}
    check("9. a failed job is critical: header red, ❌, automation red, attention red",
          cc["header"]["color"] == OE.COLOR["critical"] and "❌ Critical" in all_text(cc["header"])
          and auto_c["LEO Inbound"] == "❌ Last execution failed"
          and cc["automation"]["color"] == OE.COLOR["critical"]
          and cc["attention"]["color"] == OE.COLOR["critical"]
          and "LEO Inbound" in cc["attention"]["description"])
    mq = metrics(automation(tmp))
    mq.update({"research_failed": 0, "human_reviews_open": 0, "replies_unmatched": 0,
               "sender_warnings": [], "verification": dict(mq["verification"], risky=0, unknown=0)})
    check("   nothing to flag -> no attention card at all",
          OE.card_attention(mq, ids) is None and "🚨 NEEDS ATTENTION" not in titles(OE.build_messages(mq, built, ids)))
    perf = next(e for e in embeds if e["title"] == "📈 PERFORMANCE")
    check("   small sample: one warning line on the performance card only",
          perf["description"].startswith("⚠️ Small sample size") and "Small sample" not in attention["description"]
          and all("Small sample" not in f["value"] for f in perf["fields"]))
    check("   rates read as percentages",
          {f["name"]: f["value"] for f in perf["fields"]} == {"Reply Rate": "23.5%", "Positive Rate": "11.8%",
                                                                "Meeting Rate": "5.9%", "Bounce Rate": "0.0%"})

    # -------------------------------------------------------- 10. limits
    print("\n--- 10. Discord limits ---")
    check("the full build passes validation", OE.validate(messages) == [], str(OE.validate(messages)))
    big = [lead(i, whatsapp="+971 55 %03d" % i, main_opportunity="opportunity " * 500,
                recommended_offer="offer " * 400, business_name="Business %d " % i * 30)
           for i in range(1, 61)]
    big_msgs = OE.build_messages(m, built_for(big), ids)
    check("60 verbose leads: every message within 10 embeds and 6000 characters",
          OE.validate(big_msgs) == [] and all(len(x.get("embeds") or []) <= 10 for x in big_msgs)
          and all(sum(OE.embed_chars(e) for e in x.get("embeds") or []) <= 6000 for x in big_msgs),
          "%d messages" % len(big_msgs))
    card = OE.lead_card(big[0])
    check("  a verbose lead is clipped, not dropped",
          len(card["title"]) <= 256 and all(len(f["value"]) <= 1024 for f in card["fields"])
          and card["title"].endswith("…"))
    many = OE.embed("t", fields=[OE.field("f%d" % i, "v") for i in range(40)])
    check("  a card never carries more than 25 fields", len(many["fields"]) == 25)
    packed = OE.pack([OE.embed("x", "y" * 2000) for _ in range(7)])
    check("  packing splits on the character budget before the embed count",
          [len(p) for p in packed] == [2, 2, 2, 1], str([len(p) for p in packed]))
    check("  an empty value never becomes a field",
          OE.embed("t", fields=[OE.field("a", ""), OE.field("b", None), OE.field("c", "1")]).get("fields") == [
              {"name": "c", "value": "1", "inline": True}])

    # ----------------------------------------------- 14-15. nothing changed
    print("\n--- 14-15. Metrics and logic untouched ---")
    real_db = fresh_db(tmp, "real.db")
    os.environ["AGENCY_DB"] = real_db
    os.environ["ORBIT_MIN_SAMPLE"] = "20"
    real_path = orbit.CRON_JOBS_PATH
    orbit.CRON_JOBS_PATH = jobs_file(tmp, name="jobs_real.json")
    try:
        m_real = orbit.collect(real_db)
    finally:
        orbit.CRON_JOBS_PATH = real_path
    snapshot = copy.deepcopy(m_real)
    text_before = orbit.report(m_real)
    ids_real = OE.sender_identities(db=real_db, m=m_real)
    msgs_real = OE.build_messages(m_real, built, ids_real)
    check("14. building the cards changes no metric",
          m_real == snapshot and orbit.report(m_real) == text_before)
    header_real = msgs_real[0]["embeds"][0]
    leads_real = {f["name"]: f["value"] for f in msgs_real[0]["embeds"][1]["fields"]}
    out_real = {f["name"]: f["value"] for f in msgs_real[0]["embeds"][2]["fields"]}
    check("    card values are the collected values, verbatim",
          leads_real["In Hermes"] == str(m_real["leads"])
          and leads_real["Research Completed"] == str(m_real["research_complete"])
          and out_real["Sent"] == str(m_real["outbound_total"])
          and out_real["Queued"] == str(m_real["queued"])
          and header_real["fields"][0]["value"] == OE.fmt_day(m_real.get("intake_day")))
    src = (HERE / "orbit_embeds.py").read_text(encoding="utf-8")
    writes = re.findall(r"\b(UPDATE|DELETE FROM|INSERT INTO)\s+(\w+)",
                        src.replace("DO UPDATE SET", ""))
    check("15. the presentation module writes nothing but the delivery ledger",
          all(t == "report_deliveries" for _, t in writes) and len(writes) == 1, str(writes))
    check("    it never imports the pipeline or the orchestrator at module level",
          not re.search(r"^import (pipeline|orchestrator|supabase_sync)", src, re.M)
          and not re.search(r"^from (pipeline|orchestrator|supabase_sync)", src, re.M))
    check("    metrics stay read-only for the identity lookup (mode=ro)", "mode=ro" in src)
    daily = (HERE / "scripts" / "orbit_daily.py").read_text(encoding="utf-8")
    check("    the cards post themselves from the daily script (no cron wrapper)",
          "OE.post_all" in daily and "print(orbit.report(metrics))" in daily)
    # The installer lives in the repository, not on the host the agency runs
    # on; the check is only meaningful where the file exists.
    installer_paths = [HERE.parent / "scripts" / "install-agency.py",
                       pathlib.Path("/app/scripts/install-agency.py")]
    installer_file = next((p for p in installer_paths if p.exists()), None)
    if installer_file is not None:
        installer = installer_file.read_text(encoding="utf-8")
        check("    the installer schedules orbit-daily with local delivery",
              re.search(r'"orbit-daily":\s*\(HERMES_HOME,\s*"0 8 \* \* \*",\s*"orbit_daily\.py",\s*"local"\)',
                        installer) is not None)
    else:
        check("    the installer schedules orbit-daily with local delivery (not on this host, skipped)", True)
    check("    the daily script still stamps no-email leads only after a clean delivery",
          'NE.mark_reported(_built["leads"], _day) if _res["failed"] == 0 else 0' in daily)

    # --------------------------------------------------------- delivery
    print("\n--- Delivery: resumable, ordered, rate-limit aware ---")
    calls = []
    fail_at = {"n": 3}
    hits = {"n": 0}

    def fake_send(method, path, body, files):
        hits["n"] += 1
        if hits["n"] == 1:
            return 429, {"retry_after": 0.01}
        calls.append((body.get("embeds") or [{}])[0].get("title") if body.get("embeds") else "file")
        if len(calls) == fail_at["n"]:
            return 500, {"message": "boom"}
        return 200, {"id": "msg%d" % len(calls)}

    slept = []
    con = sqlite3.connect(fresh_db(tmp, "ledger.db"))
    con.row_factory = sqlite3.Row
    r1 = OE.post_all(con, messages, "2026-09-02", channel="chan", send=fake_send, sleep=slept.append)
    check("a 429 is retried after retry_after, not counted as a failure",
          hits["n"] >= 2 and calls[0] == "📊 HERMES AGENCY — DAILY REPORT" and 0.01 <= slept[0] <= 1.0,
          str((r1, slept[:1])))
    check("the first failure stops the run, in order",
          r1["failed"] == 1 and r1["sent"] == 2 and r1["parts"] == len(messages), str(r1))
    calls.clear(); fail_at["n"] = -1
    r2 = OE.post_all(con, messages, "2026-09-02", channel="chan", send=fake_send, sleep=slept.append)
    check("a rerun resumes from the failed message without resending what landed",
          r2["skipped"] == 2 and r2["failed"] == 0 and r2["sent"] == len(messages) - 2
          and calls[0] == "🔄 PIPELINE HEALTH", str((r2, calls[:2])))
    check("  the CSV goes last, as a file", calls[-1] == "file")
    calls.clear()
    r3 = OE.post_all(con, messages, "2026-09-02", channel="chan", send=fake_send, sleep=slept.append)
    check("a third run on the same day sends nothing", r3["sent"] == 0 and not calls, str(r3))
    rows = con.execute("SELECT part_no, delivered_at, discord_message_id FROM report_deliveries"
                       " WHERE section=? ORDER BY part_no", (OE.SECTION,)).fetchall()
    check("  the ledger records every part as delivered with its message id",
          len(rows) == len(messages) and all(r["delivered_at"] and r["discord_message_id"] for r in rows))
    check("  the old plaintext no-email section is a separate ledger section",
          OE.SECTION != NE.SECTION)
    preview = OE.render_text(messages, max_leads=2)
    check("the text preview renders every executive card and caps lead cards",
          all(t in preview for t in EXEC_TITLES) and "more lead card(s)" in preview)

    print("\n" + "=" * 76)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
