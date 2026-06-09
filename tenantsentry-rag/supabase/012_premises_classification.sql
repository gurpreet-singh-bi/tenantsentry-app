-- Migration 012: AQ-NEW-5 — Premises Classification
-- Adds columns to audit_run to store the pre-audit questionnaire answers
-- and the resulting applicable statute determination.
--
-- These fields are critical for correct audit output:
-- Without premises_use + entity_type + gla_sqm, the engine cannot determine
-- whether retail tenancy legislation applies — a retail tenant classified as
-- commercial loses all statutory protections.
--
-- Run in Supabase SQL editor.

ALTER TABLE audit_run
  ADD COLUMN IF NOT EXISTS premises_use      TEXT,          -- "retail"|"office"|"industrial"|"mixed"|"other"
  ADD COLUMN IF NOT EXISTS entity_type       TEXT,          -- "individual"|"company"|"trust"|"government"
  ADD COLUMN IF NOT EXISTS gla_sqm           NUMERIC(10,2), -- gross lettable area in sqm (nullable)
  ADD COLUMN IF NOT EXISTS applicable_statute TEXT,         -- full act name, e.g. "Retail Leases Act 2003 (VIC)"
  ADD COLUMN IF NOT EXISTS statute_code      TEXT,          -- short code, e.g. "retail_vic"
  ADD COLUMN IF NOT EXISTS is_retail_lease   BOOLEAN;       -- true = retail tenancy legislation applies

-- Optional: constrain premises_use and entity_type to known values
-- (comment out if you want to allow free-form values during migration period)
ALTER TABLE audit_run
  ADD CONSTRAINT chk_premises_use
    CHECK (premises_use IS NULL OR premises_use IN ('retail','office','industrial','mixed','other')),
  ADD CONSTRAINT chk_entity_type
    CHECK (entity_type IS NULL OR entity_type IN ('individual','company','trust','government'));

-- Index statute_code for analytics queries (e.g. "how many retail_vic audits this month")
CREATE INDEX IF NOT EXISTS idx_audit_run_statute_code ON audit_run(statute_code);

COMMENT ON COLUMN audit_run.premises_use      IS 'AQ-NEW-5: use of premises — determines retail vs commercial act';
COMMENT ON COLUMN audit_run.entity_type       IS 'AQ-NEW-5: tenant entity type — affects retail act eligibility';
COMMENT ON COLUMN audit_run.gla_sqm           IS 'AQ-NEW-5: gross lettable area — triggers SA 1,000 sqm threshold';
COMMENT ON COLUMN audit_run.applicable_statute IS 'AQ-NEW-5: full act name injected into LLM prompts';
COMMENT ON COLUMN audit_run.statute_code       IS 'AQ-NEW-5: short code for filtering (retail_vic, commercial_wa, etc.)';
COMMENT ON COLUMN audit_run.is_retail_lease    IS 'AQ-NEW-5: true = retail tenancy legislation applies';
