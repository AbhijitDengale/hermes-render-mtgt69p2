-- Discord delivery state for a human-review escalation.
--
-- The review-alerts cron runs every two minutes. `notified_at` alone answers
-- "has this ever been announced", which is enough to post once but not enough
-- to tell whether the card is still accurate. Without that, the only safe
-- behaviours are to post once and let the card go stale, or to repost every
-- cycle and flood the channel.
--
-- These four columns let it post once, edit the message when what the card
-- would say has materially changed, and otherwise leave Discord alone.
-- `notified_at` is kept and still written, so nothing that reads it changes.

ALTER TABLE human_escalations ADD COLUMN first_alerted_at    TEXT;
ALTER TABLE human_escalations ADD COLUMN last_alerted_at     TEXT;
ALTER TABLE human_escalations ADD COLUMN discord_message_id  TEXT;
ALTER TABLE human_escalations ADD COLUMN alert_version       INTEGER NOT NULL DEFAULT 0;
-- Hash of the fields the card is built from, so "has it changed" is a
-- comparison rather than a guess.
ALTER TABLE human_escalations ADD COLUMN alert_fingerprint   TEXT;

-- Anything already announced under the old scheme counts as posted, so
-- deploying this does not repost the open queue.
UPDATE human_escalations
   SET first_alerted_at = notified_at,
       last_alerted_at  = notified_at,
       alert_version    = 1
 WHERE notified_at IS NOT NULL AND first_alerted_at IS NULL;
