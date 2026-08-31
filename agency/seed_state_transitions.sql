-- =====================================================================
-- state_transitions — the ONLY legal lead state moves.
-- MAYA consults this table before every transition. Anything not listed
-- here is rejected, so the workflow cannot drift into an invalid state.
--
-- Fail-safe principle: every state can reach HUMAN_REVIEW and ERROR.
-- Terminal states (CLOSED) have no outbound rows.
-- =====================================================================

DELETE FROM state_transitions;

INSERT OR IGNORE INTO state_transitions (from_state, to_state) VALUES
-- intake
('NEW','RESEARCH_PENDING'),
('NEW','UNSUBSCRIBED'),          -- already on suppression list at import
('NEW','ERROR'),
('NEW','HUMAN_REVIEW'),
('NEW','CLOSED'),

-- research (NOVA)
('RESEARCH_PENDING','RESEARCHING'),
('RESEARCH_PENDING','ERROR'),
('RESEARCH_PENDING','HUMAN_REVIEW'),
('RESEARCHING','RESEARCH_COMPLETE'),
('RESEARCHING','ERROR'),                -- RESEARCH_FAILED -> MAYA decides
('RESEARCHING','HUMAN_REVIEW'),
('RESEARCHING','RESEARCH_PENDING'),     -- retry after transient browser failure
('RESEARCH_COMPLETE','COPY_PENDING'),
('RESEARCH_COMPLETE','CLOSED'),         -- disqualified by research
('RESEARCH_COMPLETE','HUMAN_REVIEW'),

-- copywriting (ARIA)
('COPY_PENDING','COPY_READY'),
('COPY_PENDING','ERROR'),
('COPY_PENDING','HUMAN_REVIEW'),
('COPY_READY','QA_PENDING'),
('COPY_READY','ERROR'),

-- QA (SENTINEL)
('QA_PENDING','READY_TO_SEND'),
('QA_PENDING','QA_REJECTED'),
('QA_PENDING','HUMAN_REVIEW'),
('QA_PENDING','ERROR'),
('QA_REJECTED','COPY_PENDING'),         -- rewrite loop
('QA_REJECTED','HUMAN_REVIEW'),
('QA_REJECTED','CLOSED'),

-- send (MAIL SERVICE, gated by MAYA)
('READY_TO_SEND','SENT'),
('READY_TO_SEND','ERROR'),
('READY_TO_SEND','HUMAN_REVIEW'),
('READY_TO_SEND','UNSUBSCRIBED'),       -- suppression hit at send time
('READY_TO_SEND','CLOSED'),

-- post-send / follow-up (ECHO)
('SENT','FOLLOWUP_WAITING'),
('SENT','REPLIED'),
('SENT','BOUNCED'),
('SENT','UNSUBSCRIBED'),
('SENT','ERROR'),
('FOLLOWUP_WAITING','FOLLOWUP_PENDING'),
('FOLLOWUP_WAITING','REPLIED'),
('FOLLOWUP_WAITING','BOUNCED'),
('FOLLOWUP_WAITING','UNSUBSCRIBED'),
('FOLLOWUP_WAITING','CLOSED'),          -- sequence exhausted
('FOLLOWUP_PENDING','QA_PENDING'),      -- follow-ups are QA'd too
('FOLLOWUP_PENDING','SENT'),
('FOLLOWUP_PENDING','REPLIED'),
('FOLLOWUP_PENDING','BOUNCED'),
('FOLLOWUP_PENDING','UNSUBSCRIBED'),
('FOLLOWUP_PENDING','ERROR'),
('FOLLOWUP_PENDING','CLOSED'),

-- replies (LEO)
('REPLIED','POSITIVE'),
('REPLIED','NEGATIVE'),
('REPLIED','MEETING_STAGE'),
('REPLIED','UNSUBSCRIBED'),
('REPLIED','HUMAN_REVIEW'),
('REPLIED','CLOSED'),
('POSITIVE','MEETING_STAGE'),
('POSITIVE','HUMAN_REVIEW'),
('POSITIVE','CLOSED'),
('POSITIVE','NEGATIVE'),
('NEGATIVE','CLOSED'),
('NEGATIVE','UNSUBSCRIBED'),
('NEGATIVE','HUMAN_REVIEW'),
('MEETING_STAGE','HUMAN_REVIEW'),
('MEETING_STAGE','CLOSED'),
('MEETING_STAGE','NEGATIVE'),

-- human control
('HUMAN_REVIEW','RESEARCH_PENDING'),
('HUMAN_REVIEW','COPY_PENDING'),
('HUMAN_REVIEW','QA_PENDING'),
('HUMAN_REVIEW','READY_TO_SEND'),
('HUMAN_REVIEW','FOLLOWUP_WAITING'),
('HUMAN_REVIEW','POSITIVE'),
('HUMAN_REVIEW','NEGATIVE'),
('HUMAN_REVIEW','MEETING_STAGE'),
('HUMAN_REVIEW','UNSUBSCRIBED'),
('HUMAN_REVIEW','CLOSED'),
('HUMAN_REVIEW','ERROR'),

-- error recovery
('ERROR','RESEARCH_PENDING'),
('ERROR','COPY_PENDING'),
('ERROR','QA_PENDING'),
('ERROR','READY_TO_SEND'),
('ERROR','HUMAN_REVIEW'),
('ERROR','CLOSED'),

-- terminal-ish
('BOUNCED','CLOSED'),
('BOUNCED','HUMAN_REVIEW'),
('UNSUBSCRIBED','CLOSED');
-- CLOSED is terminal: no outbound transitions by design.
