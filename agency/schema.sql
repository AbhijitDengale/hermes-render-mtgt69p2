-- =====================================================================
-- agency.db — Hermes Agency System, Phase 2
-- Domain state for outreach. Deliberately SEPARATE from kanban.db so a
-- Hermes upgrade can never migrate/clobber campaign data.
--
-- Secrets are NEVER stored here. sender_accounts.auth_secret_ref holds the
-- NAME of a Render env var; the Mail Service resolves it at runtime.
-- =====================================================================

PRAGMA journal_mode = WAL;      -- concurrent readers while a writer commits
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- ---------------------------------------------------------------------
-- campaigns
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaigns (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','active','paused','archived')),
    country             TEXT,
    niche               TEXT,
    persona             TEXT,
    service_offer       TEXT,
    -- ECHO timing is per-campaign, never hardcoded globally.
    followup_schedule   TEXT NOT NULL DEFAULT '[3,7,12]',   -- JSON: days after send
    max_followups       INTEGER NOT NULL DEFAULT 3,
    allow_repeat_outreach INTEGER NOT NULL DEFAULT 0,
    daily_send_cap      INTEGER,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- sender_accounts — 12+ mailboxes. NO passwords here.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sender_accounts (
    id                  TEXT PRIMARY KEY,          -- slug, e.g. 'acct_ops1'
    email               TEXT NOT NULL UNIQUE,
    display_name        TEXT NOT NULL,
    -- Name of the Render env var PREFIX holding credentials.
    -- e.g. 'MAIL_ACCT_OPS1' -> MAIL_ACCT_OPS1_PASSWORD etc.
    auth_secret_ref     TEXT NOT NULL,
    imap_host           TEXT NOT NULL DEFAULT 'imap.gmail.com',
    imap_port           INTEGER NOT NULL DEFAULT 993,
    smtp_host           TEXT NOT NULL DEFAULT 'smtp.gmail.com',
    smtp_port           INTEGER NOT NULL DEFAULT 587,
    campaign_id         TEXT REFERENCES campaigns(id),   -- NULL = any campaign
    country             TEXT,                            -- NULL = any country
    enabled             INTEGER NOT NULL DEFAULT 1,
    daily_limit         INTEGER NOT NULL DEFAULT 40,
    hourly_limit        INTEGER NOT NULL DEFAULT 8,
    sent_today          INTEGER NOT NULL DEFAULT 0,
    sent_this_hour      INTEGER NOT NULL DEFAULT 0,
    sent_total          INTEGER NOT NULL DEFAULT 0,
    error_count         INTEGER NOT NULL DEFAULT 0,
    consecutive_errors  INTEGER NOT NULL DEFAULT 0,
    last_sent_at        TEXT,
    last_error_at       TEXT,
    last_error          TEXT,
    cooldown_until      TEXT,
    counters_reset_day  TEXT,
    counters_reset_hour TEXT,
    health              TEXT NOT NULL DEFAULT 'healthy'
                        CHECK (health IN ('healthy','degraded','cooldown','disabled','auth_failed')),
    warmup_stage        INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sender_pick
    ON sender_accounts(enabled, health, campaign_id);

-- ---------------------------------------------------------------------
-- leads + state machine
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
    id                  TEXT PRIMARY KEY,
    campaign_id         TEXT REFERENCES campaigns(id),
    business_name       TEXT,
    contact_name        TEXT,
    email               TEXT,
    phone               TEXT,
    website             TEXT,
    city                TEXT,
    -- NOTE: your lead CSV field "state" (US state / province) maps HERE, to
    -- `region`. The column `state` below is the WORKFLOW state machine.
    region              TEXT,
    country             TEXT,
    niche               TEXT,
    source              TEXT,
    notes               TEXT,

    state               TEXT NOT NULL DEFAULT 'NEW' CHECK (state IN (
                            'NEW','RESEARCH_PENDING','RESEARCHING','RESEARCH_COMPLETE',
                            'COPY_PENDING','COPY_READY','QA_PENDING','QA_REJECTED',
                            'READY_TO_SEND','SENT','FOLLOWUP_WAITING','FOLLOWUP_PENDING',
                            'REPLIED','POSITIVE','NEGATIVE','UNSUBSCRIBED','BOUNCED',
                            'HUMAN_REVIEW','MEETING_STAGE','CLOSED','ERROR')),
    state_reason        TEXT,
    state_changed_at    TEXT NOT NULL DEFAULT (datetime('now')),

    -- per-lead lock: prevents two agents acting on one lead at once.
    -- Lease-based so a Render restart cannot deadlock a lead forever.
    locked_by           TEXT,
    locked_until        TEXT,

    research_json       TEXT,       -- NOVA structured output
    research_confidence REAL,
    followup_stage      INTEGER NOT NULL DEFAULT 0,
    next_action_at      TEXT,
    replied_at          TEXT,
    error_count         INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,

    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Duplicate protection: one lead per email per campaign.
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_campaign_email
    ON leads(campaign_id, lower(email)) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_state    ON leads(state);
CREATE INDEX IF NOT EXISTS idx_leads_due      ON leads(next_action_at) WHERE next_action_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_lock     ON leads(locked_until)   WHERE locked_until IS NOT NULL;

-- Legal transitions. MAYA enforces workflow by consulting this table.
CREATE TABLE IF NOT EXISTS state_transitions (
    from_state          TEXT NOT NULL,
    to_state            TEXT NOT NULL,
    PRIMARY KEY (from_state, to_state)
);

-- ---------------------------------------------------------------------
-- suppression — checked before EVERY send. Fail closed.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suppression (
    email               TEXT PRIMARY KEY,          -- store lowercased
    reason              TEXT NOT NULL
                        CHECK (reason IN ('unsubscribed','bounced','do_not_contact','complaint','manual')),
    detail              TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- email_threads — Gmail thread <-> lead mapping (inbound routing)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_threads (
    id                  TEXT PRIMARY KEY,
    lead_id             TEXT NOT NULL REFERENCES leads(id),
    campaign_id         TEXT REFERENCES campaigns(id),
    sender_account_id   TEXT NOT NULL REFERENCES sender_accounts(id),
    provider_thread_id  TEXT,
    root_message_id     TEXT,       -- RFC822 Message-ID of our first send
    subject             TEXT,
    recipient           TEXT NOT NULL,
    last_inbound_at     TEXT,
    last_outbound_at    TEXT,
    state               TEXT NOT NULL DEFAULT 'open'
                        CHECK (state IN ('open','replied','closed','bounced')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_threads_provider ON email_threads(provider_thread_id);
CREATE INDEX IF NOT EXISTS idx_threads_root     ON email_threads(root_message_id);
CREATE INDEX IF NOT EXISTS idx_threads_lead     ON email_threads(lead_id);

-- ---------------------------------------------------------------------
-- messages — every inbound and outbound email
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id                  TEXT PRIMARY KEY,
    lead_id             TEXT REFERENCES leads(id),
    campaign_id         TEXT REFERENCES campaigns(id),
    thread_id           TEXT REFERENCES email_threads(id),
    sender_account_id   TEXT REFERENCES sender_accounts(id),
    direction           TEXT NOT NULL CHECK (direction IN ('outbound','inbound')),
    kind                TEXT NOT NULL DEFAULT 'outreach'
                        CHECK (kind IN ('outreach','followup','reply','bounce','auto_reply')),
    followup_stage      INTEGER,
    recipient           TEXT,
    from_email          TEXT,
    subject             TEXT,
    body                TEXT,
    provider_message_id TEXT,       -- RFC822 Message-ID
    in_reply_to         TEXT,
    qa_status           TEXT CHECK (qa_status IN ('approved','rejected','needs_review')),
    qa_issues           TEXT,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','queued','sent','failed','simulated','received')),
    dry_run             INTEGER NOT NULL DEFAULT 1,
    error               TEXT,
    sent_at             TEXT,
    received_at         TEXT,
    classification      TEXT,       -- LEO reply category
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Inbound dedupe: the same Message-ID must never be processed twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_provider_id
    ON messages(provider_message_id) WHERE provider_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id);

-- ---------------------------------------------------------------------
-- send_jobs — outbox. Idempotency across Render restarts.
-- A restart mid-send can NEVER produce a duplicate email.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS send_jobs (
    id                  TEXT PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,   -- lead_id:kind:stage
    lead_id             TEXT NOT NULL REFERENCES leads(id),
    message_id          TEXT REFERENCES messages(id),
    campaign_id         TEXT REFERENCES campaigns(id),
    sender_account_id   TEXT REFERENCES sender_accounts(id),
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','claimed','sent','failed','dead','cancelled','simulated')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    claimed_by          TEXT,
    claimed_until       TEXT,
    run_after           TEXT NOT NULL DEFAULT (datetime('now')),
    last_error          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_send_jobs_due ON send_jobs(status, run_after);

-- ---------------------------------------------------------------------
-- followups — ECHO's schedule
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS followups (
    id                  TEXT PRIMARY KEY,
    lead_id             TEXT NOT NULL REFERENCES leads(id),
    campaign_id         TEXT REFERENCES campaigns(id),
    stage               INTEGER NOT NULL,
    scheduled_for       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'scheduled'
                        CHECK (status IN ('scheduled','sent','cancelled','skipped','failed')),
    cancel_reason       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_followups_lead_stage ON followups(lead_id, stage);
CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(status, scheduled_for);

-- ---------------------------------------------------------------------
-- agent_tasks — the task envelope from the spec
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_tasks (
    id                  TEXT PRIMARY KEY,
    lead_id             TEXT REFERENCES leads(id),
    campaign_id         TEXT REFERENCES campaigns(id),
    from_agent          TEXT NOT NULL,
    to_agent            TEXT NOT NULL,
    task_type           TEXT NOT NULL,
    priority            TEXT NOT NULL DEFAULT 'normal'
                        CHECK (priority IN ('low','normal','high','urgent')),
    payload             TEXT,       -- JSON
    result              TEXT,       -- JSON
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','claimed','running','done','failed','dead','cancelled')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    claimed_by          TEXT,
    claimed_until       TEXT,
    run_after           TEXT NOT NULL DEFAULT (datetime('now')),
    last_error          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_queue ON agent_tasks(status, run_after, to_agent);

-- ---------------------------------------------------------------------
-- agent_runs — observability per spec section
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_runs (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT REFERENCES agent_tasks(id),
    lead_id             TEXT REFERENCES leads(id),
    agent               TEXT NOT NULL,
    action              TEXT NOT NULL,
    result              TEXT,
    duration_ms         INTEGER,
    error               TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_runs_lead ON agent_runs(lead_id);

-- ---------------------------------------------------------------------
-- events — append-only workflow audit (state transitions etc.)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id             TEXT,
    campaign_id         TEXT,
    agent               TEXT,
    event_type          TEXT NOT NULL,
    from_state          TEXT,
    to_state            TEXT,
    detail              TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id, created_at);

-- ---------------------------------------------------------------------
-- audit_logs — security-sensitive actions (sends, approvals, overrides)
-- Never contains credentials.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    actor               TEXT NOT NULL,      -- agent name or 'human'
    action              TEXT NOT NULL,
    subject_type        TEXT,
    subject_id          TEXT,
    detail              TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- human_escalations — HUMAN_REVIEW queue
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS human_escalations (
    id                  TEXT PRIMARY KEY,
    lead_id             TEXT REFERENCES leads(id),
    campaign_id         TEXT REFERENCES campaigns(id),
    raised_by           TEXT NOT NULL,
    reason              TEXT NOT NULL,
    reply_summary       TEXT,
    recommended_action  TEXT,
    draft_response      TEXT,
    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','approved','rejected','edited','resolved')),
    human_response      TEXT,
    resolved_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_escalations_open ON human_escalations(status);

-- ---------------------------------------------------------------------
-- campaign_metrics — ORBIT (read-only consumer)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaign_metrics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    day                 TEXT NOT NULL,
    campaign_id         TEXT,
    sender_account_id   TEXT,
    country             TEXT,
    niche               TEXT,
    leads_entered       INTEGER NOT NULL DEFAULT 0,
    emails_attempted    INTEGER NOT NULL DEFAULT 0,
    emails_sent         INTEGER NOT NULL DEFAULT 0,
    failures            INTEGER NOT NULL DEFAULT 0,
    bounces             INTEGER NOT NULL DEFAULT 0,
    replies             INTEGER NOT NULL DEFAULT 0,
    positive_replies    INTEGER NOT NULL DEFAULT 0,
    negative_replies    INTEGER NOT NULL DEFAULT 0,
    unsubscribes        INTEGER NOT NULL DEFAULT 0,
    meetings_requested  INTEGER NOT NULL DEFAULT 0,
    followups_sent      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (day, campaign_id, sender_account_id)
);

-- ---------------------------------------------------------------------
-- browser/research audit (shared worker, per your Q1 requirements)
-- ---------------------------------------------------------------------
-- research_cache — avoid re-fetching an unchanged page.
-- content_hash lets NOVA skip re-analysis when a page hasn't changed.
CREATE TABLE IF NOT EXISTS research_cache (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id             TEXT,
    url                 TEXT NOT NULL,
    source_url          TEXT,
    page_type           TEXT,       -- home|about|services|contact|pricing|booking|locations|other
    retrieval_method    TEXT NOT NULL CHECK (retrieval_method IN ('http','search','firecrawl','steel','cache')),
    content_hash        TEXT,
    structured_data     TEXT,       -- JSON
    status              TEXT NOT NULL DEFAULT 'ok'
                        CHECK (status IN ('ok','partial','failed','blocked','timeout')),
    retrieved_at        TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_cache_url ON research_cache(url);
CREATE INDEX IF NOT EXISTS idx_research_cache_lead ON research_cache(lead_id);
CREATE INDEX IF NOT EXISTS idx_research_cache_exp  ON research_cache(expires_at);

CREATE TABLE IF NOT EXISTS research_fetches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id             TEXT,
    url                 TEXT NOT NULL,
    domain              TEXT,
    tier                TEXT NOT NULL CHECK (tier IN ('http','search','firecrawl','browser')),
    status              TEXT NOT NULL,
    http_status         INTEGER,
    duration_ms         INTEGER,
    bytes               INTEGER,
    error               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fetch_domain ON research_fetches(domain, created_at);
