#!/usr/bin/env python3
"""Live probe of the email verification service.

Synthetic addresses only -- no prospect address is ever sent here. The point is
to learn the service's real contract (status vocabulary, flag names, latency,
auth behaviour) before any policy is written on top of it, rather than trusting
the documented shape.

The API key is read from the environment and never printed.

    python3 live/verifier_probe.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = (os.getenv("EMAIL_VERIFIER_URL") or "").rstrip("/")
KEY = os.getenv("EMAIL_VERIFIER_API_KEY") or ""
TIMEOUT = int(os.getenv("EMAIL_VERIFIER_TIMEOUT_SECONDS", "30"))

# Deliberately spans the verdict space. Every domain is either a reserved
# example/invalid domain or a well-known free provider -- none belongs to a
# real prospect, and none of these mailboxes will ever be written to.
PROBES = [
    ("plain gmail address",      "hermes.verifier.probe@gmail.com"),
    ("missing at sign",          "not-an-email"),
    ("missing domain",           "someone@"),
    ("reserved invalid TLD",     "someone@example.invalid"),
    ("non-existent domain",      "someone@this-domain-does-not-exist-hz7.com"),
    ("role account",             "admin@example.com"),
    ("likely typo",              "someone@gmial.com"),
    ("reserved example.com",     "someone@example.com"),
]


def call(path, body=None, key=None, timeout=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data else "GET")
    if key:
        req.add_header("x-api-key", key)
    req.add_header("Content-Type", "application/json")
    # Cloudflare refuses urllib's default "Python-urllib/3.x" with a 403
    # before the Worker ever sees the request, so every call must name itself.
    req.add_header("User-Agent", "hermes-agency-verifier/1.0")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
            return r.status, json.loads(r.read().decode() or "null"), \
                int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "null")
        except Exception:
            payload = None
        return e.code, payload, int((time.time() - t0) * 1000)
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}, \
            int((time.time() - t0) * 1000)


def main():
    if not BASE or not KEY:
        print("EMAIL_VERIFIER_URL / EMAIL_VERIFIER_API_KEY not set")
        return 1

    print("=" * 76)
    print("EMAIL VERIFIER LIVE PROBE  (synthetic addresses only)")
    print("=" * 76)

    code, body, ms = call("/health")
    print("\n--- /health ---")
    print("  http %s in %dms  %s" % (code, ms, json.dumps(body)[:110]))

    print("\n--- auth ---")
    c_no, _, _ = call("/verify", {"email": "someone@example.com"}, key=None)
    c_bad, _, _ = call("/verify", {"email": "someone@example.com"},
                       key="definitely-not-the-key")
    c_ok, _, _ = call("/verify", {"email": "someone@example.com"}, key=KEY)
    print("  no key   -> http %s" % c_no)
    print("  bad key  -> http %s" % c_bad)
    print("  real key -> http %s" % c_ok)
    print("  auth is enforced: %s"
          % (c_no in (401, 403) and c_bad in (401, 403) and c_ok == 200))

    print("\n--- POST /verify, one at a time ---")
    print("  %-24s %-40s %-8s %-6s %-5s %s"
          % ("case", "address", "status", "deliv", "score", "reason / flags"))
    single = {}
    for label, email in PROBES:
        code, body, ms = call("/verify", {"email": email}, key=KEY)
        if code != 200 or not isinstance(body, dict):
            print("  %-24s %-40s http %s %s" % (label, email, code,
                                                json.dumps(body)[:40]))
            continue
        single[email] = body
        flags = ",".join(body.get("flags") or []) or "-"
        print("  %-24s %-40s %-8s %-6s %-5s %s | %s"
              % (label, email[:40], body.get("status"),
                 str(body.get("deliverable")), str(body.get("score")),
                 (body.get("reason") or "-")[:26], flags[:22]))
        dym = body.get("did_you_mean")
        if dym:
            print("  %-24s %-40s did_you_mean: %s" % ("", "", dym))

    print("\n--- POST /verify/batch ---")
    emails = [e for _, e in PROBES]
    code, body, ms = call("/verify/batch", {"emails": emails}, key=KEY)
    results = (body or {}).get("results") if isinstance(body, dict) else None
    print("  http %s in %dms for %d addresses" % (code, ms, len(emails)))
    if results is None:
        print("  unexpected shape: %s" % json.dumps(body)[:200])
    else:
        print("  %d result(s) returned" % len(results))
        got = {r.get("email") for r in results if isinstance(r, dict)}
        missing = [e for e in emails if e not in got]
        print("  every address accounted for: %s%s"
              % (not missing, "" if not missing else "  MISSING: %s" % missing))
        agree = sum(1 for r in results if isinstance(r, dict)
                    and single.get(r.get("email"), {}).get("status") == r.get("status"))
        print("  batch verdicts agree with single: %d/%d" % (agree, len(results)))
        cached = sum(1 for r in results if isinstance(r, dict) and r.get("cached"))
        print("  cached: %d/%d" % (cached, len(results)))

    print("\n--- the status vocabulary this service actually returns ---")
    seen = sorted({r.get("status") for r in (results or [])
                   if isinstance(r, dict)} | {b.get("status") for b in single.values()})
    print("  %s" % ", ".join(str(s) for s in seen))
    allflags = sorted({f for b in single.values() for f in (b.get("flags") or [])})
    print("  flags seen: %s" % (", ".join(allflags) or "(none)"))

    print("\n" + "=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
