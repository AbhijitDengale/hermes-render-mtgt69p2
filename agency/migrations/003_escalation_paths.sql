-- Any state a lead can wait in must be able to reach a human.
--
-- Found by live test 7B: an out-of-office with no parseable return date is
-- supposed to pause the lead for a human, but FOLLOWUP_WAITING -> HUMAN_REVIEW
-- did not exist, so the escalation was refused and swallowed. The lead was
-- left in FOLLOWUP_WAITING with no OOO hold, where blocked_reason() returns
-- None — meaning a future follow-up would have been cleared to send while the
-- person was still away. That is precisely the outcome the OOO handling exists
-- to prevent.
--
-- The gap was structural: escalation paths were defined out of the *working*
-- states and out of REPLIED, but not out of the two states a lead rests in
-- after a send. Being unable to ask for help is never the safe default.

INSERT OR IGNORE INTO state_transitions (from_state, to_state) VALUES
    ('SENT',             'HUMAN_REVIEW'),
    ('FOLLOWUP_WAITING', 'HUMAN_REVIEW'),
    ('FOLLOWUP_WAITING', 'ERROR'),
    -- A bounce or an opt-out can arrive while a follow-up is mid-flight.
    ('FOLLOWUP_PENDING', 'HUMAN_REVIEW'),
    ('FOLLOWUP_PENDING', 'FOLLOWUP_WAITING');
