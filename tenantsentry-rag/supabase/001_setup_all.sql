-- ================================================================
-- TenantSentry.ai — Master Supabase Setup
-- Run this ONCE in the Supabase SQL Editor (Dashboard → SQL Editor)
-- Safe to re-run: all statements use IF NOT EXISTS / OR REPLACE
-- ================================================================


-- ────────────────────────────────────────────────────────────────
-- 0. Extensions
-- ────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;         -- pgvector for embeddings
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";    -- gen_random_uuid() fallback


-- ================================================================
-- 1. LEASE CHUNKS  (RAG vector store)
-- ================================================================
CREATE TABLE IF NOT EXISTS lease_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content         TEXT NOT NULL,
    embedding       VECTOR(1024),
    metadata        JSONB,
    document_id     TEXT,
    chunk_type      TEXT CHECK (chunk_type IN ('lease', 'legislation', 'rule')),
    jurisdiction    TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lease_chunks_type ON lease_chunks (chunk_type);
CREATE INDEX IF NOT EXISTS idx_lease_chunks_jurisdiction ON lease_chunks (jurisdiction);
CREATE INDEX IF NOT EXISTS idx_lease_chunks_document ON lease_chunks (document_id);

-- IVFFlat index for fast approximate nearest-neighbour search
-- (Run AFTER bulk-inserting your first dataset for best performance)
CREATE INDEX IF NOT EXISTS idx_lease_chunks_embedding
    ON lease_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- RLS
ALTER TABLE lease_chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all" ON lease_chunks
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ================================================================
-- 2. AUDIT RUN  (job state persistence — G5)
-- ================================================================
CREATE TABLE IF NOT EXISTS audit_run (
    job_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename            TEXT NOT NULL,
    jurisdiction        TEXT NOT NULL,
    tenant_name         TEXT NOT NULL DEFAULT 'Unknown',
    status              TEXT NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued', 'processing', 'complete', 'failed')),
    progress            INT NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    stage               TEXT NOT NULL DEFAULT 'Queued',
    error               TEXT,
    findings            JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    -- Human-in-the-loop gate (G4)
    reviewed_by_human   BOOLEAN NOT NULL DEFAULT FALSE,
    reviewer_notes      TEXT,
    reviewed_at         TIMESTAMPTZ,
    released            BOOLEAN NOT NULL DEFAULT FALSE,
    released_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_audit_run_status       ON audit_run (status);
CREATE INDEX IF NOT EXISTS idx_audit_run_reviewed     ON audit_run (reviewed_by_human);
CREATE INDEX IF NOT EXISTS idx_audit_run_released     ON audit_run (released);
CREATE INDEX IF NOT EXISTS idx_audit_run_completed_at ON audit_run (completed_at DESC);

ALTER TABLE audit_run ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all" ON audit_run
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ================================================================
-- 3. INVOICE  (G3: estimate vs actuals — future)
-- ================================================================
CREATE TABLE IF NOT EXISTS invoice (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES audit_run(job_id) ON DELETE CASCADE,
    invoice_type    TEXT NOT NULL CHECK (invoice_type IN ('estimate', 'actuals', 'monthly_rent')),
    period_start    DATE,
    period_end      DATE,
    amount_cents    BIGINT,
    line_items      JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE invoice ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all" ON invoice
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ================================================================
-- 4. DISPUTE LETTER  (G6: evidence pack — future)
-- ================================================================
CREATE TABLE IF NOT EXISTS dispute_letter (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES audit_run(job_id) ON DELETE CASCADE,
    flag_id         TEXT,
    letter_text     TEXT,
    evidence_pack   JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE dispute_letter ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all" ON dispute_letter
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ================================================================
-- 5. RPC FUNCTION — vector similarity search
-- ================================================================
CREATE OR REPLACE FUNCTION match_chunks(
    query_embedding     VECTOR(1024),
    match_count         INT,
    filter_chunk_type   TEXT DEFAULT NULL,
    filter_jurisdiction TEXT DEFAULT NULL
)
RETURNS TABLE (
    id          UUID,
    content     TEXT,
    metadata    JSONB,
    similarity  FLOAT
)
LANGUAGE SQL STABLE AS $$
    SELECT
        id,
        content,
        metadata,
        1 - (embedding <=> query_embedding) AS similarity
    FROM lease_chunks
    WHERE (filter_chunk_type   IS NULL OR chunk_type   = filter_chunk_type)
      AND (filter_jurisdiction IS NULL OR jurisdiction = filter_jurisdiction)
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$$;


-- ================================================================
-- Done. Verify with:
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public';
-- ================================================================
