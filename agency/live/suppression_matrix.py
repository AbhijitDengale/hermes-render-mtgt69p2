#!/usr/bin/env python3
"""The suppression verification matrix, run against the deployed MailHub.

Every assertion is made with a real key against the live API. Nothing here is
inferred from source. Reads the four agency keys from the profile envs that
already hold them, so no credential is passed on a command line or printed.

    python3 suppression_matrix.py
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

PASS, FAIL, SKIP = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


def load(path, want="MAILHUB_API_TOKEN"):
    p = pathlib.Path(path)
    if not p.exists():
        return None, None
    env = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            env[k.strip()] = v.strip()
    return env.get(want), env.get("MAILHUB_BASE_URL")


BASE = None
KEYS = {}
for label, path in (
        ("MAYA", "/opt/data/.env"),
        ("SENTINEL", "/opt/data/profiles/sentinel/.env"),
        ("LEO", "/opt/data/profiles/leo/.env"),
        ("ORBIT (read-only)", "/opt/data/profiles/orbit/.env"),
        ("review-suppress", "/opt/data/agency/.review.env")):
    tok, base = load(path)
    if tok:
        KEYS[label] = tok
        BASE = BASE or base
BASE = (BASE or "").rstrip("/")


def call(token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def suppress(token, email, reason="do_not_contact"):
    return call(token, "POST", "/api/v1/suppression",
                {"email": email, "reason": reason})


print("base:", BASE)
print("keys available:", ", ".join(sorted(KEYS)) or "NONE")
if not KEYS or not BASE:
    print("cannot run: no credentials found"); sys.exit(2)

print("\n=== 0. the fix is actually deployed ===")
code, body = suppress(KEYS.get("ORBIT (read-only)", ""), "probe@example.com",
                      "not_a_valid_reason")
deployed = code in (400, 403)
check("the running service rejects cleanly (not a 500)", deployed,
      "HTTP %s %s" % (code, body[:80]))
if code == 500:
    print("\n  The deploy has NOT landed. Everything below would test the old"
          "\n  code and prove nothing. Stopping.")
    sys.exit(1)

print("\n=== 1. read-only key ===")
tok = KEYS.get("ORBIT (read-only)")
if tok:
    code, _ = call(tok, "GET", "/api/v1/accounts")
    check("read works", code == 200, "HTTP %s" % code)
    code, body = call(tok, "POST", "/api/v1/messages",
                      {"to_email": "m@example.com", "subject": "s",
                       "body_text": "b", "idempotency_key": "matrix-ro-queue"})
    check("queue refused", code == 403, "HTTP %s %s" % (code, body[:70]))
    code, body = call(tok, "POST", "/api/v1/approvals",
                      {"subject": "s", "body_text": "b", "verdict": "approved"})
    check("approve refused", code == 403, "HTTP %s %s" % (code, body[:70]))
    code, body = suppress(tok, "ro-probe@example.com")
    check("suppress refused with 403", code == 403,
          "HTTP %s %s" % (code, body[:70]))

print("\n=== 2. SENTINEL key (read, approve) ===")
tok = KEYS.get("SENTINEL")
if tok:
    code, _ = call(tok, "GET", "/api/v1/accounts")
    check("read works", code == 200, "HTTP %s" % code)
    code, body = call(tok, "POST", "/api/v1/messages",
                      {"to_email": "m@example.com", "subject": "s",
                       "body_text": "b", "idempotency_key": "matrix-sen-queue"})
    check("queue refused", code == 403, "HTTP %s %s" % (code, body[:70]))
    code, body = suppress(tok, "sentinel-probe@example.com")
    check("suppress refused (not scoped for it)", code == 403,
          "HTTP %s %s" % (code, body[:70]))

print("\n=== 3. MAYA key (read, queue, suppress) ===")
tok = KEYS.get("MAYA")
if tok:
    code, body = suppress(tok, "maya-probe@example.com")
    check("suppress works (intentionally scoped)", code == 200,
          "HTTP %s %s" % (code, body[:70]))
    code, body = call(tok, "POST", "/api/v1/approvals",
                      {"subject": "s", "body_text": "b", "verdict": "approved"})
    check("approve refused — no key holds both approve and queue",
          code == 403, "HTTP %s %s" % (code, body[:70]))

print("\n=== 4. LEO key (read, suppress) ===")
tok = KEYS.get("LEO")
if tok:
    code, _ = call(tok, "GET", "/api/v1/accounts")
    check("read works", code == 200, "HTTP %s" % code)
    code, body = suppress(tok, "leo-probe@example.com")
    check("suppress works", code == 200, "HTTP %s %s" % (code, body[:70]))
    code, body = call(tok, "POST", "/api/v1/messages",
                      {"to_email": "m@example.com", "subject": "s",
                       "body_text": "b", "idempotency_key": "matrix-leo-queue"})
    check("queue refused", code == 403, "HTTP %s %s" % (code, body[:70]))
    code, body = call(tok, "POST", "/api/v1/approvals",
                      {"subject": "s", "body_text": "b", "verdict": "approved"})
    check("approve refused", code == 403, "HTTP %s %s" % (code, body[:70]))

print("\n=== 5. reason validation ===")
tok = KEYS.get("review-suppress") or KEYS.get("LEO") or KEYS.get("MAYA")
for reason in ("unsubscribed", "bounced", "do_not_contact", "complaint",
               "manual"):
    code, _ = suppress(tok, "reason-%s@example.com" % reason, reason)
    check("valid reason %-14s accepted" % reason, code == 200, "HTTP %s" % code)
code, body = suppress(tok, "bad-reason@example.com", "probe")
check("invalid reason is a clean 4xx, NOT a 500", code == 400,
      "HTTP %s %s" % (code, body[:90]))
check("  and the message names the valid options",
      "do_not_contact" in body, body[:90])

print("\n=== 6. multi-tenant isolation ===")
print("  Requires two tenants' keys. The agency holds only one tenant's, so"
      "\n  this is asserted by MailHub's own test_web.py, which provisions two"
      "\n  owners against a real Postgres schema:"
      "\n    - a SECOND owner can suppress the same address (was a 500)"
      "\n    - it is then suppressed for both, independently"
      "\n    - a third party is still unaffected")
SKIP.append("multi-tenant isolation (covered by MailHub test_web.py)")

print("\n" + "=" * 78)
print("PASSED: %d    FAILED: %d    DEFERRED: %d" % (len(PASS), len(FAIL), len(SKIP)))
if FAIL:
    print("failed:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
