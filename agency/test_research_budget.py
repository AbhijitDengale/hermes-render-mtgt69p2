#!/usr/bin/env python3
"""NOVA research throughput: budgets, page caps, cache, concurrency.

Steel is replaced by a fake provider with controllable latency, so the timing
assertions are about our own logic rather than about somebody else's network.
The one thing never faked is the evidence rule: a failed fetch must produce a
failure, never a fabricated page.

    python3 test_research_budget.py
"""

import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

PASS, FAIL = [], []
_SEQ = [0]


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh_db(tmp):
    _SEQ[0] += 1
    path = os.path.join(tmp, "r%d.db" % _SEQ[0])
    con = sqlite3.connect(path)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed_state_transitions.sql").read_text(encoding="utf-8"))
    for m in sorted((HERE / "migrations").glob("*.sql")):
        con.executescript(m.read_text(encoding="utf-8"))
    con.close()
    return path


class FakeProvider:
    """A Steel stand-in. Records every call, and honours the caller's timeout."""

    name = "steel"

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.latency = 0.05
        self.fail_hosts = set()
        self.live = 0
        self.max_live = 0

    def fetch(self, url, formats, timeout=None):
        with self.lock:
            self.calls.append((url, timeout))
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        try:
            host = url.split("/")[2]
            delay = self.latency
            # A request must never outlive the timeout it was handed.
            if timeout is not None and delay > timeout:
                time.sleep(timeout)
                raise TimeoutError("fetch exceeded %ss" % timeout)
            time.sleep(delay)
            if host in self.fail_hosts:
                raise RuntimeError("simulated failure for %s" % host)
            return {"url": url, "markdown": "# %s\ncontent" % host,
                    "html": None, "links": [],
                    "metadata": {"status_code": 200, "title": host}}
        finally:
            with self.lock:
                self.live -= 1

    def screenshot(self, url):
        return {"status": "ok"}

    def health_check(self):
        return {"provider": "steel", "healthy": True}


TUNABLES = ("NOVA_RESEARCH_TARGET_SECONDS", "NOVA_RESEARCH_HARD_LIMIT_SECONDS",
            "NOVA_RESEARCH_SAVE_MARGIN_SECONDS", "NOVA_MAX_PAGES_PER_LEAD",
            "NOVA_MIN_OBSERVATIONS", "NOVA_TARGET_OBSERVATIONS",
            "NOVA_MAX_OBSERVATIONS", "BROWSER_TIMEOUT_SECONDS",
            "BROWSER_MAX_CONCURRENCY", "BROWSER_DOMAIN_INTERVAL_SECONDS")


def load(db, **env):
    """Import research_mcp with a given configuration, freshly each time.

    Every tunable is cleared first. Leaving one set from a previous case is how
    a later test silently runs against the wrong limit and passes for the wrong
    reason — which is exactly what happened before this reset existed.
    """
    import importlib
    for k in TUNABLES:
        os.environ.pop(k, None)
    os.environ["AGENCY_DB"] = db
    for k, v in env.items():
        os.environ[k] = str(v)
    if "research_mcp" in sys.modules:
        del sys.modules["research_mcp"]
    import research_mcp as R
    importlib.reload(R)
    fake = FakeProvider()
    R.get_provider = lambda: fake
    R.validate_url = lambda u: u        # the SSRF guard has its own tests
    return R, fake


def main():
    tmp = tempfile.mkdtemp()

    print("--- 1. Defaults are the production numbers ---")
    db = fresh_db(tmp)
    for key in ("NOVA_RESEARCH_TARGET_SECONDS", "NOVA_RESEARCH_HARD_LIMIT_SECONDS",
                "NOVA_RESEARCH_SAVE_MARGIN_SECONDS", "NOVA_MAX_PAGES_PER_LEAD",
                "BROWSER_TIMEOUT_SECONDS", "BROWSER_MAX_CONCURRENCY",
                "NOVA_MIN_OBSERVATIONS", "NOVA_TARGET_OBSERVATIONS",
                "NOVA_MAX_OBSERVATIONS"):
        os.environ.pop(key, None)
    R, fake = load(db)
    check("target is 30s", R.TARGET_SECONDS == 30, str(R.TARGET_SECONDS))
    check("hard limit is 40s", R.HARD_LIMIT_SECONDS == 40, str(R.HARD_LIMIT_SECONDS))
    check("save margin is 3s", R.SAVE_MARGIN_SECONDS == 3, str(R.SAVE_MARGIN_SECONDS))
    check("max 3 pages per lead", R.MAX_PAGES_PER_LEAD == 3, str(R.MAX_PAGES_PER_LEAD))
    check("browser timeout is 18s (was 45)", R.TIMEOUT_S == 18, str(R.TIMEOUT_S))
    check("concurrency is 6 (was 2)", R.MAX_CONCURRENCY == 6, str(R.MAX_CONCURRENCY))
    check("observation thresholds 3/4/5",
          (R.MIN_OBSERVATIONS, R.TARGET_OBSERVATIONS, R.MAX_OBSERVATIONS) == (3, 4, 5))
    check("env still overrides every one of them",
          load(fresh_db(tmp), NOVA_MAX_PAGES_PER_LEAD=7)[0].MAX_PAGES_PER_LEAD == 7)

    print("\n--- 2. A normal lead finishes well inside the budget ---")
    db = fresh_db(tmp)
    R, fake = load(db, BROWSER_DOMAIN_INTERVAL_SECONDS=0)
    t0 = time.time()
    for i, page in enumerate(("home", "about")):
        out = R.research_fetch("https://a.example/%s" % page, "L-1", page)
        check("page %d fetched ok" % (i + 1), out["status"] == "ok", out.get("error", ""))
    elapsed = time.time() - t0
    check("finished under the 40s hard limit", elapsed < 40, "%.2fs" % elapsed)
    st = R.budget_state("L-1")
    check("budget reports 2 pages used, 1 left",
          st["pages_fetched"] == 2 and st["pages_remaining"] == 1, str(st["pages_fetched"]))
    check("and it may still fetch", st["may_fetch"] is True)

    print("\n--- 3. Early stop: enough evidence, third page never fetched ---")
    # The stopping rule is NOVA's; what the server guarantees is that not
    # fetching costs nothing and the run records only what was read.
    R.finalize_research("L-1", observations=4, status="complete")
    row = sqlite3.connect(db).execute(
        "SELECT pages_attempted, observations_count, research_status,"
        "       duration_ms FROM research_runs WHERE lead_id='L-1'").fetchone()
    check("run recorded exactly 2 pages", row[0] == 2, str(row[0]))
    check("  4 observations", row[1] == 4, str(row[1]))
    check("  status complete", row[2] == "complete", str(row[2]))
    check("  and a measured duration", row[3] is not None and row[3] >= 0, str(row[3]))
    check("no third Steel call was made", len(fake.calls) == 2, str(len(fake.calls)))

    print("\n--- 4. The page cap is enforced by the server ---")
    db = fresh_db(tmp)
    R, fake = load(db, BROWSER_DOMAIN_INTERVAL_SECONDS=0)
    for i in range(3):
        R.research_fetch("https://b.example/p%d" % i, "L-2", "other")
    before = len(fake.calls)
    out = R.research_fetch("https://b.example/p4", "L-2", "other")
    check("a fourth page is refused", out["status"] == "budget_exhausted", out["status"])
    check("  the reason is the page limit", out.get("reason") == "page_limit",
          str(out.get("reason")))
    check("  and no Steel call was made for it", len(fake.calls) == before,
          "%d calls" % len(fake.calls))
    check("  may_fetch is now false", R.budget_state("L-2")["may_fetch"] is False)

    print("\n--- 5. The 40s total budget is enforced ---")
    db = fresh_db(tmp)
    R, fake = load(db, NOVA_RESEARCH_HARD_LIMIT_SECONDS=2,
                   NOVA_RESEARCH_SAVE_MARGIN_SECONDS=0.5,
                   BROWSER_DOMAIN_INTERVAL_SECONDS=0)
    R.research_fetch("https://c.example/1", "L-3", "home")
    b = R._budget("L-3")
    with R._budget_lock:
        b["started"] = time.time() - 1.9      # nearly spent
    before = len(fake.calls)
    out = R.research_fetch("https://c.example/2", "L-3", "about")
    check("a fetch is refused once the budget is spent",
          out["status"] == "budget_exhausted", out["status"])
    check("  the reason is the time limit", out.get("reason") == "time_limit",
          str(out.get("reason")))
    check("  no Steel call was made", len(fake.calls) == before)
    check("  and the run is marked budget_exhausted",
          sqlite3.connect(db).execute(
              "SELECT budget_exhausted FROM research_runs WHERE lead_id='L-3'"
          ).fetchone()[0] == 1)

    print("\n--- 6. A fetch never outlives the remaining budget ---")
    db = fresh_db(tmp)
    R, fake = load(db, BROWSER_TIMEOUT_SECONDS=18,
                   NOVA_RESEARCH_HARD_LIMIT_SECONDS=40,
                   NOVA_RESEARCH_SAVE_MARGIN_SECONDS=3,
                   BROWSER_DOMAIN_INTERVAL_SECONDS=0)
    b = R._budget("L-4")
    with R._budget_lock:
        b["started"] = time.time() - 28       # 12s left, 9s usable
    R.research_fetch("https://d.example/x", "L-4", "home")
    used = fake.calls[-1][1]
    check("timeout was shrunk to fit the budget, not the 18s default",
          used is not None and used <= 9, "timeout=%s" % used)
    check("  and it is still a usable timeout", used >= 1, "timeout=%s" % used)
    # With plenty of budget it should use the configured ceiling.
    b2 = R._budget("L-5")
    R.research_fetch("https://d.example/y", "L-5", "home")
    check("with a full budget the configured 18s is used",
          fake.calls[-1][1] == 18, "timeout=%s" % fake.calls[-1][1])

    print("\n--- 7. Cache first: a hit makes no Steel request ---")
    db = fresh_db(tmp)
    R, fake = load(db, BROWSER_DOMAIN_INTERVAL_SECONDS=0)
    R.research_fetch("https://e.example/home", "L-6", "home")
    check("first read hit Steel", len(fake.calls) == 1)
    out = R.research_fetch("https://e.example/home", "L-7", "home")
    check("second read came from cache", out.get("_cached") is True, str(out.get("source")))
    check("  and made no Steel request", len(fake.calls) == 1, "%d calls" % len(fake.calls))
    check("  it is still marked ok", out["status"] == "ok")
    check("  and it is labelled as cache-sourced", out.get("source") == "cache",
          str(out.get("source")))

    print("\n--- 8. A cached page is still properly sourced evidence ---")
    check("the cached result carries its URL", out.get("url") == "https://e.example/home"
          or "e.example" in str(out), str(out.get("url")))
    check("  and its content", bool(out.get("markdown")))
    fetches = sqlite3.connect(db).execute(
        "SELECT tier, status FROM research_fetches WHERE lead_id='L-7'").fetchall()
    check("  the cache hit is audited (tier='cache')",
          ("cache", "ok") in [tuple(r) for r in fetches], str(fetches))
    run = sqlite3.connect(db).execute(
        "SELECT pages_from_cache, pages_attempted FROM research_runs"
        " WHERE lead_id='L-7'").fetchone()
    check("  and counted as a cache page on the run", run[0] == 1 and run[1] == 1,
          str(tuple(run)))

    print("\n--- 9. A failed fetch produces a failure, never a page ---")
    db = fresh_db(tmp)
    R, fake = load(db, BROWSER_DOMAIN_INTERVAL_SECONDS=0)
    fake.fail_hosts.add("bad.example")
    out = R.research_fetch("https://bad.example/home", "L-8", "home")
    check("status is failed", out["status"] == "failed", out["status"])
    check("  no markdown is invented", not out.get("markdown"), str(out)[:60])
    check("  the error is reported", bool(out.get("error")))
    check("  and it is recorded as a failed page",
          sqlite3.connect(db).execute(
              "SELECT pages_failed FROM research_runs WHERE lead_id='L-8'"
          ).fetchone()[0] >= 1)

    print("\n--- 10. Insufficient evidence is a failure, not a guess ---")
    R.finalize_research("L-8", observations=1, status="failed")
    row = sqlite3.connect(db).execute(
        "SELECT observations_count, research_status FROM research_runs"
        " WHERE lead_id='L-8'").fetchone()
    check("the run is recorded as failed", row[1] == "failed", str(row[1]))
    check("  with the real observation count", row[0] == 1, str(row[0]))
    check("  which is below the minimum of %d" % R.MIN_OBSERVATIONS,
          row[0] < R.MIN_OBSERVATIONS,
          "%d < %d" % (row[0], R.MIN_OBSERVATIONS))

    print("\n--- 11/12. Six leads research at once, and never more ---")
    db = fresh_db(tmp)
    R, fake = load(db, BROWSER_MAX_CONCURRENCY=6, BROWSER_DOMAIN_INTERVAL_SECONDS=0)
    fake.latency = 0.4
    threads, results = [], {}

    def work(i):
        results[i] = R.research_fetch(
            "https://host%d.example/home" % i, "L-C%d" % i, "home")

    t0 = time.time()
    for i in range(6):
        t = threading.Thread(target=work, args=(i,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    span = time.time() - t0
    check("all six leads researched", len(results) == 6 and
          all(r["status"] == "ok" for r in results.values()))
    check("  they overlapped rather than queued",
          span < 6 * fake.latency, "%.2fs for 6 x %.2fs" % (span, fake.latency))
    check("  peak concurrency reached 6", fake.max_live == 6, str(fake.max_live))
    check("  and never exceeded the configured maximum",
          fake.max_live <= R.MAX_CONCURRENCY, "%d <= %d" % (fake.max_live, R.MAX_CONCURRENCY))

    db = fresh_db(tmp)
    R2, fake2 = load(db, BROWSER_MAX_CONCURRENCY=2, BROWSER_DOMAIN_INTERVAL_SECONDS=0)
    fake2.latency = 0.3
    threads = []
    for i in range(6):
        t = threading.Thread(target=lambda i=i: R2.research_fetch(
            "https://h%d.example/home" % i, "L-D%d" % i, "home"))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    check("a lower cap is honoured too", fake2.max_live <= 2, str(fake2.max_live))

    print("\n--- 13/14. Same domain throttles; different domains do not ---")
    db = fresh_db(tmp)
    R, fake = load(db, BROWSER_MAX_CONCURRENCY=6, BROWSER_DOMAIN_INTERVAL_SECONDS=0.5)
    fake.latency = 0.01
    t0 = time.time()
    for i in range(3):
        R.research_fetch("https://same.example/p%d" % i, "L-E", "other")
    same_span = time.time() - t0
    check("three same-domain fetches are spaced out",
          same_span >= 1.0, "%.2fs for 3 (>= 2 x 0.5s)" % same_span)

    t0 = time.time()
    threads = []
    for i in range(3):
        t = threading.Thread(target=lambda i=i: R.research_fetch(
            "https://diff%d.example/p" % i, "L-F%d" % i, "other"))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    diff_span = time.time() - t0
    check("  while three different domains run concurrently",
          diff_span < same_span, "%.2fs vs %.2fs" % (diff_span, same_span))

    print("\n--- 15. Concurrent writes stay consistent ---")
    db = fresh_db(tmp)
    R, fake = load(db, BROWSER_MAX_CONCURRENCY=6, BROWSER_DOMAIN_INTERVAL_SECONDS=0)
    fake.latency = 0.05
    threads = []
    for i in range(12):
        t = threading.Thread(target=lambda i=i: R.research_fetch(
            "https://w%d.example/p" % i, "L-W%d" % (i % 4), "other"))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    con = sqlite3.connect(db)
    runs = con.execute("SELECT lead_id, pages_attempted FROM research_runs"
                       " WHERE lead_id LIKE 'L-W%'").fetchall()
    check("one run row per lead, no duplicates", len(runs) == 4, str(len(runs)))
    check("  every page is accounted for",
          sum(r[1] for r in runs) == 12, str(sum(r[1] for r in runs)))
    check("  the fetch log has one row per call",
          con.execute("SELECT COUNT(*) FROM research_fetches"
                      " WHERE lead_id LIKE 'L-W%'").fetchone()[0] == 12)
    check("  and the cache has one row per URL",
          con.execute("SELECT COUNT(*) FROM (SELECT url FROM research_cache"
                      " GROUP BY url HAVING COUNT(*) > 1)").fetchone()[0] == 0)

    print("\n--- 16. Duration metadata is real ---")
    db = fresh_db(tmp)
    R, fake = load(db, BROWSER_DOMAIN_INTERVAL_SECONDS=0)
    fake.latency = 0.3
    t0 = time.time()
    R.research_fetch("https://m.example/home", "L-M", "home")
    R.finalize_research("L-M", observations=3, status="complete")
    real = time.time() - t0
    row = sqlite3.connect(db).execute(
        "SELECT duration_ms, pages_attempted, pages_succeeded, pages_from_cache,"
        "       observations_count, completed_at FROM research_runs"
        " WHERE lead_id='L-M'").fetchone()
    recorded = row[0] / 1000.0
    check("recorded duration matches the wall clock",
          abs(recorded - real) < 1.0, "%.2fs recorded vs %.2fs real" % (recorded, real))
    check("  pages attempted/succeeded/cached are right",
          (row[1], row[2], row[3]) == (1, 1, 0), str((row[1], row[2], row[3])))
    check("  observations recorded", row[4] == 3, str(row[4]))
    check("  and completed_at is set", bool(row[5]))

    print("\n--- 17. The metrics read back correctly ---")
    import importlib
    import research_metrics as RM
    importlib.reload(RM)
    m = RM.collect(db)
    check("one completed run", m["completed"] == 1, str(m["completed"]))
    check("  average is a real number", m["avg_seconds"] is not None)
    check("  p50 and p95 exist", m["p50_seconds"] is not None
          and m["p95_seconds"] is not None)
    check("  cache hit rate computed", m["cache_hit_rate"] is not None)
    check("percentile of nothing is None, not zero",
          RM.percentile([], 95) is None)
    check("rate with no denominator is None, not zero",
          RM.rate(0, 0) is None)
    check("nearest-rank p95 of 1..100 is 95", RM.percentile(list(range(1, 101)), 95) == 95)

    print("\n--- 18. The evidence protocol is untouched ---")
    soul = (HERE / "souls" / "nova.md").read_text(encoding="utf-8")
    check("the evidence protocol still overrides everything",
          "EVIDENCE PROTOCOL" in soul)
    check("  prior knowledge is still not evidence",
          "not\nevidence" in soul or "is not evidence" in soul
          or "knowledge is not" in soul, "phrase present")
    check("  the stopping rules were added", "STOPPING RULES" in soul)
    check("  and they do not licence invention",
          "unrecoverable mistake" in soul or "never a reason" in soul)
    src = (HERE / "research_mcp.py").read_text(encoding="utf-8")
    check("a refused fetch returns no content",
          "budget_exhausted" in src and "do not fetch again" in src)

    print("\n" + "=" * 78)
    print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
