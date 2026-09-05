#!/usr/bin/env python3
"""Recover the Discord connection after a token rotation, without a redeploy.

THE BUG THIS CLOSES
-------------------
The Hermes gateway reads ``DISCORD_BOT_TOKEN`` once, when it builds its
platform config, and holds it for the life of the process. When Discord
answers ``401``, the adapter classifies it ``discord_auth_error`` with
``retryable=False``; the gateway then marks the platform ``fatal``, logs
"removing from retry queue", disposes the adapter and deletes the platform
from ``_failed_platforms``. Nothing re-reads the credential afterwards.

That classification is *correct* — retrying a revoked token in a loop is how
a bot gets banned — but it is only half a policy. The missing half is
noticing that the operator fixed the credential. Rotating the token updates
``.env``, which every cron script re-reads on its next run, so the review
cards and ORBIT reports recover within two minutes while the interactive bot
stays dead until something restarts the process. On 2026-09-04 that gap was
fifteen hours.

WHY THIS LIVES HERE AND NOT IN THE GATEWAY
------------------------------------------
``/opt/hermes`` is the container image, on the overlay filesystem. Only
``/opt/data`` is the mounted disk. A patch to ``gateway/run.py`` would work
until the next deploy and then silently revert — a worse failure than the one
being fixed, because the fix would appear to be in place. So the recovery
runs from the persistent side, as a cron job, and drives the gateway through
the one interface that is stable across upgrades: the restart signal.

THE POLICY
----------
Never retry a credential that has already been rejected. Watch the credential
*fingerprint* instead; act only when it changes. Concretely::

    AUTH_FAILED + fingerprint unchanged   -> do nothing, forever
    AUTH_FAILED + fingerprint changed     -> validate ONCE against Discord
        validation 200                    -> restart the gateway, once
        validation 401/403                -> remember this one is bad too
        validation 429/5xx/timeout        -> bounded exponential backoff

which means a wrong token is tried exactly once per distinct wrong token, and
a right token is picked up within one cron interval of being written.

No token value is logged, persisted, or returned by anything in this module;
identity is carried as a truncated SHA-256 (see :mod:`credentials`).
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

try:
    import credentials as C
except ImportError:  # running from a checkout rather than /opt/data/agency
    from . import credentials as C  # type: ignore

__all__ = [
    "DISCONNECTED", "CONNECTING", "HEALTHY", "AUTH_FAILED", "TEMPORARY_FAILURE",
    "ACT_NOTHING", "ACT_VALIDATE", "ACT_RESTART", "ACT_REPORT_RECOVERY",
    "classify", "decide", "validate_token", "read_gateway_state",
    "load_state", "save_state", "request_gateway_restart", "tick",
]

# ── operational states (section 8) ──────────────────────────────────────────
DISCONNECTED = "DISCONNECTED"
CONNECTING = "CONNECTING"
HEALTHY = "HEALTHY"
AUTH_FAILED = "AUTH_FAILED"
TEMPORARY_FAILURE = "TEMPORARY_FAILURE"

# ── what a tick may do ──────────────────────────────────────────────────────
ACT_NOTHING = "nothing"
ACT_VALIDATE = "validate"
ACT_RESTART = "restart"
ACT_REPORT_RECOVERY = "report_recovery"

TOKEN_ENV = "DISCORD_BOT_TOKEN"
IDENTITY_URL = "https://discord.com/api/v10/users/@me"

# Backoff for transient validation failures only. A rejected credential is not
# on a timer at all — it waits for a new fingerprint, however long that takes.
BACKOFF_BASE_SECONDS = 300
BACKOFF_CAP_SECONDS = 3600

# A restart we asked for but have not yet seen take effect. Prevents a second
# signal every tick while the gateway is still draining.
RESTART_GRACE_SECONDS = 180


def state_path(home: Optional[pathlib.Path] = None) -> pathlib.Path:
    return (home or C.hermes_home()) / "state" / "discord_health.json"


def gateway_state_path(home: Optional[pathlib.Path] = None) -> pathlib.Path:
    return (home or C.hermes_home()) / "gateway_state.json"


# ── inputs ──────────────────────────────────────────────────────────────────

def read_gateway_state(path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """The gateway's own view of its Discord platform. Never raises."""
    try:
        raw = json.loads((path or gateway_state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def classify(gateway_state: Dict[str, Any]) -> Tuple[str, str]:
    """Map the gateway's platform status onto our state model.

    Returns ``(state, error_code)``. A ``fatal`` platform that is NOT an auth
    problem (privileged intents, for example) deliberately does not become
    ``AUTH_FAILED``: rotating a token cannot fix it, so a credential watcher
    must not pretend it is watching something relevant. It reports
    ``DISCONNECTED`` and leaves it for a human.
    """
    platforms = gateway_state.get("platforms")
    if not isinstance(platforms, dict):
        return DISCONNECTED, ""
    block = platforms.get("discord")
    if not isinstance(block, dict):
        return DISCONNECTED, ""

    raw_state = str(block.get("state") or "").strip().lower()
    code = str(block.get("error_code") or "")

    if raw_state == "connected":
        return HEALTHY, ""
    if raw_state in ("connecting", "starting"):
        return CONNECTING, code
    if raw_state == "retrying":
        # The gateway is already backing off on its own schedule. Interfering
        # would only add a second, competing retry loop.
        return TEMPORARY_FAILURE, code
    if raw_state == "fatal":
        return (AUTH_FAILED, code) if code == "discord_auth_error" else (DISCONNECTED, code)
    return DISCONNECTED, code


# ── persisted operational state (never a credential) ────────────────────────

def load_state(path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    try:
        raw = json.loads((path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("state", DISCONNECTED)
    raw.setdefault("rejected_fingerprints", [])
    raw.setdefault("validation_failures", 0)
    raw.setdefault("next_probe_after", 0)
    raw.setdefault("incident_open", False)
    raw.setdefault("recovery_reported", True)
    raw.setdefault("restart_requested_at", 0)
    return raw


def save_state(data: Dict[str, Any], path: Optional[pathlib.Path] = None) -> None:
    p = path or state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    scrubbed = {k: v for k, v in data.items() if "token" not in k.lower()}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(scrubbed, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


# ── the decision (pure: no clock, no network, no disk) ──────────────────────

def decide(state: Dict[str, Any], observed: str, fingerprint: str,
           now: float) -> Tuple[str, str]:
    """Return ``(action, reason)`` for this tick.

    Pure by construction so the whole policy — including the "do not hammer"
    rule, which is the part that would get a bot banned if it regressed — is
    testable without a network, a clock, or a gateway.
    """
    if observed == HEALTHY:
        if state.get("incident_open") and not state.get("recovery_reported"):
            return ACT_REPORT_RECOVERY, "discord recovered"
        return ACT_NOTHING, "healthy"

    if observed in (CONNECTING, TEMPORARY_FAILURE):
        # Someone else's retry loop owns this. Stay out of it.
        return ACT_NOTHING, "gateway is handling it (%s)" % observed.lower()

    if observed == DISCONNECTED:
        return ACT_NOTHING, "not a credential fault; needs a human"

    # observed == AUTH_FAILED
    if fingerprint == "absent":
        return ACT_NOTHING, "no credential configured"

    pending = float(state.get("restart_requested_at") or 0)
    if pending and now - pending < RESTART_GRACE_SECONDS:
        return ACT_NOTHING, "restart already requested %ds ago" % int(now - pending)

    if fingerprint in (state.get("rejected_fingerprints") or []):
        # THE central rule. Discord already told us this exact credential is
        # bad; asking again cannot change the answer and risks a ban.
        return ACT_NOTHING, "credential unchanged since rejection"

    next_probe = float(state.get("next_probe_after") or 0)
    if now < next_probe:
        return ACT_NOTHING, "backing off for %ds" % int(next_probe - now)

    return ACT_VALIDATE, "credential fingerprint changed"


def backoff_seconds(failures: int) -> int:
    """Exponential with a cap, for transient validation failures only."""
    if failures <= 0:
        return BACKOFF_BASE_SECONDS
    return int(min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** min(failures, 8))))


# ── effects ─────────────────────────────────────────────────────────────────

def validate_token(token: str, *, timeout: float = 20.0) -> Tuple[int, str, str]:
    """One identity call. Returns ``(status, username, user_id)``.

    Status ``0`` means the request never completed (DNS, TLS, timeout) and is
    treated as transient, not as a rejection — misfiling a network blip as a
    bad credential would park a perfectly good token until it changed again.
    """
    req = urllib.request.Request(
        IDENTITY_URL,
        headers={"Authorization": "Bot " + token,
                 "User-Agent": "hermes-discord-watchdog/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, str(body.get("username") or ""), str(body.get("id") or "")
    except urllib.error.HTTPError as exc:
        return exc.code, "", ""
    except Exception:
        return 0, "", ""


def _is_hermes_gateway(pid: int) -> bool:
    """Confirm the pid really is a Hermes gateway before signalling it.

    PIDs are recycled. The gateway state file can name a pid that died and was
    replaced by something unrelated, and sending SIGUSR1 to an arbitrary
    process is not an acceptable failure mode for a watchdog.
    """
    try:
        cmdline = pathlib.Path("/proc/%d/cmdline" % pid).read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return False
    return "hermes" in cmdline and "gateway" in cmdline


def request_gateway_restart(pid: int) -> Tuple[bool, str]:
    """Ask the gateway to restart itself the way Hermes intends.

    SIGUSR1 is wired to ``request_restart(via_service=True)``, which drains
    in-flight turns and then exits 75 (``EX_TEMPFAIL``). The s6 ``finish``
    script passes 75 through as "restart me", so the supervisor brings the
    gateway straight back with a freshly-read ``.env``.

    Deliberately NOT ``hermes gateway stop``: a clean stop exits 0, the finish
    script turns that into 125, and s6 leaves the gateway down for good.
    """
    if pid <= 0:
        return False, "no gateway pid recorded"
    if not _is_hermes_gateway(pid):
        return False, "pid %d is not a hermes gateway (stale pid)" % pid
    try:
        os.kill(pid, signal.SIGUSR1)
    except (OSError, ProcessLookupError, PermissionError) as exc:
        return False, "signal failed: %s" % type(exc).__name__
    return True, "SIGUSR1 sent to pid %d (graceful restart, exit 75)" % pid


# ── one cron tick ───────────────────────────────────────────────────────────

def tick(*, home: Optional[pathlib.Path] = None, now: Optional[float] = None,
         validator=validate_token, restarter=request_gateway_restart) -> Dict[str, Any]:
    """Run the watchdog once. Returns a summary dict for the cron log."""
    now = time.time() if now is None else now
    home = home or C.hermes_home()

    token = C.resolve(TOKEN_ENV, "", home=home)
    fp = C.fingerprint(token)
    gw = read_gateway_state(gateway_state_path(home))
    observed, code = classify(gw)
    state = load_state(state_path(home))

    action, reason = decide(state, observed, fp, now)
    out: Dict[str, Any] = {
        "observed": observed, "error_code": code, "fingerprint": fp,
        "action": action, "reason": reason, "credential_present": bool(token),
        "changed": False,
    }

    if observed == HEALTHY:
        if action == ACT_REPORT_RECOVERY:
            out["recovered"] = True
            out["changed"] = True
        state.update(state=HEALTHY, incident_open=False, recovery_reported=True,
                     validation_failures=0, next_probe_after=0,
                     restart_requested_at=0, healthy_fingerprint=fp,
                     last_healthy_at=now)
    else:
        if observed == AUTH_FAILED and not state.get("incident_open"):
            state.update(incident_open=True, recovery_reported=False,
                         incident_opened_at=now, incident_fingerprint=fp)
            out["incident_opened"] = True
            out["changed"] = True
        state["state"] = observed

    if action == ACT_VALIDATE:
        status, username, user_id = validator(token)
        out["validation_status"] = status
        if status == 200:
            out["bot_username"] = username
            out["bot_id"] = user_id
            ok, detail = restarter(int(gw.get("pid") or 0))
            out["action"] = ACT_RESTART
            out["restart_ok"] = ok
            out["restart_detail"] = detail
            out["changed"] = True
            if ok:
                state.update(restart_requested_at=now, validation_failures=0,
                             next_probe_after=0, validated_fingerprint=fp)
        elif status in (401, 403):
            rejected = list(state.get("rejected_fingerprints") or [])
            if fp not in rejected:
                rejected.append(fp)
            # Keep the list short; only the current credential really matters.
            state["rejected_fingerprints"] = rejected[-10:]
            state["validation_failures"] = 0
            state["next_probe_after"] = 0
            out["changed"] = True
            out["reason"] = "new credential also rejected (%d); will not retry" % status
        else:
            failures = int(state.get("validation_failures") or 0) + 1
            wait = backoff_seconds(failures)
            state["validation_failures"] = failures
            state["next_probe_after"] = now + wait
            state["state"] = TEMPORARY_FAILURE
            out["backoff_seconds"] = wait
            out["changed"] = True
            out["reason"] = "validation transient (%s); retry in %ds" % (status or "network", wait)

    state["last_checked_at"] = now
    state["last_fingerprint"] = fp
    save_state(state, state_path(home))
    return out


def format_line(result: Dict[str, Any]) -> str:
    """One-line cron log entry. Contains a fingerprint, never a token."""
    bits = ["discord: %s" % result["observed"].lower()]
    if result.get("error_code"):
        bits.append(result["error_code"])
    bits.append("cred=%s" % result["fingerprint"])
    bits.append("action=%s" % result["action"])
    if result.get("validation_status") is not None:
        bits.append("identity=%s" % result["validation_status"])
    if result.get("bot_username"):
        bits.append("bot=%s" % result["bot_username"])
    if result.get("restart_detail"):
        bits.append(result["restart_detail"])
    if result.get("recovered"):
        bits.append("RECOVERED")
    if result.get("incident_opened"):
        bits.append("INCIDENT OPENED")
    bits.append("(%s)" % result["reason"])
    return " ".join(bits)


if __name__ == "__main__":
    print(format_line(tick()))
