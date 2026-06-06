-- ============================================================
-- TenantSentry.ai — Add stage_costs column to audit_run
-- Per-model token counts and USD costs for pipeline economics.
-- Run once in Supabase SQL editor.
-- ============================================================

ALTER TABLE audit_run
    ADD COLUMN IF NOT EXISTS stage_costs JSONB;

COMMENT ON COLUMN audit_run.stage_costs IS
    'Per-model token counts and USD costs from the audit pipeline: '
    '{ '
    '  haiku_input_tokens, haiku_output_tokens, haiku_cost_usd, '
    '  sonnet_input_tokens, sonnet_output_tokens, sonnet_cost_usd, '
    '  opus_input_tokens, opus_output_tokens, opus_cost_usd, '
    '  total_input_tokens, total_output_tokens, total_cost_usd '
    '}. '
    'Null for jobs completed before this column was added. '
    'Pricing: Haiku $0.80/$4.00, Sonnet $3.00/$15.00, Opus $15.00/$75.00 per MTok in/out.';

-- Convenience view: flattened economics per audit for dashboards and cost monitoring.
-- Usage: SELECT * FROM audit_economics ORDER BY completed_at DESC LIMIT 50;
CREATE OR REPLACE VIEW audit_economics AS
SELECT
    job_id,
    tenant_name,
    jurisdiction,
    filename,
    status,
    completed_at,
    (stage_costs ->> 'total_cost_usd')::numeric        AS total_cost_usd,
    (stage_costs ->> 'haiku_cost_usd')::numeric         AS haiku_cost_usd,
    (stage_costs ->> 'sonnet_cost_usd')::numeric        AS sonnet_cost_usd,
    (stage_costs ->> 'opus_cost_usd')::numeric          AS opus_cost_usd,
    (stage_costs ->> 'total_input_tokens')::int         AS total_input_tokens,
    (stage_costs ->> 'total_output_tokens')::int        AS total_output_tokens,
    (stage_costs ->> 'haiku_input_tokens')::int         AS haiku_input_tokens,
    (stage_costs ->> 'sonnet_input_tokens')::int        AS sonnet_input_tokens,
    (stage_costs ->> 'opus_input_tokens')::int          AS opus_input_tokens,
    (stage_timings ->> 'total_ms')::int                 AS total_ms,
    (stage_timings ->> 'triage_ms')::int                AS triage_ms,
    (stage_timings ->> 'analysis_ms')::int              AS analysis_ms,
    (stage_timings ->> 'ocr_ms')::int                   AS ocr_ms
FROM audit_run
WHERE stage_costs IS NOT NULL;
