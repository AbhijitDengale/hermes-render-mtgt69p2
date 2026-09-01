-- Phase C — columns the outbound pipeline needs on agency.db.
--
-- Additive only. SQLite's ALTER TABLE ADD COLUMN rewrites no data and cannot
-- fail partway, so this is safe to run against the live database.
--
-- Source-of-truth note: `messages` here holds DRAFTS and their QA state — what
-- the agency intends to say. MailHub owns what actually went out. The two are
-- linked by the ids below rather than duplicated, so there is still exactly one
-- authority for delivery.

-- What ARIA cited, so SENTINEL can check each claim against NOVA's evidence.
ALTER TABLE messages ADD COLUMN claims_used TEXT;

-- sha256(subject || NUL || body) of the text SENTINEL approved. MailHub stores
-- the same hash; if they differ, the copy changed after review.
ALTER TABLE messages ADD COLUMN content_hash TEXT;

-- Linkage to MailHub. Kept as ids, not copies: duplicating the message body
-- into two databases is how they drift.
ALTER TABLE messages ADD COLUMN approval_id TEXT;
ALTER TABLE messages ADD COLUMN mailhub_queue_id TEXT;
ALTER TABLE messages ADD COLUMN mailhub_account_id TEXT;
ALTER TABLE messages ADD COLUMN provider_thread_id TEXT;

-- Deterministic, derived from lead + campaign + stage + approved content hash.
-- A retried send task presents the same key, so MailHub returns the original
-- message instead of creating a second one.
ALTER TABLE messages ADD COLUMN idempotency_key TEXT;

ALTER TABLE messages ADD COLUMN updated_at TEXT;

-- One draft per lead per stage. The message id is derived from both, so a
-- rewrite after a QA rejection replaces the draft instead of leaving two rows
-- with nothing to say which was approved.
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_lead_stage
    ON messages(lead_id, followup_stage);

CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
