"""
make_good_rules.py
------------------
AQ-NEW-8: Make-Good Clause Analysis Module

Make-good is the single highest-risk clause category for commercial tenants.
The cost of stripping a fitout to 'base building condition' can reach $500k+
on a fit-out-heavy office or retail tenancy in Sydney/Melbourne.

This module provides:
1. Jurisdiction-specific make-good standards (what is 'standard market practice')
2. A structured analysis function that flags exploitative provisions
3. A prompt injection block for the LLM (injected by audit_pipeline when a
   make_good clause is detected)
4. Critical date linking — make-good deadline tied to lease_dates

DEV/LIVE:
- analyse_make_good() uses MOCK_MODE guard: DEV returns synthetic findings,
  LIVE calls Haiku for structured risk extraction on the clause text.
- build_make_good_prompt_block() is pure data — no LLM, no external calls.

Structure
---------
    MAKE_GOOD_STANDARDS[jurisdiction]   — market standard per state
    analyse_make_good(...)              -> MakeGoodAnalysis
    build_make_good_prompt_block(...)   -> str  (inject into analyse_clause prompt)
    make_good_to_dict(analysis)         -> dict
"""

import os
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


# ── Jurisdiction-specific market standards ────────────────────────────────────

# What is 'standard market practice' for make-good in each Australian jurisdiction.
# Used to contextualise flagged clauses — a tenant paying $200k on make-good when
# the market standard is 'paint and patch' has been exploited.
#
# Sources: AICD Commercial Leasing Guidelines; Property Council of Australia
# industry practice notes; state-specific retail tenancy guidelines.

MAKE_GOOD_STANDARDS: dict[str, dict] = {
    "NSW": {
        "standard":    "Fair wear and tear standard — patch, paint, clean, remove tenant fixtures",
        "exploitative":"'Base building condition' requirement; reinstatement of partitions; no fair wear and tear carve-out",
        "typical_cost":"$100–$500/sqm depending on fitout complexity; $80k–$500k+ for office/retail tenancy",
        "legislation": "Retail Leases Act 1994 (NSW) — no specific make-good restriction for retail; common law governs commercial",
        "key_risks": [
            "No fair wear and tear exception",
            "Obligation to reinstate to 'base building condition' (requires strip-out to shell)",
            "Reinstatement of partitions, cabling, floor coverings installed by landlord",
            "Fitout contributions from landlord creating reinstatement obligation",
            "No time limit on make-good obligations post-expiry",
            "Landlord's right to elect cash payment instead of works (landlord retains windfall)",
        ],
        "safe_clauses": [
            "Repair, patch, and paint to a reasonable standard consistent with normal use",
            "Remove tenant-installed fixtures at landlord's written election",
            "Fair wear and tear expressly excluded",
        ],
    },
    "VIC": {
        "standard":    "Fair wear and tear standard; removal of tenant fixtures; no structural reinstatement",
        "exploitative":"Reinstatement to base building standard; obligation to remove landlord-installed partitions",
        "typical_cost":"$80–$400/sqm; $60k–$400k+ for Melbourne CBD office",
        "legislation": "Retail Leases Act 2003 (VIC) s.41 — tenant not required to restore beyond permitted use alterations",
        "key_risks": [
            "No fair wear and tear carve-out",
            "Obligation to reinstate to base building or shell condition",
            "Ambiguous 'as close as possible to original condition' wording",
            "Obligation applies to landlord-approved fitout improvements",
            "Make-good scope not defined — leaves disputes open",
        ],
        "safe_clauses": [
            "Repair damage caused by tenant's works",
            "Remove tenant-installed fixtures",
            "Fair wear and tear and reasonable use excluded",
        ],
    },
    "QLD": {
        "standard":    "Removal of tenant-installed fixtures; fair wear and tear carve-out; no base building reinstatement",
        "exploitative":"Reinstatement to pre-fitout or base building standard without independent assessment",
        "typical_cost":"$60–$350/sqm; $50k–$350k+ for Brisbane CBD and surrounds",
        "legislation": "Retail Shop Leases Act 1994 (QLD) s.47 — tenant not required to restore beyond reasonable repair",
        "key_risks": [
            "No fair wear and tear exception for commercial leases",
            "Undefined 'good repair' standard creating scope creep",
            "Landlord-approved works subject to reinstatement",
        ],
        "safe_clauses": [
            "Repair and patch to the standard of the commencement condition",
            "Remove tenant-branded signage and fixtures",
        ],
    },
    "WA": {
        "standard":    "Removal of tenant fixtures; patch and paint; no structural reinstatement required as standard",
        "exploitative":"Reinstatement to base building condition; no fair wear and tear carve-out",
        "typical_cost":"$50–$300/sqm; $40k–$300k for Perth CBD office",
        "legislation": "CTRS Act 1985 (WA) — no specific make-good limits; common law fair wear and tear applies",
        "key_risks": [
            "Base building condition requirement (most expensive obligation)",
            "No fair wear and tear exception",
            "Vague 'reinstate' wording that could mean full structural works",
        ],
        "safe_clauses": [
            "Patch, paint, clean; remove tenant fixtures",
            "Fair wear and tear excluded",
        ],
    },
    "SA": {
        "standard":    "Removal of tenant works; fair wear and tear carve-out; reasonable repair standard",
        "exploitative":"Reinstatement to as-new condition; no fair wear and tear exception",
        "typical_cost":"$40–$250/sqm; $30k–$200k for Adelaide office",
        "legislation": "Retail and Commercial Leases Act 1995 (SA) — tenant repair obligations must be reasonable",
        "key_risks": [
            "No fair wear and tear carve-out",
            "Overly broad 'as new' or 'as at commencement' standard",
        ],
        "safe_clauses": [
            "Repair damage caused by tenant's fitout",
            "Fair wear and tear excluded",
        ],
    },
    "ACT": {
        "standard":    "Fair wear and tear; remove tenant fixtures on request; patch and paint",
        "exploitative":"Full reinstatement to original state without fair wear and tear carve-out",
        "typical_cost":"$50–$300/sqm; $40k–$250k for Canberra office",
        "legislation": "Leases (Commercial and Retail) Act 2001 (ACT) s.57 — tenant not required to restore beyond reasonable repair",
        "key_risks": [
            "No fair wear and tear exception",
            "Obligation to reinstate landlord-approved improvements",
        ],
        "safe_clauses": [
            "Repair to reasonable standard; fair wear and tear excluded",
        ],
    },
    "TAS": {
        "standard":    "Remove tenant fixtures; repair damage; fair wear and tear carve-out",
        "exploitative":"Reinstatement to base building condition without fair wear and tear carve-out",
        "typical_cost":"$30–$200/sqm; $20k–$150k for Hobart office",
        "legislation": "Common law applies; no specific make-good legislation for commercial leases",
        "key_risks": [
            "Vague reinstatement standard creating scope disputes",
            "No fair wear and tear exception",
        ],
        "safe_clauses": [
            "Repair damage caused by tenant use",
            "Fair wear and tear excluded",
        ],
    },
    "NT": {
        "standard":    "Remove tenant fixtures; repair damage; fair wear and tear carve-out",
        "exploitative":"Full reinstatement to original or base building condition",
        "typical_cost":"$30–$180/sqm; $20k–$130k for Darwin office",
        "legislation": "Business Tenancies Act 2003 (NT) — reasonable repair standard applies",
        "key_risks": [
            "No fair wear and tear exception",
            "Overly broad 'restore' obligation",
        ],
        "safe_clauses": [
            "Reasonable repair; fair wear and tear excluded",
        ],
    },
}

# Patterns that indicate exploitative make-good — trigger HIGH flags
_EXPLOITATIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bbase\s*building\s*condition\b", re.I),
     "Obligation to restore to 'base building condition' requires full strip-out and is the most expensive possible make-good standard."),
    (re.compile(r"\bshell\s*(?:and\s*core|condition|state)\b", re.I),
     "Restoration to 'shell and core' requires removal of all tenant fit-out and landlord-installed fitout — typically $200–$500k+ liability."),
    (re.compile(r"\boriginal\s*(?:condition|state)\b", re.I),
     "Obligation to restore to 'original condition' may require reinstatement of pre-tenancy fitout installed by the landlord — verify scope carefully."),
    (re.compile(r"\bfair\s*wear\s*and\s*tear\b(?!.*exclud)", re.I),
     "Make-good clause does not appear to include a fair wear and tear carve-out — the most common tenant protection missing from exploitative clauses."),
    (re.compile(r"\breinstate\b.*\bpartition", re.I),
     "Obligation to reinstate partitions can involve significant fitout works regardless of whether the partitions were tenant- or landlord-installed."),
    (re.compile(r"\bcash\s*(?:equivalent|in\s*lieu|payment)\b", re.I),
     "Landlord right to elect cash payment instead of works — landlord could compel a large cash payment rather than accept completed make-good works."),
]

# Pattern that indicates the clause HAS a fair wear and tear carve-out (protective)
_FWT_CARVE_OUT_PATTERN = re.compile(
    r"\bfair\s*wear\s*(?:and\s*tear)?\b.*\bexclud|exclud.*\bfair\s*wear\b",
    re.I
)


@dataclass
class MakeGoodFinding:
    risk_type: str          # "exploitative" | "missing_protection" | "ambiguous" | "compliant"
    severity: str           # "high" | "medium" | "low"
    description: str
    clause_excerpt: str     # verbatim snippet that triggered this finding
    recommendation: str
    legislation_ref: Optional[str] = None
    financial_estimate: Optional[str] = None


@dataclass
class MakeGoodAnalysis:
    jurisdiction: str
    findings: list[MakeGoodFinding] = field(default_factory=list)
    has_fair_wear_tear_carve_out: bool = False
    requires_base_building: bool = False
    estimated_liability_range: Optional[str] = None
    standard_description: Optional[str] = None
    engine_status: str = "complete"  # "complete" | "skipped" | "failed" | "mock"
    warnings: list[str] = field(default_factory=list)

    @property
    def is_exploitative(self) -> bool:
        return any(f.risk_type == "exploitative" for f in self.findings)

    @property
    def highest_severity(self) -> str:
        sev_order = {"high": 0, "medium": 1, "low": 2}
        if not self.findings:
            return "low"
        return min(self.findings, key=lambda f: sev_order.get(f.severity, 2)).severity


# ── Main analyser ─────────────────────────────────────────────────────────────

def analyse_make_good(
    clause_text: str,
    jurisdiction: str,
    lease_term_years: Optional[float] = None,
    floor_area_sqm: Optional[float] = None,
    job_id: Optional[str] = None,
) -> MakeGoodAnalysis:
    """
    AQ-NEW-8: Analyse a make-good clause and return structured findings.

    Two-mode operation:
    - DEV (MOCK_MODE=true): Returns synthetic findings matching the fixture lease
    - LIVE: Runs deterministic pattern matching + Haiku structural analysis

    Pattern matching is ALWAYS run (deterministic, no LLM).
    Haiku adds nuanced findings that patterns miss (e.g. ambiguous scope,
    missing definitions, implied obligations).

    Args:
        clause_text:        Full text of the make-good clause
        jurisdiction:       State code
        lease_term_years:   Used to estimate total liability over the term
        floor_area_sqm:     Used to estimate dollar liability range
        job_id:             For log correlation

    Returns:
        MakeGoodAnalysis
    """
    mock_mode = os.environ.get("MOCK_MODE", "true").lower() == "true"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    jur = jurisdiction.upper().strip()
    standards = MAKE_GOOD_STANDARDS.get(jur, MAKE_GOOD_STANDARDS["NSW"])

    if mock_mode or not api_key or api_key.startswith("sk-ant-your"):
        logger.info(f"[{job_id}] make_good_rules: MOCK_MODE — returning synthetic findings")
        return _mock_analysis(jur, standards)

    # ── 1. Deterministic pattern matching ─────────────────────────────────────
    analysis = MakeGoodAnalysis(
        jurisdiction=jur,
        standard_description=standards["standard"],
    )

    has_fwt = bool(_FWT_CARVE_OUT_PATTERN.search(clause_text))
    analysis.has_fair_wear_tear_carve_out = has_fwt
    requires_base_building = bool(
        re.search(r"\bbase\s*building\b", clause_text, re.I)
        or re.search(r"\bshell\s*(?:and\s*core|condition)\b", clause_text, re.I)
    )
    analysis.requires_base_building = requires_base_building

    for pattern, description in _EXPLOITATIVE_PATTERNS:
        m = pattern.search(clause_text)
        if m:
            # Get surrounding context (50 chars each side)
            start = max(0, m.start() - 50)
            end   = min(len(clause_text), m.end() + 50)
            excerpt = clause_text[start:end].strip()

            finding = MakeGoodFinding(
                risk_type="exploitative",
                severity="high",
                description=description,
                clause_excerpt=f"...{excerpt}...",
                recommendation=f"Delete or amend this provision. {standards['safe_clauses'][0] if standards['safe_clauses'] else 'Replace with fair wear and tear standard.'}",
                legislation_ref=standards.get("legislation"),
                financial_estimate=standards.get("typical_cost"),
            )
            analysis.findings.append(finding)

    if not has_fwt:
        analysis.findings.append(MakeGoodFinding(
            risk_type="missing_protection",
            severity="high",
            description=(
                "No fair wear and tear carve-out detected. Without this exception, "
                "the tenant may be liable for normal deterioration from regular use."
            ),
            clause_excerpt="[fair wear and tear exception not found in clause text]",
            recommendation=(
                "Insert: 'For the avoidance of doubt, the Tenant's make-good obligations do not "
                "extend to fair wear and tear from normal and reasonable use of the premises.'"
            ),
            legislation_ref=standards.get("legislation"),
        ))

    # ── 2. Dollar liability estimate ──────────────────────────────────────────
    if floor_area_sqm and requires_base_building:
        # Use higher end of cost range for base building requirement
        cost_per_sqm_low  = 150
        cost_per_sqm_high = 350
        low  = int(floor_area_sqm * cost_per_sqm_low)
        high = int(floor_area_sqm * cost_per_sqm_high)
        analysis.estimated_liability_range = f"~${low:,}–${high:,} (base building strip-out for {floor_area_sqm:.0f} sqm)"
    elif floor_area_sqm:
        low  = int(floor_area_sqm * 50)
        high = int(floor_area_sqm * 150)
        analysis.estimated_liability_range = f"~${low:,}–${high:,} (standard make-good for {floor_area_sqm:.0f} sqm)"
    else:
        analysis.estimated_liability_range = standards.get("typical_cost")

    # ── 3. Haiku nuanced analysis ─────────────────────────────────────────────
    try:
        from llm.router import get_client, HAIKU_MODEL
        client = get_client()

        _SYS = (
            "You are an expert Australian commercial lease make-good analyst. "
            "Identify risks in make-good clauses beyond obvious pattern matches. "
            "Return ONLY valid JSON — no preamble, no markdown."
        )
        _PROMPT = f"""
JURISDICTION: {jur}
MARKET STANDARD: {standards['standard']}
EXPLOITATIVE STANDARD: {standards['exploitative']}
KEY RISK FACTORS: {json.dumps(standards['key_risks'])}

MAKE-GOOD CLAUSE TEXT:
{clause_text[:4000]}

Identify additional risks NOT covered by these obvious patterns:
  - "base building condition" obligation
  - missing fair wear and tear carve-out

Look for:
1. Ambiguous scope (e.g. "good repair" without definition)
2. Landlord's right to elect cash instead of works
3. Obligations applying to landlord-installed fitout/improvements
4. No time limit on completion of make-good works after expiry
5. Landlord's sole discretion to assess make-good quality
6. Missing commencement condition report (no baseline = tenant liable for pre-existing damage)

Return JSON array of findings (empty array if no additional risks):
[
  {{
    "risk_type": "exploitative" | "missing_protection" | "ambiguous",
    "severity": "high" | "medium" | "low",
    "description": "One-sentence finding",
    "clause_excerpt": "Verbatim snippet that triggered this",
    "recommendation": "Specific amendment or deletion to request"
  }}
]
"""
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1024,
            system=_SYS,
            messages=[{"role": "user", "content": _PROMPT}],
            timeout=30.0,
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        extra_findings = json.loads(raw)

        for ef in extra_findings:
            if not isinstance(ef, dict):
                continue
            analysis.findings.append(MakeGoodFinding(
                risk_type=ef.get("risk_type", "ambiguous"),
                severity=ef.get("severity", "medium"),
                description=ef.get("description", ""),
                clause_excerpt=ef.get("clause_excerpt", ""),
                recommendation=ef.get("recommendation", "Review with your legal advisor."),
                legislation_ref=standards.get("legislation"),
            ))

        logger.info(f"[{job_id}] make_good_rules: Haiku found {len(extra_findings)} additional findings")

    except Exception as e:
        analysis.warnings.append(f"Haiku nuanced analysis failed (non-fatal): {e}")
        logger.warning(f"[{job_id}] make_good_rules: Haiku analysis error: {e}")

    logger.info(
        f"[{job_id}] make_good_rules: {len(analysis.findings)} total findings, "
        f"exploitative={analysis.is_exploitative}, FWT={analysis.has_fair_wear_tear_carve_out}"
    )
    return analysis


def build_make_good_prompt_block(
    jurisdiction: str,
    lease_term_years: Optional[float] = None,
    floor_area_sqm: Optional[float] = None,
) -> str:
    """
    AQ-NEW-8: Build a context block for injection into the clause analysis prompt
    when a make-good clause is detected.

    Injected by audit_pipeline._analyse_one() when clause_type == "make_good"
    or when make-good keywords appear in the clause text.

    Provides:
    - Jurisdiction standard vs exploitative benchmark
    - Dollar cost estimates calibrated to floor area
    - Key risk patterns to check
    - Specific amendment language to suggest
    """
    jur = jurisdiction.upper().strip()
    standards = MAKE_GOOD_STANDARDS.get(jur, MAKE_GOOD_STANDARDS["NSW"])

    cost_note = ""
    if floor_area_sqm:
        low  = int(floor_area_sqm * 50)
        high = int(floor_area_sqm * 350)
        cost_note = (
            f"For a {floor_area_sqm:.0f} sqm tenancy this lease, "
            f"estimated make-good liability ranges from ${low:,} (standard) to "
            f"${high:,} (base building condition)."
        )
    else:
        cost_note = f"Typical cost range for {jur}: {standards['typical_cost']}"

    term_note = ""
    if lease_term_years and lease_term_years >= 7:
        term_note = (
            f"\nLONG LEASE NOTE ({lease_term_years:.0f} years): "
            "Premises condition diverges significantly from commencement over a long term. "
            "Without a commencement condition report, any defect claimed at expiry is "
            "presumed to be the tenant's liability — this is a critical protection gap."
        )

    key_risks_str = "\n".join(f"  - {r}" for r in standards["key_risks"])
    safe_clauses_str = "\n".join(f"  - {s}" for s in standards["safe_clauses"])

    return (
        f"╔══ AQ-NEW-8: MAKE-GOOD ANALYSIS CONTEXT ({jur}) ═══════════════════════╗\n"
        f"  Market standard ({jur}): {standards['standard']}\n"
        f"  Exploitative standard:  {standards['exploitative']}\n"
        f"  Relevant legislation:   {standards.get('legislation', 'Common law')}\n"
        f"\n"
        f"  COST ESTIMATE: {cost_note}{term_note}\n"
        f"\n"
        f"  KEY RISK PATTERNS (flag each if present):\n"
        f"{key_risks_str}\n"
        f"\n"
        f"  ACCEPTABLE ('SAFE') MAKE-GOOD LANGUAGE:\n"
        f"{safe_clauses_str}\n"
        f"\n"
        f"  MANDATORY: Compare the clause to the exploitative standard above.\n"
        f"  If the clause imposes 'base building condition', flag as HIGH with\n"
        f"  financial_impact_estimate set to the cost range above.\n"
        f"  Always check for: (1) fair wear and tear carve-out, "
        f"(2) commencement condition report reference, "
        f"(3) post-expiry deadline for completing works.\n"
        f"╚════════════════════════════════════════════════════════════════════════╝"
    )


def make_good_to_dict(analysis: MakeGoodAnalysis) -> dict:
    """Serialise MakeGoodAnalysis to plain dict for JSON storage."""
    return {
        "jurisdiction":                 analysis.jurisdiction,
        "has_fair_wear_tear_carve_out": analysis.has_fair_wear_tear_carve_out,
        "requires_base_building":       analysis.requires_base_building,
        "is_exploitative":              analysis.is_exploitative,
        "highest_severity":             analysis.highest_severity,
        "estimated_liability_range":    analysis.estimated_liability_range,
        "standard_description":         analysis.standard_description,
        "engine_status":                analysis.engine_status,
        "warnings":                     analysis.warnings,
        "findings": [
            {
                "risk_type":         f.risk_type,
                "severity":          f.severity,
                "description":       f.description,
                "clause_excerpt":    f.clause_excerpt,
                "recommendation":    f.recommendation,
                "legislation_ref":   f.legislation_ref,
                "financial_estimate":f.financial_estimate,
            }
            for f in analysis.findings
        ],
    }


# ── Mock helper ───────────────────────────────────────────────────────────────

def _mock_analysis(jurisdiction: str, standards: dict) -> MakeGoodAnalysis:
    """Return a synthetic make-good analysis matching the fixture lease (WA, Edward Millen)."""
    analysis = MakeGoodAnalysis(
        jurisdiction=jurisdiction,
        has_fair_wear_tear_carve_out=False,
        requires_base_building=True,
        estimated_liability_range="~$180,000–$420,000 (base building strip-out, 1,200 sqm est.)",
        standard_description=standards["standard"],
        engine_status="mock",
    )
    analysis.findings = [
        MakeGoodFinding(
            risk_type="exploitative",
            severity="high",
            description=(
                "[MOCK] Tenant required to restore premises to 'base building condition' on expiry. "
                "This is the most expensive possible make-good standard and is not market practice "
                "for a 20-year lease of this type. Estimated liability: $180k–$420k."
            ),
            clause_excerpt="...the Tenant must restore the Premises to base building condition...",
            recommendation=(
                "Negotiate to replace 'base building condition' with fair wear and tear standard: "
                "'The Tenant must repair damage caused by the Tenant's works and remove Tenant-"
                "installed fixtures at the Landlord's written election, fair wear and tear excepted.'"
            ),
            legislation_ref=standards.get("legislation"),
            financial_estimate="~$180k–$420k (base building, est. 1,200 sqm)",
        ),
        MakeGoodFinding(
            risk_type="missing_protection",
            severity="high",
            description=(
                "[MOCK] No fair wear and tear carve-out in the make-good clause. "
                "Without this protection, normal deterioration from 20 years of regular use "
                "is treated as damage the tenant must remedy."
            ),
            clause_excerpt="[fair wear and tear exception not found in clause text — MOCK]",
            recommendation=(
                "Insert: 'For the avoidance of doubt, the Tenant's make-good obligations "
                "do not extend to fair wear and tear from normal and reasonable use of the premises.'"
            ),
            legislation_ref=standards.get("legislation"),
        ),
        MakeGoodFinding(
            risk_type="missing_protection",
            severity="medium",
            description=(
                "[MOCK] No commencement condition report referenced in the make-good clause. "
                "After a 20-year term, any pre-existing defect at commencement will be "
                "attributed to the tenant without a documented baseline."
            ),
            clause_excerpt="[commencement condition report not referenced — MOCK]",
            recommendation=(
                "Insert a requirement for both parties to sign a commencement condition report "
                "within 14 days of the lease start date, to be attached as a schedule."
            ),
            legislation_ref=None,
        ),
    ]
    analysis.warnings = [
        "MOCK_MODE: Make-good analysis is synthetic. Set MOCK_MODE=false for real analysis."
    ]
    return analysis


# ── Audit pipeline integration hook ──────────────────────────────────────────

# Make-good clause detection keywords — used by audit_pipeline to know when
# to call build_make_good_prompt_block() for context injection.
MAKE_GOOD_KEYWORDS: frozenset[str] = frozenset([
    "make good", "make-good", "makegood",
    "reinstate", "reinstatement",
    "restore", "restoration",
    "yield up", "hand back",
    "dilapidations",
])


def is_make_good_clause(chunk) -> bool:
    """
    Return True if a chunk likely contains a make-good obligation.
    Used by audit_pipeline to decide whether to inject make-good context.
    """
    combined = " ".join([
        chunk.content,
        chunk.metadata.get("clause_heading", ""),
        (chunk.metadata.get("clause_type") or ""),
    ]).lower()
    return any(kw in combined for kw in MAKE_GOOD_KEYWORDS)
