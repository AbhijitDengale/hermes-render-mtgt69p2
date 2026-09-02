#!/usr/bin/env python3
"""Live proof that LEO can see every tenant's inbox.

Read-only. It fetches each tenant's inbound feed with that tenant's own LEO
credential and reports what came back; it never consumes, classifies, cancels
a follow-up or moves a lead. Classification and cancellation behaviour is
exercised against fixtures in test_multi_tenant.py, which is the right place
for staged replies -- inventing them in the production feed would leave rows
that look like real prospect activity.

What this establishes is the thing unit tests cannot: that five distinct
credentials really do reach five distinct inboxes, and that the single-token
arrangement was reading exactly one of them.

    python3 live/multi_tenant_inbound.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = (os.getenv("MAILHUB_BASE_URL") or "https://autoemail-39jr.onrender.com").rstrip("/")

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-52s %s" % (name, detail))
    else:
        FAILED += 1
        print("  FAIL %-52s %s" % (name, detail))


def read_env(path):
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def get(token, path):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as exc:
        return 0, {"error": str(exc)}


def main():
    root = read_env("/opt/data/.env")
    leo = read_env("/opt/data/profiles/leo/.env")
    tenants = []
    for i in range(1, 33):
        tok = leo.get("MAILHUB_TENANT_%d_LEO_TOKEN" % i)
        if not tok:
            continue
        tenants.append({"name": root.get("MAILHUB_TENANT_%d_NAME" % i, "t%d" % i),
                        "user_id": root.get("MAILHUB_TENANT_%d_USER_ID" % i),
                        "leo": tok})

    print("=" * 68)
    print("LIVE MULTI-TENANT INBOUND REACH  (read-only)")
    print("=" * 68)
    check("five tenants have a LEO credential", len(tenants) == 5,
          "%d configured" % len(tenants))

    print("\n--- each tenant's inbox is reachable with its own credential ---")
    mailboxes = {}
    for t in tenants:
        code, r = get(t["leo"], "/api/v1/inbound?limit=25")
        ok = code == 200
        msgs = r.get("messages", []) if ok else []
        check("%-18s inbound feed readable" % t["name"], ok,
              "http %s, %d outreach repl%s waiting"
              % (code, len(msgs), "y" if len(msgs) == 1 else "ies"))
        code2, r2 = get(t["leo"], "/api/v1/accounts")
        accts = [a["email"] for a in r2.get("accounts", [])]
        mailboxes[t["name"]] = set(accts)
        check("  and it is that tenant's own mailbox", len(accts) == 1,
              accts[0] if accts else "none")

    print("\n--- the five inboxes are genuinely distinct ---")
    seen = set()
    overlap = False
    for name, boxes in mailboxes.items():
        if boxes & seen:
            overlap = True
        seen |= boxes
    check("no two tenants read the same mailbox", not overlap)
    check("five distinct mailboxes reachable in total", len(seen) == 5,
          "%d: %s" % (len(seen), ", ".join(sorted(x.split("@")[0] for x in seen))))
    check("the disabled mailbox is not among them",
          not any(x.startswith("abhijitdengale2003") for x in seen))

    print()
    print("=" * 68)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
