-- ============================================================
-- TenantSentry.ai — Migration 009
-- F14: Invoice PDF upload + reconciliation result storage
--
-- Adds columns to the invoice table to support:
--   - Deduplication via PDF content hash
--   - Storage URL for the uploaded PDF (Supabase Storage)
--   - Full reconciliation result (JSONB from outgoings_engine)
--   - Reconciliation status tracking
--
-- Safe to run multiple times (IF NOT EXISTS / DO $$ guards).
-- ============================================================

DO $$
BEGIN
    -- pdf_hash: SHA-256 hex of the uploaded PDF bytes.
    -- Used for deduplication — if same PDF is uploaded again, return existing invoice_id.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'invoice' AND column_name = 'pdf_hash'
    ) THEN
        ALTER TABLE invoice ADD COLUMN pdf_hash TEXT;
        RAISE NOTICE 'Added pdf_hash column to invoice table';
    ELSE
        RAISE NOTICE 'pdf_hash already present — skipped';
    END IF;

    -- pdf_url: Supabase Storage path for the uploaded PDF.
    -- Format: invoices/{job_id}/{invoice_id}.pdf
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'invoice' AND column_name = 'pdf_url'
    ) THEN
        ALTER TABLE invoice ADD COLUMN pdf_url TEXT;
        RAISE NOTICE 'Added pdf_url column to invoice table';
    ELSE
        RAISE NOTICE 'pdf_url already present — skipped';
    END IF;

    -- filename: Original filename from the upload.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'invoice' AND column_name = 'filename'
    ) THEN
        ALTER TABLE invoice ADD COLUMN filename TEXT;
        RAISE NOTICE 'Added filename column to invoice table';
    ELSE
        RAISE NOTICE 'filename already present — skipped';
    END IF;

    -- reconciliation_result: Full ReconciliationResult from outgoings_engine, serialised to JSONB.
    -- Contains: findings[], total_disputed_cents, warnings[], lease_clauses_used[], engine_status
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'invoice' AND column_name = 'reconciliation_result'
    ) THEN
        ALTER TABLE invoice ADD COLUMN reconciliation_result JSONB;
        RAISE NOTICE 'Added reconciliation_result column to invoice table';
    ELSE
        RAISE NOTICE 'reconciliation_result already present — skipped';
    END IF;

    -- recon_status: Processing state for the reconciliation.
    -- Values: 'pending' | 'complete' | 'skipped' | 'failed'
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'invoice' AND column_name = 'recon_status'
    ) THEN
        ALTER TABLE invoice ADD COLUMN recon_status TEXT DEFAULT 'pending'
            CHECK (recon_status IN ('pending', 'complete', 'skipped', 'failed'));
        RAISE NOTICE 'Added recon_status column to invoice table';
    ELSE
        RAISE NOTICE 'recon_status already present — skipped';
    END IF;
END $$;

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_invoice_pdf_hash   ON invoice (pdf_hash) WHERE pdf_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_invoice_recon_status ON invoice (recon_status);

-- ============================================================
-- Verify:
--   SELECT column_name, data_type
--   FROM information_schema.columns
--   WHERE table_name = 'invoice'
--   ORDER BY ordinal_position;
-- ============================================================
