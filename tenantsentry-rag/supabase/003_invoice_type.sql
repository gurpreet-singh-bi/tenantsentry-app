-- ============================================================
-- TenantSentry.ai — G3: Invoice type enum
-- Run in Supabase SQL editor if the invoice table doesn't
-- already exist (e.g. if you skipped 001_setup_all.sql).
-- Safe to run multiple times (IF NOT EXISTS guards).
-- ============================================================

-- Create invoice table with invoice_type constraint.
-- invoice_type values:
--   'estimate'     — Monthly outgoings estimate issued by landlord mid-year
--   'actuals'      — EOFY reconciliation (actual vs estimated outgoings)
--   'monthly_rent' — Standard rent invoice (base rent + GST)
--
-- Without this distinction the audit engine cannot apply the correct
-- logic: estimates use budgeted rates; actuals trigger the reconciliation
-- audit; monthly_rent drives the ongoing anomaly monitor (F16).

CREATE TABLE IF NOT EXISTS invoice (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES audit_run(job_id) ON DELETE CASCADE,
    invoice_type    TEXT NOT NULL
                        CHECK (invoice_type IN ('estimate', 'actuals', 'monthly_rent')),
    period_start    DATE,
    period_end      DATE,
    amount_cents    BIGINT,          -- total invoice amount in AUD cents (avoids float rounding)
    line_items      JSONB,           -- itemised outgoings lines {name, amount_cents, category}
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- If the table already existed without the invoice_type column, add it:
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'invoice' AND column_name = 'invoice_type'
    ) THEN
        ALTER TABLE invoice
            ADD COLUMN invoice_type TEXT NOT NULL DEFAULT 'monthly_rent'
                CHECK (invoice_type IN ('estimate', 'actuals', 'monthly_rent'));
        RAISE NOTICE 'Added invoice_type column to existing invoice table';
    ELSE
        RAISE NOTICE 'invoice_type column already present — no changes made';
    END IF;
END $$;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_invoice_job_id      ON invoice (job_id);
CREATE INDEX IF NOT EXISTS idx_invoice_type        ON invoice (invoice_type);
CREATE INDEX IF NOT EXISTS idx_invoice_period      ON invoice (period_start, period_end);

-- RLS
ALTER TABLE invoice ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'invoice' AND policyname = 'service_role_all'
    ) THEN
        CREATE POLICY "service_role_all" ON invoice
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
END $$;

-- ============================================================
-- Verify:
--   SELECT column_name, data_type, column_default
--   FROM information_schema.columns
--   WHERE table_name = 'invoice';
-- ============================================================
