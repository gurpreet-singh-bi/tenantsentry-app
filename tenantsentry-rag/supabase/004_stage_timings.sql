-- ============================================================
-- TenantSentry.ai — Add stage_timings column to audit_run
-- Pipeline performance instrumentation (per-stage ms).
-- Run once in Supabase SQL editor.
-- ============================================================

ALTER TABLE audit_run
    ADD COLUMN IF NOT EXISTS stage_timings JSONB;

COMMENT ON COLUMN audit_run.stage_timings IS
    'Per-stage pipeline durations in milliseconds: '
    '{ ocr_ms, chunking_ms, embedding_ms, triage_ms, analysis_ms, dates_ms, total_ms }. '
    'Null for jobs that completed before this column was added.';
