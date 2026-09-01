#!/usr/bin/env python3
"""ORBIT — read-only metrics.

Every number here comes from a database query, never from a model. ORBIT may
be asked to summarise or recommend from this payload later, but the counts and
rates are computed in code so a report cannot drift from the data it describes.

Authoritative sources, deliberately not duplicated:

    agency.db          leads, states, follow-ups, escalations, pipeline events
    MailHub / Supabase what was actually queued and sent, provider confirmations,
                       replies, bounces, sender health, suppression

The obsolete agency.db mail tables are not read. They were superseded and
reviving them would recreate the two-sources-of-truth problem.

ORBIT never writes. It may recommend pausing a sender; it will not do it.

    python3 orbit.py metrics [--json]
    python3 orbit.py report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline as P  # noqa: E402

MAILHUB_BASE = os.getenv("MAILHUB_BASE_URL", "").rstrip("/")
MAILHUB_TOKEN = os.getenv("MAILHUB_API_TOKEN", "")

# Below this, a rate is noise. Reporting "100% reply rate" off one send is
# worse than saying nothing, because someone will act on it.
MIN_SAMPLE = int(os.getenv("ORBIT_MIN_SAMPLE", "20"))

REPLY_STATES = ("REPLIED", "POSITIVE", "NEGATIVE", "MEETING_STAGE",
                "HUMAN_REVIEW", "UNSUBSCRIBED")

# Every rate is "of the people we actually emailed". Numerator and denominator
# must be drawn from this same set, or a lead who was never contacted can push
# a percentage past 100.
CONTACTED = ("SELECT lead_id FROM messages "
             " WHERE status IN ('sent','simulated')")


def rate(numerator: int, denominator: int) -> Optional[float]:
    """A rate, or None when there is no honest denominator.

    Returning None rather than 0.0 matters: zero replies out of zero sends is
    not a 0% reply rate, it is an unknown one.
    """
    if not denominator:
        return None
    return round(100.0 * numerator / denominator, 1)


def pct(value: Optional[float], sample: int = None) -> str:
    if value is None:
        return "n/a"
    if sample is not None and sample < MIN_SAMPLE:
        return "%.1f%% (n=%d, insufficient data)" % (value, sample)
    return "%.1f%%" % value


def mailhub(path: str) -> Dict[str, Any]:
    if not MAILHUB_BASE or not MAILHUB_TOKEN:
        return {"error": "MailHub not configured"}
    req = urllib.request.Request(MAILHUB_BASE + path)
    req.add_header("Authorization", "Bearer " + MAILHUB_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": "http %d" % e.code}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def collect(db: str = None) -> Dict[str, Any]:
    m: Dict[str, Any] = {}
    with P.connect(db) as con:
        def scalar(sql, args=()):
            row = con.execute(sql, args).fetchone()
            return row[0] if row else 0

        m["leads"] = scalar("SELECT COUNT(*) FROM leads")
        m["by_state"] = {r["state"]: r["n"] for r in con.execute(
            "SELECT state, COUNT(*) n FROM leads GROUP BY state ORDER BY state")}

        m["research_complete"] = scalar(
            "SELECT COUNT(*) FROM leads WHERE research_json IS NOT NULL")
        m["research_failed"] = scalar(
            "SELECT COUNT(*) FROM events WHERE event_type='state.changed'"
            "   AND to_state='HUMAN_REVIEW' AND detail LIKE 'research failed%'")

        # Outbound: agency.db records what the pipeline produced. MailHub is
        # asked separately for what a provider actually confirmed.
        m["initial_sent"] = scalar(
            "SELECT COUNT(*) FROM messages WHERE followup_stage=0"
            "   AND status IN ('sent','simulated')")
        m["followups_sent"] = scalar(
            "SELECT COUNT(*) FROM messages WHERE followup_stage>0"
            "   AND status IN ('sent','simulated')")
        m["outbound_total"] = m["initial_sent"] + m["followups_sent"]
        m["send_failures"] = scalar(
            "SELECT COUNT(*) FROM messages WHERE status='failed'")

        # Denominators are *people*, not messages. Counting raw sends lets a
        # lead who was emailed three times and replied twice produce a reply
        # rate above 100%, which is how this first reported "400%".
        m["leads_contacted"] = scalar(
            "SELECT COUNT(DISTINCT lead_id) FROM messages "
            " WHERE status IN ('sent','simulated')")
        m["leads_replied"] = scalar(
            "SELECT COUNT(DISTINCT lead_id) FROM inbound_replies "
            " WHERE lead_id IN (%s)" % CONTACTED)
        m["replies"] = scalar("SELECT COUNT(*) FROM inbound_replies")
        # Replies from leads with no send on record mean the two stores have
        # drifted. Surfacing the count is honest; folding them into a rate
        # would quietly inflate it.
        m["replies_unmatched"] = scalar(
            "SELECT COUNT(*) FROM inbound_replies "
            " WHERE lead_id NOT IN (%s)" % CONTACTED)
        m["replies_classified"] = scalar(
            "SELECT COUNT(*) FROM inbound_replies WHERE classified_at IS NOT NULL")
        m["by_classification"] = {r["classification"] or "unclassified": r["n"]
                                  for r in con.execute(
            "SELECT classification, COUNT(*) n FROM inbound_replies "
            " GROUP BY classification ORDER BY n DESC")}
        m["positive_replies"] = scalar(
            "SELECT COUNT(DISTINCT lead_id) FROM inbound_replies "
            " WHERE classification IN ('positive','interested') "
            "   AND lead_id IN (%s)" % CONTACTED)
        m["negative_replies"] = scalar(
            "SELECT COUNT(DISTINCT lead_id) FROM inbound_replies "
            " WHERE classification='negative' AND lead_id IN (%s)" % CONTACTED)
        m["unsubscribes"] = scalar(
            "SELECT COUNT(*) FROM leads WHERE state='UNSUBSCRIBED' "
            "   AND id IN (%s)" % CONTACTED)
        m["bounces"] = scalar(
            "SELECT COUNT(DISTINCT lead_id) FROM inbound_replies "
            " WHERE is_bounce=1 AND lead_id IN (%s)" % CONTACTED)
        m["meetings"] = scalar(
            "SELECT COUNT(*) FROM leads WHERE state='MEETING_STAGE' "
            "   AND id IN (%s)" % CONTACTED)

        m["human_reviews"] = scalar("SELECT COUNT(*) FROM human_escalations")
        m["human_reviews_open"] = scalar(
            "SELECT COUNT(*) FROM human_escalations WHERE status='open'")
        m["human_reviews_resolved"] = scalar(
            "SELECT COUNT(*) FROM human_escalations WHERE status<>'open'")

        m["followups"] = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM followups GROUP BY status")}
        m["leads_followed_up"] = scalar(
            "SELECT COUNT(DISTINCT lead_id) FROM messages "
            " WHERE followup_stage > 0 AND status IN ('sent','simulated')")
        m["followup_replies"] = scalar(
            "SELECT COUNT(DISTINCT ir.lead_id) FROM inbound_replies ir "
            "  JOIN messages msg ON msg.lead_id = ir.lead_id "
            " WHERE msg.followup_stage > 0 "
            "   AND msg.status IN ('sent','simulated')")

        # --- breakdowns, only where the dimension actually exists ----------
        def breakdown(column: str) -> List[Dict[str, Any]]:
            rows = []
            for r in con.execute(
                    "SELECT COALESCE(l.%s,'(unset)') AS k, COUNT(*) AS leads,"
                    "       SUM(CASE WHEN l.state IN %s THEN 1 ELSE 0 END) AS replied,"
                    "       SUM(CASE WHEN l.state IN ('POSITIVE','MEETING_STAGE')"
                    "                THEN 1 ELSE 0 END) AS positive "
                    "  FROM leads l GROUP BY k ORDER BY leads DESC"
                    % (column, str(REPLY_STATES))):
                rows.append({"key": r["k"], "leads": r["leads"],
                             "replied": r["replied"], "positive": r["positive"],
                             "reply_rate": rate(r["replied"], r["leads"])})
            return rows

        m["by_campaign"] = breakdown("campaign_id")
        m["by_country"] = breakdown("country")
        m["by_niche"] = breakdown("niche")
        m["by_stage"] = {r["followup_stage"]: r["n"] for r in con.execute(
            "SELECT followup_stage, COUNT(*) n FROM messages "
            " WHERE status IN ('sent','simulated') GROUP BY followup_stage")}

    # --- rates, computed only where a denominator exists -------------------
    # Every rate below is per-lead-contacted, so none of them can exceed 100%.
    people = m["leads_contacted"]
    m["rates"] = {
        "reply_rate": rate(m["leads_replied"], people),
        "positive_reply_rate": rate(m["positive_replies"], people),
        "negative_reply_rate": rate(m["negative_replies"], people),
        "bounce_rate": rate(m["bounces"], people),
        "unsubscribe_rate": rate(m["unsubscribes"], people),
        "meeting_rate": rate(m["meetings"], people),
        "followup_reply_rate": rate(m["followup_replies"],
                                   m["leads_followed_up"]),
    }
    m["sample"] = people
    m["min_sample"] = MIN_SAMPLE

    # --- sender health, from MailHub only ----------------------------------
    accounts = mailhub("/api/v1/accounts")
    m["senders"] = []
    m["sender_warnings"] = []
    if accounts.get("error"):
        m["senders_error"] = accounts["error"]
    else:
        for a in accounts.get("accounts", []):
            entry = {
                "email": a.get("email"), "health": a.get("health"),
                "enabled": a.get("enabled"), "sent_today": a.get("sent_today"),
                "effective_daily_limit": a.get("effective_daily_limit"),
                "daily_limit": a.get("daily_limit"),
                "sent_total": a.get("sent_total"),
                "consecutive_errors": a.get("consecutive_errors"),
            }
            m["senders"].append(entry)
            # Observations only. ORBIT does not change sender configuration.
            if not a.get("enabled"):
                m["sender_warnings"].append("%s is paused" % a.get("email"))
            if (a.get("consecutive_errors") or 0) > 0:
                m["sender_warnings"].append(
                    "%s has %d consecutive errors — investigate before it is "
                    "put back in rotation"
                    % (a.get("email"), a["consecutive_errors"]))
            cap = a.get("effective_daily_limit") or 0
            if cap and (a.get("sent_today") or 0) >= cap:
                m["sender_warnings"].append(
                    "%s has reached today's cap (%d) — allocation is the limit, "
                    "not a fault" % (a.get("email"), cap))
    return m


def best(rows: List[Dict[str, Any]]) -> str:
    """The best performer, or an honest refusal.

    A "best campaign" chosen from four leads is a coin toss with a rosette on
    it, so below the sample floor this says so instead.
    """
    usable = [r for r in rows if r["leads"] >= MIN_SAMPLE
              and r["reply_rate"] is not None]
    if not usable:
        return "insufficient data"
    top = max(usable, key=lambda r: r["reply_rate"])
    return "%s (%.1f%% of %d)" % (top["key"], top["reply_rate"], top["leads"])


def report(m: Dict[str, Any]) -> str:
    L = ["**AGENCY DAILY**", ""]
    L.append("Leads: %d   Research complete: %d   Research failed: %d"
             % (m["leads"], m["research_complete"], m["research_failed"]))
    L.append("Initial sent: %d   Follow-ups: %d   Total out: %d   Failures: %d"
             % (m["initial_sent"], m["followups_sent"], m["outbound_total"],
                m["send_failures"]))
    L.append("Leads contacted: %d   Leads that replied: %d   (%d inbound total)"
             % (m["leads_contacted"], m["leads_replied"], m["replies"]))
    L.append("Positive: %d   Negative: %d   Meetings: %d"
             % (m["positive_replies"], m["negative_replies"], m["meetings"]))
    L.append("Unsubscribes: %d   Bounces: %d"
             % (m["unsubscribes"], m["bounces"]))
    L.append("")
    r = m["rates"]
    n = m["sample"]
    L.append("Reply rate:    %s" % pct(r["reply_rate"], n))
    L.append("Positive rate: %s" % pct(r["positive_reply_rate"], n))
    L.append("Bounce rate:   %s" % pct(r["bounce_rate"], n))
    L.append("Unsub rate:    %s" % pct(r["unsubscribe_rate"], n))
    L.append("")
    L.append("Best campaign: %s" % best(m["by_campaign"]))
    L.append("Best niche:    %s" % best(m["by_niche"]))
    L.append("")
    if m["sender_warnings"]:
        L.append("Sender warnings:")
        for w in m["sender_warnings"][:5]:
            L.append("  - %s" % w)
    else:
        L.append("Sender warnings: none")
    L.append("")
    L.append("Human reviews waiting: %d (of %d raised)"
             % (m["human_reviews_open"], m["human_reviews"]))
    if m.get("replies_unmatched"):
        L.append("Note: %d inbound repl(y/ies) have no send on record — "
                 "excluded from every rate above."
                 % m["replies_unmatched"])

    recs = []
    if m["human_reviews_open"]:
        recs.append("%d escalation(s) need a decision — `review list`"
                    % m["human_reviews_open"])
    if n < MIN_SAMPLE:
        recs.append("Only %d lead(s) contacted. Rates from fewer than %d are "
                    "not meaningful; read the counts, not the percentages."
                    % (n, MIN_SAMPLE))
    if (r["bounce_rate"] or 0) > 3 and n >= MIN_SAMPLE:
        recs.append("Bounce rate above 3% — pause and check list quality "
                    "before sending more.")
    if m.get("senders_error"):
        recs.append("Sender health unavailable: %s" % m["senders_error"])
    if recs:
        L.append("")
        L.append("Recommendations:")
        for x in recs:
            L.append("  - %s" % x)
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    mm = sub.add_parser("metrics"); mm.add_argument("--json", action="store_true")
    sub.add_parser("report")
    args = ap.parse_args(argv)

    m = collect()
    if args.cmd == "metrics":
        print(json.dumps(m, indent=2) if args.json else
              "\n".join("  %-24s %s" % (k, v) for k, v in m.items()
                        if not isinstance(v, (dict, list))))
        return 0
    print(report(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
