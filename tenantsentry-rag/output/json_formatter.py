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


class RiskFlag(BaseModel):
    flag_id: str
    description: str
    severity: str               # "high" | "medium" | "low"
    legislation_ref: Optional[str] = None


class ClauseAnalysis(BaseModel):
    clause_heading: str
    clause_text: str
    clause_type: Optional[str] = None
    key_terms: list[str] = Field(default_factory=list)
    risk_flags: list[dict] = Field(default_factory=list)
    plain_english_summary: Optional[str] = None
    recommended_action: Optional[str] = None
    error: Optional[str] = None     # Set if LLM parsing failed


class AuditResult(BaseModel):
    tenant_name: str
    jurisdiction: str
    filename: str
    audit_date: datetime = Field(default_factory=datetime.utcnow)
    total_clauses: int
    risk_score: int             # 0-100
    clause_analyses: list[ClauseAnalysis]
    all_risk_flags: list[dict]

    @property
    def high_risk_flags(self) -> list[dict]:
        return [f for f in self.all_risk_flags if f.get("severity") == "high"]

    @property
    def risk_level(self) -> str:
        if self.risk_score >= 60:
            return "HIGH"
        elif self.risk_score >= 30:
            return "MEDIUM"
        return "LOW"

    def to_summary(self) -> dict:
        return {
            "tenant": self.tenant_name,
            "jurisdiction": self.jurisdiction,
            "file": self.filename,
            "audit_date": self.audit_date.isoformat(),
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "total_clauses_reviewed": self.total_clauses,
            "total_flags": len(self.all_risk_flags),
            "high_risk_flags": len(self.high_risk_flags),
        }
