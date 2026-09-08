#!/usr/bin/env python3
"""One place that decides which Discord channel a sales message goes to.

Before this module the destination was a default argument in whichever file
happened to send the message, and all three defaults pointed at #maya-office
(or #alerts). That is how an operator's private channel became the dumping
ground for a 554-lead CSV, the ORBIT daily report and the human-review backlog
at once: nobody chose it, it was just the constant nearest to hand.

Resolution order for every route, most specific first:

  1. the route's own environment variable (``SALES_LEADS_CHANNEL``, …) — a
     one-off override without touching state;
  2. a legacy variable the route used to answer to, so an operator who already
     set ``ORBIT_REPORT_DISCORD_CHANNEL`` keeps working unchanged;
  3. ``$HERMES_HOME/state/sales_channels.json`` — the discovered, persisted
     map, which is what production actually uses;
  4. the built-in fallback below.

Ids live in state rather than in code because they are per-guild facts: a
second deployment, or a rebuilt server, has different ids and should not need
a code change. They are not secrets — a channel id is useless without the bot
token — so persisting them in plain JSON is fine, and it keeps them out of the
modules that do the sending.

A route that cannot be resolved returns "" rather than guessing. Callers must
treat "" as "do not post": silently falling back to a default channel is how
sales data ends up somewhere nobody is reading.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Dict, Optional

__all__ = ["ROUTES", "channel", "all_channels", "state_path", "describe",
           "load_state", "SALES_ROUTES"]

# route -> (env var, legacy env vars, built-in fallback id, human description)
ROUTES: Dict[str, dict] = {
    "sales_control": {
        "env": "SALES_CONTROL_CHANNEL", "legacy": (),
        "default": "", "desc": "MAYA control + concise status"},
    "sales_leads": {
        "env": "SALES_LEADS_CHANNEL", "legacy": ("NO_EMAIL_DISCORD_CHANNEL",),
        "default": "", "desc": "no-email leads, manual contact, CSV"},
    "sales_outreach": {
        "env": "SALES_OUTREACH_CHANNEL", "legacy": (),
        "default": "", "desc": "outbound execution + cycle summaries"},
    "sales_replies": {
        "env": "SALES_REPLIES_CHANNEL", "legacy": (),
        "default": "", "desc": "inbound replies + LEO classifications"},
    "sales_review": {
        "env": "SALES_REVIEW_CHANNEL",
        "legacy": ("REVIEW_ALERTS_DISCORD_CHANNEL", "AGENCY_DISCORD_ALERTS_CHANNEL"),
        "default": "", "desc": "human-review queue (SENTINEL cards)"},
    "sales_alerts": {
        "env": "SALES_ALERTS_CHANNEL", "legacy": (),
        "default": "", "desc": "sales incidents + recoveries"},
    "sales_analytics": {
        "env": "SALES_ANALYTICS_CHANNEL", "legacy": ("ORBIT_REPORT_DISCORD_CHANNEL",),
        "default": "", "desc": "ORBIT daily report + analytics"},
    "sales_mailboxes": {
        "env": "SALES_MAILBOXES_CHANNEL", "legacy": (),
        "default": "", "desc": "sender health + operator pause/resume console"},
    # Pre-existing channels, kept addressable so nothing has to hardcode them.
    "maya_office": {
        "env": "MAYA_OFFICE_CHANNEL", "legacy": (),
        "default": "1484778503529304145", "desc": "talk to MAYA directly"},
    "global_alerts": {
        "env": "GLOBAL_ALERTS_CHANNEL", "legacy": (),
        "default": "1484778510383054898", "desc": "shared, non-sales incidents"},
}

SALES_ROUTES = tuple(k for k in ROUTES if k.startswith("sales_"))


def state_path(home: Optional[pathlib.Path] = None) -> pathlib.Path:
    base = home or pathlib.Path(os.getenv("HERMES_HOME", "/opt/data"))
    return base / "state" / "sales_channels.json"


def load_state(home: Optional[pathlib.Path] = None) -> Dict[str, str]:
    """The persisted route -> channel id map. Never raises."""
    try:
        raw = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    chans = raw.get("channels") if isinstance(raw, dict) else None
    if not isinstance(chans, dict):
        return {}
    return {str(k): str(v) for k, v in chans.items() if v}


def channel(route: str, *, home: Optional[pathlib.Path] = None) -> str:
    """Channel id for a logical route, or "" when it is not configured."""
    spec = ROUTES.get(route)
    if not spec:
        return ""
    value = os.getenv(spec["env"], "").strip()
    if value:
        return value
    for legacy in spec["legacy"]:
        value = os.getenv(legacy, "").strip()
        if value:
            return value
    value = load_state(home).get(route, "").strip()
    if value:
        return value
    return str(spec["default"] or "").strip()


def all_channels(*, home: Optional[pathlib.Path] = None) -> Dict[str, str]:
    return {r: channel(r, home=home) for r in ROUTES}


def describe(*, home: Optional[pathlib.Path] = None) -> str:
    """Human-readable routing table, for the cron log and `--routes`."""
    rows = []
    width = max(len(r) for r in ROUTES)
    for route, spec in ROUTES.items():
        cid = channel(route, home=home)
        rows.append("  %-*s %-22s %s" % (width, route, cid or "(unconfigured)", spec["desc"]))
    return "\n".join(rows)


if __name__ == "__main__":
    print(describe())
