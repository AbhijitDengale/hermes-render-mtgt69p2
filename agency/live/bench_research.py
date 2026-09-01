#!/usr/bin/env python3
"""Research-only load test. Sends nothing.

Twenty leads through the real research path — real Steel, real cache, real
concurrency limits — to find out whether research can stay ahead of a 400/day
send schedule.

The mix is deliberate rather than convenient: repeated domains so the
per-domain throttle is exercised, repeated URLs so the cache is, and a site
that is known to fail so the failure path is measured rather than assumed.

    python3 live/bench_research.py            # 20 leads, concurrency from env
    python3 live/bench_research.py --leads 40 --concurrency 6
    python3 live/bench_research.py --dry-run  # show the plan, fetch nothing

No email is sent. No lead state is changed. This touches research_cache,
research_fetches and research_runs only.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sqlite3
import sys
import threading
import time

sys.path.insert(0, "/opt/data/agency")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Real sites, chosen because they are public, stable and quick to scrape.
# Duplicates are intentional: cache hits and same-domain throttling are part of
# what is being measured.
SITES = [
    ("https://plausible.io", "plausible.io"),
    ("https://posthog.com", "posthog.com"),
    ("https://www.basecamp.com", "basecamp.com"),
    ("https://linear.app", "linear.app"),
    ("https://plausible.io", "plausible.io"),            # cache hit
    ("https://plausible.io/about", "plausible.io"),      # same domain, new page
    ("https://posthog.com", "posthog.com"),              # cache hit
    ("https://www.basecamp.com", "basecamp.com"),        # cache hit
    ("https://cadenceworks.com", "cadenceworks.com"),    # known to fail
    ("https://linear.app", "linear.app"),                # cache hit
]


def build(n):
    plan = []
    for i in range(n):
        url, host = SITES[i % len(SITES)]
        plan.append(("BENCH-%02d" % i, url, host))
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--leads", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.concurrency:
        os.environ["BROWSER_MAX_CONCURRENCY"] = str(args.concurrency)
    import research_mcp as R
    import research_metrics as RM

    # Count concurrency where it actually matters — inside the provider call,
    # under the semaphore. Counting threads instead just counts how many leads
    # were started, which is not a limit anything enforces.
    live_fetch = {"now": 0, "max": 0}
    fetch_lock = threading.Lock()
    _real_provider = R.get_provider

    def counting_provider():
        prov = _real_provider()
        inner = prov.fetch

        def fetch(url, formats, timeout=None):
            with fetch_lock:
                live_fetch["now"] += 1
                live_fetch["max"] = max(live_fetch["max"], live_fetch["now"])
            try:
                return inner(url, formats, timeout)
            finally:
                with fetch_lock:
                    live_fetch["now"] -= 1

        prov.fetch = fetch
        return prov

    R.get_provider = counting_provider

    plan = build(args.leads)
    print("leads: %d   concurrency: %d   per-lead budget: %.0fs (target %.0fs)"
          % (len(plan), R.MAX_CONCURRENCY, R.HARD_LIMIT_SECONDS, R.TARGET_SECONDS))
    print("max pages/lead: %d   fetch timeout: %ds   domain interval: %.1fs"
          % (R.MAX_PAGES_PER_LEAD, R.TIMEOUT_S, R.PER_DOMAIN_MIN_INTERVAL_S))
    print("distinct domains: %d   repeated urls: %d"
          % (len({h for _, _, h in plan}),
             len(plan) - len({u for _, u, _ in plan})))
    if args.dry_run:
        for lead, url, host in plan:
            print("  %-10s %s" % (lead, url))
        return 0

    started = os.popen("date -u +'%Y-%m-%d %H:%M:%S'").read().strip()
    results, lock = {}, threading.Lock()
    live = {"now": 0, "max": 0}

    def work(lead, url):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        t0 = time.time()
        try:
            out = R.research_fetch(url, lead, "home")
            status = out.get("status")
            cached = bool(out.get("_cached"))
            # One page is what a lead needs when the homepage is enough. The
            # budget allows up to three; this benchmark measures the common
            # case rather than the worst one.
            obs = 4 if status == "ok" else 0
            R.finalize_research(lead, observations=obs,
                                status="complete" if status == "ok" else "failed")
        except Exception as exc:
            status, cached = "error: %s" % type(exc).__name__, False
        finally:
            with lock:
                live["now"] -= 1
        with lock:
            results[lead] = {"status": status, "cached": cached,
                             "seconds": round(time.time() - t0, 2), "url": url}

    t0 = time.time()
    threads = []
    for lead, url, _ in plan:
        t = threading.Thread(target=work, args=(lead, url))
        t.start()
        threads.append(t)
        # A trickle rather than a thundering herd: real leads arrive from the
        # orchestrator a few at a time, and launching twenty at once would
        # measure the semaphore, not the pipeline.
        time.sleep(0.15)
    for t in threads:
        t.join()
    total = time.time() - t0

    print("\n=== per lead ===")
    for lead, _, _ in plan:
        r = results.get(lead, {})
        print("  %-10s %-8s %-6s %6.2fs  %s"
              % (lead, r.get("status"), "cache" if r.get("cached") else "steel",
                 r.get("seconds", 0), r.get("url", "")))

    ok = [r for r in results.values() if r["status"] == "ok"]
    failed = [r for r in results.values() if r["status"] != "ok"]
    cached = [r for r in ok if r["cached"]]
    secs = sorted(r["seconds"] for r in results.values())

    print("\n=== benchmark ===")
    print("  total runtime          %.2fs" % total)
    print("  leads                  %d" % len(plan))
    print("  succeeded              %d" % len(ok))
    print("  failed -> HUMAN_REVIEW %d" % len(failed))
    print("  from cache             %d" % len(cached))
    print("  steel requests         %d" % (len(ok) - len(cached) + len(failed)))
    print("  leads in flight (peak)   %d" % live["max"])
    print("  max concurrent Steel calls %d   (limit %d)"
          % (live_fetch["max"], R.MAX_CONCURRENCY))
    if secs:
        print("  avg per lead           %.2fs" % (sum(secs) / len(secs)))
        print("  p50                    %.2fs" % RM.percentile(secs, 50))
        print("  p95                    %.2fs" % RM.percentile(secs, 95))
        print("  max                    %.2fs" % secs[-1])
    if total > 0:
        rate = len(plan) / total * 3600
        print("  throughput             %.0f leads/hour  (~%d/day)"
              % (rate, int(rate * 24)))

    print("\n=== as recorded in research_runs ===")
    print(RM.report(RM.collect(since=started)))
    print("\nNo email was sent. No lead state was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
