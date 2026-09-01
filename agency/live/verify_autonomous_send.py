#!/usr/bin/env python3
"""Confirm READY_TO_SEND became SENT on its own, once quota allowed it.

Run this after the mailbox's daily counter has rolled over. It asks three
questions and does not guess at any of them:

  1. Did MailHub actually hand the message to Gmail? (provider id present)
  2. Did the lead reach SENT without anybody running a tick?
  3. Did anything get sent twice?

Run nothing else first — in particular, do NOT run `orchestrator.py tick`.
The whole point is that the cron did it.

    cd /opt/data/agency
    set -a; . /opt/data/.env; set +a
    python3 live/verify_autonomous_send.py
"""

import glob
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

JOB = "6416a10f0628"
PASS, FAIL, PENDING = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-56s %s" % ("PASS" if ok else "FAIL", name, detail))


def mailhub(path):
    base = os.environ.get("MAILHUB_BASE_URL", "").rstrip("/")
    tok = os.environ.get("MAILHUB_API_TOKEN", "")
    if not base or not tok:
        return {"error": "MailHub not configured — source /opt/data/.env"}
    req = urllib.request.Request(base + path)
    req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": "http %d" % e.code}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


c = sqlite3.connect("/opt/data/agency.db")
c.row_factory = sqlite3.Row

print("=== sender capacity ===")
acc = mailhub("/api/v1/accounts")
for a in acc.get("accounts", []):
    print("  %s sent_today=%s/%s health=%s"
          % (a.get("email"), a.get("sent_today"),
             a.get("effective_daily_limit"), a.get("health")))

print("\n=== messages queued while the cap was exhausted ===")
rows = list(c.execute(
    "SELECT m.id, m.lead_id, m.followup_stage, m.status, m.approval_id,"
    "       m.mailhub_queue_id, m.provider_message_id, m.provider_thread_id,"
    "       l.state AS lead_state, l.email"
    "  FROM messages m JOIN leads l ON l.id = m.lead_id"
    " WHERE m.mailhub_queue_id IS NOT NULL"
    " ORDER BY CAST(m.mailhub_queue_id AS INTEGER)"))
for r in rows:
    print("  #%-3s %-24s stage=%s agency=%-9s lead=%-16s provider=%s"
          % (r["mailhub_queue_id"], r["lead_id"], r["followup_stage"],
             r["status"], r["lead_state"], r["provider_message_id"] or "-"))

print("\n=== what MailHub says about each ===")
confirmed = []
for r in rows:
    q = mailhub("/api/v1/messages/%s" % r["mailhub_queue_id"])
    print("  #%-3s mailhub=%-10s provider=%-20s sent_at=%s"
          % (r["mailhub_queue_id"], q.get("status"),
             q.get("provider_message_id") or "-", q.get("sent_at") or "-"))
    if q.get("status") in ("sent", "simulated") and q.get("provider_message_id"):
        confirmed.append((r, q))

if not confirmed:
    print("\n  Nothing has been provider-confirmed yet. Either the daily counter"
          "\n  has not rolled over, or the worker has not reached these. Re-run"
          "\n  later; this is not a failure.")
    sys.exit(2)

print("\n=== did the cron record them without help? ===")
for r, q in confirmed:
    lead, pid = r["lead_id"], q["provider_message_id"]
    row = c.execute("SELECT status, provider_message_id, provider_thread_id,"
                    "       sent_at FROM messages WHERE id=?",
                    (r["id"],)).fetchone()
    st = c.execute("SELECT state FROM leads WHERE id=?", (lead,)).fetchone()["state"]
    check("%s: agency recorded the provider id" % lead[:18],
          row["provider_message_id"] == pid,
          "%s vs %s" % (row["provider_message_id"], pid))
    check("  message marked sent", row["status"] in ("sent", "simulated"),
          row["status"])
    check("  lead advanced past READY_TO_SEND", st != "READY_TO_SEND", st)

    # Which cron run did it? The output files are the record.
    who = []
    for path in sorted(glob.glob("/opt/data/cron/output/%s/*.md" % JOB)):
        body = open(path, encoding="utf-8").read()
        if lead in body and ("SENT" in body or "sent" in body):
            who.append(os.path.basename(path).replace(".md", ""))
    check("  a cron run is on record for it", bool(who),
          ", ".join(who[-3:]) or "no cron output mentions it")

print("\n=== nothing sent twice ===")
dupes = list(c.execute(
    "SELECT provider_message_id, COUNT(*) n FROM messages"
    " WHERE provider_message_id IS NOT NULL"
    " GROUP BY provider_message_id HAVING n > 1"))
check("no provider id against two messages", not dupes, str([dict(x) for x in dupes]))
dupes = list(c.execute(
    "SELECT mailhub_queue_id, COUNT(*) n FROM messages"
    " WHERE mailhub_queue_id IS NOT NULL"
    " GROUP BY mailhub_queue_id HAVING n > 1"))
check("no queue id against two messages", not dupes, str([dict(x) for x in dupes]))

print("\n" + "=" * 74)
print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
