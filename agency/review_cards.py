#!/usr/bin/env python3
"""Human-review escalations as Discord embed cards.

Presentation only. Every value comes from the escalation row, the reply it was
raised from and the lead it belongs to; nothing here decides anything, so a
card cannot disagree with the state machine it describes.

What it replaces: one long plaintext block per escalation, reposted from a
console-style template, ending in a paragraph of command syntax. It was hard
to scan, it buried the one fact that mattered -- what actually happened -- and
a hard bounce arrived looking exactly like a genuine reply needing thought.

    python3 review_cards.py preview        # real data, posts nothing
    python3 review_cards.py preview --json out.json

Delivery state lives on the escalation row (see migration 011), so a card is
posted once, edited when something material changes, and otherwise left alone.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import delivery_status as DS  # noqa: E402

# Discord's embed limits. Same numbers the ORBIT cards respect.
TITLE_MAX = 256
DESC_MAX = 4096
FIELDS_MAX = 25
NAME_MAX = 256
VALUE_MAX = 1024
FOOTER_MAX = 2048
EMBED_CHARS_MAX = 6000
EMBEDS_PER_MESSAGE = 10
MESSAGE_CHARS_TARGET = int(os.getenv("REVIEW_EMBED_CHARS_TARGET", "5600"))

COLOR = {
    "critical": 0xE74C3C,   # red    -- a hard failure
    "warning": 0xF39C12,    # orange -- needs a decision
    "success": 0x2ECC71,    # green  -- a good outcome
    "info": 0x3498DB,       # blue   -- informational
    "neutral": 0x95A5A6,    # grey   -- no action likely
}

CHANNEL_ID = os.getenv("REVIEW_ALERTS_DISCORD_CHANNEL",
                       os.getenv("AGENCY_DISCORD_ALERTS_CHANNEL",
                                 "1484778510383054898"))          # #alerts

# One row per review type: the title it gets, its accent colour, whether a
# reply is a sensible thing to draft, and which actions apply to it.
#
# "Reply appropriate" is the point of the column. Drafting a courteous reply to
# a mailer-daemon is not a small cosmetic problem: it invites someone to send
# it, and nobody is listening at the other end.
TYPES: Dict[str, Dict[str, Any]] = {
    "hard_bounce":      {"title": "📭 Hard Bounce",       "color": "critical",
                         "reply": False, "actions": ["close", "dnc", "manual"]},
    "delivery_issue":   {"title": "⚠️ Delivery Issue",    "color": "warning",
                         "reply": False, "actions": ["close", "hold", "manual"]},
    "temporary_failure": {"title": "🕒 Temporary Delivery Failure", "color": "neutral",
                          "reply": False, "actions": ["close", "resume"]},
    "unsubscribe":      {"title": "🚫 Unsubscribe",       "color": "critical",
                         "reply": False, "actions": ["dnc", "close"]},
    "out_of_office":    {"title": "🏖️ Out of Office",     "color": "neutral",
                         "reply": False, "actions": ["hold", "resume", "close"]},
    "interested":       {"title": "🤝 Interested Lead",   "color": "success",
                         "reply": True,  "actions": ["approve", "edit", "reject", "manual"]},
    "positive":         {"title": "🤝 Interested Lead",   "color": "success",
                         "reply": True,  "actions": ["approve", "edit", "reject", "manual"]},
    "meeting_request":  {"title": "🗓️ Meeting Request",   "color": "success",
                         "reply": True,  "actions": ["approve", "edit", "manual"]},
    "pricing_question": {"title": "💰 Pricing Question",  "color": "info",
                         "reply": True,  "actions": ["approve", "edit", "reject"]},
    "negative":         {"title": "🙅 Not Interested",    "color": "neutral",
                         "reply": False, "actions": ["close", "dnc"]},
    "unclear":          {"title": "💬 Unclear Reply",     "color": "warning",
                         "reply": True,  "actions": ["approve", "edit", "reject", "close"]},
}
DEFAULT_TYPE = {"title": "⚠️ Human Review Required", "color": "warning",
                "reply": True, "actions": ["approve", "edit", "reject", "close"]}

ACTION_CHIPS = {
    "approve": "✅ `review approve %s`",
    "reject":  "❌ `review reject %s`",
    "edit":    "✏️ `review edit %s --text \"…\"`",
    "manual":  "📞 `review manual %s`",
    "hold":    "⏸️ `review hold %s`",
    "dnc":     "🚫 `review dnc %s`",
    "resume":  "▶️ `review resume %s`",
    "close":   "✅ `review close %s`",
}

# What a "material change" is. Anything else -- a timestamp ticking, a field
# being reformatted -- leaves the posted card alone.
MATERIAL = ("reason", "status", "recommended_action", "draft_response",
            "reply_summary", "human_response", "action", "lead_state")


# --- formatting --------------------------------------------------------------

def _clip(s: Any, n: int) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _tidy(text: Any, limit: int = VALUE_MAX) -> str:
    """Collapse whitespace and clip. DSN bodies arrive as pages of quoted
    original message; a field is not the place for them."""
    return _clip(" ".join(str(text or "").split()), limit)


def field(name: str, value: Any, inline: bool = True) -> Dict[str, Any]:
    return {"name": _clip(name, NAME_MAX) or "​",
            "value": _clip("" if value is None else str(value), VALUE_MAX) or "—",
            "inline": bool(inline)}


def embed_chars(e: Dict[str, Any]) -> int:
    n = len(e.get("title") or "") + len(e.get("description") or "")
    n += len((e.get("footer") or {}).get("text") or "")
    for f in e.get("fields") or []:
        n += len(f.get("name") or "") + len(f.get("value") or "")
    return n


def embed(title: str, description: Optional[str] = None,
          fields: Optional[List[Dict[str, Any]]] = None,
          color: int = COLOR["warning"],
          footer: Optional[str] = None) -> Dict[str, Any]:
    """One card, inside Discord's limits. An empty value is not a field:
    a card of blank rows is worse than a shorter card."""
    e: Dict[str, Any] = {"title": _clip(title, TITLE_MAX), "color": int(color)}
    if description:
        e["description"] = _clip(description, DESC_MAX)
    kept = [f for f in (fields or [])
            if f and (f.get("value") or "").strip() not in ("", "—")]
    if kept:
        e["fields"] = kept[:FIELDS_MAX]
    if footer:
        e["footer"] = {"text": _clip(footer, FOOTER_MAX)}
    while embed_chars(e) > MESSAGE_CHARS_TARGET and e.get("fields"):
        longest = max(e["fields"], key=lambda f: len(f["value"]))
        if len(longest["value"]) <= 40:
            break
        longest["value"] = _clip(longest["value"], max(40, len(longest["value"]) // 2))
    return e


def pack(embeds: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    out, cur, n = [], [], 0
    for e in embeds:
        size = embed_chars(e)
        if cur and (len(cur) >= EMBEDS_PER_MESSAGE or n + size > MESSAGE_CHARS_TARGET):
            out.append(cur)
            cur, n = [], 0
        cur.append(e)
        n += size
    if cur:
        out.append(cur)
    return out


# --- deciding what kind of review this is ------------------------------------

def review_kind(row: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """(key, style) for one escalation.

    The stored classification is used unless the reply is plainly a delivery
    notification, in which case the DSN itself is the better authority: the
    reason a bounce used to be filed as "unclear" is that a model was asked to
    interpret a machine-generated report it had no need to interpret.
    """
    verdict = delivery_verdict(row)
    if verdict and verdict["status"] != DS.NOT_A_BOUNCE:
        if verdict["status"] == DS.HARD_BOUNCE:
            return "hard_bounce", TYPES["hard_bounce"]
        if verdict["status"] == DS.TEMPORARY_FAILURE:
            return "temporary_failure", TYPES["temporary_failure"]
        return "delivery_issue", TYPES["delivery_issue"]
    key = (row.get("reason") or row.get("classification") or "").strip().lower()
    return key, TYPES.get(key, DEFAULT_TYPE)


def delivery_verdict(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The DSN reading for this escalation's reply, or None if it is not one."""
    if not DS.looks_like_dsn(row.get("from_email") or "",
                             row.get("subject") or "",
                             row.get("body_text") or ""):
        return None
    return DS.classify(row.get("from_email") or "", row.get("subject") or "",
                       row.get("body_text") or "", row.get("recipient_email"))


# --- the card ----------------------------------------------------------------

def contact_fields(row: Dict[str, Any], shown: Tuple[str, ...] = ()) -> List[Dict[str, Any]]:
    """Only what this lead actually has. No empty rows, no raw JSON.

    `shown` names values already on the card, so the market does not appear
    once as Market and again as Country.
    """
    out = []
    for label, key in (("Recipient", "recipient_email"), ("Website", "website"),
                       ("Phone", "phone"), ("WhatsApp", "whatsapp"),
                       ("City", "city"), ("Country", "country")):
        v = row.get(key)
        if _blank(v) or str(v).strip() in shown:
            continue
        out.append(field(label, _tidy(v, 200)))
    return out


def what_happened(row: Dict[str, Any], verdict: Optional[Dict[str, Any]]) -> str:
    """A sentence a person can act on, not a page of quoted MIME."""
    if verdict and verdict["status"] != DS.NOT_A_BOUNCE:
        who = verdict.get("recipient") or row.get("recipient_email") or "the recipient"
        lines = ["Delivery to **%s** failed." % who]
        if verdict.get("code"):
            lines.append("Reported code: `%s`" % verdict["code"])
        if verdict.get("reason"):
            lines.append("Server said: %s" % _tidy(verdict["reason"], 400))
        return "\n".join(lines)
    sender = row.get("from_email")
    subject = row.get("subject")
    lines = []
    if sender:
        lines.append("Reply from **%s**" % _tidy(sender, 120)
                     + ((" — %s" % _tidy(subject, 140)) if subject else ""))
    body = _tidy(row.get("body_text"), 600)
    if body:
        lines.append("> " + body)
    return "\n".join(lines) or "A reply arrived that needs a person to read it."


def leo_analysis(row: Dict[str, Any], verdict: Optional[Dict[str, Any]]) -> str:
    if verdict and verdict["status"] != DS.NOT_A_BOUNCE:
        txt = ["Classification: `%s`" % verdict["status"],
               "Confidence: %.2f  (read from the delivery report, not inferred)"
               % verdict["confidence"]]
        if verdict["needs_human"]:
            txt.append("Needs a person: the report says the *server* refused "
                       "the message, which is not evidence the address is bad.")
        return "\n".join(txt)
    cls = (row.get("classification") or row.get("reason") or "unclear").strip()
    conf = row.get("confidence")
    txt = ["Classification: `%s`" % cls.upper()]
    if conf is not None:
        txt.append("Confidence: %.2f" % float(conf))
    if cls.lower() == "unclear":
        txt.append("Why it is unclear: the reply did not match a known intent "
                   "with enough confidence to act on, so it is left to a person.")
    if not _blank(row.get("reply_summary")):
        txt.append("Summary: " + _tidy(row["reply_summary"], 500))
    return "\n".join(txt)


def recommended(row: Dict[str, Any], verdict: Optional[Dict[str, Any]]) -> str:
    if verdict and verdict["status"] != DS.NOT_A_BOUNCE:
        return "\n".join("✓ %s" % a for a in DS.recommended_actions(verdict))
    stored = row.get("recommended_action")
    if not _blank(stored):
        return "\n".join("✓ %s" % _tidy(p, 200) for p in
                         re.split(r"[\n;]+", str(stored)) if p.strip())[:VALUE_MAX]
    return ""


def action_chips(review_id: str, style: Dict[str, Any]) -> str:
    return "  ·  ".join(ACTION_CHIPS[a] % review_id
                        for a in style["actions"] if a in ACTION_CHIPS)


def render(row: Dict[str, Any]) -> Dict[str, Any]:
    """One escalation as one embed."""
    key, style = review_kind(row)
    verdict = delivery_verdict(row)
    business = _tidy(row.get("business_name") or "Unknown company", 200)

    fields = [
        field("Business", business),
        field("Review", "`%s`" % (row.get("id") or "-")),
        field("Lead", "`%s`" % (row.get("lead_id") or "-")),
    ]
    market = row.get("country") or row.get("city")
    shown: Tuple[str, ...] = ()
    if not _blank(market):
        fields.append(field("Market", _tidy(market, 80)))
        shown = (str(market).strip(),)
    if not _blank(row.get("campaign_id")):
        fields.append(field("Campaign", "`%s`" % _tidy(row["campaign_id"], 80)))
    if not _blank(row.get("lead_state")):
        fields.append(field("State", "`%s`" % row["lead_state"]))
    conf = verdict["confidence"] if verdict and verdict["status"] != DS.NOT_A_BOUNCE \
        else row.get("confidence")
    if conf is not None:
        fields.append(field("Confidence", "%d%%" % round(float(conf) * 100)))

    fields.extend(contact_fields(row, shown))

    happened = what_happened(row, verdict)
    if happened:
        fields.append(field("📨 What Happened", happened, inline=False))
    analysis = leo_analysis(row, verdict)
    if analysis:
        fields.append(field("🤖 Analysis", analysis, inline=False))
    rec = recommended(row, verdict)
    if rec:
        fields.append(field("✅ Recommended Action", rec, inline=False))

    # A draft only where answering is the right move. Everywhere else this says
    # so plainly, rather than offering words to send to a machine.
    if style["reply"] and not _blank(row.get("draft_response")):
        fields.append(field("✍️ Draft Reply",
                            _tidy(row["draft_response"], 900), inline=False))
    else:
        fields.append(field("✍️ Draft Reply", "No reply required.", inline=False))

    fields.append(field("Actions", action_chips(row.get("id") or "-", style),
                        inline=False))

    raised = row.get("created_at") or ""
    return embed(style["title"], business, fields,
                 color=COLOR[style["color"]],
                 footer="Raised %s · %s" % (str(raised)[:19], row.get("id") or "-"))


def digest(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One compact card counting what is waiting, by kind."""
    counts: Dict[str, int] = {}
    for r in rows:
        key, style = review_kind(r)
        counts[style["title"]] = counts.get(style["title"], 0) + 1
    fields = [field(title, n) for title, n in
              sorted(counts.items(), key=lambda kv: -kv[1])]
    fields.append(field("Total pending", len(rows)))
    return embed("⚠️ Human Review Queue", None, fields, color=COLOR["warning"],
                 footer="Individual cards follow for anything new or changed")


DIGEST_THRESHOLD = int(os.getenv("REVIEW_DIGEST_THRESHOLD", "4"))


# --- delivery state ----------------------------------------------------------

def fingerprint(row: Dict[str, Any]) -> str:
    """What the card would say. Two rows with the same fingerprint produce the
    same card, so reposting one adds nothing."""
    material = {k: row.get(k) for k in MATERIAL}
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:16]


def pending(con: sqlite3.Connection, limit: int = 25) -> List[Dict[str, Any]]:
    """Open escalations, with the lead and newest reply they describe."""
    # WhatsApp is a Supabase field and has no column here; contact_fields
    # renders it when a caller supplies one and omits it otherwise.
    rows = con.execute(
        "SELECT h.*, l.business_name, l.state AS lead_state, l.country, l.city,"
        "       l.website, l.phone, l.email AS recipient_email,"
        "       r.classification, r.confidence, r.from_email, r.subject,"
        "       r.body_text, r.received_at, r.is_bounce"
        "  FROM human_escalations h"
        "  LEFT JOIN leads l ON l.id = h.lead_id"
        "  LEFT JOIN inbound_replies r ON r.id = ("
        "        SELECT id FROM inbound_replies WHERE lead_id = h.lead_id"
        "         ORDER BY COALESCE(received_at,'') DESC, id DESC LIMIT 1)"
        " WHERE h.status = 'open'"
        " ORDER BY h.created_at LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def to_post(rows: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], str]]:
    """(row, action) for each row that needs Discord touched at all.

    "new"    never posted
    "update" posted, but what the card would say has changed materially

    A row whose fingerprint is unchanged is absent from this list, which is
    what stops the two-minute cron reposting the same queue for ever.
    """
    out = []
    for r in rows:
        fp = fingerprint(r)
        if not r.get("first_alerted_at"):
            out.append((r, "new"))
        elif (r.get("alert_fingerprint") or "") != fp:
            out.append((r, "update"))
    return out


def validate(messages: List[Dict[str, Any]]) -> List[str]:
    problems = []
    for i, msg in enumerate(messages, 1):
        embeds = msg.get("embeds") or []
        if len(embeds) > EMBEDS_PER_MESSAGE:
            problems.append("message %d has %d embeds" % (i, len(embeds)))
        if sum(embed_chars(e) for e in embeds) > EMBED_CHARS_MAX:
            problems.append("message %d exceeds %d characters" % (i, EMBED_CHARS_MAX))
        for e in embeds:
            if len(e.get("title") or "") > TITLE_MAX:
                problems.append("title too long: %s" % (e.get("title") or "")[:40])
            if len(e.get("description") or "") > DESC_MAX:
                problems.append("description too long in %s" % e.get("title"))
            if len(e.get("fields") or []) > FIELDS_MAX:
                problems.append("too many fields in %s" % e.get("title"))
            for f in e.get("fields") or []:
                if len(f.get("name") or "") > NAME_MAX or len(f.get("value") or "") > VALUE_MAX:
                    problems.append("field too long in %s" % e.get("title"))
                if not (f.get("value") or "").strip():
                    problems.append("empty field %r in %s" % (f.get("name"), e.get("title")))
    return problems


def post(con: sqlite3.Connection, rows: Optional[List[Dict[str, Any]]] = None,
         channel: str = CHANNEL_ID, send: Optional[Callable] = None,
         edit: Optional[Callable] = None) -> Dict[str, Any]:
    """Post what is new, edit what changed, leave the rest alone."""
    import no_email_report as NE
    send = send or NE._discord
    # The same transport: _discord takes the method, so a PATCH to an existing
    # message is the same call as a POST of a new one.
    edit = edit or send
    rows = pending(con) if rows is None else rows
    work = to_post(rows)
    out = {"pending": len(rows), "new": 0, "updated": 0, "unchanged": len(rows) - len(work),
           "failed": 0, "digest": False}
    if not work:
        return out

    if len(rows) >= DIGEST_THRESHOLD:
        code, _ = send("POST", "/channels/%s/messages" % channel,
                       {"embeds": [digest(rows)]}, None)
        out["digest"] = code in (200, 201)

    now = datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()
    for row, action in work:
        card = render(row)
        fp = fingerprint(row)
        msg_id = row.get("discord_message_id")
        ok = False
        resp: Dict[str, Any] = {}
        if action == "update" and msg_id and edit:
            code, resp = edit("PATCH", "/channels/%s/messages/%s" % (channel, msg_id),
                              {"embeds": [card]}, None)
            ok = code in (200, 201)
        if not ok:
            # Either it is new, or the edit was refused -- an old message can
            # be too old to edit. Posting again is right only because the
            # content genuinely changed; an unchanged row never reaches here.
            code, resp = send("POST", "/channels/%s/messages" % channel,
                              {"embeds": [card]}, None)
            ok = code in (200, 201)
            msg_id = (resp or {}).get("id") or msg_id
        if not ok:
            out["failed"] += 1
            continue
        con.execute(
            "UPDATE human_escalations"
            "   SET first_alerted_at = COALESCE(first_alerted_at, ?),"
            "       last_alerted_at = ?, discord_message_id = ?,"
            "       alert_version = COALESCE(alert_version, 0) + 1,"
            "       alert_fingerprint = ?, notified_at = COALESCE(notified_at, ?)"
            " WHERE id = ?", (now, now, msg_id, fp, now, row["id"]))
        con.commit()
        out["new" if action == "new" else "updated"] += 1
    return out


# --- preview -----------------------------------------------------------------

_COLOR_NAMES = {v: k for k, v in COLOR.items()}


def render_text(messages: List[Dict[str, Any]]) -> str:
    L = []
    for i, msg in enumerate(messages, 1):
        embeds = msg.get("embeds") or []
        L.append("━━━━━━━━━━ MESSAGE %d  (%d embed%s, %d chars) ━━━━━━━━━━"
                 % (i, len(embeds), "" if len(embeds) == 1 else "s",
                    sum(embed_chars(e) for e in embeds)))
        for e in embeds:
            L.append("┌ %s   [%s]" % (e.get("title"),
                                      _COLOR_NAMES.get(e.get("color"), "?")))
            if e.get("description"):
                L.append("│ %s" % e["description"])
            for f in e.get("fields") or []:
                lines = f["value"].splitlines() or [""]
                L.append("│ %s%s: %s" % ("▸ " if f.get("inline") else "▾ ",
                                         f["name"], lines[0]))
                for extra in lines[1:]:
                    L.append("│     %s" % extra)
            L.append("└ %s" % ((e.get("footer") or {}).get("text") or ""))
        L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Human-review cards")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("preview", help="render open reviews; post nothing")
    pv.add_argument("--json", help="write the payloads here")
    pv.add_argument("--limit", type=int, default=25)
    args = ap.parse_args(argv)

    os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")
    import pipeline as P
    with P.connect() as con:
        rows = pending(con, args.limit)
    cards = [render(r) for r in rows]
    messages = []
    if len(rows) >= DIGEST_THRESHOLD:
        messages.append({"embeds": [digest(rows)]})
    for chunk in pack(cards):
        messages.append({"embeds": chunk})
    problems = validate(messages)
    print(render_text(messages))
    print("%d open review(s), %d message(s), limits: %s"
          % (len(rows), len(messages), "ok" if not problems else "; ".join(problems)))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"messages": messages}, fh, ensure_ascii=False, indent=1)
        print("payloads written to", args.json)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
