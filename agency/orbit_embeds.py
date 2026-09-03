#!/usr/bin/env python3
"""ORBIT's daily report as Discord embed cards.

Presentation only. Every figure is read from the metrics dict that
orbit.collect() already produces and from the no-email list that
no_email_report.build() already produces. Nothing is computed here beyond
formatting: no query, no rate, no state is decided in this file, so the cards
cannot disagree with the plaintext report that stays in the cron log.

The cards are posted straight to Discord as embeds, several per message, and
tracked in the same delivery ledger the no-email list uses. A rerun on the
same day sends only what has not landed, so a mid-way failure never produces
a duplicate card.

Discord has no CSS. What it does have is used: an accent colour per card that
follows its status, inline fields that line up as columns, progress bars,
a footer per card, and one topic per card.

    python3 orbit_embeds.py preview                 # real data, nothing posted
    python3 orbit_embeds.py preview --json out.json # also dump the payloads
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- Discord limits ----------------------------------------------------------
# https://discord.com/developers/docs/resources/message#embed-object-embed-limits
TITLE_MAX = 256
DESC_MAX = 4096
FIELDS_MAX = 25
NAME_MAX = 256
VALUE_MAX = 1024
FOOTER_MAX = 2048
EMBED_CHARS_MAX = 6000          # summed over every embed in ONE message
EMBEDS_PER_MESSAGE = 10
# Headroom under the hard limits, so an edge case in Discord's own counting
# (it counts some markdown differently) never bounces a message.
MESSAGE_CHARS_TARGET = int(os.getenv("ORBIT_EMBED_CHARS_TARGET", "5600"))
LONG_VALUE_MAX = int(os.getenv("ORBIT_EMBED_LONG_VALUE", "600"))

# Status-oriented accent colours. Discord takes an integer RGB.
COLOR = {
    "healthy": 0x2ECC71,    # green
    "info": 0x3498DB,       # blue
    "warning": 0xF39C12,    # orange
    "critical": 0xE74C3C,   # red
    "hot": 0xE67E22,
    "warm": 0xF1C40F,
    "neutral": 0x95A5A6,
}

SECTION = "orbit_embeds"    # ledger section, distinct from the old plaintext parts
DISCORD_API = "https://discord.com/api/v10"
CHANNEL_ID = os.getenv("ORBIT_REPORT_DISCORD_CHANNEL",
                       os.getenv("NO_EMAIL_DISCORD_CHANNEL", "1484778503529304145"))
POST_GAP_SECONDS = float(os.getenv("ORBIT_EMBED_POST_GAP", "1.1"))

JOB_LABELS = {
    "maya-orchestrator": "MAYA Orchestrator",
    "supabase-lead-sync": "Lead Sync",
    "email-verifier": "Email Verifier",
    "echo-followups": "ECHO Follow-ups",
    "leo-inbound": "LEO Inbound",
    "review-alerts": "Review Alerts",
    "orbit-daily": "ORBIT Daily",
    "tenant-health": "Tenant Health",
}

CHANNEL_ICONS = [
    ("whatsapp", "📱 WhatsApp"),
    ("phone", "📞 Call / SMS"),
    ("instagram_url", "📸 Instagram DM"),
    ("facebook_url", "💬 Facebook Message"),
    ("website", "🌐 Website Contact Form"),
    ("google_maps_url", "📍 Google Maps"),
]
PREFERRED_LABEL = {
    "whatsapp": "📱 WhatsApp Preferred",
    "phone": "📞 Call / SMS",
    "instagram_url": "📸 Instagram DM",
    "facebook_url": "💬 Facebook Message",
    "website": "🌐 Website Contact Form",
    "google_maps_url": "📍 Google Maps",
}
LEAD_FOOTER = "NO EMAIL — MANUAL CONTACT REQUIRED"

_GMAIL_RE = re.compile(r"[\w.+-]+@(?:gmail|googlemail)\.com", re.I)


# --- small formatting helpers -----------------------------------------------

def _clip(s: Any, n: int) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def num(v: Any, default: str = "n/a") -> str:
    return default if v is None else _fmt(v)


def _link(url: Any, label: Optional[str] = None) -> str:
    """A markdown link for a URL, labelled by its host unless told otherwise;
    anything that is not a URL is returned as it is."""
    u = str(url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        return u
    if not label:
        host = re.sub(r"^https?://(www\.)?", "", u, flags=re.I).split("/")[0]
        label = host or u
    return "[%s](%s)" % (label, u)


def pct(v: Optional[float]) -> str:
    return "n/a" if v is None else "%.1f%%" % v


def bar(done: Any, total: Any, width: int = 10) -> str:
    """A progress bar that renders in every Discord client."""
    try:
        done, total = float(done or 0), float(total or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    filled = int(round(width * min(1.0, done / total)))
    return "▰" * filled + "▱" * (width - filled)


def fmt_day(day: Optional[str]) -> str:
    """'2026-09-02' -> '02 Sep 2026'; anything else is shown as it is."""
    try:
        return datetime.date.fromisoformat(str(day)[:10]).strftime("%d %b %Y")
    except Exception:
        return str(day or "—")


def job_label(name: str) -> str:
    return JOB_LABELS.get(name) or name.replace("-", " ").title()


def parse_identity(text: str) -> Tuple[str, str]:
    """'Lisa Chen <demon@socialnexa.cv>' -> ('Lisa Chen', 'demon@socialnexa.cv')."""
    text = (text or "").strip()
    m = re.match(r"^(.*?)\s*<([^>]+)>\s*$", text)
    if m:
        return m.group(1).strip().strip('"'), m.group(2).strip()
    return "", text


# --- embed construction, always within Discord's limits ---------------------

def field(name: str, value: Any, inline: bool = True) -> Dict[str, Any]:
    value = "" if value is None else str(value)
    return {"name": _clip(name, NAME_MAX) or "​",
            "value": _clip(value, VALUE_MAX) or "—",
            "inline": bool(inline)}


def embed_chars(e: Dict[str, Any]) -> int:
    """The characters Discord counts against the 6000-per-message limit."""
    n = len(e.get("title") or "") + len(e.get("description") or "")
    n += len((e.get("footer") or {}).get("text") or "")
    n += len((e.get("author") or {}).get("name") or "")
    for f in e.get("fields") or []:
        n += len(f.get("name") or "") + len(f.get("value") or "")
    return n


def embed(title: str, description: Optional[str] = None,
          fields: Optional[Iterable[Dict[str, Any]]] = None,
          color: int = COLOR["info"], footer: Optional[str] = None) -> Dict[str, Any]:
    """One card, clipped to Discord's limits. Empty fields are dropped."""
    e: Dict[str, Any] = {"title": _clip(title, TITLE_MAX), "color": int(color)}
    if description:
        e["description"] = _clip(description, DESC_MAX)
    kept = [f for f in (fields or []) if f and (f.get("value") or "").strip() not in ("", "—")]
    if kept:
        e["fields"] = kept[:FIELDS_MAX]
    if footer:
        e["footer"] = {"text": _clip(footer, FOOTER_MAX)}
    # A single card must fit a message on its own. Shrink the longest values
    # first; a card never fails to post because one field was verbose.
    while embed_chars(e) > MESSAGE_CHARS_TARGET and e.get("fields"):
        longest = max(e["fields"], key=lambda f: len(f["value"]))
        if len(longest["value"]) <= 40:
            break
        longest["value"] = _clip(longest["value"], max(40, len(longest["value"]) // 2))
    if embed_chars(e) > MESSAGE_CHARS_TARGET and e.get("description"):
        room = max(0, MESSAGE_CHARS_TARGET - (embed_chars(e) - len(e["description"])))
        e["description"] = _clip(e["description"], room)
    return e


def pack(embeds: List[Dict[str, Any]], per_message: int = EMBEDS_PER_MESSAGE,
         chars: int = MESSAGE_CHARS_TARGET) -> List[List[Dict[str, Any]]]:
    """Group cards into messages: at most `per_message` cards and `chars`
    counted characters each, in the order given."""
    out: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    cur_chars = 0
    for e in embeds:
        n = embed_chars(e)
        if cur and (len(cur) >= per_message or cur_chars + n > chars):
            out.append(cur)
            cur, cur_chars = [], 0
        cur.append(e)
        cur_chars += n
    if cur:
        out.append(cur)
    return out


# --- sender identities: the names prospects see, never the transport --------

def sender_identities(db: Optional[str] = None,
                      m: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """{'by_user': {user_id: {'name','email'}}, 'by_transport': {gmail: email}}.

    Read from what already exists, in this order of trust: an explicit
    ORBIT_SENDER_IDENTITIES mapping if the operator set one, the identity
    MailHub reported on messages the orchestrator confirmed as sent (agency.db
    messages.from_email, per tenant), and the identity fields MailHub returns
    on the one mailbox ORBIT's own key can see. Read-only.
    """
    by_user: Dict[int, Dict[str, str]] = {}
    by_transport: Dict[str, str] = {}
    tenants = (m or {}).get("tenants") or []
    transport_of = {int(t["user_id"]): (t.get("mailbox_email") or "").lower()
                    for t in tenants if t.get("user_id") is not None}

    raw = os.getenv("ORBIT_SENDER_IDENTITIES", "").strip()
    if raw:
        try:
            for uid, ident in json.loads(raw).items():
                name, email = parse_identity(ident)
                if email and not _GMAIL_RE.fullmatch(email):
                    by_user[int(uid)] = {"name": name, "email": email}
        except Exception:
            pass

    path = db or os.getenv("AGENCY_DB", "/opt/data/agency.db")
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            rows = con.execute(
                "SELECT tenant_user_id, from_email FROM messages"
                " WHERE from_email IS NOT NULL AND tenant_user_id IS NOT NULL"
                "   AND direction='outbound' AND status IN ('sent','simulated')"
                " ORDER BY COALESCE(sent_at, updated_at) DESC").fetchall()
        finally:
            con.close()
        for uid, ident in rows:
            uid = int(uid)
            if uid in by_user:
                continue
            name, email = parse_identity(ident)
            if email and not _GMAIL_RE.fullmatch(email):
                by_user[uid] = {"name": name, "email": email}
    except Exception:
        pass

    for a in (m or {}).get("senders") or []:
        if a.get("identity_status") == "verified" and a.get("from_email"):
            mailbox = (a.get("email") or "").lower()
            for uid, tr in transport_of.items():
                if tr == mailbox and uid not in by_user:
                    by_user[uid] = {"name": a.get("from_name") or "",
                                    "email": a["from_email"]}

    for uid, ident in by_user.items():
        tr = transport_of.get(uid)
        if tr:
            by_transport[tr] = ident["email"]
    return {"by_user": by_user, "by_transport": by_transport}


def hide_transport(text: str, identities: Optional[Dict[str, Any]] = None) -> str:
    """Replace any Gmail transport address with the professional identity that
    sends through it, or with a neutral phrase when that is not known."""
    by_transport = (identities or {}).get("by_transport") or {}

    def sub(match):
        return by_transport.get(match.group(0).lower()) or "a Gmail mailbox"
    return _GMAIL_RE.sub(sub, text or "")


# --- system status, derived from the same signals the plaintext lists -------

def automation_state(m: Dict[str, Any]) -> Dict[str, Any]:
    a = m.get("automation") or {}
    failed, stale, never = [], [], []
    for j in a.get("jobs") or []:
        st = (j.get("status") or "").lower()
        if st in ("error", "failed", "fail", "crashed"):
            failed.append(j["name"])
        elif j.get("last_run_at") is None:
            never.append(j["name"])
        elif j.get("stale"):
            stale.append(j["name"])
    return {"failed": failed, "stale": stale, "never": never,
            "error": a.get("error"), "duplicates": a.get("duplicates") or []}


def system_status(m: Dict[str, Any]) -> Tuple[str, str]:
    """('healthy'|'warning'|'critical', label)."""
    auto = automation_state(m)
    if auto["error"] or auto["failed"]:
        return "critical", "❌ Critical"
    warn = (auto["stale"] or auto["never"] or auto["duplicates"]
            or m.get("senders_error") or m.get("outbox_failed")
            or m.get("intake_error")
            or (m.get("verification") or {}).get("error")
            or (m.get("no_email") or {}).get("error"))
    if warn:
        return "warning", "⚠️ Attention"
    return "healthy", "✅ Healthy"


# --- the cards ---------------------------------------------------------------

def card_header(m: Dict[str, Any]) -> Dict[str, Any]:
    level, label = system_status(m)
    target = m.get("intake_target")
    today = m.get("intake_today")
    if target is not None and today is not None:
        target_txt = "%d / %d  %s" % (today, target, bar(today, target))
        if today >= target:
            target_txt += "  ✅"
    else:
        target_txt = "not configured"
    return embed(
        "📊 HERMES AGENCY — DAILY REPORT",
        "Daily Agency Operations Dashboard",
        [field("Date", fmt_day(m.get("intake_day"))),
         field("Timezone", m.get("timezone") or "operational day"),
         field("System Status", label),
         field("Daily Target", target_txt),
         field("Sender Capacity", "%s remaining" % num(m.get("capacity_usable"), "n/a"))],
        color=COLOR[level], footer="Generated automatically by ORBIT")


def card_leads(m: Dict[str, Any]) -> Dict[str, Any]:
    st = m.get("by_state") or {}
    ready = m.get("supabase_ready")
    target, today = m.get("intake_target"), m.get("intake_today")
    return embed(
        "📥 LEADS", None,
        [field("Supabase Available", "unavailable" if ready is None else ready),
         field("Daily Target", ("%d / %d" % (today, target))
               if target is not None and today is not None else "not configured"),
         field("Remaining Target", num(m.get("intake_remaining"))),
         field("In Hermes", m.get("leads", 0)),
         field("Researching", st.get("RESEARCHING", 0) + st.get("RESEARCH_PENDING", 0)),
         field("Research Completed", m.get("research_complete", 0)),
         field("Research Review", m.get("research_failed", 0))],
        color=COLOR["info"],
        footer=("Daily target reached — no further leads are claimed until the next operational day"
                if target is not None and today is not None and today >= target else None))


def card_outreach(m: Dict[str, Any], identities: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    st = m.get("by_state") or {}
    fields = [field("Copy Ready", st.get("COPY_READY", 0)),
              field("QA Approved", st.get("READY_TO_SEND", 0)),
              field("QA Rejected", st.get("QA_REJECTED", 0)),
              field("Queued", m.get("queued", 0)),
              field("Sent", m.get("outbound_total", 0)),
              field("Initial", m.get("initial_sent", 0)),
              field("Follow-ups", m.get("followups_sent", 0)),
              field("Failed", m.get("send_failures", 0))]
    sent_as = [(addr, n) for addr, n in (m.get("sent_as") or [])]
    if sent_as:
        lines = []
        for addr, n in sent_as:
            shown = hide_transport(addr, identities) if addr else "(sender not recorded)"
            lines.append("%s — %d" % (shown, n))
        fields.append(field("Sent As", "\n".join(lines), inline=False))
    return embed("✉️ OUTREACH", None, fields,
                 color=COLOR["warning"] if m.get("send_failures") else COLOR["info"],
                 footer="Counts are cumulative since launch")


def card_replies(m: Dict[str, Any]) -> Dict[str, Any]:
    cls = m.get("by_classification") or {}
    return embed(
        "💬 REPLIES", None,
        [field("Contacted", m.get("leads_contacted", 0)),
         field("Replied", m.get("leads_replied", 0)),
         field("Total Inbound", m.get("replies", 0)),
         field("Positive", cls.get("positive", 0) + cls.get("interested", 0)),
         field("Pricing", cls.get("pricing_question", 0)),
         field("Meetings", cls.get("meeting_request", 0)),
         field("Negative", cls.get("negative", 0)),
         field("Out of Office", cls.get("out_of_office", 0)),
         field("Unsubscribe", m.get("unsubscribes", 0)),
         field("Bounced", m.get("bounces", 0)),
         field("Human Review", m.get("human_reviews_open", 0))],
        color=COLOR["info"], footer="Counts are cumulative since launch")


def card_performance(m: Dict[str, Any]) -> Dict[str, Any]:
    r = m.get("rates") or {}
    n = m.get("sample") or 0
    min_sample = m.get("min_sample") or 20
    desc = ("⚠️ Small sample size — rates are not statistically meaningful yet."
            if n < min_sample else None)
    bounce = r.get("bounce_rate") or 0
    color = COLOR["warning"] if (bounce > 3 and n >= min_sample) else COLOR["info"]
    return embed(
        "📈 PERFORMANCE", desc,
        [field("Reply Rate", pct(r.get("reply_rate"))),
         field("Positive Rate", pct(r.get("positive_reply_rate"))),
         field("Meeting Rate", pct(r.get("meeting_rate"))),
         field("Bounce Rate", pct(r.get("bounce_rate")))],
        color=color, footer="Rates are per lead contacted (%d)" % n)


def _sender_field(t: Dict[str, Any], ident: Optional[Dict[str, str]]) -> Dict[str, Any]:
    limit = t.get("daily_limit") or 0
    sent = t.get("sent_today") or 0
    remaining = t.get("remaining")
    if remaining is None:
        remaining = max(0, limit - sent)
    if t.get("ready") is False or (t.get("ready") is None and not t.get("mailbox_ok")):
        missing = [n for n, c in (("queue", "queue_ok"), ("approve", "approve_ok"),
                                  ("leo", "leo_ok"), ("mailbox", "mailbox_ok"))
                   if not t.get(c)]
        status = "⚠️ Not ready" + (" (missing: %s)" % ", ".join(missing) if missing else "")
    elif limit and sent >= limit:
        status = "⛔ Daily cap reached"
    else:
        status = "✅ Ready"
    name = (ident or {}).get("name") or ""
    email = (ident or {}).get("email") or ""
    title = name or (email.split("@")[0] if email else "Sender %s" % t.get("user_id", "?"))
    lines = [email or "identity not yet recorded",
             "Sent: %d / %d  %s" % (sent, limit, bar(sent, limit)),
             "Remaining: %d" % remaining,
             "Status: %s" % status]
    return field(title, "\n".join(lines), inline=True)


def card_senders(m: Dict[str, Any], identities: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    by_user = (identities or {}).get("by_user") or {}
    tenants = sorted(m.get("tenants") or [], key=lambda t: int(t.get("user_id") or 0))
    fields = [field("Active Senders", "%d / %d" % (m.get("tenants_ready") or 0, len(tenants))),
              field("Configured Capacity", "%d/day" % (m.get("capacity_configured") or 0)),
              field("Remaining Today", m.get("capacity_usable") or 0)]
    desc = None
    stale_hours = None
    for t in tenants:
        ts = t.get("mailbox_checked_at")
        if ts:
            try:
                checked = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=datetime.timezone.utc)
                age = (datetime.datetime.now(datetime.timezone.utc) - checked).total_seconds() / 3600
                stale_hours = max(stale_hours or 0, age)
            except Exception:
                pass
    if stale_hours is not None and stale_hours > 3:
        desc = "⚠️ Sender figures were last refreshed %dh ago." % int(stale_hours)
    elif tenants and stale_hours is None:
        desc = "⚠️ Sender figures have not been refreshed yet."
    if tenants:
        for t in tenants:
            fields.append(_sender_field(t, by_user.get(int(t.get("user_id") or 0))))
    elif m.get("senders"):
        # Only ORBIT's own mailbox is visible through its key; identity fields
        # come straight from MailHub for it.
        for a in m["senders"]:
            ident = ({"name": a.get("from_name") or "", "email": a.get("from_email") or ""}
                     if a.get("identity_status") == "verified" and a.get("from_email") else None)
            fields.append(_sender_field(
                {"daily_limit": a.get("effective_daily_limit") or a.get("daily_limit"),
                 "sent_today": a.get("sent_today"), "ready": bool(a.get("enabled")),
                 "mailbox_ok": bool(a.get("enabled"))}, ident))
    else:
        desc = (desc + "\n" if desc else "") + "No sender data available."
    ready = m.get("tenants_ready") or 0
    # Green only when every tenant is ready AND the figures are fresh: a card
    # that carries a warning line is not a green card.
    color = COLOR["healthy"] if (tenants and ready == len(tenants) and not desc) \
        else COLOR["warning"]
    return embed("📬 SENDER CAPACITY", desc, fields, color=color,
                 footer="Professional sender identities; transport mailboxes are never shown")


def card_verification(m: Dict[str, Any]) -> Dict[str, Any]:
    v = m.get("verification") or {}
    if v.get("error"):
        return embed("✅ EMAIL VERIFICATION", "⚠️ unavailable: %s" % v["error"],
                     color=COLOR["warning"])
    return embed(
        "✅ EMAIL VERIFICATION", None,
        [field("Pending", num(v.get("pending"))),
         field("Valid", num(v.get("valid"))),
         field("Invalid", num(v.get("invalid"))),
         field("Risky / Held", num(v.get("risky"))),
         field("Unknown / Retry", num(v.get("unknown"))),
         field("Pass Rate", pct(m.get("verification_pass_rate")))],
        color=COLOR["healthy"], footer="Only VALID emails enter automated outreach.")


def card_automation(m: Dict[str, Any]) -> Dict[str, Any]:
    a = m.get("automation") or {}
    state = automation_state(m)
    if a.get("error"):
        return embed("⚙️ AUTOMATION HEALTH", "❌ cron store unreadable: %s" % a["error"],
                     color=COLOR["critical"])
    fields = []
    # The pipeline's own order (orchestrator first, report last), unknown jobs
    # after, by name.
    order = {name: i for i, name in enumerate(JOB_LABELS)}
    jobs = sorted(a.get("jobs") or [],
                  key=lambda j: (order.get(j["name"], len(order)), j["name"]))
    for j in jobs:
        name = j["name"]
        age = j.get("age_minutes")
        if name in state["failed"]:
            value = "❌ Last execution failed"
        elif name in state["never"]:
            value = "⚠️ Never run"
        elif name in state["stale"]:
            value = "⚠️ Last run %s ago" % ("%dm" % age if age is not None else "?")
        else:
            value = "✅ Healthy"
        fields.append(field(job_label(name), value))
    if not fields:
        return embed("⚙️ AUTOMATION HEALTH", "⚠️ no scheduled jobs found", color=COLOR["warning"])
    desc = None
    if state["duplicates"]:
        desc = "⚠️ Duplicate job names: %s" % ", ".join(job_label(x) for x in state["duplicates"])
    color = (COLOR["critical"] if state["failed"]
             else COLOR["warning"] if (state["stale"] or state["never"] or state["duplicates"])
             else COLOR["healthy"])
    return embed("⚙️ AUTOMATION HEALTH", desc, fields, color=color)


def card_pipeline(m: Dict[str, Any]) -> Dict[str, Any]:
    desc = None
    unmatched = m.get("replies_unmatched") or 0
    if unmatched:
        desc = ("⚠️ %d inbound repl%s no matching recorded send and %s excluded from "
                "performance metrics." % (unmatched, "y has" if unmatched == 1 else "ies have",
                                          "is" if unmatched == 1 else "are"))
    if m.get("intake_error"):
        desc = (desc + "\n" if desc else "") + "⚠️ Intake metrics unavailable: %s" % m["intake_error"]
    color = COLOR["warning"] if (m.get("outbox_failed") or unmatched or m.get("intake_error")) \
        else COLOR["healthy"]
    return embed(
        "🔄 PIPELINE HEALTH", desc,
        [field("Supabase Mapped", num(m.get("supabase_mapped"))),
         field("Sync Pending", num(m.get("outbox_pending"))),
         field("Sync Failures", num(m.get("outbox_failed"))),
         field("Human Review", m.get("human_reviews_open", 0))],
        color=color)


def attention_items(m: Dict[str, Any], identities: Optional[Dict[str, Any]] = None) -> List[str]:
    """What needs a person. Same signals the plaintext lists, minus the
    small-sample note, which lives on the performance card."""
    items: List[str] = []
    state = automation_state(m)
    if state["error"]:
        items.append("Cron store unreadable: %s" % state["error"])
    if state["failed"]:
        items.append("Failed jobs: %s" % ", ".join(job_label(x) for x in state["failed"]))
    if state["never"]:
        items.append("Never run: %s" % ", ".join(job_label(x) for x in state["never"]))
    if state["stale"]:
        items.append("Stale jobs (not run recently): %s — check the gateway"
                     % ", ".join(job_label(x) for x in state["stale"]))
    if m.get("research_failed"):
        items.append("%d NOVA research failure(s) need review" % m["research_failed"])
    if m.get("human_reviews_open"):
        items.append("%d escalation(s) need a decision — `review list`" % m["human_reviews_open"])
    v = m.get("verification") or {}
    if v.get("risky"):
        items.append("%d role-account / risky emails are held" % v["risky"])
    if v.get("unknown"):
        items.append("%d verification result(s) waiting for retry" % v["unknown"])
    if v.get("error"):
        items.append("Email verification unavailable: %s" % v["error"])
    if m.get("outbox_failed"):
        items.append("%d Supabase write-back(s) gave up after retrying — `supabase_sync.py reconcile`"
                     % m["outbox_failed"])
    if m.get("replies_unmatched"):
        items.append("%d inbound repl%s no matching recorded send"
                     % (m["replies_unmatched"], "y has" if m["replies_unmatched"] == 1 else "ies have"))
    r = m.get("rates") or {}
    if (r.get("bounce_rate") or 0) > 3 and (m.get("sample") or 0) >= (m.get("min_sample") or 20):
        items.append("Bounce rate above 3% — pause and check list quality")
    for w in (m.get("sender_warnings") or [])[:3]:
        items.append(hide_transport(w, identities))
    if m.get("senders_error"):
        items.append("Sender health unavailable: %s" % m["senders_error"])
    if (m.get("no_email") or {}).get("error"):
        items.append("No-email lead list unavailable: %s" % m["no_email"]["error"])
    if m.get("intake_error"):
        items.append("Intake metrics unavailable: %s" % m["intake_error"])
    return items


def card_attention(m: Dict[str, Any], identities: Optional[Dict[str, Any]] = None
                   ) -> Optional[Dict[str, Any]]:
    items = attention_items(m, identities)
    if not items:
        return None
    level, _ = system_status(m)
    return embed("🚨 NEEDS ATTENTION", "\n".join("• %s" % x for x in items),
                 color=COLOR["critical"] if level == "critical" else COLOR["warning"])


def card_summary(m: Dict[str, Any]) -> Dict[str, Any]:
    level, label = system_status(m)
    return embed(
        "📌 TODAY'S SUMMARY", None,
        [field("Sent", m.get("outbound_total", 0)),
         field("Replies", m.get("leads_replied", 0)),
         field("Meetings", m.get("meetings", 0)),
         field("Failures", m.get("send_failures", 0)),
         field("Bounces", m.get("bounces", 0)),
         field("Remaining Capacity", num(m.get("capacity_usable"), "n/a")),
         field("System", label)],
        color=COLOR[level], footer="Cumulative counts since launch; capacity is today's")


def card_no_email_header(s: Dict[str, Any]) -> Dict[str, Any]:
    if s.get("error"):
        return embed("📞 NO EMAIL — MANUAL CONTACT REQUIRED",
                     "⚠️ unavailable: %s" % s["error"], color=COLOR["warning"])
    fields = [field("Total No-Email Leads", num(s.get("total"))),
              field("New Today", num(s.get("new_today"))),
              field("Still Missing", num(s.get("still_missing")))]
    for label, key in (("By Country", "by_country"), ("Top Cities", "by_city")):
        d = s.get(key) or {}
        if d:
            fields.append(field(label, ", ".join("%s: %d" % kv for kv in list(d.items())[:5])))
    fields.append(field(
        "Owner Action",
        "Contact these businesses manually using the available Phone, WhatsApp, "
        "Instagram, Facebook, Website or Google Maps details.", inline=False))
    return embed("📞 NO EMAIL — MANUAL CONTACT REQUIRED", None, fields,
                 color=COLOR["warning"] if s.get("total") else COLOR["healthy"],
                 footer="One card per lead follows, then the full list as CSV")


def available_channels(lead: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [(key, label) for key, label in CHANNEL_ICONS if not _blank(lead.get(key))]


def lead_card(lead: Dict[str, Any]) -> Dict[str, Any]:
    """One lead as one card. Only fields the row actually has."""
    pr = str(lead.get("priority") or "").strip().lower()
    icon = "🔥" if pr in ("hot", "high") else "⭐" if pr in ("warm", "medium") else "📇"
    color = COLOR["hot"] if pr in ("hot", "high") else COLOR["warm"] if pr in ("warm", "medium") \
        else COLOR["info"]
    name = lead.get("business_name") or lead.get("contact_name") or lead.get("id") or "Lead"
    fields: List[Dict[str, Any]] = []

    def add(label, value, inline=True, long=False):
        if _blank(value):
            return
        v = _fmt(value)
        fields.append(field(label, _clip(v, LONG_VALUE_MAX) if long else v, inline))

    btype = lead.get("business_type") or lead.get("niche")
    add("Business Type", btype)
    if lead.get("niche") and lead.get("business_type") and lead["niche"] != lead["business_type"]:
        add("Niche", lead["niche"])
    area = lead.get("area_locality")
    loc = ", ".join(x for x in (lead.get("city"), lead.get("country")) if not _blank(x))
    if not _blank(area):
        add("Area", area)
        if loc and loc.lower() != str(area).strip().lower():
            add("Location", loc)
    else:
        add("Location", loc)
    add("Priority", (lead.get("priority") or "").strip().title() if lead.get("priority") else None)
    add("Score", lead.get("score"))
    if not _blank(lead.get("rating")):
        rev = lead.get("review_count")
        add("Rating", "%s ★%s" % (_fmt(lead["rating"]),
                                  " (%s reviews)" % _fmt(rev) if not _blank(rev) else ""))
    add("Phone", lead.get("phone"))
    add("WhatsApp", lead.get("whatsapp"))
    # Links carry a short label; a bare 300-character maps URL is not a card.
    add("Website", _link(lead.get("website")))
    add("Instagram", _link(lead.get("instagram_url"), "Instagram profile"))
    add("Facebook", _link(lead.get("facebook_url"), "Facebook page"))
    add("Google Maps", _link(lead.get("google_maps_url"), "Open in Google Maps"))
    add("Owner", lead.get("owner_name") or lead.get("contact_name"))
    add("Main Services", lead.get("main_services"), inline=False, long=True)
    add("Main Opportunity", lead.get("main_opportunity"), inline=False, long=True)
    add("Recommended Offer", lead.get("recommended_offer"), inline=False, long=True)

    chans = available_channels(lead)
    if chans:
        add("Manual Contact", PREFERRED_LABEL[chans[0][0]], inline=True)
        if len(chans) > 1:
            add("Channels", "  ·  ".join(label for _, label in chans), inline=False)
    else:
        add("Manual Contact", "❌ No contact details on record")
    return embed("%s %s" % (icon, name), None, fields, color=color, footer=LEAD_FOOTER)


def lead_cards(leads: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One card per lead, each lead once, in the order given."""
    seen = set()
    out = []
    for lead in leads:
        key = lead.get("id") or lead.get("external_lead_id") or (
            (lead.get("business_name") or "").lower(), lead.get("phone"), lead.get("website"))
        if key in seen:
            continue
        seen.add(key)
        out.append(lead_card(lead))
    return out


# --- assembly ------------------------------------------------------------------

CARD_ORDER = ["header", "leads", "outreach", "replies", "performance", "senders",
              "verification", "automation", "pipeline", "attention", "summary"]


def build_report_cards(m: Dict[str, Any], identities: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Optional[Dict[str, Any]]]:
    """Every executive card by key, in the fixed order; `attention` is None
    when nothing needs attention."""
    return {
        "header": card_header(m),
        "leads": card_leads(m),
        "outreach": card_outreach(m, identities),
        "replies": card_replies(m),
        "performance": card_performance(m),
        "senders": card_senders(m, identities),
        "verification": card_verification(m),
        "automation": card_automation(m),
        "pipeline": card_pipeline(m),
        "attention": card_attention(m, identities),
        "summary": card_summary(m),
    }


def build_report_embeds(m: Dict[str, Any], identities: Optional[Dict[str, Any]] = None
                        ) -> List[Dict[str, Any]]:
    """The executive cards, in the fixed order. `attention` only when needed."""
    cards = build_report_cards(m, identities)
    return [cards[k] for k in CARD_ORDER if cards[k] is not None]


# Which executive cards travel together. Each group is re-packed against the
# limits anyway, so a verbose day splits a group rather than failing.
MESSAGE_GROUPS = [["header", "leads", "outreach", "replies"],
                  ["performance", "senders", "verification", "automation"],
                  ["pipeline", "attention", "summary"]]


def build_messages(m: Dict[str, Any], built: Optional[Dict[str, Any]] = None,
                   identities: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Every Discord message of the day, in order: executive cards (three
    messages), the no-email header, the lead cards, and the CSV."""
    cards = build_report_cards(m, identities)
    messages: List[Dict[str, Any]] = []
    for group in MESSAGE_GROUPS:
        group_cards = [cards[k] for k in group if cards.get(k) is not None]
        for chunk in pack(group_cards):
            messages.append({"embeds": chunk})

    ne = (built or {}).get("summary") or (m.get("no_email") or {})
    messages.append({"embeds": [card_no_email_header(ne)]})
    leads = (built or {}).get("leads") or []
    for chunk in pack(lead_cards(leads)):
        messages.append({"embeds": chunk})
    if built and built.get("csv"):
        messages.append({"content": "📎 NO EMAIL LEADS — full list as CSV (%d leads)" % len(leads),
                         "file": ("no_email_leads_%s.csv" % built.get("day", "today"),
                                  built["csv"], "text/csv")})
    return messages


def validate(messages: List[Dict[str, Any]]) -> List[str]:
    """Every limit, checked; an empty list means Discord will accept it."""
    problems = []
    for i, msg in enumerate(messages, 1):
        embeds = msg.get("embeds") or []
        if len(embeds) > EMBEDS_PER_MESSAGE:
            problems.append("message %d has %d embeds" % (i, len(embeds)))
        if sum(embed_chars(e) for e in embeds) > EMBED_CHARS_MAX:
            problems.append("message %d exceeds %d characters" % (i, EMBED_CHARS_MAX))
        for e in embeds:
            if len(e.get("title") or "") > TITLE_MAX:
                problems.append("title too long: %s" % e.get("title")[:30])
            if len(e.get("description") or "") > DESC_MAX:
                problems.append("description too long in %s" % e.get("title"))
            if len(e.get("fields") or []) > FIELDS_MAX:
                problems.append("too many fields in %s" % e.get("title"))
            for f in e.get("fields") or []:
                if len(f.get("name") or "") > NAME_MAX or len(f.get("value") or "") > VALUE_MAX:
                    problems.append("field too long in %s" % e.get("title"))
                if not (f.get("value") or "").strip():
                    problems.append("empty field %r in %s" % (f.get("name"), e.get("title")))
            if len((e.get("footer") or {}).get("text") or "") > FOOTER_MAX:
                problems.append("footer too long in %s" % e.get("title"))
        if not embeds and not msg.get("content"):
            problems.append("message %d is empty" % i)
    return problems


# --- delivery ------------------------------------------------------------------

def _hash(payload: Dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "file"}
    if payload.get("file"):
        body["file"] = [payload["file"][0], len(payload["file"][1])]
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:16]


def _send_with_retry(send: Callable, channel: str, payload: Dict[str, Any],
                     sleep: Callable = time.sleep, retries: int = 3) -> Tuple[int, Any]:
    """POST one message; honour Discord's 429 retry_after rather than failing."""
    body = {k: v for k, v in payload.items() if k in ("content", "embeds")}
    files = [payload["file"]] if payload.get("file") else None
    code, resp = 0, {}
    for attempt in range(retries + 1):
        code, resp = send("POST", "/channels/%s/messages" % channel, body, files)
        if code != 429:
            return code, resp
        wait = 2.0
        try:
            wait = float((resp or {}).get("retry_after") or wait)
        except (TypeError, ValueError):
            pass
        sleep(min(wait + 0.25, 30.0))
    return code, resp


def post_all(con: sqlite3.Connection, messages: List[Dict[str, Any]], day: str,
             channel: str = CHANNEL_ID, send: Optional[Callable] = None,
             sleep: Callable = time.sleep) -> Dict[str, Any]:
    """Deliver every message once, in order, resumably.

    The ledger row for (day, section, part) with a matching content hash means
    that message already reached Discord today; it is skipped. The first
    failure stops the run so ordering is preserved; the next run continues
    from that part without resending what landed.
    """
    import no_email_report as NE
    send = send or NE._discord
    total = len(messages)
    out = {"sent": 0, "skipped": 0, "failed": 0, "parts": total}
    for part_no, payload in enumerate(messages, 1):
        content_hash = _hash(payload)
        row = con.execute(
            "SELECT * FROM report_deliveries WHERE report_day=? AND section=? AND part_no=?",
            (day, SECTION, part_no)).fetchone()
        if row is not None and row["delivered_at"] and row["content_hash"] == content_hash:
            out["skipped"] += 1
            continue
        code, resp = _send_with_retry(send, channel, payload, sleep)
        ok = code in (200, 201)
        err = None
        if not ok:
            try:
                import email_verifier as EV
                err = EV.scrub(json.dumps(resp)[:200])
            except Exception:
                err = json.dumps(resp)[:200]
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
             (resp or {}).get("id") if ok else None,
             datetime.datetime.utcnow().replace(microsecond=0).isoformat() if ok else None,
             err))
        con.commit()
        out["sent" if ok else "failed"] += 1
        if not ok:
            return out
        if part_no < total and POST_GAP_SECONDS > 0:
            sleep(POST_GAP_SECONDS)
    return out


# --- preview -------------------------------------------------------------------

_COLOR_NAMES = {v: k for k, v in COLOR.items()}


def render_text(messages: List[Dict[str, Any]], max_leads: Optional[int] = None) -> str:
    """A plain-text picture of the cards, for a terminal or a review."""
    L = []
    shown_leads = 0
    for i, msg in enumerate(messages, 1):
        embeds = msg.get("embeds") or []
        chars = sum(embed_chars(e) for e in embeds)
        L.append("━━━━━━━━━━ MESSAGE %d  (%d embed%s, %d chars) ━━━━━━━━━━"
                 % (i, len(embeds), "" if len(embeds) == 1 else "s", chars))
        if msg.get("content"):
            L.append(msg["content"])
        skipped = 0
        for e in embeds:
            is_lead = (e.get("footer") or {}).get("text") == LEAD_FOOTER
            if is_lead:
                if max_leads is not None and shown_leads >= max_leads:
                    skipped += 1
                    continue
                shown_leads += 1
            L.append("┌ %s   [%s]" % (e.get("title"), _COLOR_NAMES.get(e.get("color"), hex(e.get("color", 0)))))
            if e.get("description"):
                for line in e["description"].splitlines():
                    L.append("│ %s" % line)
            for f in e.get("fields") or []:
                lines = f["value"].splitlines() or [""]
                L.append("│ %s%s: %s" % ("▸ " if f.get("inline") else "▾ ", f["name"], lines[0]))
                for extra in lines[1:]:
                    L.append("│     %s" % extra)
            if e.get("footer"):
                L.append("└ %s" % e["footer"]["text"])
            else:
                L.append("└")
        if skipped:
            L.append("… %d more lead card(s) in this message" % skipped)
        L.append("")
    return "\n".join(L)


def _load_env_for_preview() -> None:
    """The same two files the cron job reads, minus the Discord token."""
    import pathlib
    for path, keys in (("/opt/data/profiles/orbit/.env", ("MAILHUB_BASE_URL", "MAILHUB_API_TOKEN")),
                       ("/opt/data/.env", ("SUPABASE_URL", "SUPABASE_SECRET_KEY",
                                           "NO_EMAIL_DISCORD_CHANNEL"))):
        p = pathlib.Path(path)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in keys:
                os.environ.setdefault(key.strip(), value.strip())


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="ORBIT daily report as Discord embed cards")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("preview", help="render today's cards from real data; post nothing")
    pv.add_argument("--json", help="write the message payloads (without the CSV) here")
    pv.add_argument("--leads", type=int, default=3, help="lead cards to show in the text preview")
    args = ap.parse_args(argv)

    _load_env_for_preview()
    os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")
    import orbit
    import no_email_report as NE
    import supabase_sync as S
    m = orbit.collect()
    day = S.operational_day()
    built = NE.build(day)
    identities = sender_identities(m=m)
    messages = build_messages(m, built, identities)
    problems = validate(messages)
    print(render_text(messages, max_leads=args.leads))
    n_embeds = sum(len(x.get("embeds") or []) for x in messages)
    print("%d message(s), %d embed(s), %d lead card(s); limits: %s"
          % (len(messages), n_embeds, len(built["leads"]),
             "ok" if not problems else "; ".join(problems)))
    if args.json:
        dump = [{k: v for k, v in msg.items() if k != "file"} for msg in messages]
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"day": day, "messages": dump, "identities": identities["by_user"]},
                      fh, ensure_ascii=False, indent=1)
        print("payloads written to", args.json)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
