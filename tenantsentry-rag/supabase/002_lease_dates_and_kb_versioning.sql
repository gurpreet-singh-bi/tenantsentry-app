-- ================================================================
-- TenantSentry.ai — Migration 002
-- lease_dates table + source_version on lease_chunks
--
-- Run in Supabase SQL Editor after 001_setup_all.sql
-- Safe to re-run (all statements use IF NOT EXISTS / DO blocks)
-- ================================================================


-- ────────────────────────────────────────────────────────────────
-- 1. Add source_version to lease_chunks
--    Tracks legislation version so re-loads can be scoped and
--    stale chunks can be identified/purged without nuking the table.
-- ────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'lease_chunks' AND column_name = 'source_version'
    ) THEN
        ALTER TABLE lease_chunks ADD COLUMN source_version TEXT;
        COMMENT ON COLUMN lease_chunks.source_version IS
            'e.g. "NSW_RLA_1994_v2026-06" — used to dedup re-loads and track staleness';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_lease_chunks_source_version
    ON lease_chunks (source_version);


-- ────────────────────────────────────────────────────────────────
-- 2. lease_dates — critical date registry extracted per audit
--    Powers the 12-Month Monitoring feature (date alerts + deadlines)
-- ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lease_dates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id              UUID NOT NULL REFERENCES audit_run(job_id) ON DELETE CASCADE,

    -- What kind of date is this
    date_type           TEXT NOT NULL CHECK (date_type IN (
        'lease_commencement',
        'lease_expiry',
        'option_exercise_deadline',
        'rent_review_cpi',
        'rent_review_market',
        'rent_review_fixed',
        'outgoings_reconciliation',
        'rent_free_end',
        'fitout_completion_deadline',
        'demolition_notice_window',
        'bank_guarantee_expiry',
        'make_good_deadline',
        'other'
    )),

    date_value          DATE,                   -- NULL if only a period/description is known
    date_description    TEXT NOT NULL,          -- Plain-English label, e.g. "Option 1 exercise deadline"
    clause_reference    TEXT,                   -- e.g. "Clause 4.2(b)" — where in the lease
    recurrence          TEXT,                   -- NULL | 'annual' | 'monthly' — for repeating reviews
    alert_days_before   INT NOT NULL DEFAULT 90,-- How many days before the date to alert
    alert_sent          BOOLEAN NOT NULL DEFAULT FALSE,
    alert_sent_at       TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lease_dates_job     ON lease_dates (job_id);
CREATE INDEX IF NOT EXISTS idx_lease_dates_type    ON lease_dates (date_type);
CREATE INDEX IF NOT EXISTS idx_lease_dates_value   ON lease_dates (date_value);
-- Index for alert engine: find dates needing alerts in next N days
CREATE INDEX IF NOT EXISTS idx_lease_dates_alert
    ON lease_dates (date_value, alert_sent)
    WHERE alert_sent = FALSE AND date_value IS NOT NULL;

ALTER TABLE lease_dates ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all" ON lease_dates
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ────────────────────────────────────────────────────────────────
-- 3. Verification query (run manually to confirm)
-- ────────────────────────────────────────────────────────────────
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public' ORDER BY table_name;
--
-- SELECT column_name, data_type FROM information_schema.columns
-- WHERE table_name = 'lease_chunks' AND column_name = 'source_version';
