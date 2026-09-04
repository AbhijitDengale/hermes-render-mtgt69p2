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


# A profile's .env is the source of truth for tenant credentials, but a
# subprocess only receives the variables its config.yaml happens to name. That
# whitelist was written when there were five tenants and is a second place the
# tenant list has to be remembered; when four more were added it was not
# updated, so the process that assigns leads could not see them and every new
# lead went to the original five. The file is read here directly so that
# adding a tenant to the .env is sufficient, whatever any whitelist says.
_FILE_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "values": {}}


def _env_file_path() -> str:
    """This profile's own .env.

    HERMES_HOME when the process has it. An MCP subprocess does not: it
    receives only the variables its config.yaml names, and HERMES_HOME is not
    among them. AGENCY_ROLE is, and it identifies the profile -- which matters
    because each profile's file holds only its own kind of credential:
    SENTINEL the approve tokens, LEO the suppress tokens, root the queue
    tokens. Reading the wrong file would find the tenant and none of its keys.
    """
    home = (os.getenv("HERMES_HOME", "") or "").strip()
    if home:
        return os.path.join(home, ".env")
    role = (os.getenv("AGENCY_ROLE", "") or "").strip().lower()
    if role and role not in ("root", "maya"):
        return "/opt/data/profiles/%s/.env" % role
    return "/opt/data/.env"


def _env_file_values() -> Dict[str, str]:
    """Tenant variables from this profile's .env, re-read when it changes.

    Only MAILHUB_TENANT_* is taken. Nothing else in the file is imported, so
    this cannot quietly hand a profile a credential it was not given.
    """
    path = _env_file_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _FILE_CACHE["path"] == path and _FILE_CACHE["mtime"] == mtime:
        return _FILE_CACHE["values"]
    values: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("MAILHUB_TENANT_") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    except OSError:
        return _FILE_CACHE["values"]
    _FILE_CACHE.update(path=path, mtime=mtime, values=values)
    return values


def _env(name: str) -> str:
    """The process environment first, then the profile's own .env file.

    The environment wins so an operator can still override a single value
    without editing the file.
    """
    value = (os.getenv(name, "") or "").strip()
    if value:
        return value
    return (_env_file_values().get(name, "") or "").strip()


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


def paused(con: Optional[sqlite3.Connection] = None) -> Dict[int, Dict[str, Any]]:
    """Mailboxes standing down, by user id, with the reason and until when.

    A pause is deliberately not a credential change. Revoking OAuth or
    clearing an alias to stop a mailbox sending would take a working account
    apart to answer a temporary reputation problem, and putting it back is not
    the same account to Google. This is a row in our own table that expires.
    """
    if con is None:
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    try:
        rows = con.execute(
            "SELECT user_id, paused_until, paused_reason, paused_at"
            "  FROM tenant_health"
            " WHERE paused_until IS NOT NULL"
            "   AND paused_until > datetime('now')").fetchall()
    except sqlite3.Error:
        return out                       # migration not applied yet
    for r in rows:
        out[r["user_id"]] = dict(r)
    return out


def is_paused(con: Optional[sqlite3.Connection], user_id: Any) -> Optional[Dict[str, Any]]:
    try:
        return paused(con).get(int(user_id))
    except (TypeError, ValueError):
        return None


def ready(con: Optional[sqlite3.Connection] = None,
          pool: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Tenants that are complete enough to be given a new lead.

    Fails closed: an unchecked tenant is not ready. A tenant that is missing
    any one credential would strand leads -- unapprovable, unqueueable, or with
    replies nobody reads -- so it is left out of routing rather than allowed to
    half-work.

    A paused mailbox is excluded here too, so no NEW lead is allocated to it.
    What it already owns stays its own: pausing is about protecting an account
    whose reputation is in trouble, not about moving its conversations
    somewhere else.
    """
    pool = load() if pool is None else pool
    if con is None:
        return pool
    health = health_rows(con)
    off = paused(con)
    out = []
    for t in pool:
        h = health.get(t["name"])
        if h and all(h.get(c) for c in READY_COLUMNS) \
                and t.get("user_id") not in off:
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

def capacity(con: sqlite3.Connection) -> Dict[int, Dict[str, Any]]:
    """Per tenant: what it has sent and what it has left, from tenant_health.

    tenant_health is what each profile recorded from MailHub, which is the
    only place the real counters live. A tenant with no health row has no
    known capacity and is treated as having none.
    """
    out: Dict[int, Dict[str, Any]] = {}
    try:
        rows = con.execute(
            "SELECT user_id, daily_limit, sent_today, health,"
            "       sender_identity_status FROM tenant_health").fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        d = dict(r)
        limit = d.get("daily_limit") or 0
        sent = d.get("sent_today") or 0
        d["remaining"] = max(0, limit - sent)
        out[d["user_id"]] = d
    return out


def _assignment_load(con: sqlite3.Connection) -> Dict[int, Dict[str, Any]]:
    """How much work each tenant has been given, and when it was last given.

    Counted from the messages this agency assigned rather than from what
    MailHub has sent, because this is about sharing out NEW work: a message
    assigned a minute ago has not been sent yet but has already been promised.
    """
    out: Dict[int, Dict[str, Any]] = {}
    try:
        rows = con.execute(
            "SELECT tenant_user_id AS uid,"
            "       COUNT(*) AS total,"
            "       SUM(CASE WHEN COALESCE(tenant_assigned_at, updated_at) >="
            "                strftime('%Y-%m-%d %H:00:00','now')"
            "           THEN 1 ELSE 0 END) AS this_hour,"
            "       MAX(COALESCE(tenant_assigned_at, updated_at)) AS last_at"
            "  FROM messages WHERE tenant_user_id IS NOT NULL"
            " GROUP BY tenant_user_id").fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        out[r["uid"]] = {"total": r["total"] or 0,
                         "this_hour": r["this_hour"] or 0,
                         "last_at": r["last_at"] or ""}
    return out


def allocate(lead_id: str, con: Optional[sqlite3.Connection] = None,
             pool: Optional[List[Dict[str, Any]]] = None
             ) -> Optional[Dict[str, Any]]:
    """The tenant a NEW lead should go to: the least-used one that can send.

    Least-used rather than hashed. A hash spreads work evenly across whatever
    set it is given, which is fine until the set changes -- and when four
    tenants were added, every lead already carried an assignment made from the
    old set, so the newcomers stayed at zero and nothing corrected it. Sharing
    by current load is self-correcting: a tenant that is behind is chosen until
    it catches up, which is exactly what is wanted after one joins.

    Order: fewest assigned this hour, then fewest sent today, then longest
    since it was last given work, then tenant id. The last key makes the
    result deterministic, so the same state always produces the same choice
    and a test can assert on it.

    Capacity is part of the filter, not the score: a tenant with nothing left
    today cannot be chosen at all, however far behind it is.
    """
    usable = ready(con, pool)
    if not usable:
        return None
    if con is None:
        # No database to reason about load with. Fall back to the hash, which
        # at least spreads deterministically.
        digest = hashlib.sha256(lead_id.encode("utf-8")).digest()
        return usable[int.from_bytes(digest[:8], "big") % len(usable)]

    cap = capacity(con)
    load = _assignment_load(con)
    affordable = [t for t in usable
                  if cap.get(t["user_id"], {}).get("remaining", 0) > 0]
    # If every tenant is out of capacity for today, fall back to the full
    # ready set rather than refusing to route: the send path enforces the
    # limit itself, and refusing here would strand the lead instead.
    candidates = affordable or usable

    # The last key is the lead's own hash rather than the tenant id. With the
    # load keys level -- which is every tenant's state before any work is
    # assigned -- an id tie-break would hand every lead to the same tenant
    # until something was recorded, so a caller that asked without persisting
    # would pile the lot onto one sender. Hashing spreads those ties the way
    # the previous allocator did, while staying deterministic for a given
    # lead: the same lead in the same state always gets the same tenant.
    def spread(uid) -> bytes:
        """Rendezvous hash: one digest per (lead, tenant) pair.

        Arithmetic on a single per-lead number does not work here. `(digest +
        uid) % n` looked like a permutation but collides whenever two tenant
        ids are congruent mod n -- with seven candidates, ids 9 and 2 share a
        slot and the lower id always won, so two tenants could never take a
        tie at all. Hashing the pair gives every tenant an independent draw.
        """
        return hashlib.sha256(("%s:%s" % (lead_id, uid)).encode("utf-8")).digest()

    def key(t):
        uid = t["user_id"]
        l = load.get(uid, {})
        c = cap.get(uid, {})
        return (l.get("this_hour", 0), c.get("sent_today", 0) or 0,
                l.get("last_at", ""), spread(uid), uid or 0)

    return sorted(candidates, key=key)[0]


def for_lead(lead_id: str, con: Optional[sqlite3.Connection] = None,
             pool: Optional[List[Dict[str, Any]]] = None
             ) -> Optional[Dict[str, Any]]:
    """The tenant a NEW lead should go to, or None if none are usable.

    Once a lead has been approved its tenant is read from the message row
    instead -- see for_message -- so a change in the ready set never moves
    work that is already under way.
    """
    return allocate(lead_id, con, pool)


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
