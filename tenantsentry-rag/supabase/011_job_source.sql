-- Migration 011: Add source column to audit_run
-- Tags each job as 'dev' or 'live' so queues can be filtered by mode.
-- Existing rows default to 'live'.

ALTER TABLE audit_run
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'live'
  CHECK (source IN ('dev', 'live'));

-- Back-fill any existing rows (they were all real/live runs)
UPDATE audit_run SET source = 'live' WHERE source IS NULL;

-- Index for fast queue filtering
CREATE INDEX IF NOT EXISTS idx_audit_run_source ON audit_run (source);
