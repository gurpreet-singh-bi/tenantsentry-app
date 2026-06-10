"""
outgoings_cap_calculator.py
----------------------------
AQ-NEW-28: Deterministic outgoings cap verification.

The auditor's "80% of office and industrial leakage" problem:
Commercial and industrial leases often cap outgoings increases (e.g. "outgoings
increases capped at 5% per annum").  Landlords manipulate this via:
  1. Swapping cumulative → non-cumulative calculation mid-term
  2. "Base year reset" — redefining the base year after a high-spend year
  3. Excluding "non-controllable outgoings" (insurance, rates) from the cap —
     then routing CapEx through those categories
  4. Reporting 5% compounding as 5% simple (or vice versa)

This module:
  - Extracts cap terms from lease clause analyses (rate, base year, compounding type)
  - Performs deterministic arithmetic on multi-year outgoings history
  - Detects base year reset attempts
  - Returns a CapVerificationResult with any overcharge quantified to the cent

DEV / LIVE mode:
  MOCK_MODE=true  → returns synthetic cap verification result with an overcharge
  MOCK_MODE=false → extracts cap terms via Haiku, then pure arithmetic
"""

import os
import re
import json
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger
from dotenv import load_dotenv

load_dotenv()


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class CapTerm:
    """
    Outgoings cap terms extracted from the lease.
    """
    cap_rate_pct: Optional[float]          # e.g. 5.0 for 5% cap
    is_cumulative: bool = True             # True = cap compounds; False = non-cumulative (flat %)
    base_year: Optional[int] = None        # e.g. 2020 (financial year start)
    base_year_amount_cents: Optional[int] = None   # actual outgoings in base year
    excluded_categories: list[str] = field(default_factory=list)  # "insurance", "rates" etc.
    clause_reference: Optional[str] = None
    extraction_confidence: str = "low"    # "high" | "medium" | "low"
    notes: Optional[str] = None


@dataclass
class CapYearResult:
    """Verification result for a single year."""
    year_label: str                        # e.g. "FY2024"
    actual_amount_cents: int               # what the landlord charged
    permitted_amount_cents: int            # what the cap allows
    overcharge_cents: int                  # actual - permitted (0 if compliant)
    cumulative_factor: float               # cap multiplier applied (e.g. 1.1025 for 2 yr 5% cumulative)
    is_compliant: bool


@dataclass
class CapVerificationResult:
    """
    Full outgoings cap audit result.
    """
    cap_found: bool                        # False = no cap clause identified
    cap_term: Optional[CapTerm]
    year_results: list[CapYearResult] = field(default_factory=list)
    total_overcharge_cents: int = 0
    base_year_reset_detected: bool = False
    base_year_reset_notes: Optional[str] = None
    engine_status: str = "complete"        # "complete" | "no_cap" | "insufficient_data" | "mock" | "failed"
    warnings: list[str] = field(default_factory=list)

    @property
    def has_overcharge(self) -> bool:
        return self.total_overcharge_cents > 0

    def to_finding_dict(self) -> dict:
        """
        Produce a finding dict in the format expected by outgoings_engine
        ReconciliationFinding — for direct injection into reconciliation results.
        """
        if not self.has_overcharge and not self.base_year_reset_detected:
            return {}
        severity = "high" if self.total_overcharge_cents > 500_000 else "medium"
        lines = []
        if self.has_overcharge:
            lines.append(
                f"Outgoings cap breach detected: landlord overcharged "
                f"${self.total_overcharge_cents/100:,.2f} across "
                f"{sum(1 for y in self.year_results if not y.is_compliant)} year(s)."
            )
        if self.base_year_reset_detected:
            lines.append(f"Base year reset detected: {self.base_year_reset_notes}")
        return {
            "line_item_description": "Outgoings Cap Verification (deterministic)",
            "category": "outgoings_cap",
            "amount_cents": self.total_overcharge_cents,
            "finding_type": "overcharge",
            "severity": severity,
            "explanation": " ".join(lines),
            "legislation_ref": None,
            "clause_ref": (self.cap_term.clause_reference if self.cap_term else None),
            "disputed_amount_cents": self.total_overcharge_cents,
        }


# ── Cap term extraction from lease clauses ────────────────────────────────────

_CAP_EXTRACT_SYSTEM = """\
You are an expert Australian commercial lease auditor. Extract outgoings cap terms.
Return ONLY valid JSON — no preamble, no markdown.
"""

_CAP_EXTRACT_PROMPT = """\
Review the following lease clause analyses for outgoings cap provisions.

Find ANY clauses that:
- Cap the annual increase in total outgoings (e.g. "outgoings increases capped at 5%")
- Define a "base year" for outgoings calculations
- Define whether the cap is cumulative (compounds) or non-cumulative (flat %)
- Exclude certain outgoings categories from the cap

Return a JSON object:
{{
  "cap_found": true/false,
  "cap_rate_pct": 5.0 (or null if no cap),
  "is_cumulative": true/false (true = compound; default true if ambiguous),
  "base_year": 2020 (financial year start, integer, or null),
  "excluded_categories": ["insurance", "statutory rates"] (empty array if none),
  "clause_reference": "Clause X.X" or null,
  "confidence": "high" | "medium" | "low",
  "notes": "any important nuance about how the cap works"
}}

CLAUSE ANALYSES:
{clauses_text}

JSON only:"""


def extract_cap_terms(
    clause_analyses: list[dict],
    job_id: Optional[str] = None,
) -> CapTerm:
    """
    Extract outgoings cap terms from existing clause analyses.
    Returns a CapTerm (cap_rate_pct=None if no cap found).
    """
    mock_mode = os.environ.get("MOCK_MODE", "true").lower() == "true"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if mock_mode or not api_key or api_key.startswith("sk-ant-your"):
        return CapTerm(
            cap_rate_pct=5.0,
            is_cumulative=True,
            base_year=2022,
            base_year_amount_cents=28_000_000,   # $280,000
            excluded_categories=[],
            clause_reference="Clause 7.4 (MOCK)",
            extraction_confidence="high",
            notes="MOCK_MODE: 5% cumulative cap from 2022 base year.",
        )

    # Filter to outgoings-relevant clauses only
    outgoings_keywords = {"outgoing", "recoverable", "cap", "capped", "base year", "management fee"}
    relevant = [
        ca for ca in clause_analyses
        if isinstance(ca, dict) and (
            any(kw in (ca.get("clause_heading") or "").lower() for kw in outgoings_keywords)
            or any(kw in (ca.get("plain_english_summary") or "").lower() for kw in outgoings_keywords)
            or (ca.get("clause_type") or "").lower() == "outgoings"
        )
    ]

    if not relevant:
        return CapTerm(cap_rate_pct=None, extraction_confidence="low",
                       notes="No outgoings clauses identified in lease analysis.")

    clauses_text = "\n\n---\n\n".join(
        f"[{ca.get('clause_heading','?')}]\n{ca.get('plain_english_summary','')}\n"
        f"Text: {(ca.get('clause_text') or '')[:400]}"
        for ca in relevant[:10]   # Cap at 10 clauses to stay within token limits
    )

    from llm.router import get_client, HAIKU_MODEL
    client = get_client()
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=512,
            system=_CAP_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": _CAP_EXTRACT_PROMPT.format(clauses_text=clauses_text)}],
            timeout=30.0,
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        d = json.loads(raw)
    except Exception as e:
        logger.error(f"[{job_id}] extract_cap_terms: LLM error: {e}")
        return CapTerm(cap_rate_pct=None, extraction_confidence="low",
                       notes=f"Extraction failed: {e}")

    if not d.get("cap_found"):
        return CapTerm(cap_rate_pct=None, extraction_confidence="high",
                       notes="No outgoings cap clause found in this lease.")

    cap_rate = d.get("cap_rate_pct")
    return CapTerm(
        cap_rate_pct=float(cap_rate) if cap_rate is not None else None,
        is_cumulative=bool(d.get("is_cumulative", True)),
        base_year=d.get("base_year"),
        base_year_amount_cents=None,   # Caller supplies from historical data
        excluded_categories=d.get("excluded_categories") or [],
        clause_reference=d.get("clause_reference"),
        extraction_confidence=d.get("confidence", "low"),
        notes=d.get("notes"),
    )


# ── Deterministic cap arithmetic ──────────────────────────────────────────────

def _permitted_amount(
    base_amount_cents: int,
    cap_rate_pct: float,
    years_elapsed: int,
    is_cumulative: bool,
) -> int:
    """
    Calculate the maximum permitted outgoings amount under the cap.

    Cumulative (compound):
        permitted = base × (1 + rate/100)^years

    Non-cumulative (flat %):
        permitted = base × (1 + rate/100 × years)
        i.e. each year is capped at base*(1+rate/100), independently.
        Some leases mean "each year can be at most 5% more than the PRIOR year" —
        that is equivalent to cumulative. This function treats non-cumulative as
        the flat interpretation (increases compared to BASE year only).
    """
    r = cap_rate_pct / 100.0
    if is_cumulative:
        factor = (1 + r) ** years_elapsed
    else:
        factor = 1 + r * years_elapsed
    return int(base_amount_cents * factor)


def verify_cap(
    cap_term: CapTerm,
    yearly_actuals: dict[str, int],  # {"FY2023": cents, "FY2024": cents, ...} (ordered)
    job_id: Optional[str] = None,
) -> CapVerificationResult:
    """
    Verify outgoings cap compliance across multiple years.

    Args:
        cap_term:        Extracted cap terms (from extract_cap_terms()).
        yearly_actuals:  Dict mapping year label → actual charged amount in cents.
                         Must be in chronological order.  First entry is treated as
                         the base year if cap_term.base_year_amount_cents is None.
        job_id:          For log correlation.

    Returns:
        CapVerificationResult
    """
    mock_mode = os.environ.get("MOCK_MODE", "true").lower() == "true"
    if mock_mode:
        return _mock_cap_result()

    if not cap_term.cap_rate_pct:
        return CapVerificationResult(
            cap_found=False, cap_term=cap_term,
            engine_status="no_cap",
            warnings=["No outgoings cap clause identified in this lease."],
        )

    if len(yearly_actuals) < 2:
        return CapVerificationResult(
            cap_found=True, cap_term=cap_term,
            engine_status="insufficient_data",
            warnings=["Need at least 2 years of outgoings data to verify cap compliance."],
        )

    years = list(yearly_actuals.keys())
    amounts = list(yearly_actuals.values())

    # Establish base year
    if cap_term.base_year_amount_cents:
        base_amount = cap_term.base_year_amount_cents
        base_label = str(cap_term.base_year) if cap_term.base_year else years[0]
    else:
        # Use first year as base
        base_amount = amounts[0]
        base_label = years[0]

    result = CapVerificationResult(cap_found=True, cap_term=cap_term)
    total_overcharge = 0
    prev_amount = base_amount   # For non-cumulative checks against prior year

    for i, (year, actual) in enumerate(yearly_actuals.items()):
        if i == 0 and year == base_label:
            # Skip base year itself
            continue

        years_elapsed = i   # years since base year (base is year 0)

        # Permitted under cap
        permitted = _permitted_amount(base_amount, cap_term.cap_rate_pct, years_elapsed, cap_term.is_cumulative)
        factor = (1 + cap_term.cap_rate_pct / 100) ** years_elapsed if cap_term.is_cumulative else (
            1 + cap_term.cap_rate_pct / 100 * years_elapsed
        )

        overcharge = max(0, actual - permitted)

        yr = CapYearResult(
            year_label=year,
            actual_amount_cents=actual,
            permitted_amount_cents=permitted,
            overcharge_cents=overcharge,
            cumulative_factor=factor,
            is_compliant=(overcharge == 0),
        )
        result.year_results.append(yr)
        total_overcharge += overcharge
        prev_amount = actual

    result.total_overcharge_cents = total_overcharge

    # Base year reset detection:
    # A reset occurs when the landlord uses a later year (higher actual amount) as the new
    # base year, instead of the original contractual base year.
    # Detection heuristic: if the base_year stated in the cap_term is later than the
    # lease commencement year implied by the first data year, flag it.
    if cap_term.base_year and len(years) >= 2:
        first_year_num = _parse_year(years[0])
        if first_year_num and cap_term.base_year > first_year_num:
            result.base_year_reset_detected = True
            result.base_year_reset_notes = (
                f"Base year in cap clause ({cap_term.base_year}) is later than the first "
                f"outgoings year ({years[0]}). This may indicate the landlord reset the "
                f"base year to a higher-spend period, inflating the cap ceiling. "
                f"Verify the original base year from the lease execution date."
            )
            result.warnings.append(result.base_year_reset_notes)

    if total_overcharge > 0:
        logger.info(
            f"[{job_id}] outgoings_cap_calculator: OVERCHARGE DETECTED "
            f"${total_overcharge/100:,.2f} across "
            f"{sum(1 for y in result.year_results if not y.is_compliant)} year(s)"
        )
    else:
        logger.info(f"[{job_id}] outgoings_cap_calculator: cap compliant — no overcharge")

    return result


def _parse_year(label: str) -> Optional[int]:
    """Extract a 4-digit year from a label like 'FY2023' or '2023' or '2022-23'."""
    m = re.search(r"(20\d{2})", label)
    return int(m.group(1)) if m else None


# ── Public convenience wrapper ────────────────────────────────────────────────

def run_cap_verification(
    clause_analyses: list[dict],
    yearly_actuals: dict[str, int],
    job_id: Optional[str] = None,
) -> CapVerificationResult:
    """
    One-call interface: extract cap terms from clause analyses, then verify.

    Args:
        clause_analyses:  ClauseAnalysis dicts from the lease audit.
        yearly_actuals:   {"FY2023": cents, "FY2024": cents, ...} (chronological).
        job_id:           For log correlation.

    Returns:
        CapVerificationResult
    """
    cap_term = extract_cap_terms(clause_analyses, job_id=job_id)
    return verify_cap(cap_term, yearly_actuals, job_id=job_id)


# ── Mock result ───────────────────────────────────────────────────────────────

def _mock_cap_result() -> CapVerificationResult:
    """MOCK_MODE: synthetic cap overcharge result."""
    cap_term = CapTerm(
        cap_rate_pct=5.0,
        is_cumulative=True,
        base_year=2022,
        base_year_amount_cents=28_000_000,
        excluded_categories=[],
        clause_reference="Clause 7.4",
        extraction_confidence="high",
        notes="MOCK: 5% cumulative cap from FY2022 base.",
    )
    year_results = [
        CapYearResult(
            year_label="FY2023",
            actual_amount_cents=29_400_000,     # $294,000 — exactly 5%, compliant
            permitted_amount_cents=29_400_000,  # $294,000
            overcharge_cents=0,
            cumulative_factor=1.05,
            is_compliant=True,
        ),
        CapYearResult(
            year_label="FY2024",
            actual_amount_cents=32_500_000,     # $325,000 — exceeds 10.25% cumulative cap
            permitted_amount_cents=30_870_000,  # $308,700 = $280,000 × 1.05²
            overcharge_cents=1_630_000,         # $16,300 overcharge
            cumulative_factor=1.1025,
            is_compliant=False,
        ),
        CapYearResult(
            year_label="FY2025",
            actual_amount_cents=35_200_000,     # $352,000 — even further over
            permitted_amount_cents=32_413_500,  # $324,135 = $280,000 × 1.05³
            overcharge_cents=2_786_500,         # $27,865 overcharge
            cumulative_factor=1.157625,
            is_compliant=False,
        ),
    ]
    return CapVerificationResult(
        cap_found=True,
        cap_term=cap_term,
        year_results=year_results,
        total_overcharge_cents=4_416_500,   # $44,165 total
        base_year_reset_detected=False,
        engine_status="mock",
        warnings=[
            "MOCK_MODE: Cap verification results are synthetic.",
            "CAP OVERCHARGE: Landlord exceeded 5% cumulative cap in FY2024 and FY2025. "
            "Total overcharge: $44,165.00.",
        ],
    )
