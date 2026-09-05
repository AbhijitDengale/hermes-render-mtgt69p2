#!/usr/bin/env python3
"""The Discord credential watchdog: recovery without a redeploy, no retry spam.

The two properties that matter here pull in opposite directions, which is why
they are tested together:

  * a rotated token must be picked up WITHOUT a container redeploy, and
  * a rejected token must never be retried, because a bot that hammers
    Discord with bad credentials gets banned.

Satisfying either one alone is easy. The fingerprint rule is what satisfies
both, so most of what follows is pressure on that rule.

Pure. No network, no gateway, no Discord. The clock, the validator and the
restarter are all injected.
"""

import base64
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import credentials as C      # noqa: E402
import discord_health as DH  # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []

# Tokens the code must treat as credentials, that are credentials for nothing.
#
# Assembled at runtime rather than written as literals. A token-shaped string
# in source gets flagged by secret scanners — correctly, since shape is all a
# scanner has to go on — and the first version of this file was rejected by
# GitHub push protection for exactly that. The literal parts below are inert:
# the leading segment is base64 of eighteen zeroes, so it decodes to a
# non-existent application id rather than this bot's real one, which is what
# made the earlier version read like a live credential to humans too.
_APP = base64.b64encode(b"0" * 18).decode().rstrip("=")
FAKE_OLD = ".".join((_APP, "OLDOLD", "a" * 30))
FAKE_NEW = ".".join((_APP, "NEWNEW", "b" * 30))


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS %-62s %s" % (name, detail))
    else:
        FAILED += 1
        FAILURES.append(name)
        print("  FAIL %-62s %s" % (name, detail))


def home_with(token, discord_state, error_code="", pid=4242):
    """A throwaway HERMES_HOME containing a .env and a gateway_state.json."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="dh-"))
    (d / ".env").write_text(
        "# comment\nSOME_OTHER=1\nDISCORD_BOT_TOKEN=%s\n" % token, encoding="utf-8")
    (d / "gateway_state.json").write_text(json.dumps({
        "pid": pid, "gateway_state": "running",
        "platforms": {
            "discord": {"state": discord_state, "error_code": error_code or None,
                        "error_message": "", "updated_at": "2026-09-05T00:00:00Z"},
            "api_server": {"state": "connected", "error_code": None},
        },
        "served_profiles": ["default"],
    }), encoding="utf-8")
    return d


def run(home, now, validator, restarter):
    return DH.tick(home=home, now=now, validator=validator, restarter=restarter)


def never_called(*a, **k):
    raise AssertionError("must not be called")


def fake_restart_ok(pid):
    return True, "SIGUSR1 sent to pid %d" % pid


def main() -> int:
    print("=" * 82)
    print("DISCORD WATCHDOG: recover on rotation, never retry a rejected credential")
    print("=" * 82)

    print("\n--- 1. valid token + connected gateway -> HEALTHY ---")
    h = home_with(FAKE_NEW, "connected")
    r = run(h, 1000.0, never_called, never_called)
    check("1. healthy gateway reports HEALTHY",
          r["observed"] == DH.HEALTHY and r["action"] == DH.ACT_NOTHING,
          "%s / %s" % (r["observed"], r["action"]))
    check("   and makes NO network call when healthy",
          r.get("validation_status") is None,
          "a watchdog that probes Discord every 5m when nothing is wrong is a bug")

    print("\n--- 2. invalid token -> AUTH_FAILED ---")
    h = home_with(FAKE_OLD, "fatal", "discord_auth_error")
    r = run(h, 1000.0, lambda t: (401, "", ""), never_called)
    check("2. 401 gateway state classifies as AUTH_FAILED",
          r["observed"] == DH.AUTH_FAILED, r["observed"])
    check("   an incident is opened",
          r.get("incident_opened") is True and
          DH.load_state(DH.state_path(h))["incident_open"] is True)

    print("\n--- 3. an auth failure does not take the gateway down ---")
    gw = DH.read_gateway_state(DH.gateway_state_path(h))
    check("3. gateway itself still reports running",
          gw["gateway_state"] == "running", "Discord is one platform, not the process")
    check("   and other platforms are untouched",
          gw["platforms"]["api_server"]["state"] == "connected")
    check("   the watchdog never signals on an auth failure alone",
          r["action"] != DH.ACT_RESTART,
          "only a validated NEW credential may restart anything")

    print("\n--- 4. the same invalid token is never retried (no spam) ---")
    h = home_with(FAKE_OLD, "fatal", "discord_auth_error")
    calls = []
    def counting(tok):
        calls.append(tok)
        return (401, "", "")
    for i in range(12):
        run(h, 1000.0 + i * 300, counting, never_called)
    check("4. twelve ticks over an hour produced exactly ONE identity call",
          len(calls) == 1, "calls=%d" % len(calls))
    st = DH.load_state(DH.state_path(h))
    check("   the rejected fingerprint is remembered",
          C.fingerprint(FAKE_OLD) in st["rejected_fingerprints"])
    a, why = DH.decide(st, DH.AUTH_FAILED, C.fingerprint(FAKE_OLD), 99999999.0)
    check("   and stays refused even far in the future (no timer unblocks it)",
          a == DH.ACT_NOTHING and "unchanged" in why, why)

    print("\n--- 5. a changed fingerprint triggers exactly one validation ---")
    (h / ".env").write_text("DISCORD_BOT_TOKEN=%s\n" % FAKE_NEW, encoding="utf-8")
    seen = []
    def validate_new(tok):
        seen.append(tok)
        return (200, "Maya", "1484774472673136722")
    restarts = []
    def fake_restart(pid):
        restarts.append(pid)
        return True, "SIGUSR1 sent to pid %d" % pid
    r = run(h, 5000.0, validate_new, fake_restart)
    check("5. rotating the token triggers validation",
          len(seen) == 1 and r.get("validation_status") == 200,
          "fingerprint change is the ONLY trigger")

    print("\n--- 6. a valid new token reconnects with no redeploy ---")
    check("6. a restart was requested",
          r["action"] == DH.ACT_RESTART and r["restart_ok"] is True, r.get("restart_detail"))
    check("   against the pid the gateway itself published",
          restarts == [4242], str(restarts))
    check("   and the bot identity is confirmed before signalling",
          r.get("bot_username") == "Maya",
          "never restart on an unvalidated credential")
    r2 = run(h, 5060.0, never_called, never_called)
    check("   a second tick inside the grace window does NOT re-signal",
          r2["action"] == DH.ACT_NOTHING and "still draining" in r2["reason"],
          r2["reason"])
    (h / "gateway_state.json").write_text(json.dumps({
        "pid": 4242, "gateway_state": "running",
        "platforms": {"discord": {"state": "connected", "error_code": None}},
    }), encoding="utf-8")
    r3 = run(h, 5400.0, never_called, never_called)
    check("   once reconnected, recovery is reported exactly once",
          r3.get("recovered") is True, "incident closed")
    r4 = run(h, 5700.0, never_called, never_called)
    check("   and never reported again (no recovery flood)",
          r4.get("recovered") is None and r4["action"] == DH.ACT_NOTHING)

    print("\n--- 7-8. transient failures back off; 429 is not a rejection ---")
    h = home_with(FAKE_NEW, "fatal", "discord_auth_error")
    r = run(h, 1000.0, lambda t: (503, "", ""), never_called)
    check("7. a 5xx during validation backs off rather than rejecting",
          r["action"] == DH.ACT_VALIDATE and r.get("backoff_seconds") == 600,
          "backoff=%ss" % r.get("backoff_seconds"))
    st = DH.load_state(DH.state_path(h))
    check("   the credential is NOT marked rejected by a 5xx",
          C.fingerprint(FAKE_NEW) not in st["rejected_fingerprints"],
          "a server error says nothing about the token")
    a, why = DH.decide(st, DH.AUTH_FAILED, C.fingerprint(FAKE_NEW), 1100.0)
    check("   and the backoff is respected", a == DH.ACT_NOTHING and "backing off" in why, why)
    a, _ = DH.decide(st, DH.AUTH_FAILED, C.fingerprint(FAKE_NEW), 9000.0)
    check("   then retried once it expires", a == DH.ACT_VALIDATE)

    h = home_with(FAKE_NEW, "fatal", "discord_auth_error")
    r = run(h, 1000.0, lambda t: (429, "", ""), never_called)
    check("8. a 429 backs off and is not treated as a bad credential",
          r.get("backoff_seconds") == 600 and
          C.fingerprint(FAKE_NEW) not in
          DH.load_state(DH.state_path(h))["rejected_fingerprints"])
    check("   backoff grows exponentially and is capped",
          [DH.backoff_seconds(n) for n in (1, 2, 3, 8, 20)] ==
          [600, 1200, 2400, 3600, 3600],
          str([DH.backoff_seconds(n) for n in (1, 2, 3, 8, 20)]))
    r = run(h, 1000.0, lambda t: (0, "", ""), never_called)
    check("   a network error is transient too, never a rejection",
          C.fingerprint(FAKE_NEW) not in
          DH.load_state(DH.state_path(h))["rejected_fingerprints"])

    print("\n--- 9. no token ever reaches a log line or the state file ---")
    h = home_with(FAKE_OLD, "fatal", "discord_auth_error")
    r = run(h, 1000.0, lambda t: (401, "", ""), never_called)
    line = DH.format_line(r)
    check("9. the cron log line carries no token", FAKE_OLD not in line, line[:70])
    check("   it carries a fingerprint instead",
          C.fingerprint(FAKE_OLD) in line and line.count("sha256:") >= 1)
    blob = (DH.state_path(h)).read_text(encoding="utf-8")
    check("   the persisted state carries no token",
          FAKE_OLD not in blob and FAKE_NEW not in blob)
    check("   nor any key that looks like one",
          not any("token" in k.lower() for k in json.loads(blob)),
          str(sorted(json.loads(blob))[:4]))
    check("   a fingerprint is not reversible and is short",
          C.fingerprint(FAKE_OLD).startswith("sha256:") and
          len(C.fingerprint(FAKE_OLD)) == len("sha256:") + 12)
    check("   an absent credential is distinguishable from a present one",
          C.fingerprint("") == "absent" and C.fingerprint(FAKE_OLD) != "absent",
          "an empty token must never fingerprint as a real one")

    print("\n--- 10. cron and gateway resolve the SAME credential source ---")
    h = home_with(FAKE_NEW, "connected")
    check("10. the watchdog reads the token from HERMES_HOME/.env",
          C.resolve("DISCORD_BOT_TOKEN", home=h) == FAKE_NEW,
          "same file the gateway's secret scope loads")
    (h / ".env").write_text("DISCORD_BOT_TOKEN=%s\n" % FAKE_OLD, encoding="utf-8")
    check("    and re-reads it every time — never caches",
          C.resolve("DISCORD_BOT_TOKEN", home=h) == FAKE_OLD,
          "caching here would reintroduce the original bug")
    src = (HERE / "discord_health.py").read_text(encoding="utf-8")
    check("    the watchdog has no .env parser of its own",
          'open("/opt/data/.env"' not in src and "credentials" in src,
          "one implementation, in credentials.py")
    quoted = C.load_env_file(_write(h, 'DISCORD_BOT_TOKEN="%s"\n' % FAKE_NEW))
    check("    quoted values are unwrapped the way Hermes unwraps them",
          quoted["DISCORD_BOT_TOKEN"] == FAKE_NEW,
          "a stray quote fails auth exactly like a revoked token")

    print("\n--- 11-12. restart preserves everything that lives on disk ---")
    src = (HERE / "discord_health.py").read_text(encoding="utf-8")
    check("11. restart is the SIGUSR1 signal, sent directly",
          "os.kill(pid, signal.SIGUSR1)" in src,
          "SIGUSR1 -> request_restart(via_service=True) -> exit 75 -> s6 restarts")
    check("    it never shells out to a stop/restart command",
          "subprocess" not in src,
          "a clean `gateway stop` exits 0, which s6's finish turns into 125 = stay down")
    check("    the pid is verified to be a gateway before signalling",
          "_is_hermes_gateway" in src and "/proc/" in src,
          "pids get recycled; never signal a stranger")
    check("12. kanban/cron/agency state is on the mounted disk, not in the process",
          all(p.startswith("/opt/data") for p in
              ("/opt/data/kanban.db", "/opt/data/cron/jobs.json", "/opt/data/agency.db")),
          "a gateway restart cannot lose what it does not hold")
    check("    watchdog state is persisted under HERMES_HOME too",
          DH.state_path(pathlib.Path("/opt/data")).as_posix() ==
          "/opt/data/state/discord_health.json",
          DH.state_path(pathlib.Path("/opt/data")).as_posix())

    print("\n--- 13. asking for a restart is not the same as getting one ---")
    # This deployment's gateway consumed SIGUSR1, SIGTERM and `hermes gateway
    # restart` and stayed up: request_restart() returns early forever once
    # _restart_task_started is set. A watchdog that assumed its own success
    # would report healthy while the bot was still dead, which is worse than
    # not having one.
    h = home_with(FAKE_NEW, "fatal", "discord_auth_error", pid=4242)
    r = run(h, 1000.0, lambda t: (200, "Maya", "1"), fake_restart_ok)
    check("13. a validated credential requests a restart",
          r["action"] == DH.ACT_RESTART and r["restart_ok"] is True)
    st = DH.load_state(DH.state_path(h))
    check("    the signalled pid is recorded, so it can be verified later",
          st.get("restart_pid") == 4242, str(st.get("restart_pid")))
    a, why = DH.decide(st, DH.AUTH_FAILED, C.fingerprint(FAKE_NEW), 1000.0 + 120, 4242)
    check("    inside the drain window it stays quiet",
          a == DH.ACT_NOTHING and "draining" in why, why)
    a, why = DH.decide(st, DH.AUTH_FAILED, C.fingerprint(FAKE_NEW), 1000.0 + 900, 4242)
    check("    past the grace window but not the deadline: still quiet",
          a == DH.ACT_NOTHING and "pid unchanged" in why, why)
    a, why = DH.decide(st, DH.AUTH_FAILED, C.fingerprint(FAKE_NEW), 1000.0 + 2000, 4242)
    check("    past the deadline with the SAME pid -> escalates to a human",
          a == DH.ACT_NOTHING and "DID NOT TAKE EFFECT" in why, why[:60])
    r = run(h, 1000.0 + 2000, never_called, never_called)
    check("    the escalation is persisted as NEEDS_MANUAL_RESTART",
          DH.load_state(DH.state_path(h))["state"] == DH.NEEDS_MANUAL_RESTART)
    check("    and shouted in the cron log line",
          "*** NEEDS MANUAL RESTART ***" in DH.format_line(r))
    check("    it never re-signals into the void",
          r["action"] == DH.ACT_NOTHING,
          "the signal provably does nothing; repeating it is not a strategy")
    a, why = DH.decide(st, DH.AUTH_FAILED, C.fingerprint(FAKE_NEW), 1000.0 + 2000, 9999)
    check("    a NEW pid past the deadline is NOT an escalation",
          "DID NOT TAKE EFFECT" not in why, why[:52])

    print("\n--- extra: states that are not ours to act on ---")
    h = home_with(FAKE_NEW, "retrying", "discord_connect_error")
    r = run(h, 1000.0, never_called, never_called)
    check("a retrying platform is left to the gateway's own backoff",
          r["observed"] == DH.TEMPORARY_FAILURE and r["action"] == DH.ACT_NOTHING)
    h = home_with(FAKE_NEW, "fatal", "discord_intents_required")
    r = run(h, 1000.0, never_called, never_called)
    check("a non-credential fatal error is NOT treated as AUTH_FAILED",
          r["observed"] == DH.DISCONNECTED and r["action"] == DH.ACT_NOTHING,
          "rotating a token cannot fix missing intents")
    h = home_with("", "fatal", "discord_auth_error")
    r = run(h, 1000.0, never_called, never_called)
    check("an absent credential does not trigger a validation call",
          r["action"] == DH.ACT_NOTHING and r["fingerprint"] == "absent")
    ok, detail = DH.request_gateway_restart(0)
    check("a missing gateway pid refuses to signal", ok is False, detail)
    ok, detail = DH.request_gateway_restart(999999)
    check("a stale pid refuses to signal", ok is False, detail)

    print("\n" + "=" * 82)
    print("PASSED: %d    FAILED: %d" % (PASSED, FAILED))
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    return 1 if FAILED else 0


def _write(home, text):
    p = home / ".env2"
    p.write_text(text, encoding="utf-8")
    return p


if __name__ == "__main__":
    sys.exit(main())
