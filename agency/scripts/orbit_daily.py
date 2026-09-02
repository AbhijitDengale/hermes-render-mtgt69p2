#!/usr/bin/env python3
"""ORBIT's daily report. Hermes cron, --no-agent --deliver discord.

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

print(orbit.report(orbit.collect()))

# The complete no-email list goes out as its own multi-part Discord delivery,
# after the summary above has been handed to the cron's own delivery. It is
# posted from here rather than printed because a list of several hundred
# leads is dozens of messages, each of which must be tracked so a failure can
# resume without resending what already landed.
#
# The Discord token is read from the root env this job already runs in and is
# never printed. A failure here is reported on stdout (which the cron
# delivers) and never raises, so the summary is not lost with it.
try:
    for line in open("/opt/data/.env", encoding="utf-8"):
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("DISCORD_BOT_TOKEN", "SUPABASE_URL",
                                   "SUPABASE_SECRET_KEY", "NO_EMAIL_DISCORD_CHANNEL"):
            os.environ.setdefault(key.strip(), value.strip())
    import no_email_report as NE
    import pipeline as P
    import supabase_sync as S
    _day = S.operational_day()
    _built = NE.build(_day)
    with P.connect() as _con:
        _res = NE.post_all(_con, _built)
    _stamped = NE.mark_reported(_built["leads"], _day) if _res["failed"] == 0 else 0
    print("")
    print("NO EMAIL LEADS delivery: %d part(s) sent, %d already delivered, "
          "%d failed of %d; %d lead(s) stamped as reported"
          % (_res["sent"], _res["skipped"], _res["failed"], _res["parts"], _stamped))
    if _res["failed"]:
        print("  delivery incomplete; the next run resumes from the first "
              "undelivered part without resending what landed")
except Exception as _exc:
    print("")
    print("NO EMAIL LEADS delivery failed: %s: %s" % (type(_exc).__name__, _exc))
