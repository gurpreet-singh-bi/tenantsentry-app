"""
json_formatter.py
-----------------
Pydantic models for validated, structured audit output.
These are the canonical data shapes that flow through the pipeline
and into the report generator.
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

_SYDNEY_TZ = ZoneInfo("Australia/Sydney")


class RiskFlag(BaseModel):
    flag_id: str
    description: str
    severity: str                           # "void" | "high" | "medium" | "low"
    # "void" = clause/lease is void ab initio or void by statute (above HIGH)
    legislation_ref: Optional[str] = None
    financial_impact_estimate: Optional[str] = None  # Area 4: e.g. "~$120k make-good liability"


class ClauseAnalysis(BaseModel):
    clause_heading: str
    clause_text: str
    clause_type: Optional[str] = None
    key_terms: list[str] = Field(default_factory=list)
    risk_flags: list[dict] = Field(default_factory=list)
    plain_english_summary: Optional[str] = None
    recommended_action: Optional[str] = None
    # Area 1: PDF page number where this clause begins (1-based).
    # Populated by the chunker from page offset tracking.
    page_number: Optional[int] = None
    # Area 4: Negotiation position and email draft for this clause.
    negotiation_position: Optional[str] = None   # What to ask for
    negotiation_email: Optional[str] = None      # Ready-to-copy email paragraph
    # G9: ABS CPI series extracted from rent review clauses.
    # e.g. "sydney", "weighted_average". Used by cpi_calculator to pick the
    # correct ABS region rather than falling back to the jurisdiction default.
    cpi_index_series: Optional[str] = None
    error: Optional[str] = None     # Set if LLM parsing failed


class LeaseDate(BaseModel):
    """A critical date or deadline extracted from the lease."""
    date_type: str                      # matches lease_dates CHECK constraint
    date_description: str               # plain-English label for the tenant
    date_value: Optional[str] = None    # ISO YYYY-MM-DD, or None if relative/unknown
    clause_reference: Optional[str] = None
    recurrence: Optional[str] = None    # None | "annual" | "monthly"
    alert_days_before: int = 90
    notes: Optional[str] = None         # extra context from the LLM


class AuditResult(BaseModel):
    tenant_name: str
    jurisdiction: str
    filename: str
    audit_date: datetime = Field(default_factory=lambda: datetime.now(_SYDNEY_TZ))
    raw_clause_count: int = 0       # Total clauses extracted from the document by the chunker (OCR)
    haiku_triage_count: int = 0     # Clauses sent to Haiku for triage screening
    sonnet_analysed_count: int = 0  # Clauses deep-analysed by Sonnet
    opus_escalated_count: int = 0   # Clauses escalated to Opus (complex/high-risk keywords)
    total_clauses: int              # Total clause_analyses records in result (flagged + stubs)
    risk_score: int             # 0-100
    clause_analyses: list[ClauseAnalysis]
    all_risk_flags: list[dict]
    lease_dates: list[LeaseDate] = Field(default_factory=list)
    # G9: Aggregated extracted_rules -- written to lease.extracted_rules in Supabase.
    # Populated by audit_pipeline from clause-level extractions.
    extracted_rules: dict = Field(default_factory=dict)
    # Pipeline performance instrumentation -- per-stage durations in ms.
    # Stored in audit_run.stage_timings (separate column); excluded from reports.
    stage_timings: dict = Field(default_factory=dict)
    # Per-model token counts and USD costs from utils/cost_tracker.CostAccumulator.
    # Stored in audit_run.stage_costs (separate column); excluded from reports.
    stage_costs: dict = Field(default_factory=dict)
    # Multi-doc: reconciliation results for each outgoings/invoice doc uploaded.
    # Each entry is a serialised ReconciliationResult dict from outgoings_engine.
    reconciliation_results: list[dict] = Field(default_factory=list)
    # Non-fatal pipeline warnings (e.g. unsupported doc type, amendment not analysed).
    pipeline_warnings: list[str] = Field(default_factory=list)
    # Key lease metadata extracted from the cover / reference schedule.
    # Populated by services/lease_metadata_extractor -- None when not found or MOCK_MODE.
    landlord_name: Optional[str] = None      # Full legal entity name of the landlord
    base_rent_pa: Optional[float] = None     # Annual base rent in AUD (excl. outgoings)
    floor_area_sqm: Optional[float] = None   # Net lettable area in sqm
    lease_term_years: Optional[float] = None # Initial term in years
    # AQ-NEW-5: Premises classification -- determines which act governs this lease.
    # Populated from pre-audit questionnaire fields submitted with the upload.
    premises_use: Optional[str] = None       # "retail" | "office" | "industrial" | "mixed" | "other"
    entity_type: Optional[str] = None        # "individual" | "company" | "trust" | "government"
    gla_sqm: Optional[float] = None          # Gross lettable area (sqm) -- triggers SA threshold
    applicable_statute: Optional[str] = None # Full act name -- e.g. "Retail Leases Act 2003 (VIC)"
    statute_code: Optional[str] = None       # Short code -- e.g. "retail_vic"
    is_retail_lease: Optional[bool] = None   # True if retail tenancy legislation applies

    @property
    def void_risk_flags(self) -> list[dict]:
        """AQ-NEW-23: Findings where a clause or the lease itself is void by statute."""
        return [f for f in self.all_risk_flags if f.get("severity") == "void"]

    @property
    def high_risk_flags(self) -> list[dict]:
        """HIGH severity findings (excludes VOID — use void_risk_flags for those)."""
        return [f for f in self.all_risk_flags if f.get("severity") == "high"]

    @property
    def risk_level(self) -> str:
        if self.risk_score >= 60:
            return "HIGH"
        elif self.risk_score >= 30:
            return "MEDIUM"
        return "LOW"

    @property
    def critical_deadlines(self) -> list[LeaseDate]:
        """Dates with alert_days_before >= 90 -- highest-consequence deadlines."""
        return [d for d in self.lease_dates if d.alert_days_before >= 90 and d.date_value]

    def to_summary(self) -> dict:
        return {
            "tenant": self.tenant_name,
            "jurisdiction": self.jurisdiction,
            "file": self.filename,
            "audit_date": self.audit_date.isoformat(),
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "raw_clause_count": self.raw_clause_count,
            "haiku_triage_count": self.haiku_triage_count,
            "sonnet_analysed_count": self.sonnet_analysed_count,
            "opus_escalated_count": self.opus_escalated_count,
            "total_clauses_reviewed": self.total_clauses,
            "total_flags": len(self.all_risk_flags),
            "high_risk_flags": len(self.high_risk_flags),
            "dates_extracted": len(self.lease_dates),
            "critical_deadlines": len(self.critical_deadlines),
        }
