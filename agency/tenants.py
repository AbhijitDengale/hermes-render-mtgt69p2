"""Which MailHub tenant a given lead is sent through.

Each sender mailbox belongs to a different MailHub user, and a MailHub API key
only ever reaches its own owner's mailboxes. Sending from more than one mailbox
therefore means holding one credential per tenant and choosing between them --
there is no single key that spans them, by design.

The choice is a pure function of the lead id. SENTINEL records the approval
minutes before MAYA queues the message, in a different process, and the two
must land on the same tenant: MailHub matches an approval on (owner_user_id,
content_hash), so an approval filed under one tenant does not release a message
queued under another. Deriving the tenant from the lead id lets both processes
arrive at the same answer without sharing any state, and makes a retry pick the
tenant it picked before.

This module chooses a tenant. It never chooses a mailbox -- MailHub's own
claim_account does that, applying the hourly limit, send gap, cooldown, health
and suppression rules that a caller must not be able to reach around.

Configuration (Render environment, never in code):

    MAILHUB_TENANT_1_NAME     human label, for logs only
    MAILHUB_TENANT_1_QUEUE    key with read+queue+suppress for that tenant
    MAILHUB_TENANT_1_APPROVE  key with approve for that same tenant
    ... _2_, _3_, and so on.

With none of those set the module reports a single legacy tenant built from
MAILHUB_API_TOKEN, which is exactly today's behaviour.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional

MAX_TENANTS = 32


def _clean(name: str) -> str:
    return (os.getenv(name, "") or "").strip()


def load() -> List[Dict[str, Any]]:
    """Every configured tenant, in stable order.

    Read fresh rather than cached at import: the cron processes are short-lived
    and a key rotated in Render should take effect on the next tick.
    """
    out: List[Dict[str, Any]] = []
    for i in range(1, MAX_TENANTS + 1):
        queue = _clean("MAILHUB_TENANT_%d_QUEUE" % i)
        if not queue:
            continue
        out.append({
            "name": _clean("MAILHUB_TENANT_%d_NAME" % i) or "tenant%d" % i,
            "queue": queue,
            "approve": _clean("MAILHUB_TENANT_%d_APPROVE" % i) or None,
        })
    if out:
        return out

    legacy = _clean("MAILHUB_API_TOKEN")
    if legacy:
        # One tenant, one credential: the arrangement before rotation existed.
        # It carries approve only if that key actually has the scope, which is
        # why the approve slot is the same token rather than None -- MailHub
        # rejects it with 403 if it does not, and that is the honest answer.
        return [{"name": "legacy", "queue": legacy, "approve": legacy}]
    return []


def for_lead(lead_id: str, pool: Optional[List[Dict[str, Any]]] = None
             ) -> Optional[Dict[str, Any]]:
    """The tenant this lead sends through, or None if none are configured.

    Hashed rather than round-robin: a counter would need shared state and would
    hand the same lead to a different tenant after a restart, stranding an
    approval that was already filed under the first one.
    """
    pool = load() if pool is None else pool
    if not pool:
        return None
    digest = hashlib.sha256(lead_id.encode("utf-8")).digest()
    return pool[int.from_bytes(digest[:8], "big") % len(pool)]


def queue_token(lead_id: str) -> Optional[str]:
    t = for_lead(lead_id)
    return t["queue"] if t else None


def approve_token(lead_id: str) -> Optional[str]:
    """The approve credential for this lead's tenant, or None.

    None is a refusal, not a fallback. Approving through a different tenant's
    key would file the approval where MailHub will not look for it, and the
    message would then either stall unqueueable or -- if that tenant's QA gate
    were ever switched off -- go out unreviewed.
    """
    t = for_lead(lead_id)
    return t.get("approve") if t else None


def describe() -> str:
    """A one-line summary for startup logs. Never includes key material."""
    pool = load()
    if not pool:
        return "mailhub tenants: none configured"
    parts = []
    for t in pool:
        parts.append("%s(queue=yes,approve=%s)"
                     % (t["name"], "yes" if t["approve"] else "NO"))
    return "mailhub tenants: %d -> %s" % (len(pool), " ".join(parts))
