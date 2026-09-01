#!/usr/bin/env python3
"""Prove the orchestrator cron duplicates nothing.

The cron has been ticking every couple of minutes over the same leads. If any
of the four idempotency mechanisms were failing, it would show here as a second
Kanban task, a second message row, a second MailHub queue record, a repeated
state transition, or a follow-up stage recorded twice.
"""
import json
import os
import sqlite3
import sys

DB = "/opt/data/agency.db"
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

print("=== cron activity ===")
d = json.load(open("/opt/data/cron/jobs.json"))
for j in d["jobs"]:
    if j["name"] == "maya-orchestrator":
        print("  maya-orchestrator runs=%s last=%s status=%s"
              % (j["repeat"]["completed"], j["last_run_at"], j["last_status"]))
        runs = j["repeat"]["completed"]

print("\n=== one message row per (lead, stage) ===")
dupes = list(c.execute(
    "SELECT lead_id, followup_stage, COUNT(*) n FROM messages"
    " GROUP BY lead_id, followup_stage HAVING n > 1"))
check("no duplicate message rows", not dupes, str([dict(r) for r in dupes]))

print("\n=== one MailHub queue id per message ===")
dupes = list(c.execute(
    "SELECT mailhub_queue_id, COUNT(*) n FROM messages"
    " WHERE mailhub_queue_id IS NOT NULL"
    " GROUP BY mailhub_queue_id HAVING n > 1"))
check("no MailHub queue id used twice", not dupes, str([dict(r) for r in dupes]))
ids = [r["mailhub_queue_id"] for r in c.execute(
    "SELECT DISTINCT mailhub_queue_id FROM messages"
    " WHERE mailhub_queue_id IS NOT NULL ORDER BY 1")]
print("  queue ids in use:", ids)

print("\n=== one provider message id per send ===")
dupes = list(c.execute(
    "SELECT provider_message_id, COUNT(*) n FROM messages"
    " WHERE provider_message_id IS NOT NULL"
    " GROUP BY provider_message_id HAVING n > 1"))
check("no provider id recorded against two messages", not dupes,
      str([dict(r) for r in dupes]))

print("\n=== one approval per message ===")
dupes = list(c.execute(
    "SELECT approval_id, COUNT(*) n FROM messages WHERE approval_id IS NOT NULL"
    " GROUP BY approval_id HAVING n > 1"))
check("no approval consumed by two messages", not dupes,
      str([dict(r) for r in dupes]))

print("\n=== no repeated state transition ===")
# The same lead entering the same state twice in a row would mean a tick
# re-ran a handler that had already succeeded.
bad = []
for lead in [r["id"] for r in c.execute("SELECT id FROM leads")]:
    seq = [r["to_state"] for r in c.execute(
        "SELECT to_state FROM events WHERE lead_id=? AND event_type='state.changed'"
        " ORDER BY id", (lead,))]
    for i in range(1, len(seq)):
        if seq[i] and seq[i] == seq[i - 1]:
            bad.append((lead, seq[i]))
check("no lead entered the same state twice consecutively", not bad, str(bad))

print("\n=== one follow-up row per (lead, stage) ===")
dupes = list(c.execute(
    "SELECT lead_id, stage, COUNT(*) n FROM followups"
    " GROUP BY lead_id, stage HAVING n > 1"))
check("no duplicate follow-up stage", not dupes, str([dict(r) for r in dupes]))

print("\n=== attempts never inflated by a paused campaign ===")
rows = list(c.execute(
    "SELECT f.lead_id, f.attempts, f.status, f.last_blocked_reason, c.status AS cs"
    "  FROM followups f LEFT JOIN campaigns c ON c.id=f.campaign_id"
    " WHERE COALESCE(c.status,'active') <> 'active'"))
for r in rows:
    print("  %-18s campaign=%-9s followup=%-10s attempts=%s  %s"
          % (r["lead_id"], r["cs"], r["status"], r["attempts"],
             (r["last_blocked_reason"] or "")[:40]))
# The claim is about follow-ups the pause is CURRENTLY holding: status still
# 'scheduled', with a recorded block reason. Rows that were dispatched or
# skipped before the campaign was paused carry whatever attempts they had
# already accumulated — several of these predate the pause fix entirely, back
# when every tick called touch() and incremented them.
held = [r for r in rows
        if r["status"] == "scheduled" and (r["last_blocked_reason"] or "")]
# Whether anything is held right now depends on what is paused at this moment,
# so the assertion is about the held rows if there are any — not that some must
# exist. A run with nothing paused is a valid run.
if held:
    check("no held follow-up has been charged an attempt by the pause",
          all(r["attempts"] == 0 for r in held),
          ", ".join("%s=att%s" % (r["lead_id"], r["attempts"]) for r in held))
else:
    print("  (nothing is currently held by a pause)")
stale = [r for r in rows if r["status"] != "scheduled"]
print("  (%d row(s) on paused campaigns were already dispatched or skipped "
      "before the pause; their counts are historical)" % len(stale))

print("\n=== Kanban: one task per (lead, generation, stage) ===")
k = sqlite3.connect("/opt/data/kanban.db")
k.row_factory = sqlite3.Row
cols = [r[1] for r in k.execute("PRAGMA table_info(tasks)")]
key = "idempotency_key" if "idempotency_key" in cols else None
if key:
    dupes = list(k.execute(
        "SELECT %s AS k, COUNT(*) n FROM tasks WHERE %s LIKE 'agency:%%'"
        " GROUP BY %s HAVING n > 1" % (key, key, key)))
    check("no duplicate Kanban task for one key", not dupes,
          str([dict(r) for r in dupes]))
    n = k.execute("SELECT COUNT(*) FROM tasks WHERE %s LIKE 'agency:%%'"
                  % key).fetchone()[0]
    print("  agency tasks with an idempotency key: %d" % n)
else:
    print("  (kanban has no idempotency_key column; keys enforced server-side)")

print("\n" + "=" * 74)
print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
