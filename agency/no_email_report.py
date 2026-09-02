"""The evening list of leads that have no email address.

These leads cannot enter automated email outreach: there is nothing to
verify, nothing to send to, and they must never consume the verifier's quota
or the daily admission target. Their outreach route is a person. So every
evening the complete list -- every lead, every useful field the table holds --
goes to #maya-office, with a suggested manual channel drawn only from the
contact details that lead actually has.

Nothing here decides anything with a model, and nothing here invents data: a
field that is empty is omitted from the card, not filled in.

Delivery is multi-part and tracked. Discord messages are capped at 2,000
characters, and a list of several hundred leads runs to dozens of parts. Each
part is recorded in agency.db as it lands, so a failure on part 14 resumes at
part 14 and never resends 1-13, and a second run on the same day sends
nothing that already went.
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import os
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

import email_verifier as EV
import supabase_sync as S

SECTION = "no_email"
NO_EMAIL_KEY = "no_email"

# Discord's hard limit is 2,000 characters. Staying well under leaves room for
# the part header and for Discord's own markdown handling.
DISCORD_HARD_LIMIT = 2000
PART_TARGET = int(os.getenv("NO_EMAIL_PART_CHARS", "1800"))

DISCORD_API = "https://discord.com/api/v10"
CHANNEL_ID = os.getenv("NO_EMAIL_DISCORD_CHANNEL", "1484778503529304145")  # #maya-office

FIELDS = ("id,email,external_lead_id,business_name,business_type,niche,area_locality,"
          "address,city,region,country,phone,whatsapp,website,google_maps_url,"
          "instagram_url,facebook_url,owner_name,contact_name,main_services,"
          "main_opportunity,priority,score,score_reason,opener,category_opener,"
          "recommended_offer,rating,review_count,google_category,"
          "website_platform,has_booking,mobile_ready,source,status,"
          "hermes_status,raw_data,created_at")

CSV_COLUMNS = ["business_name", "niche", "area_locality", "city", "country",
               "phone", "whatsapp", "website", "google_maps_url", "instagram_url",
               "facebook_url", "owner_name", "source", "external_lead_id",
               "lead_id"]

HEADER = (
    "**NO EMAIL LEADS — MANUAL CONTACT REQUIRED**\n"
    "\n"
    "These leads do not have an email address.\n"
    "\n"
    "**OWNER / TEAM ACTION REQUIRED**\n"
    "These leads have no email address available.\n"
    "Please contact them manually yourself using Phone, WhatsApp, Instagram,\n"
    "Facebook, website contact form, or Google Maps contact details.\n"
    "\n"
    "Do not wait for MAYA/MailHub email outreach for these records."
)

FOOTER = "MANUAL CONTACT REQUIRED — NO EMAIL AVAILABLE"

_PRIORITY_RANK = {"hot": 3, "high": 3, "warm": 2, "medium": 2, "cold": 1,
                  "low": 1}


# --- data -------------------------------------------------------------------

def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _raw(lead: Dict[str, Any]) -> Dict[str, Any]:
    raw = lead.get("raw_data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def sort_key(lead: Dict[str, Any]) -> Tuple:
    """Strongest manual-contact leads first, from data the row already has.

    priority, score, rating, review_count -- all descending, missing values
    last. Nothing is scored here; a lead with none of these keeps its natural
    place at the end, ordered by name so the order is stable across runs.
    """
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    pr = _PRIORITY_RANK.get(str(lead.get("priority") or "").strip().lower(), 0)
    sc, ra, rc = num(lead.get("score")), num(lead.get("rating")), num(lead.get("review_count"))
    return (-pr,
            -(sc if sc is not None else -1e9),
            -(ra if ra is not None else -1e9),
            -(rc if rc is not None else -1e9),
            (lead.get("business_name") or "").lower())


def fetch_no_email_leads(limit: int = 2000) -> List[Dict[str, Any]]:
    """Every active lead with no usable address. Read-only.

    NULL and the empty string come from the database filter. Whitespace-only
    addresses cannot be expressed in PostgREST; the verification worker parks
    those under hermes_status='no_email' when it meets them, and that status
    is the second filter here. Both sets are de-duplicated by id and checked
    again in Python, so a lead is listed only if it genuinely has no address.
    """
    seen, out = set(), []
    for q in ("&or=(email.is.null,email.eq.)", "&status=eq.hold&email=not.is.null"):
        rows = S._call("leads?select=%s&is_active=eq.true%s&limit=%d"
                       % (FIELDS, q, limit)) or []
        for r in rows:
            if not isinstance(r, dict) or r.get("id") in seen:
                continue
            if not _is_no_email(r):
                continue
            seen.add(r["id"])
            out.append(r)
    out.sort(key=sort_key)
    return out


def _is_no_email(lead: Dict[str, Any]) -> bool:
    """True only when the address is NULL, empty or whitespace.

    A lead that has gained an address leaves this group immediately, even if
    it is still parked -- the worker un-parks it on its next tick, and this
    report must not show a lead as unreachable when it is not.
    """
    return _blank(lead.get("email"))


# --- rendering --------------------------------------------------------------

def suggest_channel(lead: Dict[str, Any]) -> Tuple[str, str]:
    """(label, instruction) for the strongest channel this lead actually has.

    Priority: WhatsApp, phone, Instagram, Facebook, website, Google Maps.
    Only channels present on the row are ever suggested.
    """
    if not _blank(lead.get("whatsapp")):
        return "WhatsApp preferred", "WhatsApp: %s" % lead["whatsapp"].strip()
    if not _blank(lead.get("phone")):
        return "Call / SMS", "Phone: %s" % lead["phone"].strip()
    if not _blank(lead.get("instagram_url")):
        return "Instagram DM", "Instagram: %s" % lead["instagram_url"].strip()
    if not _blank(lead.get("facebook_url")):
        return "Facebook message", "Facebook: %s" % lead["facebook_url"].strip()
    if not _blank(lead.get("website")):
        return "Website contact form", "Website: %s" % lead["website"].strip()
    if not _blank(lead.get("google_maps_url")):
        return "Google Maps / business listing", "Google Maps: %s" % lead["google_maps_url"].strip()
    return "No contact details on record", "No phone, WhatsApp, social or website available"


CARD_FIELDS = [
    ("Business", "business_name"),
    ("Niche", "niche"), ("Business type", "business_type"),
    ("Area", "area_locality"), ("Address", "address"),
    ("City", "city"), ("Region", "region"), ("Country", "country"),
    ("Phone", "phone"), ("WhatsApp", "whatsapp"), ("Website", "website"),
    ("Google Maps", "google_maps_url"), ("Instagram", "instagram_url"),
    ("Facebook", "facebook_url"),
    ("Owner", "owner_name"), ("Contact", "contact_name"),
    ("Priority", "priority"), ("Score", "score"), ("Score reason", "score_reason"),
    ("Rating", "rating"), ("Reviews", "review_count"),
    ("Google category", "google_category"), ("Website platform", "website_platform"),
    ("Has booking", "has_booking"), ("Mobile ready", "mobile_ready"),
    ("Opener", "opener"), ("Category opener", "category_opener"),
    ("Main services", "main_services"), ("Main opportunity", "main_opportunity"),
    ("Recommended offer", "recommended_offer"),
    ("Source", "source"), ("External lead ID", "external_lead_id"), ("Lead ID", "id"),
]
LONG_FIELDS = {"main_services", "main_opportunity", "recommended_offer",
               "score_reason", "opener"}


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def render_card(n: int, lead: Dict[str, Any]) -> str:
    """One lead as a Discord-ready block. Empty fields are omitted, never filled."""
    lines = ["--------------------------------", "LEAD %d" % n, ""]
    for label, key in CARD_FIELDS:
        v = lead.get(key)
        if _blank(v):
            continue
        if key in LONG_FIELDS:
            lines.append("%s:" % label)
            lines.append(_fmt(v))
            lines.append("")
        else:
            lines.append("%s: %s" % (label, _fmt(v)))
    channel, detail = suggest_channel(lead)
    lines += ["", FOOTER, "MANUAL CONTACT:", channel, detail,
              "--------------------------------"]
    return "\n".join(lines)


def _split_long(card: str, room: int) -> List[str]:
    """Split a single oversized card at line boundaries, marking continuations."""
    parts, cur = [], ""
    for line in card.splitlines():
        candidate = (cur + "\n" + line) if cur else line
        if len(candidate) > room and cur:
            parts.append(cur)
            cur = "(cont.)\n" + line
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


def paginate(cards: Iterable[str], target: int = PART_TARGET) -> List[str]:
    """Group cards into messages under `target` chars, never splitting a card
    unless the card alone exceeds the target."""
    target = min(target, DISCORD_HARD_LIMIT - 120)   # room for the PART header
    pages, cur = [], ""
    for card in cards:
        pieces = [card] if len(card) <= target else _split_long(card, target)
        for piece in pieces:
            if not cur:
                cur = piece
            elif len(cur) + 2 + len(piece) <= target:
                cur = cur + "\n\n" + piece
            else:
                pages.append(cur)
                cur = piece
    if cur:
        pages.append(cur)
    return pages


def summary(leads: List[Dict[str, Any]], day: str) -> Dict[str, Any]:
    """Counts a person can act on. New vs still-missing comes from the
    first_seen_at stamp the worker writes when it parks a lead."""
    new, still = 0, 0
    for l in leads:
        first = (_raw(l).get(NO_EMAIL_KEY) or {}).get("first_seen_at") or ""
        # No stamp yet means the worker has not met this lead: it is being
        # seen for the first time, which is what "new" means. Only a stamp
        # from an earlier day makes it "still missing".
        if not first or str(first)[:10] == day:
            new += 1
        else:
            still += 1
    return {
        "total": len(leads), "new_today": new, "still_missing": still,
        "by_country": dict(Counter((l.get("country") or "unknown") for l in leads).most_common()),
        "by_city": dict(Counter((l.get("city") or "unknown") for l in leads).most_common(15)),
        "by_source": dict(Counter((l.get("source") or "unknown") for l in leads).most_common()),
    }


def summary_text(s: Dict[str, Any]) -> str:
    L = ["**NO EMAIL LEADS — MANUAL CONTACT REQUIRED**", "",
         "Total no-email leads:       %d" % s["total"],
         "New today:                  %d" % s["new_today"],
         "Still unresolved:           %d" % s["still_missing"], "",
         "Owner / Team:",
         "These leads cannot enter automated email outreach.",
         "Please contact them manually using the available phone,",
         "WhatsApp, social media, website, or Google Maps information.", ""]
    if s["by_country"]:
        L.append("By country:")
        L += ["  %s: %d" % (k, v) for k, v in list(s["by_country"].items())[:10]]
    if s["by_city"]:
        L.append("Top cities:")
        L += ["  %s: %d" % (k, v) for k, v in list(s["by_city"].items())[:10]]
    if s["by_source"]:
        L.append("By source:")
        L += ["  %s: %d" % (k, v) for k, v in s["by_source"].items()]
    return "\n".join(L)


def csv_bytes(leads: List[Dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for l in leads:
        row = {k: ("" if _blank(l.get(k)) else _fmt(l.get(k))) for k in CSV_COLUMNS}
        row["lead_id"] = l.get("id") or ""
        w.writerow(row)
    return buf.getvalue().encode("utf-8")


# --- delivery ---------------------------------------------------------------

def _discord(method: str, path: str, body: Optional[Dict[str, Any]] = None,
             files: Optional[List[Tuple[str, bytes, str]]] = None) -> Tuple[int, Any]:
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        return 0, {"error": "DISCORD_BOT_TOKEN not set"}
    url = DISCORD_API + path
    headers = {"Authorization": "Bot " + token,
               "User-Agent": "hermes-agency-orbit (https://render.com, 1.0)"}
    if files:
        boundary = "----hermes" + hashlib.sha1(os.urandom(8)).hexdigest()
        parts = []
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                      "Content-Type: application/json\r\n\r\n%s\r\n"
                      % (boundary, json.dumps(body or {}))).encode())
        for i, (name, data, ctype) in enumerate(files):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"files[%d]\"; "
                          "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                          % (boundary, i, name, ctype)).encode() + data + b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode())
        data = b"".join(parts)
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    else:
        data = json.dumps(body).encode() if body is not None else None
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as exc:
        return 0, {"error": EV.scrub("%s: %s" % (type(exc).__name__, exc))}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _ledger_get(con: sqlite3.Connection, day: str, part_no: int) -> Optional[sqlite3.Row]:
    return con.execute("SELECT * FROM report_deliveries WHERE report_day=? AND section=?"
                       " AND part_no=?", (day, SECTION, part_no)).fetchone()


def _ledger_put(con: sqlite3.Connection, day: str, part_no: int, total: int,
                content_hash: str, channel: str, ok: bool,
                message_id: Optional[str], err: Optional[str]) -> None:
    con.execute(
        "INSERT INTO report_deliveries (report_day, section, part_no, total_parts,"
        " content_hash, channel_id, discord_message_id, delivered_at, attempts, last_error)"
        " VALUES (?,?,?,?,?,?,?,?,1,?)"
        " ON CONFLICT(report_day, section, part_no) DO UPDATE SET"
        "   total_parts=excluded.total_parts, content_hash=excluded.content_hash,"
        "   discord_message_id=COALESCE(excluded.discord_message_id, report_deliveries.discord_message_id),"
        "   delivered_at=COALESCE(excluded.delivered_at, report_deliveries.delivered_at),"
        "   attempts=report_deliveries.attempts+1, last_error=excluded.last_error",
        (day, SECTION, part_no, total, content_hash, channel,
         message_id, datetime.datetime.utcnow().replace(microsecond=0).isoformat() if ok else None,
         err))


def build(day: str, leads: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Everything the evening send needs, computed once. Pure apart from the fetch."""
    leads = fetch_no_email_leads() if leads is None else leads
    cards = [render_card(i, l) for i, l in enumerate(leads, 1)]
    pages = paginate(cards)
    s = summary(leads, day)
    return {"day": day, "leads": leads, "summary": s, "pages": pages,
            "header": HEADER, "summary_text": summary_text(s),
            "csv": csv_bytes(leads)}


def post_all(con: sqlite3.Connection, built: Dict[str, Any],
             channel: str = CHANNEL_ID, send=None) -> Dict[str, Any]:
    """Deliver header, summary, every part, and the CSV -- resumably.

    Part 0 is the header+summary, parts 1..N the cards, part N+1 the CSV.
    Each is skipped if the ledger already shows it delivered today with the
    same content hash, so a rerun after a mid-way failure sends only what is
    missing and a rerun after success sends nothing.
    """
    send = send or _discord
    day, pages = built["day"], built["pages"]
    total = len(pages) + 2
    out = {"sent": 0, "skipped": 0, "failed": 0, "parts": total}

    def deliver(part_no: int, text: Optional[str], files=None) -> bool:
        content_hash = _hash(text or "csv:%d" % len(built["csv"]))
        row = _ledger_get(con, day, part_no)
        if row is not None and row["delivered_at"] and row["content_hash"] == content_hash:
            out["skipped"] += 1
            return True
        body = {"content": text} if text else {"content": "NO EMAIL LEADS — full list as CSV (%d leads)" % len(built["leads"])}
        code, resp = send("POST", "/channels/%s/messages" % channel, body, files)
        ok = code in (200, 201)
        _ledger_put(con, day, part_no, total, content_hash, channel, ok,
                    (resp or {}).get("id") if ok else None,
                    None if ok else EV.scrub(json.dumps(resp)[:200]))
        con.commit()
        out["sent" if ok else "failed"] += 1
        return ok

    if not deliver(0, built["header"] + "\n\n" + built["summary_text"]):
        # Without the header the rest is context-free; stop and retry later.
        return out
    for i, page in enumerate(pages, 1):
        head = "**NO EMAIL LEADS — PART %d/%d**\n\n" % (i, len(pages))
        if not deliver(i, head + page):
            return out
    fname = "no_email_leads_%s.csv" % day
    deliver(len(pages) + 1, None, files=[(fname, built["csv"], "text/csv")])
    return out


def mark_reported(leads: List[Dict[str, Any]], day: str) -> int:
    """Stamp last_reported_at (and first_seen_at if absent) under raw_data.no_email.

    Metadata only: no business field is touched. This is what lets tomorrow's
    report say "still missing" rather than "new" for the same lead.
    """
    n = 0
    now = datetime.datetime.utcnow().replace(microsecond=0).isoformat()
    for l in leads:
        raw = dict(_raw(l))
        meta = dict(raw.get(NO_EMAIL_KEY) or {})
        if meta.get("last_reported_at", "")[:10] == day:
            continue
        meta.setdefault("first_seen_at", now)
        meta["last_reported_at"] = now
        raw[NO_EMAIL_KEY] = meta
        try:
            S._call("leads?id=eq.%s" % l["id"], "PATCH", {"raw_data": raw},
                    prefer="return=minimal")
            n += 1
        except Exception:
            pass
    return n


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Evening no-email lead list")
    ap.add_argument("--preview", type=int, default=0,
                    help="render the first N leads to stdout; send nothing")
    ap.add_argument("--send", action="store_true",
                    help="deliver to Discord (resumable) and stamp leads as reported")
    args = ap.parse_args(argv)
    day = S.operational_day()
    built = build(day)
    if args.preview:
        print(built["summary_text"])
        print()
        for i, l in enumerate(built["leads"][:args.preview], 1):
            print(render_card(i, l))
        print("\n%d lead(s) total -> %d Discord part(s) + header + CSV (%d bytes)"
              % (len(built["leads"]), len(built["pages"]), len(built["csv"])))
        return 0
    if args.send:
        import pipeline as P
        with P.connect() as con:
            res = post_all(con, built)
        stamped = mark_reported(built["leads"], day) if res["failed"] == 0 else 0
        print("no-email report %s: %d part(s) sent, %d skipped, %d failed of %d; "
              "%d lead(s) stamped" % (day, res["sent"], res["skipped"],
                                      res["failed"], res["parts"], stamped))
        return 1 if res["failed"] else 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
