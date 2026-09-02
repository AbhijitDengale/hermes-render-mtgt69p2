-- Email verification gate for lead admission.
--
-- Apply in the Supabase SQL editor for project ggotevbwmdbkxdokvmnu (the
-- leads project). Hermes holds only a PostgREST service key for that project,
-- which cannot run DDL, so this file is applied by a person rather than by
-- the deploy. Everything here is additive and safe to re-run.
--
-- What already exists and is reused (inspected on 2026-09-02 via the
-- PostgREST OpenAPI document, 75 columns):
--   public.leads.email_verification_status  text     -- verdict
--   public.leads.email_verified             boolean  -- verdict == 'valid'
--   public.leads.raw_data                   jsonb    -- evidence, under key
--                                                       'email_verification'
-- `score`, `score_reason` and `attempts` are the LEAD's own fields and are
-- deliberately not overloaded.
--
-- Nothing below is required for the gate to hold. The Python claim path
-- already refuses and releases any claimed lead that is not verified valid
-- for its current address. What this adds is the structural version: a claim
-- function that never hands out an unverified lead in the first place, so the
-- database enforces the rule rather than the caller correcting it afterwards.

-- ---------------------------------------------------------------------------
-- 1. Cheap lookups for the worker and the claim.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS leads_email_verification_status_idx
    ON public.leads (email_verification_status)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS leads_claimable_verified_idx
    ON public.leads (status, hermes_status)
    WHERE email_verification_status = 'valid' AND email_verified = true;

-- ---------------------------------------------------------------------------
-- 2. Verification-aware claim. Option B: a new function, so the existing
--    claim_leads_for_hermes() and anything that calls it are untouched.
--
-- Before running: open claim_leads_for_hermes() in the SQL editor and confirm
-- it sets the same columns on claim (claimed_at, and whatever hermes_status
-- value it uses while a row is held). Hermes could not read that body, so the
-- SET list below mirrors the behaviour observed from the outside.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.claim_verified_leads_for_hermes(p_limit integer)
RETURNS SETOF public.leads
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  RETURN QUERY
  WITH picked AS (
    SELECT l.id
      FROM public.leads AS l
     WHERE l.status = 'ready'
       AND l.hermes_status = 'not_imported'
       AND l.is_active = true
       AND l.email IS NOT NULL
       AND btrim(l.email) <> ''
       -- the verdict
       AND l.email_verification_status = 'valid'
       AND l.email_verified = true
       -- and the verdict must be for THIS address: an email edited after
       -- verification does not inherit the old clearance.
       AND lower(btrim(l.email))
           = lower(btrim(COALESCE(l.raw_data #>> '{email_verification,verified_email}', '')))
     ORDER BY l.created_at
     LIMIT GREATEST(p_limit, 0)
       FOR UPDATE SKIP LOCKED
  )
  UPDATE public.leads AS l
     SET claimed_at = now(),
         updated_at = now()
    FROM picked
   WHERE l.id = picked.id
  RETURNING l.*;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_verified_leads_for_hermes(integer) FROM public;
GRANT EXECUTE ON FUNCTION public.claim_verified_leads_for_hermes(integer) TO service_role;

-- ---------------------------------------------------------------------------
-- 3. Once this function exists, point Hermes at it by setting, in
--    /opt/data/.env on the Hermes box:
--        SUPABASE_CLAIM_RPC=claim_verified_leads_for_hermes
--    The Python guard stays in place as defence in depth either way.
-- ---------------------------------------------------------------------------

-- Proof query (run after applying). Every row here would be admitted by the
-- new function; if any of them shows a non-valid verdict, do not switch.
--   SELECT id, email, email_verification_status, email_verified,
--          raw_data #>> '{email_verification,verified_email}' AS verified_email
--     FROM public.leads
--    WHERE status = 'ready' AND hermes_status = 'not_imported'
--      AND email_verification_status = 'valid' AND email_verified = true
--      AND lower(btrim(email)) = lower(btrim(raw_data #>> '{email_verification,verified_email}'));
