#!/usr/bin/env python3
"""A single switch that stops the agency queueing new mail, and starts it again.

Until now there was no global stop. Containing the 2026-09-04 bounce incident
meant pausing four mailboxes and holding fifty-seven leads by hand, one
statement at a time, while the pipeline kept running underneath. That is a bad
position to be in at the moment you most want everything to halt.

WHAT STOPPING DOES
    The orchestrator refuses to queue anything new. Leads stay exactly where
    they are -- a lead in READY_TO_SEND stays READY_TO_SEND -- so starting
    again resumes rather than restarts, at normal pacing, with no catch-up
    burst. Nothing is cancelled and no state is rewritten.

WHAT STOPPING DOES *NOT* DO
    It does not recall mail already handed to MailHub. MailHub owns its queue
    and will deliver what it has accepted; this is the same boundary the
    per-mailbox pause has always had. To stop mail already in flight you must
    disable the mailbox in MailHub itself.

    It also does not touch lead holds, sender pauses, campaign status or
    limits. Those are separate brakes on purpose: lifting this one does not
    lift those, and an operator who stops and starts outreach cannot
    accidentally release fifty-seven held role accounts.

FAILURE DIRECTION
    No state file means "never stopped", which is the normal running case. A
    file that exists but cannot be parsed means somebody stopped outreach and
    the record is damaged, so it reads as STOPPED. Guessing "running" there
    would resume sending on a corrupt file, which is the one outcome nobody
    would choose.

The state is re-read on every call. Caching it would reproduce exactly the bug
that left the Discord bot dead for fifteen hours: a value fetched once and
believed forever.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
from typing import Any, Dict, Optional

__all__ = ["state", "stopped", "stop", "start", "state_path", "describe"]

FILENAME = "outreach_brake.json"


def state_path(home: Optional[pathlib.Path] = None) -> pathlib.Path:
    base = home or pathlib.Path(os.getenv("HERMES_HOME", "/opt/data"))
    return base / "state" / FILENAME


def state(home: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Current brake state. Never raises."""
    p = state_path(home)
    if not p.exists():
        return {"stopped": False, "reason": "", "by": "", "at": ""}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        # Fail closed: the file exists, so somebody stopped outreach.
        return {"stopped": True, "reason": "brake state unreadable (%s) — "
                "treating as STOPPED; fix or remove %s" % (type(exc).__name__, p),
                "by": "", "at": "", "corrupt": True}
    return {"stopped": bool(raw.get("stopped")),
            "reason": str(raw.get("reason") or ""),
            "by": str(raw.get("by") or ""),
            "at": str(raw.get("at") or "")}


def stopped(home: Optional[pathlib.Path] = None) -> bool:
    return bool(state(home)["stopped"])


def _write(data: Dict[str, Any], home: Optional[pathlib.Path]) -> None:
    p = state_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def stop(reason: str = "", by: str = "operator",
         home: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Halt new queueing. Idempotent."""
    cur = state(home)
    if cur["stopped"] and not cur.get("corrupt"):
        return dict(cur, changed=False)
    data = {"stopped": True, "reason": (reason or "stopped by %s" % by)[:200],
            "by": by, "at": _now()}
    _write(data, home)
    return dict(data, changed=True)


def start(by: str = "operator", home: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Resume queueing. Idempotent. Leaves every other brake alone."""
    cur = state(home)
    if not cur["stopped"]:
        return dict(cur, changed=False)
    data = {"stopped": False, "reason": "", "by": by, "at": _now()}
    _write(data, home)
    return dict(data, changed=True)


def describe(home: Optional[pathlib.Path] = None) -> str:
    s = state(home)
    if not s["stopped"]:
        return "outreach: RUNNING"
    who = (" by %s" % s["by"]) if s["by"] else ""
    when = (" at %s" % s["at"]) if s["at"] else ""
    return "outreach: STOPPED%s%s — %s" % (who, when, s["reason"] or "no reason recorded")


if __name__ == "__main__":
    print(describe())
