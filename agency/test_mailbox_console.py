#!/usr/bin/env python3
"""The mailbox console: the operator's switch, and what it refuses to do.

This is a control that disables and re-enables live senders from a chat
message, so the tests are mostly about restraint:

  * ordinary conversation in the channel must never flip a mailbox;
  * a command is consumed exactly once, even across a crash or redeploy;
  * the console cannot act on its own confirmations;
  * `resume` names one mailbox — there is no "resume all", because the four
    mailboxes paused on 2026-09-04 were paused for four separate reputations.

Pure. No Discord, no network; the database is an in-memory fixture.
"""

import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import mailbox_console as MC  # noqa: E402

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


def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE tenant_health (
        tenant_name TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
        mailbox_email TEXT, daily_limit INTEGER, sent_today INTEGER,
        health TEXT, paused_until TEXT, paused_reason TEXT, paused_at TEXT);
    CREATE TABLE leads (id TEXT PRIMARY KEY, state TEXT);
    CREATE TABLE messages (id TEXT PRIMARY KEY, lead_id TEXT,
        tenant_user_id INTEGER, sent_at TEXT);
    CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT,
        campaign_id TEXT, agent TEXT, event_type TEXT, detail TEXT);
    """)
    for uid, mail, paused in ((2, "abh@gmail.com", None), (3, "min@gmail.com", None),
                              (9, "ami@gmail.com", MC.PAUSE_FOREVER)):
        con.execute("INSERT INTO tenant_health (tenant_name,user_id,mailbox_email,"
                    "daily_limit,sent_today,health,paused_until,paused_reason)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    ("t%d" % uid, uid, mail, 70, 3, "warming", paused,
                     "MANUAL REVIEW REQUIRED - bounce/block" if paused else None))
    # t2: 10 sent, 1 bounced.  t9: 10 sent, 3 bounced.
    for i in range(10):
        con.execute("INSERT INTO leads (id,state) VALUES (?,?)",
                    ("L2-%d" % i, "BOUNCED" if i == 0 else "SENT"))
        con.execute("INSERT INTO messages (id,lead_id,tenant_user_id,sent_at)"
                    " VALUES (?,?,?,?)", ("M2-%d" % i, "L2-%d" % i, 2, "2026-09-08T05:00:00Z"))
        con.execute("INSERT INTO leads (id,state) VALUES (?,?)",
                    ("L9-%d" % i, "BOUNCED" if i < 3 else "SENT"))
        con.execute("INSERT INTO messages (id,lead_id,tenant_user_id,sent_at)"
                    " VALUES (?,?,?,?)", ("M9-%d" % i, "L9-%d" % i, 9, "2026-09-08T05:00:00Z"))
    con.commit()
    return con


def main() -> int:
    print("=" * 84)
    print("MAILBOX CONSOLE")
    print("=" * 84)
    con = db()

    print("\n--- 1. the numbers the operator decides on ---")
    rows = {r["user_id"]: r for r in MC.snapshot(con)}
    check("1. a healthy mailbox shows a low bounce rate",
          rows[2]["sent"] == 10 and rows[2]["bounced"] == 1 and rows[2]["bounce_rate"] == 10.0,
          "t2 %s%% of %d" % (rows[2]["bounce_rate"], rows[2]["sent"]))
    check("   a damaged one shows a high one",
          rows[9]["bounce_rate"] == 30.0, "t9 %s%%" % rows[9]["bounce_rate"])
    check("   paused state and reason are surfaced",
          rows[9]["paused"] and "MANUAL REVIEW" in rows[9]["paused_reason"])
    check("   the mailbox address is masked in the card",
          "***@" in rows[9]["mailbox"], rows[9]["mailbox"])
    check("   a paused mailbox is not silently counted as active",
          rows[2]["paused"] is False and rows[9]["paused"] is True)

    print("\n--- 2. chat must never flip a mailbox ---")
    for text in ("should we pause this one?", "t9 looks bad",
                 "I think resume is risky", "", "  ", "why is t9 paused",
                 "the resume of that campaign", "paused t9 earlier"):
        cmd = MC.parse_command(text)
        acted = bool(cmd) and cmd.get("verb") in ("pause", "resume") and not cmd.get("error")
        check("2. %-42r is not a command" % text[:42], not acted, str(cmd))

    print("\n--- 3. real commands parse ---")
    check("3. `pause t9 too many bounces` parses",
          MC.parse_command("pause t9 too many bounces") ==
          {"verb": "pause", "user_id": 9, "reason": "too many bounces"})
    check("   `resume t9` parses", MC.parse_command("resume t9") ==
          {"verb": "resume", "user_id": 9, "reason": ""})
    check("   `enable t9` is an alias for resume",
          MC.parse_command("enable t9")["verb"] == "resume")
    check("   `disable t9` is an alias for pause",
          MC.parse_command("disable t9")["verb"] == "pause")
    check("   `status` asks for a redraw only",
          MC.parse_command("status") == {"verb": "status"})
    check("   a verb with no mailbox is refused, not guessed",
          MC.parse_command("resume").get("error"),
          MC.parse_command("resume").get("error"))
    check("   there is no 'resume all'",
          MC.parse_command("resume all").get("error"),
          "four mailboxes were paused for four different reputations")

    print("\n--- 4. applying a command ---")
    msg = MC.apply_command(con, MC.parse_command("resume t9"), actor="bhavesh")
    check("4. resume clears the pause", "resumed" in msg, msg)
    r = con.execute("SELECT paused_until, paused_reason FROM tenant_health WHERE user_id=9").fetchone()
    check("   paused_until and reason are cleared",
          r["paused_until"] is None and r["paused_reason"] is None)
    ev = con.execute("SELECT agent, event_type FROM events ORDER BY id DESC LIMIT 1").fetchone()
    check("   and it is audited with who did it",
          ev["event_type"] == "mailbox.resume" and ev["agent"] == "bhavesh",
          "%s by %s" % (ev["event_type"], ev["agent"]))
    check("   resuming an active mailbox is a no-op, not an error",
          "already active" in MC.apply_command(con, MC.parse_command("resume t9")))

    msg = MC.apply_command(con, MC.parse_command("pause t9 bounce spike"), actor="bhavesh")
    check("   pause records the operator's reason verbatim", "bounce spike" in msg, msg)
    r = con.execute("SELECT paused_until, paused_reason FROM tenant_health WHERE user_id=9").fetchone()
    check("   and pauses with no expiry (only an explicit resume clears it)",
          r["paused_until"] == MC.PAUSE_FOREVER, r["paused_until"])
    check("   pausing an already-paused mailbox is a no-op",
          "already paused" in MC.apply_command(con, MC.parse_command("pause t9 again")))
    check("   an unknown mailbox is refused",
          "no mailbox" in MC.apply_command(con, MC.parse_command("pause t99")))

    print("\n--- 5. the card only redraws when it would say something new ---")
    before = MC.fingerprint(MC.snapshot(con))
    check("5. an unchanged fleet keeps its fingerprint",
          MC.fingerprint(MC.snapshot(con)) == before, "no 2-minute repost")
    MC.apply_command(con, MC.parse_command("resume t9"))
    check("   flipping a mailbox changes it",
          MC.fingerprint(MC.snapshot(con)) != before)

    print("\n--- 6. command replay safety ---")
    home = pathlib.Path(tempfile.mkdtemp(prefix="mc-"))
    st = MC.load_state(home)
    check("6. a fresh install has consumed nothing",
          st["last_command_message_id"] == "" and st["card_message_id"] == "")
    st["last_command_message_id"] = "12345"
    MC.save_state(st, home)
    check("   the last handled message id survives a restart",
          MC.load_state(home)["last_command_message_id"] == "12345",
          "so `resume t9` from three days ago is never replayed")

    print("\n--- 7. the rendered card ---")
    embed = MC.render(MC.snapshot(con))
    body = str(embed)
    check("7. it is a single embed, not one card per mailbox",
          isinstance(embed, dict) and "fields" in embed)
    check("   it names the controls so nobody needs the docs",
          "pause t9" in body and "resume t9" in body)
    check("   it never prints a full mailbox address",
          "abh@gmail.com" not in body and "***@" in body)
    check("   it shows a fleet-wide bounce rate", "overall rate" in body)

    print("\n" + "=" * 84)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
