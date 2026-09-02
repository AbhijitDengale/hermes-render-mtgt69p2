-- Multi-tenant sending.
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
-- 3. Which tenant a reply arrived in, so an opt-out is suppressed in the
--    mailbox that would otherwise send the next follow-up.
--
-- On dedupe: inbound_replies keeps UNIQUE(provider_message_id) rather than
-- being rebuilt around (tenant, id). provider_message_id is Gmail's own
-- message id, which Gmail assigns per mailbox -- the same email delivered to
-- two of our mailboxes gets two different values, and the shared identifier
-- across mailboxes is rfc822_message_id, which is stored separately and not
-- used for dedupe. So the existing constraint is already tenant-unique in
-- practice, and dropping and rebuilding a live table to express that would
-- add real risk for no behavioural change.

ALTER TABLE messages ADD COLUMN tenant_user_id INTEGER;
ALTER TABLE inbound_replies ADD COLUMN tenant_user_id INTEGER;

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

CREATE INDEX IF NOT EXISTS idx_messages_tenant ON messages(tenant_user_id);
CREATE INDEX IF NOT EXISTS idx_inbound_replies_tenant
    ON inbound_replies(tenant_user_id);
