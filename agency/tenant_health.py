"""Check the MailHub credentials this profile holds, and record the outcome.

Readiness is behavioural, not declarative. A token present in the environment
proves nothing -- it may be revoked, belong to the wrong tenant, or carry the
wrong scopes -- so each credential is exercised against the live API and the
result is what gets stored.

The probes send empty bodies on purpose. Every scope check in MailHub runs
before the body is read, so 403 means the capability was refused and 400 means
it was allowed and the request was merely malformed. Nothing can be queued,
approved or suppressed by any check in this module.

Only booleans and public mailbox facts are written to agency.db. No token is
stored, logged or returned.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import tenants

BASE = (os.getenv("MAILHUB_BASE_URL", "") or "").rstrip("/")
TIMEOUT = int(os.getenv("MAILHUB_HEALTH_TIMEOUT", "30"))


def _call(token: str, method: str, path: str,
          body: Optional[Dict[str, Any]] = None) -> Tuple[int, str]:
    """Status code and a short body. Never raises, never logs the token."""
    if not BASE or not token:
        return 0, "not configured"
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode()[:400]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except Exception as exc:
        return 0, "%s: %s" % (type(exc).__name__, exc)


# A capability is "held" on 2xx or on 400/422 -- the scope gate passed and only
# the empty body was rejected. Anything else, 403 included, means not held.
def _allowed(code: int) -> bool:
    return code in (200, 201, 400, 422)


def probe(token: str) -> Dict[str, bool]:
    """What this one credential can actually do, according to MailHub."""
    return {
        "read": _call(token, "GET", "/api/v1/accounts")[0] == 200,
        "queue": _allowed(_call(token, "POST", "/api/v1/messages", {})[0]),
        "approve": _allowed(_call(token, "POST", "/api/v1/approvals", {})[0]),
        "suppress": _allowed(_call(token, "POST", "/api/v1/suppression", {})[0]),
    }


def accounts(token: str) -> List[Dict[str, Any]]:
    code, body = _call(token, "GET", "/api/v1/accounts")
    if code != 200:
        return []
    try:
        return json.loads(body or "{}").get("accounts", [])
    except Exception:
        return []


def _touch(con: sqlite3.Connection, t: Dict[str, Any]) -> None:
    con.execute(
        "INSERT INTO tenant_health (tenant_name, user_id) VALUES (?,?) "
        "ON CONFLICT(tenant_name) DO UPDATE SET user_id=excluded.user_id",
        (t["name"], t["user_id"]))


def check_queue(con: sqlite3.Connection, t: Dict[str, Any]) -> Dict[str, Any]:
    """The sender credential: must read and queue, and must NOT approve.

    The negative half matters as much as the positive one -- a key that can
    both queue and approve would let the sending path sign off on its own
    copy, which is the whole thing the split exists to prevent.
    """
    _touch(con, t)
    caps = probe(t["queue"]) if t.get("queue") else {}
    ok = bool(caps.get("read") and caps.get("queue") and not caps.get("approve"))
    acct = [a for a in (accounts(t["queue"]) if t.get("queue") else [])
            if a.get("enabled")]
    mailbox_ok = bool(acct) and all(
        a.get("health") in ("healthy", "warming")
        and int(a.get("effective_daily_limit") or 0) > 0 for a in acct)
    first = acct[0] if acct else {}
    con.execute(
        "UPDATE tenant_health SET queue_ok=?, queue_checked_at=datetime('now'),"
        "  mailbox_ok=?, mailbox_email=?, daily_limit=?, sent_today=?,"
        "  health=?, mailbox_checked_at=datetime('now')"
        " WHERE tenant_name=?",
        (1 if ok else 0, 1 if mailbox_ok else 0, first.get("email"),
         first.get("effective_daily_limit"), first.get("sent_today"),
         first.get("health"), t["name"]))
    return {"tenant": t["name"], "queue_ok": ok, "mailbox_ok": mailbox_ok,
            "caps": caps, "mailboxes": len(acct)}


def check_approve(con: sqlite3.Connection, t: Dict[str, Any]) -> Dict[str, Any]:
    """The SENTINEL credential: must approve, and must NOT queue or suppress."""
    _touch(con, t)
    caps = probe(t["approve"]) if t.get("approve") else {}
    ok = bool(caps.get("read") and caps.get("approve")
              and not caps.get("queue") and not caps.get("suppress"))
    con.execute("UPDATE tenant_health SET approve_ok=?,"
                " approve_checked_at=datetime('now') WHERE tenant_name=?",
                (1 if ok else 0, t["name"]))
    return {"tenant": t["name"], "approve_ok": ok, "caps": caps}


def check_leo(con: sqlite3.Connection, t: Dict[str, Any]) -> Dict[str, Any]:
    """The LEO credential: must read and suppress, and must NOT queue or approve."""
    _touch(con, t)
    caps = probe(t["leo"]) if t.get("leo") else {}
    ok = bool(caps.get("read") and caps.get("suppress")
              and not caps.get("queue") and not caps.get("approve"))
    con.execute("UPDATE tenant_health SET leo_ok=?,"
                " leo_checked_at=datetime('now') WHERE tenant_name=?",
                (1 if ok else 0, t["name"]))
    return {"tenant": t["name"], "leo_ok": ok, "caps": caps}


def check_all(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Check whichever credentials this profile happens to hold.

    A profile normally holds exactly one kind, so this records one column per
    tenant and leaves the others as whatever the profile that owns them last
    wrote. Running it everywhere eventually fills the table in.
    """
    out = []
    for t in tenants.load():
        r: Dict[str, Any] = {"tenant": t["name"], "user_id": t["user_id"]}
        if t.get("queue"):
            r.update(check_queue(con, t))
        if t.get("approve"):
            r.update(check_approve(con, t))
        if t.get("leo"):
            r.update(check_leo(con, t))
        out.append(r)
    return out
