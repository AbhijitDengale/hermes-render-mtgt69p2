-- ===========================================================================
-- 005 — a pause that actually pauses, and a lead that can start over.
--
-- Two edge cases found during the V1 audit.
--
-- (a) Marking a campaign 'paused' stopped nothing. due() and blocked_reason()
--     never looked at campaigns.status, so ECHO went on dispatching
--     follow-ups for a campaign the operator believed was halted.
--
--     A pause has to be reversible, so a blocked follow-up must stay
--     'scheduled' rather than being skipped — skipping would mean resuming the
--     campaign never brings it back. But ECHO ticks every two minutes, and
--     writing an event on each tick for each paused follow-up would bury the
--     real history. So the block is recorded on the row itself and an event is
--     written only when the reason changes.
--
-- (b) Kanban idempotency keys are agency:<lead_id>:<stage>, and lead ids are
--     deterministic (sha256 of campaign+email). Delete a lead and re-ingest it
--     and the key matched the COMPLETED task from its previous life: Kanban
--     returned that task, no worker was ever spawned, and the lead sat in
--     RESEARCHING forever.
--
--     The fix is a generation counter that increments each time a lead is
--     ingested afresh. The key becomes agency:<lead_id>:gen:<n>:<stage>, so a
--     new lifecycle gets new tasks while retries within one lifecycle still
--     collapse onto the same task — which is the protection worth keeping.
-- ===========================================================================

ALTER TABLE followups ADD COLUMN last_blocked_reason TEXT;
ALTER TABLE followups ADD COLUMN last_blocked_at    TEXT;

-- Starts at 1 for every lead that already exists; only a re-ingestion bumps it.
ALTER TABLE leads ADD COLUMN lifecycle_generation INTEGER NOT NULL DEFAULT 1;

-- The counter has to OUTLIVE the lead, or deleting the row would reset it to 1
-- and the collision would come straight back. This table is deliberately not
-- foreign-keyed to leads and is never cascaded: it is the memory of how many
-- lives an id has had.
CREATE TABLE IF NOT EXISTS lead_generations (
    lead_id     TEXT PRIMARY KEY,
    generation  INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Backfill, so leads ingested before this migration are already accounted for
-- rather than starting a second life at generation 1.
INSERT OR IGNORE INTO lead_generations (lead_id, generation)
    SELECT id, lifecycle_generation FROM leads;
