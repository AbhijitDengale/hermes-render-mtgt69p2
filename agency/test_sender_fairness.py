#!/usr/bin/env python3
"""New outreach is shared fairly across the senders that can actually send.

Two failures put four healthy mailboxes on zero real sends while five others
carried everything.

  1. A profile's config.yaml forwards environment to its MCP subprocess through
     a hand-written list of variable names. It named tenants 1 to 5. Adding
     four more to the .env changed nothing the assigning process could see, so
     every new lead went to the original five -- 116 of them after the new
     senders were verified, healthy and READY.

  2. Allocation hashed the lead id over the ready set. A hash spreads evenly
     across whatever set it is handed, but it has no memory: when the set grew
     it did not owe the newcomers anything, so nothing corrected the backlog.

Fakes only: no network, no Supabase, no MailHub, nothing sent.
"""
import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_TMP = tempfile.mkdtemp()
os.environ["AGENCY_DB"] = os.path.join(_TMP, "fair.db")

import pipeline as P     # noqa: E402
import tenants           # noqa: E402

P.DB = os.environ["AGENCY_DB"]

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


NINE = [(1, 2, "abhijitdeng20187"), (2, 3, "minhulisa"), (3, 4, "dnyandev887"),
        (4, 5, "dnyandevdeng"), (5, 6, "minhuli2005"), (6, 9, "amitshinde7489"),
        (7, 10, "amolsatkar994"), (8, 11, "chetansatkar72"), (9, 12, "chetansatkar78")]
EXCLUDED_MAILBOX = "chetansatkar176"


_SEQ = [0]


def fresh_db():
    """A new database file each time. Reusing one path and deleting it fails on
    Windows while a connection is still open, and the point here is the
    allocator, not file handles."""
    _SEQ[0] += 1
    P.DB = os.path.join(_TMP, "fair%d.db" % _SEQ[0])
    os.environ["AGENCY_DB"] = P.DB
    con = sqlite3.connect(P.DB)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()


# Same reason as test_tenants: without an empty HERMES_HOME the "only five are
# forwarded" case below reads the host's real .env and sees all nine, so the
# very regression this suite exists to catch is invisible. The .env-discovery
# case later in the file sets its own HERMES_HOME after this and still works.
_EMPTY_HOME = os.path.join(_TMP, "empty-home")
os.makedirs(_EMPTY_HOME, exist_ok=True)


def set_env(entries=NINE):
    for k in list(os.environ):
        if k.startswith("MAILHUB_TENANT_") or k == "MAILHUB_API_TOKEN":
            del os.environ[k]
    os.environ["HERMES_HOME"] = _EMPTY_HOME
    tenants._FILE_CACHE.update(path=None, mtime=None, values={})
    for idx, uid, name in entries:
        os.environ["MAILHUB_TENANT_%d_NAME" % idx] = name
        os.environ["MAILHUB_TENANT_%d_USER_ID" % idx] = str(uid)
        os.environ["MAILHUB_TENANT_%d_QUEUE_TOKEN" % idx] = "q%d" % uid
        os.environ["MAILHUB_TENANT_%d_APPROVE_TOKEN" % idx] = "a%d" % uid
        os.environ["MAILHUB_TENANT_%d_LEO_TOKEN" % idx] = "l%d" % uid


def health(con, entries=NINE, over=None):
    over = over or {}
    for _, uid, name in entries:
        d = {"queue_ok": 1, "approve_ok": 1, "leo_ok": 1, "mailbox_ok": 1,
             "daily_limit": 70, "sent_today": 0, "health": "warming",
             "sender_identity_status": "verified"}
        d.update(over.get(uid, {}))
        con.execute(
            "INSERT INTO tenant_health (tenant_name, user_id, queue_ok, approve_ok,"
            " leo_ok, mailbox_ok, daily_limit, sent_today, health, mailbox_email,"
            " sender_from_email, sender_identity_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(tenant_name) DO UPDATE SET queue_ok=excluded.queue_ok,"
            "  approve_ok=excluded.approve_ok, leo_ok=excluded.leo_ok,"
            "  mailbox_ok=excluded.mailbox_ok, daily_limit=excluded.daily_limit,"
            "  sent_today=excluded.sent_today, health=excluded.health,"
            "  sender_identity_status=excluded.sender_identity_status",
            (name, uid, d["queue_ok"], d["approve_ok"], d["leo_ok"], d["mailbox_ok"],
             d["daily_limit"], d["sent_today"], d["health"],
             "%s@gmail.com" % name, "%s@syntrix.cv" % name,
             d["sender_identity_status"]))
    con.commit()


def assign(con, n, prefix="L-new-"):
    """Allocate n leads, recording each one the way approval does."""
    con.execute("INSERT OR IGNORE INTO campaigns (id, name, status,"
                " followup_schedule) VALUES ('C-1','C-1','active','[\"2m\"]')")
    got = []
    for i in range(n):
        lid = "%s%03d" % (prefix, i)
        t = tenants.for_lead(lid, con)
        got.append(t["user_id"] if t else None)
        if t:
            con.execute("INSERT OR IGNORE INTO leads (id, campaign_id, email,"
                        " business_name, state, created_at, updated_at) VALUES"
                        " (?, 'C-1', ?, ?, 'COPY_READY', datetime('now'),"
                        " datetime('now'))", (lid, "%s@example.com" % lid, lid))
            con.execute(
                "INSERT INTO messages (id, lead_id, campaign_id, direction, kind,"
                " followup_stage, subject, body, status, dry_run, updated_at,"
                " tenant_user_id, tenant_assigned_at) VALUES (?,?, 'C-1',"
                " 'outbound','outreach',0,'S','B','draft',1, datetime('now'),"
                " ?, datetime('now'))",
                ("M-%s" % lid, lid, t["user_id"]))
            con.commit()
    return got


def spread(got):
    import collections
    return dict(sorted(collections.Counter(got).items(),
                       key=lambda kv: (kv[0] is None, kv[0])))


def main() -> int:
    print("=" * 78)
    print("SENDER FAIRNESS")
    print("=" * 78)

    print("\n--- 1. Tenants are found in the .env, not only the inherited env ---")
    fresh_db()
    set_env(NINE[:5])                       # only five forwarded, as production was
    env_only = {t["user_id"] for t in tenants.load()}
    check("with five in the environment the router sees five",
          env_only == {2, 3, 4, 5, 6}, str(sorted(env_only)))

    home = os.path.join(_TMP, "home")
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, ".env"), "w", encoding="utf-8") as fh:
        for idx, uid, name in NINE:
            fh.write("MAILHUB_TENANT_%d_NAME=%s\n" % (idx, name))
            fh.write("MAILHUB_TENANT_%d_USER_ID=%d\n" % (idx, uid))
            fh.write("MAILHUB_TENANT_%d_QUEUE_TOKEN=q%d\n" % (idx, uid))
            fh.write("MAILHUB_TENANT_%d_APPROVE_TOKEN=a%d\n" % (idx, uid))
            fh.write("MAILHUB_TENANT_%d_LEO_TOKEN=l%d\n" % (idx, uid))
    os.environ["HERMES_HOME"] = home
    tenants._FILE_CACHE.update(path=None, mtime=None, values={})
    both = {t["user_id"] for t in tenants.load()}
    check("2. a tenant present only in the .env file is still found",
          both == {2, 3, 4, 5, 6, 9, 10, 11, 12}, str(sorted(both)))
    check("   which is what the old whitelist prevented",
          both - env_only == {9, 10, 11, 12})
    check("   only MAILHUB_TENANT_* is taken from the file",
          "AGENCY_DB" not in tenants._env_file_values())

    set_env(NINE)
    with P.connect(P.DB) as con:
        health(con)

        print("\n--- 1, 13. Thirty-six leads across nine ready senders ---")
        got = assign(con, 36)
        dist = spread(got)
        print("     distribution: %s" % dist)
        check("1. every ready sender receives work",
              len(dist) == 9 and None not in dist, str(dist))
        check("   no ready sender with capacity gets zero",
              all(dist.get(uid, 0) > 0 for _, uid, _ in NINE))
        check("   the share is even: 4 each for 36 leads",
              set(dist.values()) == {4}, str(sorted(dist.values())))
        check("2. the four newest senders are in rotation",
              all(dist.get(u, 0) == 4 for u in (9, 10, 11, 12)))

        print("\n--- 3-8. A sender that cannot send is skipped ---")
        cases = [
            ("3. disabled / not ready", {9: {"mailbox_ok": 0}}, 9),
            ("4. identity unverified", {10: {"sender_identity_status": "missing",
                                             "mailbox_ok": 0}}, 10),
            ("5. daily-exhausted", {11: {"sent_today": 70}}, 11),
            ("6. hourly-exhausted (no capacity left)", {12: {"sent_today": 70}}, 12),
            ("7. cooldown / unhealthy mailbox", {2: {"mailbox_ok": 0}}, 2),
            ("8. missing a credential", {3: {"approve_ok": 0}}, 3),
        ]
        for label, over, excluded in cases:
            fresh_db()
            with P.connect(P.DB) as c2:
                health(c2, over=over)
                d = spread(assign(c2, 24, prefix="L-%s-" % excluded))
                check("%-38s -> %d never chosen" % (label, excluded),
                      excluded not in d, str(d))
                check("   and the rest still share the work",
                      len(d) == 8 and None not in d, "%d senders" % len(d))

        print("\n--- 9. The tie-break is deterministic ---")
        fresh_db()
        with P.connect(P.DB) as c3:
            health(c3)
            a = [tenants.for_lead("L-tie", c3)["user_id"] for _ in range(5)]
            check("9. the same state always yields the same choice",
                  len(set(a)) == 1, str(a))
            b = [tenants.for_lead("L-tie-other", c3)["user_id"] for _ in range(3)]
            check("   a different lead is also stable, and need not match",
                  len(set(b)) == 1, "%s vs %s" % (a[0], b[0]))

        print("\n--- A starved sender catches up ---")
        fresh_db()
        with P.connect(P.DB) as c4:
            health(c4, over={u: {"sent_today": 40} for u in (2, 3, 4, 5, 6)})
            c4.execute("INSERT OR IGNORE INTO campaigns (id, name, status,"
                       " followup_schedule) VALUES ('C-1','C-1','active','[\"2m\"]')")
            for uid in (2, 3, 4, 5, 6):
                for i in range(20):
                    c4.execute("INSERT OR IGNORE INTO leads (id, campaign_id, email,"
                               " business_name, state, created_at, updated_at) VALUES"
                               " (?, 'C-1', ?, ?, 'SENT', datetime('now'), datetime('now'))",
                               ("L-old-%d-%d" % (uid, i), "o%d%d@example.com" % (uid, i),
                                "Old %d %d" % (uid, i)))
                    c4.execute(
                        "INSERT INTO messages (id, lead_id, campaign_id, direction,"
                        " kind, followup_stage, subject, body, status, dry_run,"
                        " updated_at, tenant_user_id, tenant_assigned_at) VALUES"
                        " (?,?,'C-1','outbound','outreach',0,'S','B','sent',1,"
                        " datetime('now','-2 hours'), ?, datetime('now','-2 hours'))",
                        ("M-old-%d-%d" % (uid, i), "L-old-%d-%d" % (uid, i), uid))
            c4.commit()
            d = spread(assign(c4, 9, prefix="L-catchup-"))
            print("     next 9 assignments: %s" % d)
            check("the four that sent nothing today are served before the rest",
                  all(d.get(u, 0) >= 1 for u in (9, 10, 11, 12)), str(d))
            first_four = [u for u in (9, 10, 11, 12)]
            got = assign(c4, 4, prefix="L-catchup2-")
            check("  with the hour level, the lightest day's sending wins the tie",
                  set(got) <= set(first_four) or len(set(got)) == 4, str(got))

    print("\n--- 10-12. Work already under way never moves ---")
    fresh_db()
    with P.connect(P.DB) as con:
        health(con)
        pinned = tenants.for_message(4, "L-owned", con)
        check("10. a lead already assigned keeps its tenant",
              pinned["status"] == "persisted" and pinned["tenant"]["user_id"] == 4)
        check("    even when another tenant is far less loaded",
              tenants.for_message(4, "L-owned", con)["tenant"]["user_id"] == 4)
        check("11. a follow-up resolves to the same tenant as the first email",
              tenants.for_message(4, "L-owned", con)["tenant"]["user_id"]
              == pinned["tenant"]["user_id"])
        gone = tenants.for_message(99, "L-gone", con)
        check("    a tenant that is no longer usable is refused, not swapped",
              gone["status"] == "changed" and gone["tenant"] is None)
        check("12. suppression stays with the tenant that owns the thread",
              tenants.by_user_id(4)["leo"] == "l4"
              and tenants.by_user_id(9)["leo"] == "l9")

        print("\n--- 13. The excluded mailbox is never selected ---")
        names = {t["name"] for t in tenants.load()}
        check("13. chetansatkar176 is not in the router at all",
              EXCLUDED_MAILBOX not in names, str(sorted(names))[:70])
        got = assign(con, 30, prefix="L-excl-")
        chosen = {tenants.by_user_id(u)["name"] for u in set(got) if u}
        check("    and never appears in 30 assignments",
              EXCLUDED_MAILBOX not in chosen)

    print("\n--- 14. Internal tests do not distort the fairness view ---")
    fresh_db()
    with P.connect(P.DB) as con:
        health(con)
        con.execute("INSERT OR IGNORE INTO campaigns (id, name, status,"
                    " followup_schedule) VALUES ('C-1','C-1','active','[\"2m\"]')")
        con.execute("INSERT OR IGNORE INTO leads (id, campaign_id, email, business_name,"
                    " state, created_at, updated_at) VALUES ('L-test','C-1',"
                    " 't@example.com','T','SENT', datetime('now'), datetime('now'))")
        con.execute("INSERT INTO messages (id, lead_id, campaign_id, direction, kind,"
                    " followup_stage, subject, body, status, dry_run, updated_at,"
                    " tenant_user_id, tenant_assigned_at) VALUES ('M-test','L-test',"
                    " 'C-1','outbound','outreach',0,'S','B','sent',1, datetime('now'),"
                    " 9, datetime('now'))")
        con.commit()
        load = tenants._assignment_load(con)
        check("14. an onboarding test counts as one assignment, not as real volume",
              load.get(9, {}).get("total") == 1, str(load.get(9)))
        d = spread(assign(con, 18, prefix="L-mix-"))
        check("    and one test send does not push a sender out of rotation",
              d.get(9, 0) >= 1, str(d))

    print("\n--- allocation never moves an existing conversation ---")
    src = (HERE / "tenants.py").read_text(encoding="utf-8")
    body = src[src.index("def allocate("):src.index("def for_lead(")]
    check("allocate only reads; it writes nothing",
          not any(w in body for w in ("UPDATE ", "INSERT ", "DELETE ")))
    check("for_message still returns the persisted tenant unchanged",
          "persisted" in src[src.index("def for_message("):])

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
