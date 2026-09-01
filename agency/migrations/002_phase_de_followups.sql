-- Phase D/E — follow-up scheduling and inbound reply processing.
--
-- Additive only.
--
-- Division of ownership: agency.db owns follow-up WORKFLOW state (which stage
-- is due, why one was cancelled, what it produced). Hermes cron owns EXECUTION
-- TIMING. The cron job is a trigger, never the source of truth — if a job is
-- lost, the schedule is still here; if a job fires twice, the row says the
-- work is already done.

-- --- follow-up records ------------------------------------------------------
ALTER TABLE followups ADD COLUMN cron_job_id TEXT;
ALTER TABLE followups ADD COLUMN message_id TEXT;
ALTER TABLE followups ADD COLUMN cancelled_at TEXT;
ALTER TABLE followups ADD COLUMN last_execution_at TEXT;
ALTER TABLE followups ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;

-- One live schedule per (lead, stage). A cron job that fires twice, or two
-- schedulers racing, cannot create a second follow-up for the same stage.
CREATE UNIQUE INDEX IF NOT EXISTS idx_followups_lead_stage
    ON followups(lead_id, stage);

CREATE INDEX IF NOT EXISTS idx_followups_due
    ON followups(scheduled_for) WHERE status = 'scheduled';

-- --- inbound replies --------------------------------------------------------
-- Only messages MailHub has already classified as outreach replies land here.
-- The UNIQUE on provider_message_id is what makes "process a reply exactly
-- once" a database guarantee rather than a code path that must not be re-run:
-- a redelivery inserts zero rows and the caller stops.
CREATE TABLE IF NOT EXISTS inbound_replies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_message_id TEXT NOT NULL UNIQUE,
    provider_thread_id  TEXT,
    mailhub_inbound_id  INTEGER,
    lead_id             TEXT REFERENCES leads(id),
    campaign_id         TEXT REFERENCES campaigns(id),
    account_id          TEXT,
    from_email          TEXT,
    to_email            TEXT,
    subject             TEXT,
    body_text           TEXT,
    received_at         TEXT,
    matched_by          TEXT,
    is_bounce           INTEGER NOT NULL DEFAULT 0,
    is_auto_reply       INTEGER NOT NULL DEFAULT 0,

    -- Set the moment follow-ups are cancelled, BEFORE any classification runs.
    -- If LEO never completes, the prospect still receives no automated mail.
    followups_cancelled INTEGER NOT NULL DEFAULT 0,
    cancelled_count     INTEGER NOT NULL DEFAULT 0,

    classification      TEXT,
    confidence          REAL,
    summary             TEXT,
    recommended_action  TEXT,
    draft_reply         TEXT,
    requires_human      INTEGER NOT NULL DEFAULT 0,
    classified_at       TEXT,
    leo_task_id         TEXT,

    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_inbound_replies_lead ON inbound_replies(lead_id);
CREATE INDEX IF NOT EXISTS idx_inbound_replies_unclassified
    ON inbound_replies(classified_at) WHERE classified_at IS NULL;

-- --- out-of-office ----------------------------------------------------------
-- A parsed return date. Follow-ups are held until it passes; an unparseable
-- OOO gets a human instead of a guess, because guessing a return date is how
-- you email someone the morning they are still away.
ALTER TABLE leads ADD COLUMN ooo_until TEXT;
