-- 032_multiuser_source_identity_enforce_down.sql
-- Remove the conversation source-identity enforcement constraint.

ALTER TABLE personal.conversations
    DROP CONSTRAINT IF EXISTS conversations_source_identity_check;
