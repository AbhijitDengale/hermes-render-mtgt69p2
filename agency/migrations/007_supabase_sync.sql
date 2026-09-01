-- ===========================================================================
-- 007 — Supabase as an external lead source and a reporting mirror
--
-- Direction of authority is one-way and deliberate:
--
--     Supabase  ->  agency.db      leads come in, once
--     agency.db ->  Supabase       operational state goes back, continuously
--
-- agency.db and MailHub stay the sources of truth. Supabase never decides
-- anything: it supplies leads and mirrors outcomes. Nothing here reads a
-- Supabase status back into Hermes state.
--
-- The outbox exists because a Gmail send that really happened must not be
-- rolled back because a mirror was briefly unreachable. The state machine
-- commits first; the mirror catches up.
-- ===========================================================================

-- One row per lead that came from Supabase. Kept separate from `leads` so the
-- pipeline schema owes nothing to where a lead originated.
CREATE TABLE IF NOT EXISTS supabase_leads (
    lead_id             TEXT PRIMARY KEY REFERENCES leads(id),
    supabase_id         TEXT NOT NULL UNIQUE,      -- the uuid in public.leads
    source              TEXT,                      -- leadsking, hermes_sync_test, …
    external_lead_id    TEXT,
    imported_at         TEXT NOT NULL DEFAULT (datetime('now')),
    last_synced_at      TEXT,
    last_synced_state   TEXT
);

-- The source's own identity, when it has one. Preferred over email for dedupe
-- because two records can share an address while being different businesses,
-- and because the source knows its own keys better than we do.
CREATE UNIQUE INDEX IF NOT EXISTS idx_supabase_leads_source_external
    ON supabase_leads (source, external_lead_id)
    WHERE external_lead_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- The outbox. A state change is recorded here after agency.db has committed,
-- and delivered separately. Failure to deliver is a retry, never a rollback.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supabase_sync_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         TEXT NOT NULL,
    supabase_id     TEXT,
    event_type      TEXT NOT NULL,      -- state | research | qa | queued | sent | …
    payload_json    TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','processing','synced','failed')),
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    next_retry_at   TEXT NOT NULL DEFAULT (datetime('now')),
    synced_at       TEXT,

    -- Two identical events for the same lead and state are the same event.
    -- Without this a retick, a reconcile and a live transition would each
    -- enqueue their own copy and the mirror would be written three times.
    dedupe_key      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_dedupe
    ON supabase_sync_outbox (dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status <> 'failed';

CREATE INDEX IF NOT EXISTS idx_outbox_due
    ON supabase_sync_outbox (status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_outbox_lead
    ON supabase_sync_outbox (lead_id);

-- ---------------------------------------------------------------------------
-- How many leads were admitted on a given operational day. Kept as a table
-- rather than counted from `leads.created_at` so the operational day can be
-- Asia/Kolkata without every query having to know that.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead_intake_days (
    day             TEXT PRIMARY KEY,   -- YYYY-MM-DD in the operational timezone
    imported        INTEGER NOT NULL DEFAULT 0,
    target          INTEGER,
    first_at        TEXT NOT NULL DEFAULT (datetime('now')),
    last_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
