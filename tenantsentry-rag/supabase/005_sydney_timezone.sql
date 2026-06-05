-- ============================================================
-- TenantSentry.ai — Set database timezone to Sydney
-- All NOW() calls and timestamp displays use Australia/Sydney.
-- Run once in Supabase SQL editor.
-- ============================================================

ALTER DATABASE postgres SET timezone TO 'Australia/Sydney';

-- Verify (should return 'Australia/Sydney')
SHOW timezone;
