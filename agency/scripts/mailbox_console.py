#!/usr/bin/env python3
"""Mailbox console tick. Hermes cron, --no-agent, every 2 minutes.

Two jobs, in this order:

  1. read any operator commands typed in #sales-mailboxes since last tick and
     apply them;
  2. redraw the status card, but only if what it would say has changed.

Commands are applied before the redraw so a `pause t9` is reflected in the
same card the operator is looking at, rather than a tick later.

Consuming a command exactly once matters more than delivering the card
promptly: the last handled message id is persisted before anything is acted
on, so a crash mid-tick cannot replay `resume t9` on the next run. Messages
from bots -- including MAYA's own confirmations -- are skipped, so the console
cannot act on its own output.

No model decides anything here. `pause` and `resume` are the operator's words,
applied literally.
"""

import os
import pathlib
import sys

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("HERMES_HOME", "/opt/data")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

import credentials as C          # noqa: E402
import discord_routing as R      # noqa: E402

HOME = pathlib.Path(os.environ["HERMES_HOME"])
C.export(("DISCORD_BOT_TOKEN",), home=HOME)

import mailbox_console as MC     # noqa: E402
import no_email_report as NE     # noqa: E402
import pipeline as P             # noqa: E402

CHANNEL = R.channel("sales_mailboxes", home=HOME)
if not CHANNEL:
    print("mailbox console: sales_mailboxes route is not configured; not posting")
    sys.exit(0)

state = MC.load_state(HOME)
out = []

# ── 1. commands ─────────────────────────────────────────────────────────────
after = state.get("last_command_message_id") or ""
path = "/channels/%s/messages?limit=50" % CHANNEL + (("&after=%s" % after) if after else "")
code, msgs = NE._discord("GET", path, None, None)
if code == 200 and isinstance(msgs, list):
    # Discord returns newest first; apply oldest first so two commands in a
    # row land in the order the operator typed them.
    for m in sorted(msgs, key=lambda x: int(x["id"])):
        state["last_command_message_id"] = m["id"]
        if (m.get("author") or {}).get("bot"):
            continue
        cmd = MC.parse_command(m.get("content") or "")
        if not cmd:
            continue
        author = ((m.get("author") or {}).get("username")) or "discord"
        with P.connect() as con:
            reply = MC.apply_command(con, cmd, actor=author)
        if reply:
            NE._discord("POST", "/channels/%s/messages" % CHANNEL, {"content": reply}, None)
            out.append("%s: %s" % (author, reply.replace("**", "")))
        if cmd.get("verb") == "status":
            state["card_fingerprint"] = ""      # force a redraw
    MC.save_state(state, HOME)

# ── 2. the card ─────────────────────────────────────────────────────────────
with P.connect() as con:
    rows = MC.snapshot(con)
fp = MC.fingerprint(rows)

if fp != state.get("card_fingerprint"):
    embed = MC.render(rows)
    msg_id = state.get("card_message_id")
    ok = False
    if msg_id:
        code, _ = NE._discord("PATCH", "/channels/%s/messages/%s" % (CHANNEL, msg_id),
                              {"embeds": [embed]}, None)
        ok = code in (200, 201)
    if not ok:
        code, resp = NE._discord("POST", "/channels/%s/messages" % CHANNEL,
                                 {"embeds": [embed]}, None)
        ok = code in (200, 201)
        if ok:
            state["card_message_id"] = (resp or {}).get("id") or msg_id
    if ok:
        state["card_fingerprint"] = fp
        MC.save_state(state, HOME)
        out.append("card %s (%d mailboxes, %d paused)"
                   % ("updated" if msg_id else "posted", len(rows),
                      sum(1 for r in rows if r["paused"])))

if out:
    print("mailbox console: " + "; ".join(out))
