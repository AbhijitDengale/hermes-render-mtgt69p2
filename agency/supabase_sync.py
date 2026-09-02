#!/usr/bin/env python3
"""Supabase <-> Hermes. Deterministic, no LLM anywhere in it.

Supabase is two things and neither of them is an authority:

    an external lead source   leads arrive, exactly once
    a reporting mirror        operational outcomes are written back

agency.db and MailHub remain the sources of truth. Nothing here ever reads a
Supabase status back into Hermes state — the arrow only points outward. A
reconcile repairs the mirror from Hermes, never Hermes from the mirror.

Write-back goes through an outbox. The reason is simple: a Gmail message that
really left must not be un-sent because a mirror was briefly unreachable. The
state machine commits, then the mirror catches up. If Supabase is down for an
hour, Hermes keeps working and the outbox drains afterwards.

    python3 supabase_sync.py claim          # pull ready leads into agency.db
    python3 supabase_sync.py drain          # deliver pending write-backs
    python3 supabase_sync.py tick           # claim + drain (what the cron runs)
    python3 supabase_sync.py reconcile      # repair the mirror from agency.db
    python3 supabase_sync.py status         # counts, no changes
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lead_ingest as li  # noqa: E402
import pipeline as P      # noqa: E402

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET = os.getenv("SUPABASE_SECRET_KEY", "")
CLAIM_RPC = (os.getenv("SUPABASE_CLAIM_RPC", "") or "claim_leads_for_hermes").strip()
BATCH_SIZE = int(os.getenv("SUPABASE_LEAD_BATCH_SIZE", "20"))
DAILY_TARGET = int(os.getenv("AGENCY_DAILY_LEAD_TARGET", "400"))
TZ_OFFSET_MINUTES = int(os.getenv("AGENCY_TZ_OFFSET_MINUTES", "330"))   # Asia/Kolkata
TZ_NAME = os.getenv("AGENCY_TZ_NAME", "Asia/Kolkata")
HTTP_TIMEOUT = int(os.getenv("SUPABASE_HTTP_TIMEOUT", "30"))
MAX_ATTEMPTS = int(os.getenv("SUPABASE_OUTBOX_MAX_ATTEMPTS", "8"))

# Hermes state -> (outreach_status, research_status, qa_status)
# Only values the live schema actually accepts; probed against the real table
# rather than assumed. `pipeline_state` takes the Hermes state verbatim.
STATE_MAP: Dict[str, Dict[str, Optional[str]]] = {
    "NEW":               {"outreach": "not_started"},
    "RESEARCH_PENDING":  {"outreach": "researching", "research": "pending"},
    "RESEARCHING":       {"outreach": "researching", "research": "researching"},
    "RESEARCH_COMPLETE": {"outreach": "researching", "research": "completed"},
    "COPY_PENDING":      {"outreach": "researching"},
    "COPY_READY":        {"outreach": "copy_ready"},
    "QA_PENDING":        {"outreach": "qa_pending", "qa": "pending"},
    "QA_REJECTED":       {"outreach": "qa_rejected", "qa": "rejected"},
    "READY_TO_SEND":     {"outreach": "queued_to_send", "qa": "approved"},
    "SENT":              {"outreach": "sent"},
    "FOLLOWUP_WAITING":  {"outreach": "sent"},
    "FOLLOWUP_PENDING":  {"outreach": "sent"},
    "REPLIED":           {"outreach": "replied"},
    "POSITIVE":          {"outreach": "positive"},
    "NEGATIVE":          {"outreach": "negative"},
    "MEETING_STAGE":     {"outreach": "meeting"},
    "HUMAN_REVIEW":      {"outreach": "human_review"},
    "UNSUBSCRIBED":      {"outreach": "unsubscribed"},
    "BOUNCED":           {"outreach": "bounced"},
    "CLOSED":            {"outreach": "closed"},
    "ERROR":             {"outreach": "error"},
}

# Terminal outcomes have a dedicated function on the Supabase side that sets
# every related column and timestamp together. Using them keeps the mirror
# self-consistent in a way a column-by-column PATCH cannot.
STATE_RPC = {
    "POSITIVE": "mark_lead_positive",
    "NEGATIVE": "mark_lead_negative",
    "MEETING_STAGE": "mark_lead_meeting",
    "UNSUBSCRIBED": "mark_lead_unsubscribed",
    "BOUNCED": "mark_lead_bounced",
    "CLOSED": "mark_lead_closed",
}

# LEO's classification -> the vocabulary the mirror accepts.
REPLY_CLASS = {
    "positive": "interested", "interested": "interested",
    "pricing_question": "pricing", "pricing": "pricing",
    "meeting_request": "meeting", "meeting": "meeting",
    "negative": "negative", "not_interested": "negative",
    "unsubscribe": "unsubscribe", "opt_out": "unsubscribe",
    "out_of_office": "ooo", "auto_reply": "ooo", "ooo": "ooo",
}


class SupabaseError(RuntimeError):
    pass


def configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SECRET)


def _call(path: str, method: str = "GET", body: Any = None,
          prefer: str = None) -> Any:
    """One HTTP call. Never logs the key, never puts it in a URL."""
    if not configured():
        raise SupabaseError("SUPABASE_URL / SUPABASE_SECRET_KEY are not set")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SUPABASE_URL + "/rest/v1/" + path,
                                 data=data, method=method)
    req.add_header("apikey", SUPABASE_SECRET)
    req.add_header("Authorization", "Bearer " + SUPABASE_SECRET)
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        raise SupabaseError("http %d: %s" % (exc.code, _sanitize(
            exc.read().decode()[:300])))
    except Exception as exc:
        raise SupabaseError("%s: %s" % (type(exc).__name__, _sanitize(str(exc))))


def rpc(name: str, args: Dict[str, Any]) -> Any:
    return _call("rpc/" + name, "POST", args)


def _sanitize(text: str) -> str:
    """Strip anything credential-shaped before it reaches a log or a database.

    Error bodies from an auth failure quote the token back at you, and this
    text ends up in last_error on both sides.
    """
    out = text or ""
    for secret in (SUPABASE_SECRET, os.getenv("MAILHUB_API_TOKEN", ""),
                   os.getenv("STEEL_API_KEY", "")):
        if secret and len(secret) > 8:
            out = out.replace(secret, "[redacted]")
    import re
    out = re.sub(r"(sb_secret_|sb_publishable_|eyJ|ae_live_|rnd_|ghp_|github_pat_)"
                 r"[A-Za-z0-9._\-]+", "[redacted]", out)
    # Consume the whole credential, not just the first token after the
    # label: "Authorization: Bearer abc123" left "abc123" behind when
    # the pattern stopped at "Bearer".
    out = re.sub(r"(?i)\b(authorization|apikey|api[_-]?key|x-api-key)\b"
                 r"\s*[:=]\s*(?:bearer\s+)?\S+", r"\1: [redacted]", out)
    out = re.sub(r"(?i)\bbearer\s+\S+", "bearer [redacted]", out)
    return out[:500]


# ---------------------------------------------------------------------------
# Operational day
# ---------------------------------------------------------------------------

def operational_day(now: datetime.datetime = None) -> str:
    """Today in the operational timezone, as YYYY-MM-DD.

    Reporting and the daily intake target both run on Asia/Kolkata, so a day
    boundary is the same event for the operator and for the limiter.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now + datetime.timedelta(minutes=TZ_OFFSET_MINUTES)).strftime("%Y-%m-%d")


def imported_today(con, day: str = None) -> int:
    day = day or operational_day()
    row = con.execute("SELECT imported FROM lead_intake_days WHERE day=?",
                      (day,)).fetchone()
    return int(row["imported"]) if row else 0


def _record_import(con, day: str, n: int = 1) -> None:
    con.execute(
        "INSERT INTO lead_intake_days (day, imported, target) VALUES (?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET imported=imported+excluded.imported,"
        "  last_at=datetime('now'), target=excluded.target",
        (day, n, DAILY_TARGET))


# ---------------------------------------------------------------------------
# Supabase -> Hermes
# ---------------------------------------------------------------------------

FIELDS = ("business_name", "contact_name", "owner_name", "email", "phone",
          "whatsapp", "website", "google_maps_url", "instagram_url",
          "facebook_url", "city", "region", "country", "area_locality",
          "address", "niche", "business_type", "google_category", "priority",
          "score", "rating", "review_count", "opener", "category_opener",
          "recommended_offer", "main_services", "main_opportunity",
          "score_reason", "notes", "campaign_id", "source", "external_lead_id")


def normalize(row: Dict[str, Any], default_campaign: str) -> Dict[str, Any]:
    """Supabase row -> the shape lead_ingest expects.

    Everything the source knows is preserved: the columns Hermes models get
    mapped, and the rest is kept verbatim in notes so nothing is silently
    thrown away on the way in.
    """
    out = {
        "email": (row.get("email") or "").strip().lower(),
        "business_name": (row.get("business_name") or "").strip(),
        "contact_name": (row.get("contact_name") or row.get("owner_name") or "").strip(),
        "website": (row.get("website") or "").strip(),
        "phone": (row.get("phone") or row.get("whatsapp") or "").strip(),
        "niche": (row.get("niche") or row.get("business_type")
                  or row.get("google_category") or "").strip(),
        "country": (row.get("country") or "").strip(),
        "city": (row.get("city") or row.get("area_locality") or "").strip(),
        "campaign_id": (row.get("campaign_id") or default_campaign).strip(),
        "source": "supabase:%s" % (row.get("source") or "unknown"),
    }
    extra = {k: row.get(k) for k in FIELDS
             if row.get(k) not in (None, "", [], {}) and k not in out}
    if extra:
        out["notes"] = json.dumps(extra, ensure_ascii=False)[:4000]
    return out


def _verification_verdicts(ids):
    """Verification fields for the rows just claimed, keyed by id.

    Fetched in one request rather than per row. On any failure this returns
    None, which `_verification_allows` treats as "cannot confirm" -- the gate
    fails closed, so a Supabase hiccup delays leads rather than admitting
    unverified ones.
    """
    ids = [str(i) for i in ids if i]
    if not ids:
        return {}
    try:
        rows = _call("leads?select=id,email,email_verification_status,"
                     "email_verified,raw_data&id=in.(%s)" % ",".join(ids)) or []
        return {str(r.get("id")): r for r in rows if isinstance(r, dict)}
    except Exception:
        return None


def _verification_allows(sid, verdicts):
    """Whether this claimed lead may be imported. Fails closed."""
    if verdicts is None:
        return False, "could not read verification state"
    row = verdicts.get(str(sid))
    if row is None:
        return False, "no verification row"
    try:
        import verification_worker as VW
        return VW.claim_guard(row)
    except Exception as exc:
        return False, "verification check failed: %s" % _sanitize(str(exc))


def claim(limit: int = None, campaign: str = "C-LEADSKING",
          dry_run: bool = False) -> Dict[str, Any]:
    """Claim ready leads and import them. Atomic on the Supabase side.

    claim_leads_for_hermes() marks the rows claimed in one statement, so two
    sync ticks running at once cannot take the same lead. Anything we then fail
    to import is released again rather than left claimed forever.
    """
    limit = limit or BATCH_SIZE
    day = operational_day()
    out = {"day": day, "target": DAILY_TARGET, "claimed": 0, "imported": 0,
           "duplicate": 0, "released": 0, "rejected": 0, "leads": []}

    with P.connect() as con:
        already = imported_today(con, day)
    out["imported_today_before"] = already
    room = DAILY_TARGET - already
    if room <= 0:
        out["skipped"] = ("daily target reached: %d/%d for %s (%s) — leads stay "
                          "ready for tomorrow" % (already, DAILY_TARGET, day, TZ_NAME))
        return out
    limit = min(limit, room)
    out["will_claim"] = limit

    if dry_run:
        out["skipped"] = "dry run"
        return out

    # Which claim function to use is runtime configuration, not code: once
    # claim_verified_leads_for_hermes() exists in Supabase (see
    # migrations/supabase/009_email_verification_gate.sql), switching to it is
    # one env var. The Python guard below stays on either way.
    rows = rpc(CLAIM_RPC, {"p_limit": limit}) or []
    out["claimed"] = len(rows)
    out["unverified"] = 0

    # The verification gate. claim_leads_for_hermes() does not know about email
    # verification, so the verdict is fetched for exactly the rows it just
    # claimed and checked here -- at the one place every Supabase lead enters
    # Hermes, rather than anywhere a caller might forget.
    #
    # Checked after the claim rather than before it because the claim is what
    # makes the set atomic: two ticks cannot hold the same row, so releasing an
    # unverified one is safe and cannot strand it.
    verdicts = _verification_verdicts([r.get("id") for r in rows])

    for row in rows:
        sid = row.get("id")
        try:
            allowed, why = _verification_allows(sid, verdicts)
            if not allowed:
                # Released, not rejected at source: the lead is fine, it simply
                # has not been cleared yet, and the verifier tick may clear it
                # on a later pass.
                rpc("release_lead_claim", {"p_id": sid})
                out["released"] += 1
                out["unverified"] += 1
                continue

            data = normalize(row, campaign)
            if not data["email"] or not data["business_name"]:
                # Never silently lost: released so it can be fixed at source.
                rpc("release_lead_claim", {"p_id": sid})
                out["released"] += 1
                out["rejected"] += 1
                continue
            con = li.connect(None)
            try:
                with con:
                    res = li.ingest_one(con, data, source="supabase",
                                        default_campaign=data["campaign_id"])
            finally:
                con.close()

            if res["status"] == "rejected":
                rpc("release_lead_claim", {"p_id": sid})
                out["released"] += 1
                out["rejected"] += 1
                continue

            lead_id = res["lead_id"]
            with P.connect() as pcon:
                with P.writing(pcon):
                    pcon.execute(
                        "INSERT OR IGNORE INTO supabase_leads (lead_id,"
                        " supabase_id, source, external_lead_id) VALUES (?,?,?,?)",
                        (lead_id, sid, row.get("source"),
                         row.get("external_lead_id")))
                    if res["status"] == "created":
                        _record_import(pcon, day)
            rpc("mark_lead_imported", {"p_id": sid, "p_hermes_lead_id": lead_id})

            if res["status"] == "created":
                out["imported"] += 1
                enqueue(lead_id, "state", {"state": "NEW"})
            else:
                out["duplicate"] += 1
            out["leads"].append({"supabase_id": sid, "lead_id": lead_id,
                                 "status": res["status"]})
        except Exception as exc:
            # A lead we cannot import goes back on the shelf. Leaving it
            # claimed would take it out of circulation permanently.
            try:
                rpc("release_lead_claim", {"p_id": sid})
                out["released"] += 1
            except Exception:
                pass
            out.setdefault("errors", []).append(_sanitize(str(exc)))
    return out


# ---------------------------------------------------------------------------
# Hermes -> Supabase (through the outbox)
# ---------------------------------------------------------------------------

def enqueue(lead_id: str, event_type: str, payload: Dict[str, Any],
            con: sqlite3.Connection = None, force: bool = False) -> Optional[int]:
    """Record a write-back. Never raises into the caller's transaction.

    The state machine has already committed by the time this runs; a mirror
    that cannot be written is a retry, not a failure of the thing that happened.

    `force` is for reconcile. Ordinarily the dedupe key stops the same state
    being queued twice — but that is exactly what a repair has to do when a
    state synced once and the mirror has since drifted. Forcing releases the
    old key so a fresh event can be inserted, keeping the old row as history
    rather than overwriting it.
    """
    key = "%s:%s:%s" % (lead_id, event_type,
                        payload.get("state") or payload.get("value") or "")

    def _do(c):
        row = c.execute("SELECT supabase_id FROM supabase_leads WHERE lead_id=?",
                        (lead_id,)).fetchone()
        if not row:
            return None          # not a Supabase lead; nothing to mirror
        if force:
            c.execute("UPDATE supabase_sync_outbox SET dedupe_key=NULL"
                      " WHERE dedupe_key=?", (key,))
        # Scrubbed before it is written, not merely before it is sent. An
        # error body quoting a token would otherwise sit in agency.db for as
        # long as the outbox row survives, which is its own disclosure.
        safe_payload = {k: (_sanitize(v) if isinstance(v, str) else v)
                        for k, v in payload.items()}
        c.execute(
            "INSERT OR IGNORE INTO supabase_sync_outbox (lead_id, supabase_id,"
            " event_type, payload_json, dedupe_key) VALUES (?,?,?,?,?)",
            (lead_id, row["supabase_id"], event_type,
             json.dumps(safe_payload, ensure_ascii=False), key))
        return c.execute("SELECT id FROM supabase_sync_outbox WHERE dedupe_key=?",
                         (key,)).fetchone()
    try:
        if con is not None:
            r = _do(con)
            return r["id"] if r else None
        with P.connect() as c2:
            with P.writing(c2):
                r = _do(c2)
            return r["id"] if r else None
    except Exception:
        return None


def _deliver(event: sqlite3.Row) -> None:
    """Send one outbox event. Raises on failure so the caller can back off."""
    payload = json.loads(event["payload_json"])
    lead_id, sid = event["lead_id"], event["supabase_id"]
    kind = event["event_type"]

    if kind == "state":
        state = payload["state"]
        fn = STATE_RPC.get(state)
        if fn:
            rpc(fn, {"p_hermes_lead_id": lead_id})
            return
        m = STATE_MAP.get(state, {})
        # last_error is for failures. Writing every transition's reason into
        # it turned "returned to ARIA for rewrite" into a permanent error on
        # a perfectly healthy lead.
        err = payload.get("error") or ""
        if state not in ("ERROR", "BOUNCED"):
            err = ""
        rpc("update_hermes_lead_status", {
            "p_hermes_lead_id": lead_id,
            "p_pipeline_state": state,
            "p_outreach_status": m.get("outreach"),
            "p_error": _sanitize(err) or None})
        patch = {}
        if m.get("research"):
            patch["research_status"] = m["research"]
        if m.get("qa"):
            patch["qa_status"] = m["qa"]
        if patch:
            _call("leads?id=eq.%s" % sid, "PATCH", patch)
        return

    if kind == "queued":
        rpc("mark_lead_queued", {"p_hermes_lead_id": lead_id,
                                 "p_mailhub_message_id": str(payload.get("mailhub_message_id") or "")})
        return

    if kind == "sent":
        # Only ever written on a provider-confirmed send. A queued message is
        # not a sent one, and the mirror must not say otherwise.
        rpc("mark_lead_sent", {
            "p_hermes_lead_id": lead_id,
            "p_provider_message_id": payload.get("provider_message_id"),
            "p_provider_thread_id": payload.get("provider_thread_id")})
        return

    if kind == "send_failed":
        rpc("mark_lead_send_failed", {"p_hermes_lead_id": lead_id,
                                      "p_error": _sanitize(payload.get("error") or "send failed")})
        return

    if kind == "replied":
        cls = REPLY_CLASS.get((payload.get("classification") or "").lower(),
                              "human_review")
        rpc("mark_lead_replied", {"p_hermes_lead_id": lead_id,
                                  "p_classification": cls})
        return

    if kind == "research":
        _call("leads?id=eq.%s" % sid, "PATCH",
              {"research_status": payload["value"]})
        return

    if kind == "qa":
        _call("leads?id=eq.%s" % sid, "PATCH", {"qa_status": payload["value"]})
        return

    raise SupabaseError("unknown outbox event type %r" % kind)


def drain(limit: int = 200) -> Dict[str, Any]:
    """Deliver pending write-backs, oldest first, with backoff."""
    out = {"attempted": 0, "synced": 0, "failed": 0, "deferred": 0}
    if not configured():
        out["skipped"] = "Supabase is not configured"
        return out

    with P.connect() as con:
        rows = list(con.execute(
            "SELECT * FROM supabase_sync_outbox "
            " WHERE status='pending' AND next_retry_at <= datetime('now') "
            " ORDER BY id LIMIT ?", (limit,)))

        for ev in rows:
            out["attempted"] += 1
            try:
                _deliver(ev)
                with P.writing(con):
                    con.execute(
                        "UPDATE supabase_sync_outbox SET status='synced',"
                        " synced_at=datetime('now'), attempts=attempts+1,"
                        " last_error=NULL WHERE id=?", (ev["id"],))
                    con.execute(
                        "UPDATE supabase_leads SET last_synced_at=datetime('now'),"
                        " last_synced_state=? WHERE lead_id=?",
                        (json.loads(ev["payload_json"]).get("state"), ev["lead_id"]))
                out["synced"] += 1
            except Exception as exc:
                attempts = ev["attempts"] + 1
                # Exponential, capped: 30s, 1m, 2m, 4m … 16m. Long enough to
                # ride out an outage, short enough that the mirror is not
                # hours stale once it returns.
                delay = min(30 * (2 ** min(attempts, 5)), 960)
                terminal = attempts >= MAX_ATTEMPTS
                with P.writing(con):
                    con.execute(
                        "UPDATE supabase_sync_outbox SET status=?, attempts=?,"
                        " last_error=?, next_retry_at=datetime('now', ?)"
                        " WHERE id=?",
                        ("failed" if terminal else "pending", attempts,
                         _sanitize(str(exc)), "+%d seconds" % delay, ev["id"]))
                out["failed" if terminal else "deferred"] += 1
    return out


def reconcile(limit: int = 500) -> Dict[str, Any]:
    """Repair the mirror from agency.db. One direction only.

    This never reads Supabase state back into Hermes. If the two disagree,
    Hermes is right by definition and the mirror is corrected.
    """
    out = {"checked": 0, "enqueued": 0}
    with P.connect() as con:
        rows = list(con.execute(
            "SELECT s.lead_id, s.supabase_id, s.last_synced_state, l.state"
            "  FROM supabase_leads s JOIN leads l ON l.id = s.lead_id"
            " ORDER BY l.updated_at DESC LIMIT ?", (limit,)))
        for r in rows:
            out["checked"] += 1
            if r["last_synced_state"] != r["state"]:
                # force: the whole point of a repair is to re-send a state the
                # outbox already believes it delivered.
                if enqueue(r["lead_id"], "state", {"state": r["state"]},
                           force=True):
                    out["enqueued"] += 1
    return out


def status() -> Dict[str, Any]:
    day = operational_day()
    with P.connect() as con:
        n = imported_today(con, day)
        pending = con.execute(
            "SELECT COUNT(*) c FROM supabase_sync_outbox WHERE status='pending'"
        ).fetchone()["c"]
        failed = con.execute(
            "SELECT COUNT(*) c FROM supabase_sync_outbox WHERE status='failed'"
        ).fetchone()["c"]
        mapped = con.execute("SELECT COUNT(*) c FROM supabase_leads").fetchone()["c"]
    out = {"configured": configured(), "day": day, "timezone": TZ_NAME,
           "imported_today": n, "target": DAILY_TARGET,
           "remaining": max(0, DAILY_TARGET - n),
           "mapped_leads": mapped, "outbox_pending": pending,
           "outbox_failed": failed, "batch_size": BATCH_SIZE}
    if configured():
        try:
            ready = _call("leads?select=id&status=eq.ready&hermes_status=eq."
                          "not_imported&limit=1", prefer="count=exact")
            out["supabase_ready"] = "see range header"
        except Exception as exc:
            out["supabase_error"] = _sanitize(str(exc))
    return out


def ready_count() -> Optional[int]:
    """How many leads Supabase still has waiting. None if it cannot be asked."""
    if not configured():
        return None
    try:
        req = urllib.request.Request(
            SUPABASE_URL + "/rest/v1/leads?select=id&status=eq.ready"
            "&hermes_status=eq.not_imported&limit=1")
        req.add_header("apikey", SUPABASE_SECRET)
        req.add_header("Authorization", "Bearer " + SUPABASE_SECRET)
        req.add_header("Prefer", "count=exact")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            rng = r.headers.get("Content-Range") or ""
        return int(rng.split("/")[-1]) if "/" in rng else None
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("--limit", type=int, default=None)
    c.add_argument("--campaign", default=os.getenv("SUPABASE_CAMPAIGN", "C-LEADSKING"))
    c.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("drain"); d.add_argument("--limit", type=int, default=200)
    t = sub.add_parser("tick")
    t.add_argument("--campaign", default=os.getenv("SUPABASE_CAMPAIGN", "C-LEADSKING"))
    r = sub.add_parser("reconcile"); r.add_argument("--limit", type=int, default=500)
    sub.add_parser("status")
    args = ap.parse_args(argv)

    if args.cmd == "claim":
        print(json.dumps(claim(args.limit, args.campaign, args.dry_run), indent=2))
    elif args.cmd == "drain":
        print(json.dumps(drain(args.limit), indent=2))
    elif args.cmd == "tick":
        print(json.dumps({"claim": claim(campaign=args.campaign),
                          "drain": drain()}, indent=2))
    elif args.cmd == "reconcile":
        print(json.dumps(reconcile(args.limit), indent=2))
    else:
        print(json.dumps(status(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
