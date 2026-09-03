#!/usr/bin/env python3
"""ORBIT's daily report. Hermes cron, --no-agent, delivered locally.

The report reaches Discord from here, as embed cards (orbit_embeds), not
through the cron's own delivery: that path wraps stdout in a "Cronjob
Response" header and posts one wall of text, which is exactly what the cards
replace. The job therefore delivers to local, so its log keeps the plaintext
report and the delivery summary, and nothing but the cards reaches the
channel.

Runs from the root profile because that is the one with Discord configured,
but reads ORBIT's own read-only MailHub credential from ORBIT's profile
env rather than the ambient environment. Putting that key in the root .env
would shadow MAYA's queue-capable token and quietly take away her ability to
send, so the narrower credential is loaded here and nowhere else.

Every figure comes from a query, not from a model, so the report cannot drift
from the data it describes.
"""

import os
import pathlib
import sys

ENV = pathlib.Path("/opt/data/profiles/orbit/.env")

# Only the two variables the report needs, and only if the process does not
# already carry them. Loading the whole file would import unrelated secrets.
if ENV.exists():
    for line in ENV.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key in ("MAILHUB_BASE_URL", "MAILHUB_API_TOKEN"):
            os.environ.setdefault(key, value.strip())

sys.path.insert(0, "/opt/data/agency")
os.environ.setdefault("AGENCY_DB", "/opt/data/agency.db")

import orbit  # noqa: E402

metrics = orbit.collect()
# The plaintext report stays in the cron's local log: same numbers, same
# order, useful when someone reads the history without Discord.
print(orbit.report(metrics))

# Cards to Discord, resumably. The Discord token is read from the root env
# this job already runs in and is never printed. A failure here is reported on
# stdout and never raises, so the log above is not lost with it.
try:
    for line in open("/opt/data/.env", encoding="utf-8"):
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("DISCORD_BOT_TOKEN", "SUPABASE_URL",
                                   "SUPABASE_SECRET_KEY", "NO_EMAIL_DISCORD_CHANNEL",
                                   "ORBIT_REPORT_DISCORD_CHANNEL"):
            os.environ.setdefault(key.strip(), value.strip())
    import no_email_report as NE
    import orbit_embeds as OE
    import pipeline as P
    import supabase_sync as S
    _day = S.operational_day()
    _built = NE.build(_day)
    _messages = OE.build_messages(metrics, _built, OE.sender_identities(m=metrics))
    _problems = OE.validate(_messages)
    if _problems:
        print("")
        print("DISCORD CARDS: not sent, payload outside Discord limits: %s"
              % "; ".join(_problems)[:400])
    else:
        with P.connect() as _con:
            _res = OE.post_all(_con, _messages, _day)
        _stamped = NE.mark_reported(_built["leads"], _day) if _res["failed"] == 0 else 0
        print("")
        print("DISCORD CARDS: %d message(s) sent, %d already delivered, %d failed of %d; "
              "%d no-email lead(s) stamped as reported"
              % (_res["sent"], _res["skipped"], _res["failed"], _res["parts"], _stamped))
        if _res["failed"]:
            print("  delivery incomplete; the next run resumes from the first "
                  "undelivered message without resending what landed")
except Exception as _exc:
    print("")
    print("DISCORD CARDS delivery failed: %s: %s" % (type(_exc).__name__, _exc))
