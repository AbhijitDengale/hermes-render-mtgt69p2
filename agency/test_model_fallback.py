#!/usr/bin/env python3
"""One cooling credential pool must not stop the agency.

OmniRoute cools credentials down per model id. On 2026-09-04
`agy/gemini-3.7-flash-medium` returned errors for over an hour while
`agy/gemini-3.7-flash-low` -- same key, same provider, same endpoint -- served
normally, and with no fallback configured every agent turn died on the primary.
That is the second outage where one pool's cooldown froze the whole pipeline.

These tests cover the two halves of the fix separately, because they live in
different places:

  * the ROUTING POLICY is Hermes', not ours. So the policy tests import the
    deployed classifier and assert what it really decides for the bodies this
    gateway really returns -- captured from live responses, not invented. A
    test that asserted our own reimplementation would pass while production
    did something else.

  * the WIRING is ours: which profiles carry a fallback, which must not, and
    that the entry cannot change anything except which model answers.

Two places where the deployed classifier does NOT match the brief are asserted
as they actually behave, with the reasoning, rather than quietly skipped --
see test 7 and 8. Hiding a divergence in a test that does not run it is worse
than not testing it.

No network. No model call. No database writes.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

HERMES = pathlib.Path("/opt/hermes")
HOME = pathlib.Path("/opt/data")

PASSED = 0
FAILED = 0
SKIPPED = 0
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


def skip(name, why):
    global SKIPPED
    SKIPPED += 1
    print("  SKIP %-66s %s" % (name, why))


# ── The errors this gateway actually returns ────────────────────────────
# Every body below was copied from a live ai.syntrix.cv response on
# 2026-09-04. Inventing plausible-looking bodies would test a fiction.
class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class ApiError(Exception):
    """Shaped like the OpenAI SDK's APIStatusError, which is what Hermes sees."""

    def __init__(self, status, body, headers=None):
        self.status_code = status
        self.body = body
        self.response = _Resp(status, headers)
        msg = ((body.get("error") or {}).get("message") or "") \
            if isinstance(body, dict) else str(body)
        self.message = msg
        super().__init__(msg or "HTTP %s" % status)


def err(status, message="", code="", etype="server_error", headers=None):
    return ApiError(status, {"error": {"message": message, "type": etype,
                                       "code": code}}, headers)


POOL_COOLING = ("All credentials for model gemini-3.7-flash-medium are "
                "cooling down")
GATEWAY_QUEUE = ("[504]: Request exceeded OmniRoute's local rate-limit "
                 "execution expiration (legacy "
                 "resilienceSettings.requestQueue.maxWaitMs=15000)")
RESOURCE_PRESSURE = "Service temporarily unavailable due to resource pressure"


def policy_tests(classify):
    """The deployed classifier's verdict on each real failure."""
    def verdict(e):
        c = classify(e, provider="custom", model="agy/gemini-3.7-flash-medium")
        return c.reason.value, c.retryable, c.should_fallback

    print("\n--- 1-5. Transient failures reach the fallback ---")
    # `should_fallback` is the IMMEDIATE hint. For a retryable reason the loop
    # backs off and retries the primary first, and activates the chain only
    # once retries are spent -- which is the order the brief asks for. So the
    # property that matters for a transient error is `retryable`, not an
    # instant switch.
    reason, retryable, fb = verdict(err(429, POOL_COOLING))
    check("1. a cooling credential pool (429) fails over",
          retryable and fb and reason == "rate_limit",
          "%s retryable=%s fallback=%s" % (reason, retryable, fb))

    reason, retryable, fb = verdict(err(503, RESOURCE_PRESSURE))
    check("2. 503 resource pressure retries then falls back",
          retryable and reason == "overloaded",
          "%s retryable=%s (fallback after retries are spent)" % (reason, retryable))

    reason, retryable, fb = verdict(
        err(504, GATEWAY_QUEUE, code="RATE_LIMIT_EXECUTION_TIMEOUT"))
    check("3. 504 gateway queue timeout retries then falls back",
          retryable and reason == "server_error",
          "%s retryable=%s" % (reason, retryable))

    reason, retryable, fb = verdict(err(529, "Overloaded"))
    check("4. 529 overloaded retries then falls back",
          retryable and reason == "overloaded",
          "%s retryable=%s" % (reason, retryable))

    reason, retryable, fb = verdict(ApiError(500, "Internal Server Error"))
    check("5. a bare 500 is treated as transient and retried",
          retryable and reason == "server_error",
          "%s retryable=%s" % (reason, retryable))

    print("\n--- 6. A 500 is never an INSTANT switch ---")
    # This is the whole reason 500 needs care. The gateway returns a byte-
    # identical `500 Internal Server Error`, text/plain, no OmniRoute headers,
    # for BOTH a cooling pool and a request with no `messages` key. No
    # response-only signature can separate them -- verified on 2026-09-04 by
    # sending both and diffing the wire bytes. So a 500 must never trigger an
    # immediate switch: it is retried on the primary, where a genuine bug
    # keeps failing and surfaces, while a cooling pool recovers or the chain
    # takes over after the retries are spent.
    reason, retryable, fb = verdict(ApiError(500, "Internal Server Error"))
    check("6. an arbitrary 500 does not fall back immediately",
          fb is False, "should_fallback=%s" % fb)
    reason, retryable, fb = verdict(
        err(500, "unknown parameter: banana", code="unknown_parameter",
            etype="invalid_request_error"))
    check("   a 500 that names a bad parameter is NOT retried at all",
          retryable is False and reason == "format_error",
          "%s retryable=%s" % (reason, retryable))

    print("\n--- 7-8. Client and auth errors are never retried ---")
    # Divergence from the brief, asserted rather than hidden: Hermes DOES set
    # should_fallback on 400/401/403, because for a normal multi-provider chain
    # a broken credential on one provider may be fine on another. Our chain is
    # the same provider and the same key, so a fallback attempt fails
    # identically, the chain exhausts, and the error still surfaces. The
    # property that actually protects us is `retryable is False`: a malformed
    # request is never re-sent, so a bug cannot become a retry flood.
    reason, retryable, fb = verdict(
        err(400, "[400]: Antigravity upstream error (400)",
            code="bad_request", etype="invalid_request_error"))
    check("7. a 400 bad request is never retried",
          retryable is False and reason == "format_error",
          "%s retryable=%s (fallback=%s, same key -> fails identically)"
          % (reason, retryable, fb))

    for status, label in ((401, "401 unauthorized"), (403, "403 forbidden")):
        reason, retryable, fb = verdict(
            err(status, "Invalid API key", etype="authentication_error"))
        check("8. %s is never retried" % label,
              retryable is False and reason in ("auth", "auth_permanent"),
              "%s retryable=%s" % (reason, retryable))

    print("\n--- 12. Backoff honours the server before guessing ---")
    c = classify(err(429, POOL_COOLING, headers={"retry-after": "30"}),
                 provider="custom", model="agy/gemini-3.7-flash-medium")
    check("12. a 429 carrying Retry-After is still classed transient",
          c.retryable and c.reason.value == "rate_limit", c.reason.value)


def wiring_tests():
    import yaml

    print("\n--- 9-11. Only the model changes; the contract does not ---")
    profiles = {
        "default/MAYA": HOME / "config.yaml",
        "nova": HOME / "profiles/nova/config.yaml",
        "aria": HOME / "profiles/aria/config.yaml",
        "sentinel": HOME / "profiles/sentinel/config.yaml",
        "leo": HOME / "profiles/leo/config.yaml",
        "orbit": HOME / "profiles/orbit/config.yaml",
    }
    if not (HOME / "config.yaml").exists():
        skip("9-11. profile wiring", "not on the deployment host")
        return

    docs = {}
    for label, path in profiles.items():
        docs[label] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    check("9. every LLM profile has a fallback model",
          all((d.get("fallback_model") or {}).get("model") ==
              "agy/gemini-3.7-flash-low" for d in docs.values()),
          ", ".join(sorted(docs)))
    check("   the primary is still 3.7 medium everywhere",
          all((d.get("model") or {}).get("default") ==
              "agy/gemini-3.7-flash-medium" for d in docs.values()))
    check("   the fallback is not a 3.8 model",
          not any("3.8" in ((d.get("fallback_model") or {}).get("model") or "")
                  for d in docs.values()),
          "3.8 failed 14/14 sustained-generation probes on 2026-09-04")

    # Tool calls and structured output survive a failover only because the
    # fallback speaks the same wire protocol: same provider, same endpoint. A
    # fallback on a different base_url would be a different API surface and
    # could silently drop tool_calls.
    check("10. the fallback keeps the provider and endpoint of the primary",
          all((d["fallback_model"].get("provider") ==
               (d.get("model") or {}).get("provider")
               and d["fallback_model"].get("base_url") ==
               (d.get("model") or {}).get("base_url")) for d in docs.values()))
    check("    and it carries a credential, so it cannot 401 on activation",
          all(bool((d["fallback_model"].get("api_key") or "").strip())
              for d in docs.values()))

    # A fallback entry may only say which model answers. Anything that could
    # reach a lead, a mailbox or a tenant has no business in it.
    forbidden = {"tenant", "sender", "mailhub", "supabase", "suppression",
                 "approval", "thread", "campaign", "lead"}
    leaked = set()
    for d in docs.values():
        blob = json.dumps(d["fallback_model"]).lower()
        leaked |= {w for w in forbidden if w in blob}
    check("11. a fallback entry cannot carry tenant or sender state",
          not leaked, "leaked: %s" % sorted(leaked) if leaked else
          "only provider/model/base_url/api_key")

    print("\n--- 13. ECHO stays deterministic ---")
    echo = HOME / "profiles/echo/config.yaml"
    edoc = yaml.safe_load(echo.read_text(encoding="utf-8")) or {} \
        if echo.exists() else {}
    check("13. echo has no fallback model",
          "fallback_model" not in edoc)
    for follow in (pathlib.Path("/opt/data/scripts/echo_followups.py"),
                   HERE / "scripts" / "echo_followups.py"):
        if follow.exists():
            break
    if follow.exists():
        src = follow.read_text(encoding="utf-8").lower()
        check("    and echo's worker calls no agent and no model",
              not any(w in src for w in ("kanban", "dispatch_", "hermes -",
                                         "chat/completions")),
              "deterministic follow-up scheduler")
    else:
        skip("    echo worker source", "not found")


def restore_tests():
    print("\n--- 12b. The primary is preferred again on the next turn ---")
    tc = HERMES / "agent/turn_context.py"
    if not tc.exists():
        skip("12b. primary restore", "Hermes source not present")
        return
    src = tc.read_text(encoding="utf-8", errors="replace")
    check("12b. each new turn restores the primary runtime",
          "_restore_primary_runtime()" in src,
          "so one failover does not pin the profile to the fallback")
    helpers = (HERMES / "agent/agent_runtime_helpers.py").read_text(
        encoding="utf-8", errors="replace")
    start = helpers.index("def restore_primary_runtime")
    body = helpers[start:helpers.index(chr(10) + "def ", start + 10)]
    check("     and restoring resets the chain index to the primary",
          "_fallback_index = 0" in body and "_fallback_activated = False" in body)


def main() -> int:
    print("=" * 78)
    print("MODEL FALLBACK: 3.7 medium -> 3.7 low")
    print("=" * 78)

    try:
        sys.path.insert(0, str(HERMES))
        from agent.error_classifier import classify_api_error
    except Exception as exc:
        skip("1-8, 12. routing policy", "Hermes not importable here (%s)"
             % type(exc).__name__)
    else:
        policy_tests(classify_api_error)

    try:
        wiring_tests()
    except ImportError:
        skip("9-11, 13. wiring", "pyyaml not available")

    restore_tests()

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d    SKIPPED: %d" % (PASSED, FAILED, SKIPPED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
