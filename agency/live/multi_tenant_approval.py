#!/usr/bin/env python3
"""Live proof that a SENTINEL approval is bound to one tenant.

Runs against the real MailHub with the real per-tenant credentials.

Nothing is queued and no provider send happens. The approval is filed for real,
then matched with GET /api/v1/approvals/check, which runs exactly the lookup
the enqueue QA gate runs -- approvals.find_open(caller.user_id, subject, body)
-- so a match here is the same fact the send path would establish, without a
message existing. The synthetic text is unique per run and never queued, so the
approvals it creates cannot release any real outreach.

    python3 live/multi_tenant_approval.py
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (os.getenv("MAILHUB_BASE_URL") or "https://autoemail-39jr.onrender.com").rstrip("/")
STAMP = os.getenv("MT_STAMP", "mt-live")

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-58s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-58s %s" % (name, detail))


def read_env(path):
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def call(token, method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def approval_seen(token, subject, body):
    q = urllib.parse.urlencode({"subject": subject, "body_text": body})
    code, r = call(token, "GET", "/api/v1/approvals/check?" + q)
    return code == 200 and bool(r.get("approved")), r


def main():
    root = read_env("/opt/data/.env")
    sent = read_env("/opt/data/profiles/sentinel/.env")
    leo = read_env("/opt/data/profiles/leo/.env")

    tenants = []
    for i in range(1, 33):
        q = root.get("MAILHUB_TENANT_%d_QUEUE_TOKEN" % i)
        a = sent.get("MAILHUB_TENANT_%d_APPROVE_TOKEN" % i)
        l = leo.get("MAILHUB_TENANT_%d_LEO_TOKEN" % i)
        if not (q or a or l):
            continue
        tenants.append({
            "i": i, "name": root.get("MAILHUB_TENANT_%d_NAME" % i, "t%d" % i),
            "user_id": root.get("MAILHUB_TENANT_%d_USER_ID" % i),
            "queue": q, "approve": a, "leo": l})

    print("=" * 74)
    print("LIVE MULTI-TENANT APPROVAL  (%d tenants, no message is queued)" % len(tenants))
    print("=" * 74)
    if len(tenants) < 2:
        print("need at least two tenants configured")
        return 1

    print("\n--- 1. Each tenant approves and matches its own content ---")
    for t in tenants:
        subj = "[%s] synthetic check %s" % (STAMP, t["name"])
        body = ("Synthetic content for tenant %s (user %s). Never queued, "
                "never sent." % (t["name"], t["user_id"]))
        t["subject"], t["body"] = subj, body

        before, _ = approval_seen(t["queue"], subj, body)
        check("%-18s no approval before SENTINEL runs" % t["name"], not before)

        code, res = call(t["approve"], "POST", "/api/v1/approvals", {
            "subject": subj, "body_text": body, "qa_status": "approved",
            "qa_agent": "sentinel", "qa_reason": "live multi-tenant check"})
        check("  SENTINEL files it through the tenant approve key",
              code == 200 and res.get("id"), "http %s" % code)
        t["hash"] = res.get("content_hash")

        after, r = approval_seen(t["queue"], subj, body)
        check("  the tenant's own QUEUE key finds it", after,
              "content_hash %s" % (r.get("content_hash") or "")[:12])

    print("\n--- 2. An approval does NOT cross tenants ---")
    # This is the failure the whole design exists to prevent: an approval filed
    # by one tenant releasing a message queued by another.
    for t in tenants:
        others = [o for o in tenants if o is not t]
        leaked = []
        for o in others:
            seen, _ = approval_seen(o["queue"], t["subject"], t["body"])
            if seen:
                leaked.append(o["name"])
        check("%-18s approval invisible to the other %d tenants"
              % (t["name"], len(others)), not leaked,
              "LEAKED to %s" % leaked if leaked else "")

    a, b = tenants[1], tenants[2]
    seen, _ = approval_seen(b["queue"], a["subject"], a["body"])
    check("explicit: %s approval + %s queue = REJECTED"
          % (a["name"], b["name"]), not seen)

    print("\n--- 3. Editing the text after review invalidates it ---")
    for t in tenants[:2]:
        seen, _ = approval_seen(t["queue"], t["subject"], t["body"] + " (edited)")
        check("%-18s altered body no longer matches" % t["name"], not seen)
        seen, _ = approval_seen(t["queue"], t["subject"] + "!", t["body"])
        check("  altered subject no longer matches", not seen)

    print("\n--- 4. Capability separation, live ---")
    for t in tenants:
        c, _ = call(t["approve"], "POST", "/api/v1/messages", {})
        check("%-18s approve key CANNOT queue" % t["name"], c == 403, "http %s" % c)
        c, _ = call(t["queue"], "POST", "/api/v1/approvals", {})
        check("  queue key CANNOT approve", c == 403, "http %s" % c)
        c, _ = call(t["leo"], "POST", "/api/v1/messages", {})
        check("  LEO key CANNOT queue", c == 403, "http %s" % c)
        c, _ = call(t["leo"], "POST", "/api/v1/approvals", {})
        check("  LEO key CANNOT approve", c == 403, "http %s" % c)

    print("\n--- 5. Each credential sees only its own mailbox ---")
    for t in tenants:
        code, r = call(t["queue"], "GET", "/api/v1/accounts")
        accts = r.get("accounts", [])
        check("%-18s sees exactly one mailbox" % t["name"], len(accts) == 1,
              accts[0]["email"] if accts else "none")

    print()
    print("=" * 74)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:")
        for f in FAILURES:
            print("  " + f)
    print("=" * 74)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
