-- Delivery ledger for multi-part Discord reports.
--
-- The evening no-email list runs to dozens of Discord messages. If delivery
-- fails on part 14 of 35, the right recovery is to send parts 14-35 later and
-- never resend 1-13. That needs a durable record of which parts of which
-- report on which day actually landed, keyed so a retry is idempotent.
--
-- content_hash lets a retry detect that the underlying data changed since the
-- part was rendered (a lead gained an email in the meantime); the report is
-- then re-rendered for the undelivered remainder rather than sending stale
-- cards. Nothing about a lead is stored here -- only what was sent and when.

CREATE TABLE IF NOT EXISTS report_deliveries (
    report_day          TEXT    NOT NULL,   -- operational day (Asia/Kolkata)
    section             TEXT    NOT NULL,   -- e.g. 'no_email'
    part_no             INTEGER NOT NULL,   -- 1-based; 0 = the summary/header
    total_parts         INTEGER NOT NULL,
    content_hash        TEXT    NOT NULL,
    channel_id          TEXT    NOT NULL,
    discord_message_id  TEXT,
    delivered_at        TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (report_day, section, part_no)
);

CREATE INDEX IF NOT EXISTS idx_report_deliveries_pending
    ON report_deliveries (report_day, section)
    WHERE delivered_at IS NULL;
