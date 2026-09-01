-- ===========================================================================
-- 006 — research throughput: per-lead budgets, and the metadata to measure them
--
-- NOVA's research had no wall-clock ceiling. Each fetch had its own 45-second
-- timeout, so one slow site could hold a lead for minutes and there was no
-- record of how long anything took. At 400 leads a day that is the difference
-- between research finishing ahead of the send schedule and becoming the
-- bottleneck.
--
-- research_runs is one row per lead per research attempt. It is written by the
-- research MCP as fetches happen, and finalised when NOVA saves its findings,
-- so the timings are measured rather than reported by the agent.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS research_runs (
    lead_id             TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT,
    duration_ms         INTEGER,

    pages_attempted     INTEGER NOT NULL DEFAULT 0,
    pages_succeeded     INTEGER NOT NULL DEFAULT 0,
    pages_from_cache    INTEGER NOT NULL DEFAULT 0,
    pages_failed        INTEGER NOT NULL DEFAULT 0,
    pages_refused       INTEGER NOT NULL DEFAULT 0,   -- refused by budget or page cap

    observations_count  INTEGER,
    budget_exhausted    INTEGER NOT NULL DEFAULT 0,
    timed_out           INTEGER NOT NULL DEFAULT 0,
    research_status     TEXT,        -- ok | failed | insufficient_evidence

    -- Wall-clock seconds the fetches themselves consumed, so time lost to
    -- queueing behind the concurrency limit is visible separately from time
    -- spent waiting on Steel.
    fetch_seconds       REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_research_runs_started
    ON research_runs (started_at);
CREATE INDEX IF NOT EXISTS idx_research_runs_status
    ON research_runs (research_status);

-- ---------------------------------------------------------------------------
-- A cache hit was being audited with tier='cache', which the original CHECK
-- did not allow. _audit swallows its own exceptions so nothing ever surfaced —
-- every cache hit has been silently missing from research_fetches since the
-- cache was added, which makes a cache-hit rate impossible to compute.
--
-- SQLite cannot alter a CHECK in place, so the table is rebuilt. Existing rows
-- are carried over unchanged.
-- ---------------------------------------------------------------------------

PRAGMA foreign_keys=off;

CREATE TABLE research_fetches_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id             TEXT,
    url                 TEXT NOT NULL,
    domain              TEXT,
    tier                TEXT NOT NULL
                        CHECK (tier IN ('http','search','firecrawl','browser','cache')),
    status              TEXT NOT NULL,
    http_status         INTEGER,
    duration_ms         INTEGER,
    bytes               INTEGER,
    error               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO research_fetches_new
    (id, lead_id, url, domain, tier, status, http_status, duration_ms, bytes,
     error, created_at)
SELECT id, lead_id, url, domain, tier, status, http_status, duration_ms, bytes,
       error, created_at
  FROM research_fetches;

DROP TABLE research_fetches;
ALTER TABLE research_fetches_new RENAME TO research_fetches;

CREATE INDEX IF NOT EXISTS idx_research_fetches_lead
    ON research_fetches (lead_id);
CREATE INDEX IF NOT EXISTS idx_research_fetches_created
    ON research_fetches (created_at);

PRAGMA foreign_keys=on;
