-- ============================================================
-- TenantSentry.ai — audit_run table migration
-- G5: Jobs persistence (replaces in-memory _jobs dict)
-- Run once in Supabase SQL editor.
-- ============================================================

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
    findings            JSONB,          -- full AuditResult dict (set on complete)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    -- Human-in-the-loop gate (G4)
    reviewed_by_human   BOOLEAN NOT NULL DEFAULT FALSE,
    reviewer_notes      TEXT,
    reviewed_at         TIMESTAMPTZ,
    released            BOOLEAN NOT NULL DEFAULT FALSE,
    released_at         TIMESTAMPTZ
);

-- Indexes for the admin queue queries
CREATE INDEX IF NOT EXISTS idx_audit_run_status          ON audit_run (status);
CREATE INDEX IF NOT EXISTS idx_audit_run_reviewed        ON audit_run (reviewed_by_human);
CREATE INDEX IF NOT EXISTS idx_audit_run_released        ON audit_run (released);
CREATE INDEX IF NOT EXISTS idx_audit_run_completed_at    ON audit_run (completed_at DESC);

-- Row Level Security: only the service role key can read/write (enforced by Supabase)
ALTER TABLE audit_run ENABLE ROW LEVEL SECURITY;

-- Allow service role full access (API uses service key)
CREATE POLICY "service_role_all" ON audit_run
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
