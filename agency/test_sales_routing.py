#!/usr/bin/env python3
"""Sales Discord routing: each thing goes to its own channel, exactly once.

Two properties matter here and they pull against each other:

  * every sales output must land in the channel meant for it, and
  * changing the destination must not replay a single delivered message.

The second is the dangerous one. `report_deliveries` is keyed
(report_day, section, part_no) with channel_id as a plain column -- if
channel_id were part of that key, repointing ORBIT would re-deliver every part
of every day into the new channel, which for the current ledger is 206
messages. The tests below pin that key shape, because a future migration that
"helpfully" adds channel_id to it would look reasonable in review and flood the
channel on deploy.

Pure. No network, no Discord, no live database.
"""

import json
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import discord_routing as R  # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []

IDS = {
    "sales_control":   "1546762315108450346",
    "sales_leads":     "1546762318757502978",
    "sales_outreach":  "1546762322251354153",
    "sales_replies":   "1546762325854265394",
    "sales_review":    "1546762329448779786",
    "sales_alerts":    "1546762332884172800",
    "sales_analytics": "1546762336692473947",
}
MAYA_OFFICE = "1484778503529304145"
GLOBAL_ALERTS = "1484778510383054898"


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-64s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-64s %s" % (name, detail))


def home_with_routes(mapping=None):
    d = pathlib.Path(tempfile.mkdtemp(prefix="routing-"))
    (d / "state").mkdir(parents=True, exist_ok=True)
    (d / "state" / "sales_channels.json").write_text(json.dumps(
        {"guild_id": "1484761835138715691",
         "category_id": "1546762313246310414",
         "channels": IDS if mapping is None else mapping}), encoding="utf-8")
    return d


def clear_env():
    import os
    for spec in R.ROUTES.values():
        os.environ.pop(spec["env"], None)
        for legacy in spec["legacy"]:
            os.environ.pop(legacy, None)


def main() -> int:
    import os
    print("=" * 86)
    print("SALES DISCORD ROUTING")
    print("=" * 86)
    clear_env()
    home = home_with_routes()

    print("\n--- 15. the config resolves all seven SALES channels ---")
    resolved = {r: R.channel(r, home=home) for r in R.SALES_ROUTES}
    check("15. seven sales routes defined", len(R.SALES_ROUTES) == 7, str(len(R.SALES_ROUTES)))
    check("    every one resolves to an id", all(resolved.values()),
          str([r for r, v in resolved.items() if not v]) or "all resolved")
    check("    ids are distinct — no two routes share a channel",
          len(set(resolved.values())) == 7, str(len(set(resolved.values()))))

    print("\n--- 1-3, 5. each producer's destination ---")
    for route, label, num in (("sales_leads", "no-email report + CSV", "1-2"),
                              ("sales_analytics", "ORBIT daily report", "3"),
                              ("sales_review", "human-review cards", "5")):
        check("%s. %-30s -> %s" % (num, label, route),
              R.channel(route, home=home) == IDS[route], IDS[route])

    print("\n--- 4, 6-8. routes that exist for producers not yet built ---")
    for route, label, num in (("sales_replies", "LEO reply / positive reply", "4"),
                              ("sales_alerts", "bounce spike", "6"),
                              ("sales_alerts", "recovery card", "7"),
                              ("sales_outreach", "outreach cycle summary", "8")):
        check("%s. %-30s -> %s" % (num, label, route),
              R.channel(route, home=home) == IDS[route], IDS[route])

    print("\n--- 9. MAYA's own channel is untouched ---")
    check("9. maya_office still resolves to #maya-office",
          R.channel("maya_office", home=home) == MAYA_OFFICE, MAYA_OFFICE)
    check("   and no SALES route points at it",
          MAYA_OFFICE not in resolved.values(),
          "sales output can no longer land in the operator's channel")
    check("   the shared #alerts channel is still addressable and separate",
          R.channel("global_alerts", home=home) == GLOBAL_ALERTS
          and GLOBAL_ALERTS not in resolved.values(),
          "sales incidents go to sales_alerts, not the global channel")

    print("\n--- 10-11. re-routing must not replay delivered history ---")
    con = sqlite3.connect(":memory:")
    # report_deliveries is created by migration 009, not the base schema.
    con.executescript((HERE / "migrations" / "009_no_email_report.sql").read_text(encoding="utf-8"))
    row = con.execute("SELECT sql FROM sqlite_master WHERE name='report_deliveries'").fetchone()
    ddl = (row[0] if row else "")
    check("10. report_deliveries is keyed WITHOUT channel_id",
          "PRIMARY KEY (report_day, section, part_no)" in ddl.replace("\n", " "),
          "a delivered part stays delivered wherever it went")
    check("    channel_id is recorded, just not part of the key",
          "channel_id" in ddl, "kept for audit: where did this actually go")

    con.execute("INSERT INTO report_deliveries (report_day, section, part_no,"
                " total_parts, content_hash, channel_id, discord_message_id, delivered_at)"
                " VALUES ('2026-09-07','no_email',1,3,'abc',?, '999', '2026-09-07T20:00:00Z')",
                (MAYA_OFFICE,))
    con.commit()
    dup = con.execute("SELECT COUNT(*) FROM report_deliveries WHERE report_day='2026-09-07'"
                      " AND section='no_email' AND part_no=1 AND delivered_at IS NOT NULL").fetchone()[0]
    check("11. a part delivered to the OLD channel still reads as delivered",
          dup == 1, "the new channel does not resurrect it")
    try:
        con.execute("INSERT INTO report_deliveries (report_day, section, part_no,"
                    " total_parts, content_hash, channel_id) VALUES"
                    " ('2026-09-07','no_email',1,3,'abc',?)", (IDS["sales_leads"],))
        clash = False
    except sqlite3.IntegrityError:
        clash = True
    check("    re-inserting the same part for a NEW channel is refused",
          clash, "so a channel switch cannot create a second copy")

    print("\n--- 12. review cards do not duplicate across the move ---")
    import review_cards as RC
    unchanged = {"id": "H-1", "first_alerted_at": "2026-09-07T10:00:00Z",
                 "alert_fingerprint": None, "status": "open", "reason": "x",
                 "lead_id": "L-1", "reply_summary": "s", "recommended_action": "a",
                 "created_at": "2026-09-07T09:00:00Z"}
    unchanged["alert_fingerprint"] = RC.fingerprint(unchanged)
    check("12. an unchanged escalation is not re-posted after re-routing",
          RC.to_post([unchanged]) == [],
          "only new or materially changed rows are ever sent")
    changed = dict(unchanged, reply_summary="something materially different")
    check("    one whose content changed posts exactly once",
          [a for _, a in RC.to_post([changed])] == ["update"])

    print("\n--- 13-14. what must NOT become an alert ---")
    src = (HERE / "discord_health.py").read_text(encoding="utf-8")
    check("13. held MailHub rows are not an alert source",
          "HELD:" not in src,
          "'not visible to any tenant credential' is containment noise, not an incident")
    check("14. a healthy watchdog tick posts nothing",
          'return ACT_NOTHING, "healthy"' in src,
          "no healthy/idle spam")

    print("\n--- override precedence ---")
    os.environ["SALES_LEADS_CHANNEL"] = "111"
    check("an explicit env override wins over persisted state",
          R.channel("sales_leads", home=home) == "111")
    del os.environ["SALES_LEADS_CHANNEL"]
    os.environ["NO_EMAIL_DISCORD_CHANNEL"] = "222"
    check("a legacy env var an operator already set still works",
          R.channel("sales_leads", home=home) == "222",
          "migration does not break an existing override")
    del os.environ["NO_EMAIL_DISCORD_CHANNEL"]
    check("state is used when no env var is set",
          R.channel("sales_leads", home=home) == IDS["sales_leads"])
    empty = pathlib.Path(tempfile.mkdtemp(prefix="norouting-"))
    check("an unconfigured sales route returns '' (do not post)",
          R.channel("sales_leads", home=empty) == "",
          "callers must treat this as 'skip', never as a default channel")
    check("   and the producers honour that",
          all("do not post" in (HERE / f).read_text(encoding="utf-8")
              for f in ("no_email_report.py", "orbit_embeds.py", "review_cards.py")),
          "each post path returns unrouted instead of posting to /channels//messages")

    print("\n" + "=" * 86)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
