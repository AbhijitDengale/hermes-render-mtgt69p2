#!/usr/bin/env python3
"""Research throughput, measured.

Read-only. Every number is a query over research_runs and research_fetches —
tables the research server writes as it works — so a figure here cannot drift
from what actually happened.

Percentiles use nearest-rank on the sorted sample rather than interpolation:
with a few dozen leads an interpolated p95 invents a duration no lead had.

    python3 research_metrics.py
    python3 research_metrics.py --json
    python3 research_metrics.py --since '2026-09-01 12:00:00'
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

DB = os.getenv("AGENCY_DB", "/opt/data/agency.db")

TARGET_SECONDS = float(os.getenv("NOVA_RESEARCH_TARGET_SECONDS", "30"))
HARD_LIMIT_SECONDS = float(os.getenv("NOVA_RESEARCH_HARD_LIMIT_SECONDS", "40"))


def percentile(values: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile, or None when there is nothing to rank."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, int(round(pct / 100.0 * len(ordered))))
    return ordered[min(k, len(ordered)) - 1]


def rate(numerator: int, denominator: int) -> Optional[float]:
    """A percentage, or None when there is no honest denominator."""
    if not denominator:
        return None
    return round(100.0 * numerator / denominator, 1)


def collect(db: str = None, since: str = None) -> Dict[str, Any]:
    con = sqlite3.connect(db or DB)
    con.row_factory = sqlite3.Row
    where, args = "", []
    if since:
        where = " WHERE started_at >= ?"
        args = [since]

    runs = list(con.execute(
        "SELECT * FROM research_runs%s ORDER BY started_at" % where, args))
    done = [r for r in runs if r["completed_at"] and r["duration_ms"]]
    secs = [r["duration_ms"] / 1000.0 for r in done]

    m: Dict[str, Any] = {
        "runs": len(runs),
        "completed": len(done),
        "in_flight": len(runs) - len(done),
        "target_seconds": TARGET_SECONDS,
        "hard_limit_seconds": HARD_LIMIT_SECONDS,
    }

    m["avg_seconds"] = round(sum(secs) / len(secs), 2) if secs else None
    m["p50_seconds"] = percentile(secs, 50)
    m["p95_seconds"] = percentile(secs, 95)
    m["min_seconds"] = round(min(secs), 2) if secs else None
    m["max_seconds"] = round(max(secs), 2) if secs else None
    for key in ("p50_seconds", "p95_seconds"):
        if m[key] is not None:
            m[key] = round(m[key], 2)

    m["within_target"] = rate(sum(1 for x in secs if x <= TARGET_SECONDS), len(secs))
    m["within_hard_limit"] = rate(
        sum(1 for x in secs if x <= HARD_LIMIT_SECONDS), len(secs))
    m["budget_exhausted_rate"] = rate(
        sum(1 for r in runs if r["budget_exhausted"]), len(runs))
    m["timed_out_rate"] = rate(sum(1 for r in runs if r["timed_out"]), len(runs))

    attempted = sum(r["pages_attempted"] or 0 for r in runs)
    cached = sum(r["pages_from_cache"] or 0 for r in runs)
    succeeded = sum(r["pages_succeeded"] or 0 for r in runs)
    failed = sum(r["pages_failed"] or 0 for r in runs)
    refused = sum(r["pages_refused"] or 0 for r in runs)
    m["pages_attempted"] = attempted
    m["pages_succeeded"] = succeeded
    m["pages_from_cache"] = cached
    m["pages_failed"] = failed
    m["pages_refused"] = refused
    m["avg_pages_per_lead"] = round(attempted / len(runs), 2) if runs else None
    m["cache_hit_rate"] = rate(cached, attempted)

    # A Steel request is an attempted page that was not served from cache.
    steel_attempts = attempted - cached
    m["steel_requests"] = steel_attempts
    m["steel_success_rate"] = rate(succeeded - cached, steel_attempts)

    by_status: Dict[str, int] = {}
    for r in runs:
        by_status[r["research_status"] or "in_flight"] = \
            by_status.get(r["research_status"] or "in_flight", 0) + 1
    m["by_status"] = by_status

    # Throughput. Measured over the span actually worked rather than assumed,
    # so a short benchmark does not get extrapolated as though it ran all day.
    if len(done) >= 2:
        span = con.execute(
            "SELECT (julianday(MAX(completed_at)) - julianday(MIN(started_at)))"
            " * 24.0 AS hours FROM research_runs%s" % where, args).fetchone()["hours"]
        if span and span > 0:
            m["span_hours"] = round(span, 3)
            m["leads_per_hour"] = round(len(done) / span, 1)
            m["projected_per_day"] = int(m["leads_per_hour"] * 24)
        else:
            m["span_hours"] = 0.0
            m["leads_per_hour"] = None
            m["projected_per_day"] = None
    else:
        m["span_hours"] = None
        m["leads_per_hour"] = None
        m["projected_per_day"] = None

    # Cache effectiveness from the fetch log, which records every call and
    # whether it was served from cache.
    f = con.execute(
        "SELECT tier, status, COUNT(*) n FROM research_fetches"
        "%s GROUP BY tier, status" % (" WHERE created_at >= ?" if since else ""),
        args).fetchall()
    m["fetches"] = {"%s/%s" % (r["tier"], r["status"]): r["n"] for r in f}
    con.close()
    return m


def report(m: Dict[str, Any]) -> str:
    def s(v, unit="s"):
        return "n/a" if v is None else "%.2f%s" % (v, unit)

    def p(v):
        return "n/a" if v is None else "%.1f%%" % v

    L = ["**NOVA RESEARCH THROUGHPUT**", ""]
    L.append("Runs: %d   completed: %d   in flight: %d"
             % (m["runs"], m["completed"], m["in_flight"]))
    L.append("")
    L.append("Duration   avg %-8s p50 %-8s p95 %-8s max %s"
             % (s(m["avg_seconds"]), s(m["p50_seconds"]),
                s(m["p95_seconds"]), s(m["max_seconds"])))
    L.append("Within target (%.0fs): %s      within hard limit (%.0fs): %s"
             % (m["target_seconds"], p(m["within_target"]),
                m["hard_limit_seconds"], p(m["within_hard_limit"])))
    L.append("Budget exhausted: %s   timed out: %s"
             % (p(m["budget_exhausted_rate"]), p(m["timed_out_rate"])))
    L.append("")
    L.append("Pages     attempted %d  succeeded %d  cached %d  failed %d  refused %d"
             % (m["pages_attempted"], m["pages_succeeded"], m["pages_from_cache"],
                m["pages_failed"], m["pages_refused"]))
    L.append("          avg per lead %s   cache hit rate %s"
             % ("n/a" if m["avg_pages_per_lead"] is None
                else "%.2f" % m["avg_pages_per_lead"], p(m["cache_hit_rate"])))
    L.append("Steel     requests %d   success rate %s"
             % (m["steel_requests"], p(m["steel_success_rate"])))
    L.append("")
    if m["leads_per_hour"] is not None:
        L.append("Throughput: %.1f leads/hour over %.3f h  ->  ~%d/day at this rate"
                 % (m["leads_per_hour"], m["span_hours"], m["projected_per_day"]))
    else:
        L.append("Throughput: not enough completed runs to measure")
    L.append("Status: %s" % (m["by_status"] or "none"))
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--since", default=None,
                    help="only runs started at or after this timestamp")
    args = ap.parse_args(argv)
    m = collect(since=args.since)
    print(json.dumps(m, indent=2) if args.json else report(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
