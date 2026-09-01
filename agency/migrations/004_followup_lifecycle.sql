-- Explicit follow-up lifecycle, and escalation notification bookkeeping.
--
-- The bug: a follow-up ECHO had already dispatched stayed `scheduled` until
-- the message finally sent, so every subsequent cron tick re-evaluated it and
-- bumped `attempts`. Never unsafe — the compare-and-swap refused the repeat
-- transition — but the counter lied about how many times work had been handed
-- out, which is exactly the number you reach for when something looks wrong.
--
-- SQLite cannot widen a CHECK constraint in place, so this is the standard
-- table rebuild: create, copy, drop, rename, inside one transaction. Nothing
-- references `followups`, so there are no foreign keys to re-point.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS followups_new (
    id                  TEXT PRIMARY KEY,
    lead_id             TEXT NOT NULL REFERENCES leads(id),
    campaign_id         TEXT REFERENCES campaigns(id),
    stage               INTEGER NOT NULL,
    scheduled_for       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'scheduled'
                        CHECK (status IN ('scheduled','due','dispatched',
                                          'qa_pending','queued','sent',
                                          'cancelled','skipped','failed')),
    cancel_reason       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    cron_job_id         TEXT,
    message_id          TEXT,
    cancelled_at        TEXT,
    last_execution_at   TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    dispatched_at       TEXT
);

INSERT INTO followups_new
    (id, lead_id, campaign_id, stage, scheduled_for, status, cancel_reason,
     created_at, cron_job_id, message_id, cancelled_at, last_execution_at,
     attempts)
SELECT id, lead_id, campaign_id, stage, scheduled_for, status, cancel_reason,
       created_at, cron_job_id, message_id, cancelled_at, last_execution_at,
       attempts
  FROM followups;

DROP TABLE followups;
ALTER TABLE followups_new RENAME TO followups;

CREATE UNIQUE INDEX IF NOT EXISTS idx_followups_lead_stage
    ON followups(lead_id, stage);
CREATE INDEX IF NOT EXISTS idx_followups_due
    ON followups(scheduled_for) WHERE status = 'scheduled';

PRAGMA foreign_keys = ON;

-- --- escalation notification -------------------------------------------
-- Which escalations have already been announced, so a Discord alert is sent
-- once rather than on every tick.
ALTER TABLE human_escalations ADD COLUMN notified_at TEXT;
ALTER TABLE human_escalations ADD COLUMN notify_error TEXT;
ALTER TABLE human_escalations ADD COLUMN resolved_by TEXT;
ALTER TABLE human_escalations ADD COLUMN action TEXT;

CREATE INDEX IF NOT EXISTS idx_escalations_unnotified
    ON human_escalations(notified_at) WHERE notified_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_escalations_open
    ON human_escalations(status) WHERE status = 'open';
