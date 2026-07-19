-- Rollback for 033_multiuser_share.sql
DROP FUNCTION IF EXISTS personal.share_conversation(TEXT, TEXT, TEXT, TEXT);
DROP FUNCTION IF EXISTS personal.unshare_conversation(TEXT, TEXT);
-- Grants on tables are left in place: they are harmless without the functions
-- and other multiuser objects may rely on hippocampus_definer table access.
