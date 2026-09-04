-- Two reversible brakes, added after 2026-09-04.
--
-- A sender pause: four mailboxes went to 21-29% bounce/block within a day of
-- joining the rotation, and there was no way to stand one down short of
-- breaking its credentials. Pausing must not touch OAuth, the professional
-- alias, or who owns which conversation -- it only stops new work being given
-- to that mailbox and stops it transmitting until the cooldown passes.
--
-- A lead hold: the role-account policy released several hundred guessed
-- addresses, and withdrawing them by changing their state would rewrite
-- history and lose where each lead had actually got to. A hold reason leaves
-- the state exactly as it is and simply makes the lead ineligible for work
-- until the reason is cleared.

ALTER TABLE tenant_health ADD COLUMN paused_until TEXT;
ALTER TABLE tenant_health ADD COLUMN paused_reason TEXT;
ALTER TABLE tenant_health ADD COLUMN paused_at TEXT;

ALTER TABLE leads ADD COLUMN hold_reason TEXT;
ALTER TABLE leads ADD COLUMN held_at TEXT;

CREATE INDEX IF NOT EXISTS idx_leads_hold ON leads(hold_reason)
    WHERE hold_reason IS NOT NULL;
