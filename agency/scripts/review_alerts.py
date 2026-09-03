#!/usr/bin/env python3
"""Post human-review escalations to Discord as embed cards.

Hermes cron, --no-agent, delivered locally: this script posts the cards
itself, so the cron's own delivery is the run log rather than a second copy of
the alert wrapped in a "Cronjob Response" header.

No model decides what needs a person. That was settled deterministically when
the escalation row was written; this only shows what is already waiting.

A card is posted once. It is edited when what it would say has materially
changed, and otherwise left alone -- which is what stops a two-minute cron
reposting the same queue all day.
"""

import os
import sys

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

# The Discord token lives in the root env this job already runs in and is
# never printed.
for _line in open("/opt/data/.env", encoding="utf-8"):
    _k, _sep, _v = _line.partition("=")
    if _sep and _k.strip() in ("DISCORD_BOT_TOKEN", "REVIEW_ALERTS_DISCORD_CHANNEL",
                               "AGENCY_DISCORD_ALERTS_CHANNEL", "SUPABASE_URL",
                               "SUPABASE_SECRET_KEY"):
        os.environ.setdefault(_k.strip(), _v.strip())

import pipeline as P        # noqa: E402
import review_cards as RC   # noqa: E402

with P.connect() as con:
    rows = RC.pending(con)
    work = RC.to_post(rows)
    if not work:
        print("human review: %d open, nothing new or changed to post" % len(rows))
    else:
        problems = RC.validate([{"embeds": [RC.render(r)]} for r, _ in work])
        if problems:
            print("human review: NOT posted, payload outside Discord limits: %s"
                  % "; ".join(problems)[:400])
        else:
            res = RC.post(con, rows)
            print("human review: %d open — %d new, %d updated, %d unchanged, "
                  "%d failed%s" % (res["pending"], res["new"], res["updated"],
                                   res["unchanged"], res["failed"],
                                   ", digest posted" if res["digest"] else ""))
