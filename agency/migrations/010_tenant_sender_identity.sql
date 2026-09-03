-- The professional identity each tenant's mailbox sends as.
--
-- Recorded by the queue health check straight from MailHub's own accounts
-- listing, so reporting and readiness use the identity the provider will
-- actually put in the From header rather than a mapping kept by hand.
--
-- A tenant whose mailbox has no verified identity cannot send: MailHub holds
-- its messages as needs_review. Storing the status here is what lets the
-- router refuse to route to it in the first place, instead of handing it
-- leads that would stall.

ALTER TABLE tenant_health ADD COLUMN sender_from_email    TEXT;
ALTER TABLE tenant_health ADD COLUMN sender_from_name     TEXT;
ALTER TABLE tenant_health ADD COLUMN sender_identity_status TEXT;
