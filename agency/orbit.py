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
        # Handed to MailHub, not yet confirmed by a provider. Deliberately
        # distinct from sent: a queued message has not reached anybody.
        m["queued"] = scalar(
            "SELECT COUNT(*) FROM messages WHERE status='queued'")

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

    # --- lead intake against the daily target ------------------------------
    # Counted from agency.db, never asked of a model. The operational day is
    # Asia/Kolkata so the target rolls over when the operator's day does.
    try:
        import supabase_sync as S
        day = S.operational_day()
        with P.connect(db) as con:
            m["intake_day"] = day
            m["intake_today"] = S.imported_today(con, day)
            m["intake_target"] = S.DAILY_TARGET
            m["intake_remaining"] = max(0, S.DAILY_TARGET - m["intake_today"])
            m["outbox_pending"] = con.execute(
                "SELECT COUNT(*) c FROM supabase_sync_outbox"
                " WHERE status='pending'").fetchone()["c"]
            m["outbox_failed"] = con.execute(
                "SELECT COUNT(*) c FROM supabase_sync_outbox"
                " WHERE status='failed'").fetchone()["c"]
            m["supabase_mapped"] = con.execute(
                "SELECT COUNT(*) c FROM supabase_leads").fetchone()["c"]
        m["supabase_ready"] = S.ready_count()
        m["timezone"] = S.TZ_NAME
    except Exception as exc:
        m["intake_error"] = "%s: %s" % (type(exc).__name__, exc)

    # --- research throughput ------------------------------------------------
    try:
        import research_metrics as RM
        m["research"] = RM.collect(db)
    except Exception as exc:
        m["research_error"] = "%s: %s" % (type(exc).__name__, exc)

    # --- capacity across every tenant --------------------------------------
    # ORBIT holds a read-only key for one tenant, so /api/v1/accounts shows it
    # one mailbox out of five. tenant_health is where each tenant's own profile
    # records what MailHub told it, which is the only complete picture any
    # single process can see without holding every tenant's credential.
    m["tenants"] = []
    try:
        with P.connect(db) as tcon:
            rows = tcon.execute(
                "SELECT tenant_name, user_id, mailbox_email, health, daily_limit,"
                "       sent_today, mailbox_ok, queue_ok, approve_ok, leo_ok,"
                "       mailbox_checked_at"
                "  FROM tenant_health ORDER BY tenant_name").fetchall()
    except Exception:
        rows = []
    for r in rows:
        d = dict(r)
        limit = d.get("daily_limit") or 0
        sent = d.get("sent_today") or 0
        ready = all(d.get(c) for c in ("mailbox_ok", "queue_ok", "approve_ok",
                                       "leo_ok"))
        d["ready"] = ready
        d["remaining"] = max(0, limit - sent)
        m["tenants"].append(d)

    m["capacity_configured"] = sum(t.get("daily_limit") or 0
                                   for t in m["tenants"] if t.get("mailbox_ok"))
    # Usable is what could actually be sent right now: a tenant missing any
    # credential cannot carry a lead end to end, so its mailbox is capacity on
    # paper only and is excluded rather than quietly counted.
    m["capacity_usable"] = sum(t["remaining"] for t in m["tenants"]
                               if t.get("ready"))
    m["tenants_ready"] = sum(1 for t in m["tenants"] if t.get("ready"))

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
    """The daily report, section by section.

    Every figure is a count or a rate computed from a query. Nothing here is
    written by a model, so the report cannot describe a day that did not
    happen.
    """
    n = m["sample"]
    r = m["rates"]
    res = m.get("research") or {}

    def pc(v):
        return pct(v, n)

    def num(v, suffix=""):
        return "n/a" if v is None else ("%s%s" % (v, suffix))

    def secs(v):
        return "n/a" if v is None else "%.1fs" % v

    L = ["**HERMES AGENCY — DAILY REPORT**", ""]
    L.append("Date: %s  (%s)" % (m.get("intake_day", "—"),
                                 m.get("timezone", "operational day")))
    L.append("")

    # ---- LEADS -----------------------------------------------------------
    L.append("**LEADS**")
    ready = m.get("supabase_ready")
    L.append("  Available in Supabase:      %s"
             % ("unavailable" if ready is None else ready))
    target = m.get("intake_target")
    today = m.get("intake_today")
    if target is not None and today is not None:
        done = today >= target
        L.append("  Daily lead target:          %d / %d%s"
                 % (today, target, "  \u2705" if done else ""))
        L.append("  Remaining:                  %d" % m.get("intake_remaining", 0))
        if done:
            L.append("  No further leads will be claimed until the next "
                     "operational day.")
    else:
        L.append("  Daily lead target:          not configured")
    L.append("  Leads in Hermes:            %d" % m["leads"])
    L.append("  Currently researching:      %d"
             % (m["by_state"].get("RESEARCHING", 0)
                + m["by_state"].get("RESEARCH_PENDING", 0)))
    L.append("  Research completed:         %d" % m["research_complete"])
    L.append("  Research failed / review:   %d" % m["research_failed"])
    L.append("")

    # ---- OUTREACH --------------------------------------------------------
    L.append("**OUTREACH**")
    L.append("  Copy ready:                 %d" % m["by_state"].get("COPY_READY", 0))
    L.append("  QA approved (ready):        %d" % m["by_state"].get("READY_TO_SEND", 0))
    L.append("  QA rejected:                %d" % m["by_state"].get("QA_REJECTED", 0))
    L.append("  Queued (awaiting provider): %d" % m.get("queued", 0))
    L.append("  Sent (initial + follow-up): %d  (%d + %d)"
             % (m["outbound_total"], m["initial_sent"], m["followups_sent"]))
    L.append("  Send failed:                %d" % m["send_failures"])
    L.append("")

    # ---- REPLIES ---------------------------------------------------------
    L.append("**REPLIES**")
    L.append("  Leads contacted:            %d" % m["leads_contacted"])
    L.append("  Leads that replied:         %d  (%d inbound total)"
             % (m["leads_replied"], m["replies"]))
    cls = m.get("by_classification") or {}
    for label, key in (("Positive / interested", "positive"),
                       ("Pricing", "pricing_question"),
                       ("Meetings", "meeting_request"),
                       ("Negative", "negative"),
                       ("Out of office", "out_of_office"),
                       ("Unsubscribe", "unsubscribe")):
        if cls.get(key):
            L.append("  %-27s %d" % (label + ":", cls[key]))
    L.append("  Unsubscribed:               %d" % m["unsubscribes"])
    L.append("  Bounced:                    %d" % m["bounces"])
    L.append("  Human review open:          %d" % m["human_reviews_open"])
    L.append("")

    # ---- RATES -----------------------------------------------------------
    # Numerator and denominator are drawn from the same population — leads
    # actually contacted — so none of these can exceed 100%.
    L.append("**RATES**  (of %d lead(s) contacted)" % n)
    L.append("  Reply rate:                 %s" % pc(r["reply_rate"]))
    L.append("  Positive rate:              %s" % pc(r["positive_reply_rate"]))
    L.append("  Meeting rate:               %s" % pc(r["meeting_rate"]))
    L.append("  Bounce rate:                %s" % pc(r["bounce_rate"]))
    L.append("")

    # ---- RESEARCH --------------------------------------------------------
    L.append("**RESEARCH**")
    if res.get("completed"):
        L.append("  Average:                    %s" % secs(res.get("avg_seconds")))
        L.append("  P50 / P95:                  %s / %s"
                 % (secs(res.get("p50_seconds")), secs(res.get("p95_seconds"))))
        L.append("  Cache hit rate:             %s"
                 % ("n/a" if res.get("cache_hit_rate") is None
                    else "%.1f%%" % res["cache_hit_rate"]))
        L.append("  Steel success rate:         %s"
                 % ("n/a" if res.get("steel_success_rate") is None
                    else "%.1f%%" % res["steel_success_rate"]))
        L.append("  Avg pages per lead:         %s"
                 % num(res.get("avg_pages_per_lead")))
    else:
        L.append("  no research runs recorded yet")
    L.append("")

    # ---- MAILBOX ---------------------------------------------------------
    # From MailHub, which is the only thing that knows what a provider did.
    L.append("**MAILBOX**")
    senders = m.get("senders") or []
    if m.get("senders_error"):
        L.append("  unavailable: %s" % m["senders_error"])
    elif not senders:
        L.append("  no mailboxes connected")
    else:
        active = [a for a in senders if a.get("enabled")]
        warming = [a for a in senders if a.get("health") == "warming"]
        paused = [a for a in senders if not a.get("enabled")]
        # Only enabled mailboxes: a paused one cannot be selected, so counting
        # its limit would report capacity that does not exist.
        sent_today = sum(a.get("sent_today") or 0 for a in active)
        cap = sum(a.get("effective_daily_limit") or 0 for a in active)
        L.append("  Mailboxes active:           %d of %d" % (len(active), len(senders)))
        L.append("  Sent today:                 %d" % sent_today)
        L.append("  Capacity today:             %d" % cap)
        L.append("  Remaining safe capacity:    %d" % max(0, cap - sent_today))
        L.append("  Warming:                    %d" % len(warming))
        L.append("  Paused / blocked:           %d" % len(paused))
        for a in active:
            lim = a.get("effective_daily_limit") or 0
            L.append("    %-30s %-9s %3d/%-3d  %d left"
                     % (a.get("email"), a.get("health"),
                        a.get("sent_today") or 0, lim,
                        max(0, lim - (a.get("sent_today") or 0))))
        for a in paused:
            L.append("    %-30s paused    excluded from capacity"
                     % a.get("email"))

    ts = m.get("tenants") or []
    if ts:
        L.append("")
        L.append("  Sender tenants:             %d ready of %d"
                 % (m.get("tenants_ready") or 0, len(ts)))
        for t in ts:
            missing = [n for n, c in (("queue", "queue_ok"),
                                      ("approve", "approve_ok"),
                                      ("leo", "leo_ok"),
                                      ("mailbox", "mailbox_ok"))
                       if not t.get(c)]
            L.append("    %-30s %-9s %3s/%-3s  %s"
                     % (t.get("mailbox_email") or t.get("tenant_name"),
                        t.get("health") or "-",
                        t.get("sent_today") if t.get("sent_today") is not None else "-",
                        t.get("daily_limit") if t.get("daily_limit") is not None else "-",
                        "ready" if t.get("ready")
                        else "NOT ready (missing: %s)" % ", ".join(missing)))
        L.append("  Total configured capacity:  %d/day" % (m.get("capacity_configured") or 0))
        L.append("  Currently usable capacity:  %d/day" % (m.get("capacity_usable") or 0))
    L.append("")

    # ---- PIPELINE --------------------------------------------------------
    L.append("**PIPELINE**")
    L.append("  Supabase leads mapped:      %s" % num(m.get("supabase_mapped")))
    L.append("  Sync pending:               %s" % num(m.get("outbox_pending")))
    L.append("  Sync failures:              %s" % num(m.get("outbox_failed")))
    L.append("  Human review open:          %d (of %d raised)"
             % (m["human_reviews_open"], m["human_reviews"]))
    if m.get("replies_unmatched"):
        L.append("  Note: %d inbound repl(y/ies) have no send on record — "
                 "excluded from every rate above." % m["replies_unmatched"])
    if m.get("intake_error"):
        L.append("  Intake metrics unavailable: %s" % m["intake_error"])

    # ---- what needs a person --------------------------------------------
    recs = []
    if m["human_reviews_open"]:
        recs.append("%d escalation(s) need a decision — `review list`"
                    % m["human_reviews_open"])
    if m.get("outbox_failed"):
        recs.append("%d Supabase write-back(s) gave up after retrying — "
                    "`supabase_sync.py reconcile`" % m["outbox_failed"])
    if n < MIN_SAMPLE:
        recs.append("Only %d lead(s) contacted. Rates from fewer than %d are "
                    "not meaningful; read the counts, not the percentages."
                    % (n, MIN_SAMPLE))
    if (r["bounce_rate"] or 0) > 3 and n >= MIN_SAMPLE:
        recs.append("Bounce rate above 3% — pause and check list quality.")
    for w in (m.get("sender_warnings") or [])[:3]:
        recs.append(w)
    if m.get("senders_error"):
        recs.append("Sender health unavailable: %s" % m["senders_error"])
    if recs:
        L.append("")
        L.append("**NEEDS ATTENTION**")
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
