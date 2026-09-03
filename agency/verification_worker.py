"""Drive email verification over the Supabase lead table.

Reads leads that need a verdict, verifies them in batches, and writes the
result back. No model is involved at any point.

Where the evidence goes, and why it goes there: the leads table already has
`email_verification_status` (text) and `email_verified` (boolean), so those
carry the verdict. Everything else the verifier returns -- score, reason,
flags, did_you_mean, mx_host, attempt bookkeeping -- is written into the
existing `raw_data` jsonb under a single `email_verification` key. That avoids
inventing eleven columns in a table this code does not own, and keeps one
self-describing record per lead rather than a scatter of nullable fields.

The admission gate is enforced at claim time as well as here (see
`claimable_filter`), so a lead whose address changed after it was verified
cannot be claimed on the strength of the old verdict even before this worker
has noticed the change.
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional, Tuple

import email_verifier as EV
import role_account_policy as RAP
import supabase_sync as S

RAW_KEY = "email_verification"

# Lead statuses this worker refuses to touch. A lead that has been rejected,
# unsubscribed or archived does not become interesting again because its
# address might resolve.
SKIP_LEAD_STATUS = {"rejected", "unsubscribed", "do_not_contact", "archived",
                    "duplicate", "completed", "closed"}

# Verification statuses that still want work. `valid` and `invalid` are final
# for the address they were recorded against, so they are not re-fetched every
# two minutes -- the verifier's own cache is 7 days, but the cheapest call is
# the one never made.
NEEDS_WORK = {None, "", "pending", "unknown", "retry", "error"}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def record_of(lead: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = lead.get("raw_data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        return None
    rec = raw.get(RAW_KEY)
    return rec if isinstance(rec, dict) else None


def due_for_verification(lead: Dict[str, Any],
                         now: Optional[datetime.datetime] = None) -> bool:
    """Whether this lead should be sent to the verifier on this tick."""
    now = now or _now()
    email = EV.normalise(lead.get("email"))
    if not email or "@" not in email:
        # No address to check. Handled separately so it is rejected rather
        # than retried forever against a verifier that will always say the
        # same thing.
        return False
    if not lead.get("is_active", True):
        return False
    if (lead.get("status") or "").strip().lower() in SKIP_LEAD_STATUS:
        return False

    rec = record_of(lead)
    if EV.stale(rec, email):
        # Either never verified, or verified against a different address.
        return True
    if EV.is_final(rec):
        return False

    # unknown / risky: honour the backoff the last attempt asked for.
    nxt = rec.get("next_retry_at") if rec else None
    if not nxt:
        return True
    try:
        when = datetime.datetime.fromisoformat(str(nxt))
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return True
    return now >= when


CANDIDATE_COLS = ("id,email,status,is_active,hermes_status,"
                  "email_verification_status,email_verified,raw_data,updated_at")

# A verdict of valid, invalid or risky is final; only these two mean the
# verifier still has work to do on the address as it stands.
NEEDS_VERDICT = ("email_verification_status.is.null,"
                 "email_verification_status.eq.unknown")


def _candidates(extra: str, limit: int) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    q = ("leads?select=%s&is_active=eq.true&email=not.is.null&email=neq.%s"
         "&order=updated_at.asc&limit=%d" % (CANDIDATE_COLS, extra, limit))
    return [r for r in (S._call(q) or []) if isinstance(r, dict)]


def fetch_candidates(limit: int = 200) -> List[Dict[str, Any]]:
    """Leads that plausibly need verification, narrowed further in Python.

    Rows with no verdict yet, or a non-final one, are fetched FIRST and always.
    They are the only rows that can actually change state, and taking them
    first is what keeps the worker making progress.

    One generous scan ordered by updated_at used to be the whole selection.
    That starved: every already-final row still matched the filter, and once
    there were more of them than the scan limit they filled the window
    permanently. The verifier then read the same 200 finished rows every two
    minutes and verified nothing, while hundreds of leads that had never been
    checked sat just past the end of the window and never entered outreach.

    Whatever budget is left over still goes to the generous scan, because
    PostgREST cannot express "the stored verdict was for a different address"
    across two columns and that check has to see final rows to catch an email
    edited after verification.
    """
    rows = _candidates("&or=(%s)" % NEEDS_VERDICT, limit)
    seen = {r.get("id") for r in rows}
    for r in _candidates("", max(0, limit - len(rows))):
        if r.get("id") not in seen:
            seen.add(r.get("id"))
            rows.append(r)
    return rows


def _patch(lead_id: str, fields: Dict[str, Any]) -> None:
    S._call("leads?id=eq." + str(lead_id), "PATCH", fields,
            prefer="return=minimal")


def _merged_raw(lead: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    raw = lead.get("raw_data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw = dict(raw)
    raw[RAW_KEY] = record
    return raw


def apply_result(lead: Dict[str, Any], result: Dict[str, Any],
                 now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Turn one verifier result into the columns to write. Pure; no I/O.

    Returns {"fields": {...}, "decision": ..., "record": {...}} so the caller
    can decide whether to write and the tests can assert on the mapping without
    a network or a database.
    """
    now = now or _now()
    prev = record_of(lead) or {}
    prev_attempts = int(prev.get("attempts") or 0)
    # A verdict for a different address does not carry its attempt count with
    # it: the new address deserves a full retry ladder of its own.
    attempts = 1 if EV.stale(prev, result["email"]) else prev_attempts + 1

    record = EV.verification_record(result, attempts, _iso(now))

    # Role-account policy, applied on top of the verifier's verdict and only
    # where role_account is the whole of its objection. A published front-door
    # mailbox on a domain that passed every technical check is eligible for
    # B2B outreach; an internal function is held; an automated or
    # recruitment address is refused. Nothing else in the verdict is touched,
    # and any other finding still wins -- see role_account_policy.
    if RAP.sole_reason_is_role(record):
        verdict = RAP.evaluate(result["email"], record,
                               bounced=bool(lead.get("bounced_at")),
                               suppressed=bool(lead.get("suppressed")),
                               unsubscribed=(lead.get("status") == "unsubscribed"
                                             or bool(lead.get("unsubscribed_at"))))
        if verdict["status"] and verdict["status"] != record["status"]:
            record["policy"] = RAP.audit_entry(record, verdict, _iso(now))
            record["status"] = verdict["status"]
            record["reason"] = verdict["reason"]
            record["decision"] = EV.ADMISSION.get(verdict["status"], "hold")
        elif verdict["status"]:
            record["policy"] = RAP.audit_entry(record, verdict, _iso(now))
            record["reason"] = verdict["reason"]

    decision = record["decision"]

    fields: Dict[str, Any] = {
        "email_verification_status": record["status"],
        "email_verified": record["status"] == "valid",
    }

    if decision == "eligible":
        # Deliberately does NOT touch `status` or `hermes_status`: a valid
        # address is permission to proceed, not an instruction to re-open a
        # lead the pipeline has already moved on from.
        record["next_retry_at"] = None
    elif decision == "reject":
        fields["status"] = "rejected"
        record["next_retry_at"] = None
    elif decision == "hold":
        fields["status"] = "hold"
        record["next_retry_at"] = None
    else:  # retry
        mins = EV.next_retry_minutes(attempts)
        if mins is None or EV.exhausted(attempts):
            # Out of attempts. It stays unknown and goes to a human -- it is
            # NOT reclassified as invalid, because nothing ever established
            # that it was undeliverable.
            record["next_retry_at"] = None
            record["exhausted"] = True
            fields["status"] = "hold"
        else:
            record["next_retry_at"] = _iso(
                now + datetime.timedelta(minutes=mins))

    fields["raw_data"] = _merged_raw(lead, record)
    return {"fields": fields, "decision": decision, "record": record}


def reject_unusable(lead: Dict[str, Any],
                    now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """An address the verifier need never see: absent or structurally impossible."""
    now = now or _now()
    email = EV.normalise(lead.get("email"))
    # A blank address never reaches here (it is parked, not rejected); the
    # only structurally impossible non-blank address is one without an "@".
    reason = "missing_at_sign"
    record = {
        "status": "invalid", "decision": "reject", "deliverable": False,
        "score": 0, "reason": reason, "flags": [], "did_you_mean": None,
        "mx_host": None, "cached": False, "took_ms": 0, "error": None,
        "verified_email": email, "verified_at": _iso(now),
        "last_attempt_at": _iso(now), "attempts": 1, "next_retry_at": None,
        "local_check": True,
    }
    return {"fields": {"email_verification_status": "invalid",
                       "email_verified": False, "status": "rejected",
                       "raw_data": _merged_raw(lead, record)},
            "decision": "reject", "record": record}



# The leads table has CHECK constraints on both status columns and Hermes
# holds no DDL credential, so a dedicated 'no_email' value cannot be added.
# The claim RPC filters on status = 'ready', and 'hold' is an accepted value,
# so 'hold' is the lever: a lead with no address is held out of the claim and
# marked NO_EMAIL in its metadata, with the status it had recorded so it goes
# back to exactly that when an address appears. Risky-verdict leads also sit
# in 'hold'; the two populations are told apart by the address itself.
NO_EMAIL_HOLD_STATUS = "hold"
NO_EMAIL_KEY = "no_email"
NO_EMAIL_CLASS = "NO_EMAIL"


def _blank(email: Optional[str]) -> bool:
    return not (email or "").strip()


def _raw(lead: Dict[str, Any]) -> Dict[str, Any]:
    raw = lead.get("raw_data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return dict(raw) if isinstance(raw, dict) else {}


def route_no_email(now: Optional[datetime.datetime] = None,
                   limit: int = 500) -> Dict[str, Any]:
    """Keep leads without an address out of the email claim, structurally.

    The claim RPC filters on status = 'ready'. A lead with no address is
    held (status = 'hold') and classified NO_EMAIL in its metadata, so the
    RPC never hands it out and the claim window is spent on leads that can
    actually be admitted. It is NOT marked invalid -- a missing address and an
    undeliverable one are different facts, and only one of them is a verdict
    about the prospect -- and hermes_status is untouched.

    The move is reversible by the data itself: when an address appears, the
    lead goes straight back to not_imported with no verification recorded, so
    the verifier picks it up on its next pass and only a valid verdict admits
    it. first_seen_at is kept under raw_data.no_email so the evening report
    can tell a new gap from one it has already shown.
    """
    now = now or _now()
    out = {"parked": 0, "restored": 0, "errors": []}

    # Park: in the claim window (ready, not imported) but no address.
    try:
        rows = S._call("leads?select=id,email,status,raw_data&status=eq.ready"
                       "&hermes_status=eq.not_imported&is_active=eq.true"
                       "&or=(email.is.null,email.eq.)&limit=%d" % limit) or []
    except Exception as exc:
        out["errors"].append("fetch no-email: %s" % EV.scrub(str(exc)))
        rows = []
    for lead in rows:
        if not _blank(lead.get("email")):
            continue
        try:
            _patch(lead["id"], park_fields(lead, now))
            out["parked"] += 1
        except Exception as exc:
            out["errors"].append("park %s: %s" % (lead.get("id"), EV.scrub(str(exc))))

    # Restore: held as NO_EMAIL earlier, has an address now. Selected by the
    # hold status plus a present address, then confirmed by the marker so a
    # risky-verdict hold is never mistaken for one of these.
    try:
        rows = S._call("leads?select=id,email,status,raw_data&status=eq.%s"
                       "&email=not.is.null&email=neq.&limit=%d"
                       % (NO_EMAIL_HOLD_STATUS, limit)) or []
    except Exception as exc:
        out["errors"].append("fetch restorable: %s" % EV.scrub(str(exc)))
        rows = []
    for lead in rows:
        if _blank(lead.get("email")) or not is_no_email_hold(lead):
            continue
        try:
            _patch(lead["id"], restore_fields(lead))
            out["restored"] += 1
        except Exception as exc:
            out["errors"].append("restore %s: %s"
                                 % (lead.get("id"), EV.scrub(str(exc))))
    return out


def is_no_email_hold(lead: Dict[str, Any]) -> bool:
    """Whether this lead's hold is the NO_EMAIL kind (not a risky verdict)."""
    return (_raw(lead).get(NO_EMAIL_KEY) or {}).get("classification") == NO_EMAIL_CLASS


def park_fields(lead: Dict[str, Any], now: datetime.datetime) -> Dict[str, Any]:
    """The columns that hold a no-address lead out of the claim. Pure."""
    raw = _raw(lead)
    marker = dict(raw.get(NO_EMAIL_KEY) or {})
    marker.setdefault("first_seen_at", _iso(now))
    marker["classification"] = NO_EMAIL_CLASS
    marker.setdefault("prev_status", lead.get("status") or "ready")
    raw[NO_EMAIL_KEY] = marker
    return {"status": NO_EMAIL_HOLD_STATUS, "raw_data": raw}


def restore_fields(lead: Dict[str, Any]) -> Dict[str, Any]:
    """The columns that put a lead back once it has an address. Pure.

    Back to the status it had, with no verdict recorded: the verifier decides
    on its next pass, and only a valid verdict admits it.
    """
    raw = _raw(lead)
    marker = raw.pop(NO_EMAIL_KEY, None) or {}
    return {"status": marker.get("prev_status") or "ready",
            "email_verification_status": None, "email_verified": None,
            "raw_data": raw}


def tick(limit: int = 200, batch_size: Optional[int] = None,
         now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """One pass: select, verify, persist. Returns a summary for the cron log."""
    now = now or _now()
    out = {"considered": 0, "verified": 0, "eligible": 0, "reject": 0,
           "hold": 0, "retry": 0, "unusable": 0, "errors": [], "took_ms": 0}

    if not EV.configured():
        out["errors"].append("email verifier is not configured")
        return out
    if not S.configured():
        out["errors"].append("supabase is not configured")
        return out

    routed = route_no_email(now)
    out["no_email_parked"] = routed["parked"]
    out["no_email_restored"] = routed["restored"]
    out["errors"].extend(routed["errors"])

    try:
        rows = fetch_candidates(limit)
    except Exception as exc:
        out["errors"].append("supabase: %s" % EV.scrub(str(exc)))
        return out

    out["considered"] = len(rows)
    by_email: Dict[str, List[Dict[str, Any]]] = {}
    for lead in rows:
        email = EV.normalise(lead.get("email"))
        if not email:
            # Whitespace-only slips past the database filter. It is a missing
            # address, not an invalid one, so it is parked for manual contact
            # exactly like NULL -- never handed to the verifier, never marked
            # invalid, never counted as a rejection.
            try:
                _patch(lead["id"], park_fields(lead, now))
                out["no_email_parked"] = out.get("no_email_parked", 0) + 1
            except Exception as exc:
                out["errors"].append("park %s: %s"
                                     % (lead.get("id"), EV.scrub(str(exc))))
            continue
        if "@" not in email:
            try:
                res = reject_unusable(lead, now)
                _patch(lead["id"], res["fields"])
                out["unusable"] += 1
            except Exception as exc:
                out["errors"].append("patch %s: %s"
                                     % (lead.get("id"), EV.scrub(str(exc))))
            continue
        if not due_for_verification(lead, now):
            continue
        by_email.setdefault(email, []).append(lead)

    t0 = _now()
    for chunk in EV.batches(by_email.keys(), batch_size):
        try:
            results = EV.verify_batch(chunk)
        except Exception as exc:
            out["errors"].append("verify: %s" % EV.scrub(str(exc)))
            continue
        for result in results:
            for lead in by_email.get(result["email"], []):
                try:
                    applied = apply_result(lead, result, now)
                    _patch(lead["id"], applied["fields"])
                    out["verified"] += 1
                    key = {"eligible": "eligible", "reject": "reject",
                           "hold": "hold", "retry": "retry"}[applied["decision"]]
                    out[key] += 1
                except Exception as exc:
                    out["errors"].append("patch %s: %s"
                                         % (lead.get("id"), EV.scrub(str(exc))))
    out["took_ms"] = int((_now() - t0).total_seconds() * 1000)
    return out


# --- the admission gate -----------------------------------------------------

def claimable_filter() -> str:
    """The PostgREST predicate a lead must satisfy to reach Hermes.

    Kept here, next to the policy it enforces, rather than inline at the call
    site, so that "what may be claimed" has exactly one definition. The
    verified-address check that makes a changed email invalidate its old
    verdict cannot be expressed in PostgREST across two columns, so it is
    applied by `claim_guard` on the rows this returns.
    """
    return ("status=eq.ready&hermes_status=eq.not_imported"
            "&email_verification_status=eq.valid&email_verified=is.true")


def claim_guard(lead: Dict[str, Any]) -> Tuple[bool, str]:
    """Final structural check before a lead is handed to Hermes.

    Returns (allowed, reason). This is the check that survives an email being
    edited after verification: the verdict is only good for the address it was
    recorded against.
    """
    email = EV.normalise(lead.get("email"))
    if not email:
        return False, "NO_EMAIL: lead has no address; manual contact only"
    if "@" not in email:
        return False, "no usable email"
    if (lead.get("email_verification_status") or "").lower() != "valid":
        return False, ("verification is %s, not valid"
                       % (lead.get("email_verification_status") or "missing"))
    if not lead.get("email_verified"):
        return False, "email_verified is not true"
    rec = record_of(lead)
    if EV.stale(rec, email):
        return False, ("verified address %r no longer matches %r"
                       % ((rec or {}).get("verified_email"), email))
    return True, "verified valid"


def counts(now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Verification totals for ORBIT. Deterministic, from the table itself."""
    out = {"valid": 0, "invalid": 0, "risky": 0, "unknown": 0, "pending": 0,
           "error": None}
    if not S.configured():
        out["error"] = "supabase is not configured"
        return out
    try:
        for status in ("valid", "invalid", "risky", "unknown"):
            rows = S._call(
                "leads?select=id&is_active=eq.true"
                "&email_verification_status=eq." + status, prefer="count=exact")
            out[status] = len(rows or [])
        rows = S._call("leads?select=id&is_active=eq.true"
                       "&email_verification_status=is.null"
                       "&email=not.is.null&email=neq.")
        out["pending"] = len(rows or [])
        # Leads with no address at all are a separate population: they are
        # neither pending verification nor invalid, and they are reported to
        # a person for manual contact rather than counted against the funnel.
        rows = S._call("leads?select=id&is_active=eq.true"
                       "&or=(email.is.null,email.eq.)")
        out["no_email"] = len(rows or [])
    except Exception as exc:
        out["error"] = EV.scrub(str(exc))
    return out
