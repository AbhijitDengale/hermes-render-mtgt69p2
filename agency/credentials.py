#!/usr/bin/env python3
"""One credential resolution shared by the gateway path and the cron scripts.

The Discord outage of 2026-09-04 was not caused by a bad token. It was caused
by two different pieces of code disagreeing about *when* the token is read:

  * the gateway reads ``DISCORD_BOT_TOKEN`` once, at process start, and keeps
    it for the life of the process;
  * every cron wrapper hand-rolled its own ``for line in open("/opt/data/.env")``
    loop and therefore re-read it on each run.

So rotating the token fixed the cron-driven review cards immediately and left
the interactive bot dead for fifteen hours, with nothing in either code path
able to notice the discrepancy. Two implementations of "get the token" is the
bug; this module is the single one they both go through.

Resolution order, deliberately matching what Hermes itself does:

  1. Hermes' own ``agent.secret_scope.get_secret`` when it is importable. This
     is the *same* call the gateway and every agent turn make, so it inherits
     the per-profile isolation that multiplexing installs — a cron job running
     under the echo profile sees echo's ``.env``, not the root one.
  2. ``os.environ`` — deployment-level values (Render env vars, ``docker run
     -e``) that were never written to a ``.env`` file.
  3. ``HERMES_HOME/.env`` parsed directly. The fallback for a bare script run
     outside the Hermes runtime, and the reason a cron wrapper no longer needs
     its own parser.

Nothing here logs, prints, or returns a credential by accident: the only
value-shaped thing this module is designed to emit is a
:func:`fingerprint`, which is a truncated SHA-256 and cannot be reversed.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
from typing import Dict, Optional

__all__ = ["resolve", "fingerprint", "present", "load_env_file", "hermes_home"]

_FINGERPRINT_CHARS = 12


def hermes_home() -> pathlib.Path:
    """The active HERMES_HOME, which is what selects the profile."""
    return pathlib.Path(os.getenv("HERMES_HOME", "/opt/data"))


def load_env_file(path: pathlib.Path) -> Dict[str, str]:
    """Parse a ``.env`` the way Hermes does: ``KEY=value``, ``#`` comments.

    Quotes are stripped because the dashboard writes some values quoted and
    some bare, and a token with a stray ``"`` on the end fails authentication
    in a way that looks exactly like a revoked token.
    """
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def resolve(name: str, default: str = "", *,
            home: Optional[pathlib.Path] = None) -> str:
    """Return the current value of credential ``name``.

    Always re-reads. Callers that cache the result reintroduce exactly the
    staleness this module exists to remove.
    """
    # 1. Hermes' own scope — the authority whenever we are inside the runtime.
    try:
        from agent.secret_scope import get_secret  # type: ignore
    except Exception:
        get_secret = None  # not running under the Hermes venv
    if get_secret is not None:
        try:
            scoped = get_secret(name, "")
        except Exception:
            # get_secret raises UnscopedSecretError under multiplexing when no
            # profile scope is installed. That is a real fail-closed signal for
            # an agent turn, but a cron wrapper legitimately has no scope, so
            # fall through to the file rather than crashing the job.
            scoped = ""
        if scoped:
            return str(scoped).strip()

    # 2. Process environment.
    from_env = os.environ.get(name, "")
    if from_env:
        return from_env.strip()

    # 3. The profile's own .env on disk.
    return load_env_file((home or hermes_home()) / ".env").get(name, default).strip()


def fingerprint(value: str) -> str:
    """A stable, non-reversible identity for a credential.

    This is what gets logged and persisted so that "did the token change?"
    is answerable without a secret ever reaching a log line, a state file, or
    a Discord card. An empty credential fingerprints as ``"absent"`` rather
    than as the SHA-256 of the empty string, so a missing token can never be
    mistaken for a present one that happens to hash consistently.
    """
    if not value:
        return "absent"
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:_FINGERPRINT_CHARS]


def present(name: str, *, home: Optional[pathlib.Path] = None) -> bool:
    """Whether a credential resolves to anything at all. Never returns it."""
    return bool(resolve(name, "", home=home))


def export(names, *, home: Optional[pathlib.Path] = None) -> int:
    """Copy selected keys from a profile's ``.env`` into ``os.environ``.

    ``setdefault`` semantics: anything already in the environment wins, so a
    deployment-level override is never clobbered by a file. Returns how many
    names were newly set — a count, never the values.

    Only the names asked for are loaded. Importing a whole ``.env`` would pull
    unrelated secrets into the process, and in ORBIT's case would shadow
    MAYA's queue-capable MailHub token with ORBIT's read-only one.
    """
    values = load_env_file((home or hermes_home()) / ".env")
    added = 0
    for name in names:
        value = values.get(name, "")
        if value and name not in os.environ:
            os.environ[name] = value
            added += 1
    return added
