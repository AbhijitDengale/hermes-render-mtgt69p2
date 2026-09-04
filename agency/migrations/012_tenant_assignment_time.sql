-- When a message was assigned its tenant.
--
-- The allocator shares new work by how much each tenant already has and how
-- recently it was given some. Without an explicit column that had to be
-- inferred from updated_at, which moves whenever anything about the row
-- changes and so is not the assignment time at all.
--
-- Nullable and backfilled from what is already known: existing rows keep
-- their tenant and simply carry the best timestamp available for it.

ALTER TABLE messages ADD COLUMN tenant_assigned_at TEXT;

UPDATE messages
   SET tenant_assigned_at = COALESCE(updated_at, created_at)
 WHERE tenant_user_id IS NOT NULL AND tenant_assigned_at IS NULL;
