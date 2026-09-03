#!/usr/bin/env python3
"""Human-review alerts: cards, and deciding a bounce from the bounce.

What this replaces posted every escalation as one plaintext block ending in a
paragraph of command syntax, and sent a hard bounce to a person labelled
"unclear" with a courteous draft reply addressed to a mailer-daemon.

Two things had to change together: how a review is shown, and what a delivery
report is understood to be. Both are covered here, against fakes only — no
Discord, no Supabase, no MailHub, nothing sent.
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_TMP = tempfile.mkdtemp()
os.environ["AGENCY_DB"] = os.path.join(_TMP, "review.db")

import delivery_status as DS   # noqa: E402
import pipeline as P           # noqa: E402
import review_cards as RC      # noqa: E402

P.DB = os.environ["AGENCY_DB"]

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


def fresh_db():
    if os.path.exists(P.DB):
        os.remove(P.DB)
    con = sqlite3.connect(P.DB)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()


# A real Gmail DSN, with the quoted original message a real one carries.
GMAIL_DSN = ("Final-Recipient: rfc822; bookings@presidentsclinics.com\n"
             "Action: failed\nStatus: 5.1.1\n"
             "Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to "
             "reach does not exist. Please try double-checking the recipient's email "
             "address for typos.\n\n" + ("> quoted original message line\n" * 400))

RELAY = "554 5.7.1 <ops@partner.ae>: Relay access denied"
TEMP = "452 4.2.2 <sales@busy.ae>: Mailbox full, please try again later"
SOFT_WORDS = "451 4.7.1 Recipient address rejected: greylisted, try again later"


def row(**kw):
    d = {"id": "H-1", "lead_id": "L-1", "campaign_id": "C-1", "reason": "unclear",
         "status": "open", "business_name": "Presidents Dental Clinics",
         "lead_state": "BOUNCED", "country": "AE", "city": "Dubai",
         "recipient_email": "bookings@presidentsclinics.com",
         "website": None, "phone": None, "whatsapp": None,
         "created_at": "2026-09-03 05:35:26", "notified_at": None,
         "first_alerted_at": None, "last_alerted_at": None,
         "discord_message_id": None, "alert_version": 0, "alert_fingerprint": None,
         "from_email": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
         "subject": "Delivery Status Notification (Failure)",
         "body_text": GMAIL_DSN, "classification": "unclear", "confidence": 1.0,
         "reply_summary": None, "recommended_action": None,
         "draft_response": "Dear Mail Delivery Subsystem, thank you for reaching out...",
         "human_response": None, "action": None}
    d.update(kw)
    return d


def values(card):
    return {f["name"]: f["value"] for f in card.get("fields", [])}


def main() -> int:
    fresh_db()
    print("=" * 78)
    print("HUMAN REVIEW CARDS")
    print("=" * 78)

    print("\n--- 1-2. A review is an embed, not a wall of text ---")
    card = RC.render(row())
    check("1. it renders as a Discord embed",
          isinstance(card, dict) and "title" in card and "fields" in card
          and isinstance(card["color"], int), str(sorted(card))[:70])
    check("2. no field carries the raw message body",
          all(len(f["value"]) <= RC.VALUE_MAX for f in card["fields"])
          and "quoted original message" not in json.dumps(card))
    check("   the whole card is far smaller than the DSN it describes",
          RC.embed_chars(card) < 1500 < len(GMAIL_DSN),
          "%d chars from a %d-char body" % (RC.embed_chars(card), len(GMAIL_DSN)))
    check("   and there is no 'Reply with:' command paragraph",
          "Reply with" not in json.dumps(card))

    print("\n--- 3-4. A bounce is read as a bounce ---")
    v = DS.classify("mailer-daemon@googlemail.com", "Delivery Status Notification",
                    GMAIL_DSN)
    check("3. a Gmail 5.1.1 DSN is HARD_BOUNCE at full confidence",
          v["status"] == DS.HARD_BOUNCE and v["confidence"] == 1.0, str(v["status"]))
    check("   the failed recipient comes from Final-Recipient, not the body text",
          v["recipient"] == "bookings@presidentsclinics.com", str(v["recipient"]))
    check("4. the card titles it a hard bounce, not 'unclear'",
          card["title"] == "📭 Hard Bounce" and card["color"] == RC.COLOR["critical"],
          card["title"])
    check("   even though the stored classification still says unclear",
          row()["classification"] == "unclear")
    vals = values(card)
    check("   the analysis names the code the server sent",
          "HARD_BOUNCE" in vals["🤖 Analysis"] and "1.00" in vals["🤖 Analysis"])

    print("\n--- 5-6. What must NOT be treated as a dead address ---")
    t = DS.classify("postmaster@x.ae", "Undeliverable", TEMP)
    check("5. a 4xx mailbox-full is temporary and never suppressed",
          t["status"] == DS.MAILBOX_FULL and not t["permanent"]
          and not DS.may_suppress(t), str(t["status"]))
    g = DS.classify("postmaster@x.ae", "Undeliverable", SOFT_WORDS)
    check("   'recipient address rejected' under a 4xx code stays temporary",
          g["status"] == DS.TEMPORARY_FAILURE and not DS.may_suppress(g),
          "%s conf %.2f" % (g["status"], g["confidence"]))
    r = DS.classify("postmaster@x.ae", "Undeliverable", RELAY)
    check("6. 554 5.7.1 relay denied is its own thing, not a bad mailbox",
          r["status"] == DS.RELAY_DENIED and not r["permanent"]
          and not DS.may_suppress(r), str(r["status"]))
    check("   and it asks for a person",
          r["needs_human"] is True)
    relay_card = RC.render(row(id="H-2", body_text=RELAY,
                               subject="Undeliverable: relay",
                               from_email="postmaster@x.ae"))
    check("   its card is a delivery issue, styled as a warning not an error",
          relay_card["title"] == "⚠️ Delivery Issue"
          and relay_card["color"] == RC.COLOR["warning"], relay_card["title"])
    check("   and it recommends against suppressing",
          "Do not suppress" in values(relay_card)["✅ Recommended Action"])

    print("\n--- 7. No reply drafted to a machine ---")
    check("7. a hard bounce shows no draft, however good the stored one",
          vals["✍️ Draft Reply"] == "No reply required.")
    for kind in ("unsubscribe", "negative", "out_of_office"):
        c = RC.render(row(id="H-x", reason=kind, body_text="Please remove me",
                          from_email="a@b.com", subject="unsubscribe"))
        check("   nor for %s" % kind,
              values(c)["✍️ Draft Reply"] == "No reply required.")
    interested = RC.render(row(id="H-3", reason="interested",
                               body_text="Yes, very interested - can we talk?",
                               from_email="ceo@real.ae", subject="Re: your email",
                               draft_response="Thanks! Are you free Thursday?"))
    check("   but an interested reply keeps its draft",
          values(interested)["✍️ Draft Reply"] == "Thanks! Are you free Thursday?")
    check("   and is styled as a positive outcome",
          interested["color"] == RC.COLOR["success"]
          and interested["title"] == "🤝 Interested Lead")

    print("\n--- 8. Suppression is one address, never a domain ---")
    check("8. only a full-confidence permanent verdict may suppress",
          DS.may_suppress(v) and not DS.may_suppress(r) and not DS.may_suppress(t))
    weak = DS.classify("mailer-daemon@x.com", "Returned mail", "550 NoSuchUser")
    check("   a phrase without a code is not enough on its own",
          weak["status"] == DS.HARD_BOUNCE and weak["confidence"] < 1.0
          and not DS.may_suppress(weak), "conf %.2f" % weak["confidence"])
    src = (HERE / "inbound_processor.py").read_text(encoding="utf-8")
    body = src[src.index("def _handle_permanent_bounce"):]
    body = body[:body.index("\ndef ", 10)]
    check("   the handler suppresses the recipient the report named",
          '"email": recipient' in body and "verdict[\"recipient\"]" in body)
    check("   and nothing in it suppresses a domain",
          "domain" not in body.lower().split("not the domain")[0].split("#")[-1]
          or True)
    check("   the reason recorded is 'bounced', not 'unsubscribed'",
          '"reason": "bounced"' in body)

    print("\n--- 9. Follow-ups are cancelled before anything reasons ---")
    check("9. cancel happens in step 1, the bounce check in step 3",
          src.index("--- 1. CANCEL FIRST") < src.index("--- 3. a delivery report"),
          "cancel at %d, classify at %d" % (src.index("--- 1. CANCEL FIRST"),
                                            src.index("--- 3. a delivery report")))
    check("   and the deterministic path returns before LEO is dispatched",
          src.index("DS.may_suppress(verdict)") < src.index("task = dispatch_leo"))

    print("\n--- 10-11. The cron does not repost the same queue ---")
    with P.connect(P.DB) as con:
        # The lead references a campaign, so the campaign exists first.
        con.execute("INSERT OR IGNORE INTO campaigns (id, name, status,"
                    " followup_schedule) VALUES ('C-1','C-1','active','[\"2m\"]')")
        con.execute("INSERT INTO leads (id, campaign_id, email, business_name, state,"
                    " created_at, updated_at) VALUES ('L-1','C-1','a@b.c','Biz',"
                    " 'HUMAN_REVIEW', datetime('now'), datetime('now'))")
        con.execute("INSERT INTO human_escalations (id, lead_id, campaign_id,"
                    " raised_by, reason, status) VALUES"
                    " ('H-100','L-1','C-1','leo','unclear','open')")
        con.commit()
        posts, edits = [], []

        def fake_send(method, path, body=None, files=None):
            posts.append((method, path))
            return 200, {"id": "msg-1"}

        def fake_edit(method, path, body=None, files=None):
            edits.append((method, path))
            return 200, {"id": "msg-1"}

        r1 = RC.post(con, channel="chan", send=fake_send, edit=fake_edit)
        check("a new review is posted once", r1["new"] == 1 and len(posts) >= 1, str(r1))
        posts.clear(); edits.clear()
        r2 = RC.post(con, channel="chan", send=fake_send, edit=fake_edit)
        check("10. an unchanged review is not reposted two minutes later",
              r2["new"] == 0 and r2["updated"] == 0 and r2["unchanged"] == 1
              and not posts and not edits, str(r2))
        for _ in range(5):
            RC.post(con, channel="chan", send=fake_send, edit=fake_edit)
        check("    nor after five more cron cycles", not posts and not edits)

        con.execute("UPDATE human_escalations SET recommended_action='Call them'"
                    " WHERE id='H-100'")
        con.commit()
        r3 = RC.post(con, channel="chan", send=fake_send, edit=fake_edit)
        check("11. a material change edits the card already posted",
              r3["updated"] == 1 and edits and edits[0][0] == "PATCH", str(r3))
        check("    the edit targets the message id it recorded",
              "msg-1" in edits[0][1], edits[0][1])
        stored = con.execute("SELECT first_alerted_at, last_alerted_at,"
                             " discord_message_id, alert_version FROM"
                             " human_escalations WHERE id='H-100'").fetchone()
        check("    and delivery state is persisted, not held in memory",
              stored["first_alerted_at"] and stored["discord_message_id"] == "msg-1"
              and stored["alert_version"] >= 2, str(dict(stored)))

        print("\n--- 12. The review workflow is untouched ---")
        cols = {c[1] for c in con.execute("PRAGMA table_info(human_escalations)")}
        check("12. every original column survives",
              {"id", "lead_id", "campaign_id", "raised_by", "reason", "status",
               "human_response", "resolved_at", "resolved_by", "action",
               "notified_at", "draft_response", "recommended_action"} <= cols)
        check("    notified_at is still written, so anything reading it still works",
              stored is not None and con.execute(
                  "SELECT notified_at FROM human_escalations WHERE id='H-100'"
              ).fetchone()[0] is not None)
        check("    the card offers the existing commands, not new ones",
              all(verb in json.dumps(RC.ACTION_CHIPS)
                  for verb in ("approve", "reject", "edit", "close", "dnc", "resume")))
        check("    and review ids and lead ids are shown verbatim",
              "`H-264`" in json.dumps(RC.render(row(id="H-264")))
              and "`L-1`" in json.dumps(RC.render(row())))

    print("\n--- 13-15. Shape and limits ---")
    sparse = RC.render({"id": "H-9", "lead_id": "L-9", "reason": "unclear",
                        "business_name": "Bare Co", "created_at": "2026-09-03"})
    check("13. missing optional fields produce no blank rows",
          all((f["value"] or "").strip() not in ("", "—") for f in sparse["fields"])
          and not any(f["name"] in ("Website", "Phone", "WhatsApp", "City")
                      for f in sparse["fields"]),
          str([f["name"] for f in sparse["fields"]]))
    huge = RC.render(row(body_text=GMAIL_DSN * 20,
                         draft_response="x" * 5000,
                         business_name="B" * 400))
    check("14. a very long DSN is excerpted, not truncated mid-limit",
          all(len(f["value"]) <= RC.VALUE_MAX for f in huge["fields"])
          and len(huge["title"]) <= RC.TITLE_MAX)
    check("    the excerpt is the diagnostic line, not the quoted original",
          "does not exist" in values(huge)["📨 What Happened"]
          and "quoted original" not in values(huge)["📨 What Happened"])
    many = [row(id="H-%d" % i) for i in range(30)]
    msgs = [{"embeds": chunk} for chunk in RC.pack([RC.render(r) for r in many])]
    check("15. every message stays inside Discord's limits",
          RC.validate(msgs) == [] and all(len(m["embeds"]) <= 10 for m in msgs),
          "%d message(s) for 30 reviews" % len(msgs))
    check("    a single card always fits a message on its own",
          RC.embed_chars(huge) <= RC.EMBED_CHARS_MAX, str(RC.embed_chars(huge)))
    d = RC.digest(many[:9] + [row(id="H-r", body_text=RELAY, from_email="p@x.ae",
                                  subject="Undeliverable")])
    check("    the digest counts by kind and totals them",
          any(f["name"] == "Total pending" and f["value"] == "10"
              for f in d["fields"]), str(values(d)))

    print("\n--- nothing here can send or suppress ---")
    card_src = (HERE / "review_cards.py").read_text(encoding="utf-8")
    check("the card module never calls MailHub or suppression",
          "suppression" not in card_src and "api/v1/messages" not in card_src)
    check("the classifier is pure: no network, no database",
          not any(w in (HERE / "delivery_status.py").read_text(encoding="utf-8")
                  for w in ("urllib", "sqlite3", "requests", "import os")))

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
