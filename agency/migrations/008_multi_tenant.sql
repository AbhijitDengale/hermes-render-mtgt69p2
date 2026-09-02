-- Multi-tenant sending.
--
-- Three things the pipeline could not previously express.
--
-- 1. Which MailHub tenant a message belongs to. The tenant is chosen when
--    SENTINEL approves and must still be the same one when MAYA queues,
--    because MailHub matches an approval on (owner_user_id, content_hash).
--    Recomputing it at queue time would silently move a lead if the set of
--    available tenants changed in between, and the approval would then be
--    filed where the send path does not look. Persisting it makes that change
--    detectable instead of invisible.
--
-- 2. Whether a tenant is usable at all. Each credential lives in a different
--    profile's environment -- MAYA cannot see SENTINEL's approve key by
--    design -- so no single process can answer "is this tenant complete?".
--    Each profile records the outcome of checking its own credential here and
--    the router reads the combined picture. Only booleans are stored; no token
--    ever touches this database.
--
-- 3. That a reply is unique per tenant rather than globally. inbound_replies
--    had UNIQUE(provider_message_id), which is right for one mailbox and wrong
--    for five: the second tenant to report the same id would be silently
--    dropped as a duplicate. SQLite cannot remove a column constraint, so the
--    table is rebuilt with every column, default and foreign key it had.
--    Historical rows predate tenancy and keep tenant_user_id NULL rather than
--    being assigned a tenant nobody can verify after the fact; the index puts
--    them in one bucket so their exactly-once guarantee is unchanged.

ALTER TABLE messages ADD COLUMN tenant_user_id INTEGER;

CREATE TABLE IF NOT EXISTS tenant_health (
    tenant_name     TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    -- Each flag is set by the profile that actually holds that credential,
    -- from a live call to MailHub. NULL means "never checked", which is not
    -- the same as "checked and broken" and is treated as not ready either way.
    queue_ok        INTEGER,
    approve_ok      INTEGER,
    leo_ok          INTEGER,
    mailbox_ok      INTEGER,
    mailbox_email   TEXT,
    daily_limit     INTEGER,
    sent_today      INTEGER,
    health          TEXT,
    queue_checked_at   TEXT,
    approve_checked_at TEXT,
    leo_checked_at     TEXT,
    mailbox_checked_at TEXT
);

-- --- rebuild inbound_replies with a tenant-scoped uniqueness rule ----------
PRAGMA foreign_keys = OFF;

CREATE TABLE inbound_replies_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_user_id      INTEGER,
    provider_message_id TEXT NOT NULL,
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

INSERT INTO inbound_replies_new
      (id, tenant_user_id, provider_message_id, provider_thread_id,
       mailhub_inbound_id, lead_id, campaign_id, account_id, from_email,
       to_email, subject, body_text, received_at, matched_by, is_bounce,
       is_auto_reply, followups_cancelled, cancelled_count, classification,
       confidence, summary, recommended_action, draft_reply, requires_human,
       classified_at, leo_task_id, created_at)
SELECT id, NULL, provider_message_id, provider_thread_id,
       mailhub_inbound_id, lead_id, campaign_id, account_id, from_email,
       to_email, subject, body_text, received_at, matched_by, is_bounce,
       is_auto_reply, followups_cancelled, cancelled_count, classification,
       confidence, summary, recommended_action, draft_reply, requires_human,
       classified_at, leo_task_id, created_at
  FROM inbound_replies;

DROP TABLE inbound_replies;
ALTER TABLE inbound_replies_new RENAME TO inbound_replies;

-- COALESCE keeps the pre-tenancy rows in a single bucket, so a re-poll of an
-- old reply still collides with itself exactly as it did before.
CREATE UNIQUE INDEX idx_inbound_replies_tenant_msg
    ON inbound_replies (COALESCE(tenant_user_id, -1), provider_message_id);
CREATE INDEX idx_inbound_replies_lead ON inbound_replies(lead_id);
CREATE INDEX idx_inbound_replies_unclassified
    ON inbound_replies(classified_at) WHERE classified_at IS NULL;

PRAGMA foreign_keys = ON;

CREATE INDEX IF NOT EXISTS idx_messages_tenant ON messages(tenant_user_id);
