"""Deterministic email verification gate in front of lead admission.

No model decides whether an address is deliverable. The verifier returns a
verdict and this module maps it to an admission decision through a fixed table;
that is the whole of the policy.

Four verdicts, four different meanings, and the differences matter:

    valid    the only status that may enter outreach automatically
    invalid  will never deliver -- reject the lead, but do NOT suppress the
             address in MailHub. Failing verification and asking not to be
             contacted are different facts about a person, and conflating them
             would write a permanent do-not-contact record on the strength of
             a DNS lookup.
    risky    might deliver, might be a disposable or role mailbox. Held for a
             human, never sent automatically in V1.
    unknown  the verifier could not tell -- usually a transient DNS failure.
             This is NOT invalid. An unknown address keeps its lead and is
             retried on a backoff, because discarding a real prospect because
             a nameserver was briefly unreachable is the expensive mistake.

Nothing here rewrites a prospect's address. A `did_you_mean` suggestion is
stored as evidence for a human; silently "correcting" john@gmial.com to
john@gmail.com would be inventing a contact detail and mailing a stranger.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASE = (os.getenv("EMAIL_VERIFIER_URL", "") or "").rstrip("/")
API_KEY = os.getenv("EMAIL_VERIFIER_API_KEY", "") or ""
TIMEOUT = int(os.getenv("EMAIL_VERIFIER_TIMEOUT_SECONDS", "30"))
MAX_RETRIES = int(os.getenv("EMAIL_VERIFIER_MAX_RETRIES", "5"))

API_BATCH_MAX = 100
_configured_batch = int(os.getenv("EMAIL_VERIFIER_BATCH_SIZE", "40"))
BATCH_SIZE = max(1, min(_configured_batch, API_BATCH_MAX))

# Cloudflare rejects urllib's default "Python-urllib/3.x" with a 403 before the
# Worker sees the request. Every call must name itself or the whole gate fails
# closed and no lead is ever admitted -- which is safe, but silently wrong.
USER_AGENT = os.getenv("EMAIL_VERIFIER_USER_AGENT", "hermes-agency-verifier/1.0")

STATUSES = ("valid", "invalid", "risky", "unknown")

# Admission decision per verifier status. Anything unrecognised is treated as
# unknown: a verdict we do not understand is not permission to send.
ADMISSION = {
    "valid": "eligible",
    "invalid": "reject",
    "risky": "hold",
    "unknown": "retry",
}

# Backoff between verification attempts for an unknown result, in minutes.
# After the last one the lead is held for a human rather than being called
# invalid -- the verifier never established that it was undeliverable.
RETRY_BACKOFF_MINUTES = [15, 60, 6 * 60, 24 * 60, 72 * 60]

# Matches header style (`x-api-key: abc`) and the quoted JSON an error body
# actually arrives as (`"x-api-key": "abc"`). The quotes are what let a key
# through the first version of this, which is the only reason it is this fussy.
_SECRET_NAMES = r"x-api-key|authorization|api[_-]?key|token"
# The optional scheme group matters: "authorization": "Bearer abc.def" has a
# space in the value, so a pattern that stopped at whitespace redacted the word
# "Bearer" and left the token itself in the string.
_SECRET_RE = re.compile(
    r"(?i)([\"\']?)(" + _SECRET_NAMES + r")\1"
    r"\s*[:=]\s*[\"\']?(?:(?:bearer|token|basic)\s+)?"
    r"([^\"\'\s,}\)]+)[\"\']?")


def scrub(text: str) -> str:
    """Remove anything that looks like a credential from a message.

    Applied to every error before it is stored or logged: a 401 body or a
    urllib exception can quote the request headers back, and an outbox row or
    a Discord report is not where an API key should end up.
    """
    if not text:
        return ""
    out = _SECRET_RE.sub(
        lambda m: "%s%s%s: [redacted]" % (m.group(1), m.group(2), m.group(1)),
        str(text))
    # Belt and braces for a bare "Bearer <token>" with no field name in front.
    out = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}",
                 r"\1 [redacted]", out)
    if API_KEY:
        out = out.replace(API_KEY, "[redacted]")
    return out[:500]


def normalise(email: Optional[str]) -> str:
    """The canonical form used for comparison and for the verified-email record.

    Lowercased and trimmed only. Deliberately not doing provider-specific
    tricks like stripping gmail dots or +tags: those change which mailbox is
    addressed, and the point of this value is to prove that the address we
    verified is the address we are about to mail.
    """
    return (email or "").strip().lower()


def configured() -> bool:
    return bool(BASE and API_KEY)


# --- transport --------------------------------------------------------------

def _call(path: str, body: Optional[Dict[str, Any]] = None,
          timeout: Optional[int] = None) -> Tuple[int, Any, int]:
    """One HTTP call. Returns (status, parsed body, elapsed ms). Never raises."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data is not None else "GET")
    req.add_header("x-api-key", API_KEY)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None), \
                int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "null")
        except Exception:
            payload = None
        return e.code, payload, int((time.time() - t0) * 1000)
    except Exception as exc:
        return 0, {"error": scrub("%s: %s" % (type(exc).__name__, exc))}, \
            int((time.time() - t0) * 1000)


def health() -> Dict[str, Any]:
    code, body, ms = _call("/health")
    return {"ok": code == 200 and bool((body or {}).get("ok")),
            "http": code, "took_ms": ms,
            "provider": (body or {}).get("provider")}


def verify_one(email: str) -> Dict[str, Any]:
    """Verify a single address. For debugging and spot checks, not the bulk path."""
    code, body, ms = _call("/verify", {"email": email})
    if code == 200 and isinstance(body, dict):
        return normalise_result(body, email, ms)
    return _transport_unknown(email, code, body, ms)


def verify_batch(emails: List[str]) -> List[Dict[str, Any]]:
    """Verify up to BATCH_SIZE addresses in one call.

    A batch that fails as a whole yields `unknown` for every address in it, not
    `invalid`. The service said nothing about these addresses; a network error
    is a fact about us, not about the prospect.

    An address missing from an otherwise successful response is `unknown` for
    the same reason -- the absence of a verdict is not a verdict.
    """
    emails = [e for e in emails if e]
    if not emails:
        return []
    if len(emails) > API_BATCH_MAX:
        raise ValueError("batch of %d exceeds the API maximum of %d"
                         % (len(emails), API_BATCH_MAX))

    code, body, ms = _call("/verify/batch", {"emails": emails})
    if code != 200 or not isinstance(body, dict):
        return [_transport_unknown(e, code, body, ms) for e in emails]

    results = body.get("results")
    if not isinstance(results, list):
        return [_transport_unknown(e, code, {"error": "no results array"}, ms)
                for e in emails]

    by_email = {}
    for r in results:
        if isinstance(r, dict) and r.get("email"):
            by_email[normalise(r["email"])] = r

    out = []
    for e in emails:
        r = by_email.get(normalise(e))
        if r is None:
            out.append(_transport_unknown(
                e, code, {"error": "address missing from batch response"}, ms))
        else:
            out.append(normalise_result(r, e, ms))
    return out


def _transport_unknown(email: str, code: int, body: Any,
                       ms: int) -> Dict[str, Any]:
    detail = ""
    if isinstance(body, dict):
        detail = body.get("error") or body.get("message") or ""
    return {
        "email": normalise(email),
        "status": "unknown",
        "deliverable": None,
        "score": None,
        "reason": "verifier_unavailable:http_%s" % code,
        "flags": [],
        "did_you_mean": None,
        "mx_host": None,
        "cached": False,
        "took_ms": ms,
        "error": scrub(detail) or ("http %s" % code),
    }


def normalise_result(raw: Dict[str, Any], requested: str,
                     ms: Optional[int] = None) -> Dict[str, Any]:
    """Coerce one verifier result into the shape the rest of the code relies on.

    An unrecognised status becomes `unknown` rather than being passed through.
    The admission table is exhaustive over four values by design, and a status
    this code has never seen must not fall into a branch that sends mail.
    """
    status = str(raw.get("status") or "").strip().lower()
    if status not in STATUSES:
        status = "unknown"
    flags = raw.get("flags")
    if not isinstance(flags, list):
        flags = []
    score = raw.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        score = None
    return {
        "email": normalise(raw.get("email") or requested),
        "status": status,
        "deliverable": raw.get("deliverable"),
        "score": score,
        "reason": (raw.get("reason") or "")[:200] or None,
        "flags": [str(f)[:60] for f in flags][:20],
        "did_you_mean": raw.get("did_you_mean") or None,
        "mx_host": raw.get("mx_host") or None,
        "cached": bool(raw.get("cached")),
        "took_ms": raw.get("took_ms") if isinstance(raw.get("took_ms"), int)
        else ms,
        "error": None,
    }


# --- policy -----------------------------------------------------------------

def admission(result: Dict[str, Any]) -> str:
    """eligible | reject | hold | retry — from status alone.

    Score deliberately does not participate. A high score on a `risky` address
    still means the verifier could not confirm the mailbox, and letting a
    number override the verdict is how a disposable address ends up in a send
    queue. If a score-based policy is wanted later, the evidence is all stored.
    """
    return ADMISSION.get(result.get("status"), "retry")


def next_retry_minutes(attempts: int) -> Optional[int]:
    """Minutes until the next attempt, or None once the ladder is exhausted."""
    if attempts < 1:
        attempts = 1
    if attempts > len(RETRY_BACKOFF_MINUTES):
        return None
    return RETRY_BACKOFF_MINUTES[attempts - 1]


def exhausted(attempts: int) -> bool:
    return attempts >= MAX_RETRIES or attempts >= len(RETRY_BACKOFF_MINUTES)


def batches(emails: Iterable[str],
            size: Optional[int] = None) -> Iterable[List[str]]:
    """Split into API-sized chunks, de-duplicated, order preserved.

    Duplicates within a tick would spend the same subrequest budget twice for
    an identical answer, and the Worker's cost is driven by unique domains.
    """
    size = max(1, min(size or BATCH_SIZE, API_BATCH_MAX))
    seen, chunk = set(), []
    for e in emails:
        n = normalise(e)
        if not n or n in seen:
            continue
        seen.add(n)
        chunk.append(n)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def verification_record(result: Dict[str, Any], attempts: int,
                        now_iso: str) -> Dict[str, Any]:
    """The evidence block persisted alongside the lead.

    `verified_email` is the point of this record. It pins the verdict to the
    exact address that was checked, so that changing a lead's email later
    cannot inherit the old address's clearance.
    """
    decision = admission(result)
    return {
        "status": result["status"],
        "decision": decision,
        "deliverable": result.get("deliverable"),
        "score": result.get("score"),
        "reason": result.get("reason"),
        "flags": result.get("flags") or [],
        "did_you_mean": result.get("did_you_mean"),
        "mx_host": result.get("mx_host"),
        # How far the check actually looked, and what it found at the mailbox.
        # A verifier that only resolves MX reports the domain level, and the
        # role-account policy refuses to release a guessed local part on that
        # alone -- an answering domain says nothing about whether `info@`
        # exists behind it. Absent means domain-level, which is what every
        # record written before 2026-09-04 is.
        "verification_level": result.get("verification_level") or "domain",
        "mailbox_status": result.get("mailbox_status"),
        "cached": result.get("cached"),
        "took_ms": result.get("took_ms"),
        "error": scrub(result.get("error") or "") or None,
        "verified_email": result["email"],
        "verified_at": now_iso if decision != "retry" else None,
        "last_attempt_at": now_iso,
        "attempts": attempts,
        "next_retry_at": None,          # filled in by the caller, which owns the clock
    }


def stale(record: Optional[Dict[str, Any]], current_email: str) -> bool:
    """True when a stored verdict may no longer be used for this address.

    The address is normalised on both sides before comparison, so a change of
    case or surrounding whitespace does not invalidate a good verdict, while a
    genuinely different mailbox does.
    """
    if not record:
        return True
    return normalise(record.get("verified_email")) != normalise(current_email)


def is_final(record: Optional[Dict[str, Any]]) -> bool:
    """Whether this verdict settles the matter and need not be re-checked.

    valid, invalid and risky are final for the address they were recorded
    against. risky is held for a person -- a role account or a catch-all does
    not stop being one because it was asked again two minutes later, and
    asking again 150 times a tick was the whole verifier budget. Only unknown
    is revisited, on its backoff, because unknown means the verifier could not
    tell and a working nameserver may change that.
    """
    return bool(record) and record.get("status") in ("valid", "invalid", "risky")
