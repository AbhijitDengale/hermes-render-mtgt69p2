#!/usr/bin/env python3
"""One live card in #sales-mailboxes showing every sender, and the commands
that let the operator disable or enable one without a shell.

Pausing a mailbox has, until now, meant an engineer with SSH writing a
timestamp into tenant_health. That is a bad place for a control the operator
needs during an incident -- the bounce spike of 2026-09-04 was noticed hours
before anyone could act on it. So the numbers that justify the decision and
the switch that acts on it live in the same place.

WHAT THE CARD SHOWS
    per mailbox: state, sent (total / 24h), bounces, bounce rate, today's
    usage against its daily limit, and why it is paused if it is.

WHAT THE OPERATOR CAN TYPE
    pause t9 <reason>     disable a mailbox; reason is recorded
    resume t9             re-enable it
    status                repost the card now

DELIBERATE LIMITS
  * `resume` names exactly one mailbox. There is no `resume all`: the four
    mailboxes paused on 2026-09-04 were paused for four separate reputations,
    and a single command that revives all of them is precisely the mistake
    this console should make harder, not easier.
  * The card is only re-posted when something it says has changed, so a
    two-minute cron does not fill the channel.
  * Commands are consumed once. The last processed message id is persisted,
    so a redeploy cannot replay `resume t9` from three days ago.
  * Bot messages are ignored, so the console can never act on its own output.

Nothing here sends mail, changes limits, pacing, or lead state. It flips one
column, `tenant_health.paused_until`, and writes an audit event.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import sqlite3
from typing import Any, Dict, List, Optional

import outreach_brake as OB

__all__ = ["snapshot", "render", "fingerprint", "parse_command", "apply_command",
           "state_path", "load_state", "save_state", "PAUSE_FOREVER"]

# The same sentinel the 2026-09-04 containment used: a pause with no expiry,
# which only an explicit `resume` clears.
PAUSE_FOREVER = "2099-01-01 00:00:00"

STATE_FILE = "mailbox_console.json"

_CMD_RE = re.compile(
    r"^\s*(?P<verb>pause|resume|enable|disable|status)\b\s*"
    r"(?P<target>t?\d+)?\s*(?P<rest>.*)$", re.IGNORECASE)

# The global brake is a separate verb pair so it can never be confused with a
# per-mailbox one: `stop outreach` halts everything, `pause t9` halts one
# sender. A bare `stop t9` is deliberately not accepted — it reads both ways.
_BRAKE_RE = re.compile(
    r"^\s*(?P<verb>stop|start)\s+outreach\b\s*(?P<rest>.*)$", re.IGNORECASE)

HEALTHY, RISKY, BAD = "🟢", "🟡", "🔴"


def state_path(home: Optional[pathlib.Path] = None) -> pathlib.Path:
    return (home or pathlib.Path("/opt/data")) / "state" / STATE_FILE


def load_state(home: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    try:
        raw = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("card_message_id", "")
    raw.setdefault("card_fingerprint", "")
    raw.setdefault("last_command_message_id", "")
    return raw


def save_state(data: Dict[str, Any], home: Optional[pathlib.Path] = None) -> None:
    p = state_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


# ── the numbers ─────────────────────────────────────────────────────────────

def _mask(email: str) -> str:
    user, _, dom = (email or "").partition("@")
    return "%s***@%s" % (user[:3], dom) if dom else (email or "—")


def snapshot(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    """One row per mailbox: what it has done and whether it is live.

    Bounce rate is bounced-recipients over messages-sent for that mailbox. It
    is deliberately all-time rather than windowed: a mailbox whose reputation
    was damaged last week is still damaged today, and a 24-hour window would
    show a freshly-paused sender as perfectly healthy.
    """
    rows: List[Dict[str, Any]] = []
    for t in con.execute("SELECT * FROM tenant_health ORDER BY user_id"):
        d = dict(t)
        uid = d["user_id"]
        sent = con.execute(
            "SELECT COUNT(*) FROM messages WHERE tenant_user_id=? AND sent_at IS NOT NULL",
            (uid,)).fetchone()[0]
        sent_24h = con.execute(
            "SELECT COUNT(*) FROM messages WHERE tenant_user_id=? AND sent_at IS NOT NULL"
            "   AND replace(sent_at,'T',' ') > datetime('now','-24 hours')", (uid,)).fetchone()[0]
        bounced = con.execute(
            "SELECT COUNT(DISTINCT l.id) FROM leads l JOIN messages m ON m.lead_id=l.id"
            " WHERE m.tenant_user_id=? AND l.state='BOUNCED'", (uid,)).fetchone()[0]
        rate = (bounced * 100.0 / sent) if sent else 0.0
        paused_until = d.get("paused_until")
        rows.append({
            "user_id": uid,
            "mailbox": _mask(d.get("mailbox_email") or ""),
            "paused": bool(paused_until),
            "paused_until": paused_until,
            "paused_reason": (d.get("paused_reason") or "").strip(),
            "health": d.get("health") or "unknown",
            "sent": sent, "sent_24h": sent_24h,
            "bounced": bounced, "bounce_rate": round(rate, 1),
            "daily_limit": d.get("daily_limit"),
            "sent_today": d.get("sent_today"),
        })
    return rows


def _headline() -> str:
    """The first thing the operator reads: is anything going out at all."""
    b = OB.state()
    if not b["stopped"]:
        return "🟢 **Outreach running.** Live sender health; card updates in place."
    return ("⛔ **OUTREACH STOPPED** — %s\nNo new mail is being queued."
            % (b["reason"] or "no reason recorded"))


def _dot(row: Dict[str, Any]) -> str:
    if row["paused"]:
        return BAD
    if row["bounce_rate"] >= 5.0:
        return RISKY
    return HEALTHY


def fingerprint(rows: List[Dict[str, Any]]) -> str:
    """What the card would say. Changes only when the card should be redrawn."""
    import hashlib
    b = OB.state()
    parts = ["brake|%s|%s" % (b["stopped"], b["reason"])]
    parts += ["%s|%s|%s|%s|%s|%s" % (r["user_id"], r["paused"], r["sent"],
                                     r["bounced"], r["bounce_rate"], r["health"])
              for r in rows]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def render(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A single compact embed for every mailbox — not one card each."""
    live = [r for r in rows if not r["paused"]]
    off = [r for r in rows if r["paused"]]

    def line(r):
        return ("%s **t%-2s** `%s`\n"
                "     sent **%d** (24h %d) · bounced **%d** · rate **%.1f%%** · today %s/%s"
                % (_dot(r), r["user_id"], r["mailbox"], r["sent"], r["sent_24h"],
                   r["bounced"], r["bounce_rate"],
                   r["sent_today"] if r["sent_today"] is not None else "?",
                   r["daily_limit"] if r["daily_limit"] is not None else "?"))

    fields = []
    if live:
        fields.append({"name": "🟢 Active — %d mailbox(es)" % len(live),
                       "value": "\n".join(line(r) for r in live)[:1024], "inline": False})
    if off:
        body = []
        for r in off:
            reason = r["paused_reason"] or "no reason recorded"
            body.append("%s\n     ⛔ %s" % (line(r), reason[:80]))
        fields.append({"name": "🔴 Paused — %d mailbox(es)" % len(off),
                       "value": "\n".join(body)[:1024], "inline": False})

    total_sent = sum(r["sent"] for r in rows)
    total_bounced = sum(r["bounced"] for r in rows)
    overall = (total_bounced * 100.0 / total_sent) if total_sent else 0.0
    fields.append({
        "name": "Fleet", "inline": False,
        "value": "sent **%d** · bounced **%d** · overall rate **%.1f%%**"
                 % (total_sent, total_bounced, overall)})
    fields.append({
        "name": "Controls", "inline": False,
        "value": "`pause t9 reason here` — disable one mailbox\n"
                 "`resume t9` — re-enable it\n"
                 "`stop outreach <reason>` — halt ALL new queueing\n"
                 "`start outreach` — resume queueing\n"
                 "`status` — refresh this card"})

    return {
        "title": "Mailbox Console",
        "color": 0xED4245 if (off or OB.stopped()) else 0x57F287,
        "description": _headline(),
        "fields": fields,
        "footer": {"text": "Hermes · sales mailboxes"},
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# ── commands ────────────────────────────────────────────────────────────────

def parse_command(text: str) -> Optional[Dict[str, Any]]:
    """Return a command dict, or None when the message is just chat.

    Anything that is not recognisably a command is ignored rather than
    guessed at: this channel is also where a human will discuss what to do,
    and a stray sentence must never disable a mailbox.
    """
    if not text:
        return None
    brake = _BRAKE_RE.match(text.strip())
    if brake:
        return {"verb": "%s_outreach" % brake.group("verb").lower(),
                "reason": (brake.group("rest") or "").strip()}
    m = _CMD_RE.match(text.strip())
    if not m:
        return None
    verb = m.group("verb").lower()
    if verb in ("enable",):
        verb = "resume"
    if verb in ("disable",):
        verb = "pause"
    if verb == "status":
        return {"verb": "status"}
    target = (m.group("target") or "").strip()
    if not target:
        return {"verb": verb, "error": "name a mailbox, e.g. `%s t9`" % verb}
    try:
        uid = int(target.lower().lstrip("t"))
    except ValueError:
        return {"verb": verb, "error": "could not read a mailbox id from %r" % target}
    reason = (m.group("rest") or "").strip()
    return {"verb": verb, "user_id": uid, "reason": reason}


def apply_command(con: sqlite3.Connection, cmd: Dict[str, Any],
                  actor: str = "discord") -> str:
    """Apply one command. Returns the line to post back."""
    if cmd.get("error"):
        return "⚠️ %s" % cmd["error"]
    verb = cmd["verb"]
    if verb == "status":
        return ""

    # The global brake. Separate from the per-mailbox switches on purpose:
    # stopping outreach leaves every sender pause and lead hold exactly as it
    # was, and starting it again lifts only this one.
    if verb == "stop_outreach":
        r = OB.stop(cmd.get("reason") or "", by=actor)
        if not r.get("changed"):
            return "Outreach is already stopped."
        _audit(con, 0, "outreach.stop", r.get("reason", ""), actor)
        con.commit()
        return ("⛔ **Outreach STOPPED** — %s\n"
                "Leads keep their place. Mail already handed to MailHub is not recalled."
                % (r.get("reason") or "no reason given"))
    if verb == "start_outreach":
        r = OB.start(by=actor)
        if not r.get("changed"):
            return "Outreach is already running."
        _audit(con, 0, "outreach.start", "", actor)
        con.commit()
        return ("✅ **Outreach STARTED** — queueing resumes at normal pacing.\n"
                "Sender pauses and lead holds are unchanged.")

    uid = cmd["user_id"]
    row = con.execute("SELECT tenant_name, user_id, mailbox_email, paused_until"
                      " FROM tenant_health WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        return "⚠️ no mailbox t%s" % uid
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    keys = row.keys() if hasattr(row, "keys") else ("tenant_name", "user_id", "mailbox_email", "paused_until")
    d = dict(zip(keys, row)) if not hasattr(row, "keys") else dict(row)

    if verb == "pause":
        if d.get("paused_until"):
            return "t%s is already paused." % uid
        reason = cmd.get("reason") or "paused from Discord by %s" % actor
        con.execute("UPDATE tenant_health SET paused_until=?, paused_reason=?, paused_at=?"
                    " WHERE user_id=?", (PAUSE_FOREVER, reason[:200], now, uid))
        _audit(con, uid, "mailbox.pause", reason, actor)
        con.commit()
        return "⛔ **t%s paused** — %s" % (uid, reason[:120])

    # resume
    if not d.get("paused_until"):
        return "t%s is already active." % uid
    con.execute("UPDATE tenant_health SET paused_until=NULL, paused_reason=NULL,"
                " paused_at=NULL WHERE user_id=?", (uid,))
    _audit(con, uid, "mailbox.resume", cmd.get("reason") or "", actor)
    con.commit()
    return "✅ **t%s resumed** — it will be offered work on the next cycle." % uid


def _audit(con: sqlite3.Connection, uid: int, action: str, detail: str, actor: str) -> None:
    """Every flip is recorded. A sender that went live unexplained is a bug."""
    try:
        con.execute(
            "INSERT INTO events (lead_id, campaign_id, agent, event_type, detail)"
            " VALUES (NULL, NULL, ?, ?, ?)",
            (actor, action, json.dumps({"user_id": uid, "detail": detail[:200]})))
    except sqlite3.Error:
        pass
