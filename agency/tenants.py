"""Which MailHub tenant a given lead is sent through.

Each sender mailbox belongs to a different MailHub user, and a MailHub API key
only ever reaches its own owner's mailboxes. Sending from more than one mailbox
therefore means holding one credential per tenant and choosing between them --
there is no single key that spans them, by design.

Credentials are split across profiles, not gathered in one place:

    root profile      MAILHUB_TENANT_n_QUEUE_TOKEN     MAYA queues
    sentinel profile  MAILHUB_TENANT_n_APPROVE_TOKEN   SENTINEL approves
    leo profile       MAILHUB_TENANT_n_LEO_TOKEN       LEO reads and suppresses

so MAYA cannot approve its own copy even by accident: the key is not in its
environment. Every profile carries MAILHUB_TENANT_n_USER_ID and _NAME, which
are identifiers rather than secrets, so each process knows the full tenant list
while holding only its own credential for it.

That split is also why readiness cannot be answered from environment variables
alone -- no single process can see all three credentials. Each profile checks
the one it holds against MailHub and records the outcome in tenant_health;
`ready()` reads the combined picture. Only booleans are stored there.

Tenant choice for a *new* lead is a hash of the lead id over the ready set.
Once SENTINEL approves, the tenant is written to the message row and that
persisted value wins from then on: MailHub matches an approval on
(owner_user_id, content_hash), so a lead that drifted to another tenant
between approval and queueing would carry an approval nobody looks for.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from typing import Any, Dict, List, Optional

MAX_TENANTS = 32


def _env(name: str) -> str:
    return (os.getenv(name, "") or "").strip()


def _first(*names: str) -> str:
    for n in names:
        v = _env(n)
        if v:
            return v
    return ""


def load() -> List[Dict[str, Any]]:
    """Every configured tenant, in stable order.

    Read fresh rather than cached at import: the cron processes are short-lived
    and a key rotated on disk should take effect on the next tick.
    """
    out: List[Dict[str, Any]] = []
    for i in range(1, MAX_TENANTS + 1):
        uid = _env("MAILHUB_TENANT_%d_USER_ID" % i)
        queue = _first("MAILHUB_TENANT_%d_QUEUE_TOKEN" % i,
                       "MAILHUB_TENANT_%d_QUEUE" % i)
        approve = _first("MAILHUB_TENANT_%d_APPROVE_TOKEN" % i,
                         "MAILHUB_TENANT_%d_APPROVE" % i)
        leo = _first("MAILHUB_TENANT_%d_LEO_TOKEN" % i,
                     "MAILHUB_TENANT_%d_LEO" % i)
        if not uid and not (queue or approve or leo):
            continue
        out.append({
            "index": i,
            "name": _env("MAILHUB_TENANT_%d_NAME" % i) or "tenant%d" % i,
            "user_id": int(uid) if uid.isdigit() else None,
            "queue": queue or None,
            "approve": approve or None,
            "leo": leo or None,
        })
    if out:
        return out

    legacy = _env("MAILHUB_API_TOKEN")
    if legacy:
        # One tenant, one credential: the arrangement before rotation existed.
        # The same token fills every slot because in that deployment it is
        # whatever the profile was given -- MailHub answers 403 if it lacks
        # the scope, which is the honest result rather than a guess here.
        return [{"index": 1, "name": "legacy", "user_id": None,
                 "queue": legacy, "approve": legacy, "leo": legacy}]
    return []


def by_user_id(user_id: int) -> Optional[Dict[str, Any]]:
    for t in load():
        if t["user_id"] == user_id:
            return t
    return None


# --- readiness --------------------------------------------------------------

READY_COLUMNS = ("queue_ok", "approve_ok", "leo_ok", "mailbox_ok")


def health_rows(con: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    try:
        rows = con.execute("SELECT * FROM tenant_health").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["tenant_name"]: dict(r) for r in rows}


def ready(con: Optional[sqlite3.Connection] = None,
          pool: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Tenants that are complete enough to be given a new lead.

    Fails closed: an unchecked tenant is not ready. A tenant that is missing
    any one credential would strand leads -- unapprovable, unqueueable, or with
    replies nobody reads -- so it is left out of routing rather than allowed to
    half-work.
    """
    pool = load() if pool is None else pool
    if con is None:
        return pool
    health = health_rows(con)
    out = []
    for t in pool:
        h = health.get(t["name"])
        if h and all(h.get(c) for c in READY_COLUMNS):
            out.append(t)
    return out


def unavailable(con: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """Configured tenants that are NOT ready, with the reason, for reporting."""
    pool = load()
    health = health_rows(con) if con is not None else {}
    out = []
    for t in pool:
        h = health.get(t["name"])
        missing = ([c for c in READY_COLUMNS if not (h or {}).get(c)]
                   if h else ["never checked"])
        if missing:
            out.append({**{k: t[k] for k in ("index", "name", "user_id")},
                        "missing": missing})
    return out


# --- routing ----------------------------------------------------------------

def for_lead(lead_id: str, con: Optional[sqlite3.Connection] = None,
             pool: Optional[List[Dict[str, Any]]] = None
             ) -> Optional[Dict[str, Any]]:
    """The tenant a NEW lead should go to, or None if none are usable.

    Hashed rather than round-robin: a counter would need shared state and would
    hand the same lead to a different tenant after a restart. Once a lead has
    been approved its tenant is read from the message row instead -- see
    for_message -- so a change in the ready set never moves work that is
    already under way.
    """
    usable = ready(con, pool)
    if not usable:
        return None
    digest = hashlib.sha256(lead_id.encode("utf-8")).digest()
    return usable[int.from_bytes(digest[:8], "big") % len(usable)]


def for_message(persisted_user_id: Optional[int], lead_id: str,
                con: Optional[sqlite3.Connection] = None
                ) -> Dict[str, Any]:
    """Resolve the tenant for a message that may already have one.

    Returns {"tenant": ..., "status": ...} where status is one of:
        "persisted"  the tenant recorded at approval time, still configured
        "assigned"   no tenant recorded yet; one was chosen
        "changed"    a tenant was recorded but is no longer usable
        "none"       nothing usable is configured

    "changed" is a refusal, not a fallback. The approval already sitting in
    MailHub belongs to the old tenant, so sending through a new one would need
    a fresh approval -- silently switching would either stall the message or,
    if that tenant's QA gate were off, send it unreviewed.
    """
    pool = load()
    if persisted_user_id is not None:
        for t in pool:
            if t["user_id"] == persisted_user_id:
                if t in ready(con, pool):
                    return {"tenant": t, "status": "persisted"}
                return {"tenant": None, "status": "changed",
                        "was": persisted_user_id}
        return {"tenant": None, "status": "changed", "was": persisted_user_id}
    t = for_lead(lead_id, con, pool)
    if t is None:
        return {"tenant": None, "status": "none"}
    return {"tenant": t, "status": "assigned"}


def queue_token(lead_id: str, con: Optional[sqlite3.Connection] = None
                ) -> Optional[str]:
    t = for_lead(lead_id, con)
    return t["queue"] if t else None


def approve_token(lead_id: str, con: Optional[sqlite3.Connection] = None
                  ) -> Optional[str]:
    """The approve credential for this lead's tenant, or None.

    None is a refusal, not a fallback. Approving through another tenant's key
    would file the approval where MailHub will not match it.
    """
    t = for_lead(lead_id, con)
    return t["approve"] if t else None


def describe(con: Optional[sqlite3.Connection] = None) -> str:
    """A one-line summary for logs. Never includes key material."""
    pool = load()
    if not pool:
        return "mailhub tenants: none configured"
    usable = {t["name"] for t in ready(con, pool)} if con is not None else set()
    parts = []
    for t in pool:
        held = "".join(c for c, k in (("q", "queue"), ("a", "approve"), ("l", "leo"))
                       if t[k])
        state = "ready" if t["name"] in usable else "not-ready"
        parts.append("%s(user=%s,holds=%s,%s)"
                     % (t["name"], t["user_id"], held or "-", state))
    return "mailhub tenants: %d -> %s" % (len(pool), " ".join(parts))
