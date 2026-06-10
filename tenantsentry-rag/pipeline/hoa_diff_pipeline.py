"""
hoa_diff_pipeline.py
--------------------
Area 3: HoA vs Final Lease "Lease Creep" Diff Engine.

Accepts two PDFs:
  (1) Signed Heads of Agreement (HoA)
  (2) Final draft lease

Extracts the key commercial terms and obligations from each document,
then uses Claude to identify discrepancies — terms the landlord's solicitors
added, altered, or quietly removed between the HoA and the final lease.

Output: DiffResult with a list of CreepFinding records, each describing:
  - The HoA term (or "NOT IN HOA" if entirely new)
  - The final lease term
  - Severity: "high" | "medium" | "low"
  - Category: rent | make_good | relocation | demolition | assignment |
               outgoings | fitout | bank_guarantee | options | access |
               liability | new_restriction | other
  - Recommended action for negotiation

Dev mode: returns a realistic mock DiffResult without calling Claude.
Live mode: runs real PDF extraction + Claude analysis.
"""

import os
import json
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from loguru import logger

from api.mode import is_dev

# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class CreepFinding:
    """A single discrepancy between HoA and final lease."""
    category: str                       # "rent" | "make_good" | "relocation" | etc.
    hoa_term: str                        # What the HoA said (or "NOT IN HOA")
    lease_term: str                      # What the final lease says
    severity: str                        # "high" | "medium" | "low"
    clause_reference: Optional[str]      # Final lease clause number if identifiable
    page_reference: Optional[int]        # Final lease page if identifiable
    description: str                     # Plain-English description of the creep
    recommended_action: str              # What to ask the landlord to change
    negotiation_email: Optional[str] = None  # Ready-to-copy email paragraph


@dataclass
class DiffResult:
    """Full result of an HoA vs final lease diff."""
    hoa_filename: str
    lease_filename: str
    jurisdiction: str
    total_findings: int
    high_count: int
    medium_count: int
    low_count: int
    findings: list[CreepFinding] = field(default_factory=list)
    pipeline_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hoa_filename":   self.hoa_filename,
            "lease_filename": self.lease_filename,
            "jurisdiction":   self.jurisdiction,
            "total_findings": self.total_findings,
            "high_count":     self.high_count,
            "medium_count":   self.medium_count,
            "low_count":      self.low_count,
            "findings": [
                {
                    "category":            f.category,
                    "hoa_term":            f.hoa_term,
                    "lease_term":          f.lease_term,
                    "severity":            f.severity,
                    "clause_reference":    f.clause_reference,
                    "page_reference":      f.page_reference,
                    "description":         f.description,
                    "recommended_action":  f.recommended_action,
                    "negotiation_email":   f.negotiation_email,
                }
                for f in self.findings
            ],
            "pipeline_warnings": self.pipeline_warnings,
        }


# ── Constants ─────────────────────────────────────────────────────────────────

_MOCK_FINDINGS = [
    CreepFinding(
        category="make_good",
        hoa_term="Tenant to make good fair wear and tear only",
        lease_term="Tenant to restore premises to original base-building condition at expiry, "
                   "including removal of all fitout, partition walls, cabling, and floor coverings",
        severity="high",
        clause_reference="Clause 18.3",
        page_reference=42,
        description="The HoA limited make-good to fair wear and tear. The final lease "
                    "requires full strip-out to base building — a significantly greater "
                    "obligation not agreed in the HoA. Estimated additional liability: "
                    "$80,000–$150,000 for a standard fitout.",
        recommended_action=(
            "Require the landlord to reinstate the HoA position: make-good limited to "
            "fair wear and tear. If base-building strip-out is genuinely required, "
            "negotiate a landlord fitout contribution to offset the end-of-lease cost."
        ),
        negotiation_email=(
            "We note that Clause 18.3 of the draft lease requires full restoration to "
            "base-building condition. This materially exceeds the agreed make-good "
            "position in the Heads of Agreement, which limited our obligation to fair "
            "wear and tear. We require this clause to be amended to reflect the agreed "
            "position. In the alternative, we request a landlord contribution to fitout "
            "costs to offset the disproportionate end-of-lease liability."
        ),
    ),
    CreepFinding(
        category="relocation",
        hoa_term="NOT IN HOA",
        lease_term="Landlord may relocate tenant to comparable premises in the centre on "
                   "60 days notice at landlord's election",
        severity="high",
        clause_reference="Clause 22.1",
        page_reference=51,
        description="The HoA contained no relocation right. A new Clause 22.1 grants "
                    "the landlord an unrestricted right to relocate the tenant on 60 days "
                    "notice — this was not negotiated and was not in the HoA.",
        recommended_action=(
            "Request deletion of Clause 22.1 in its entirety — no relocation right was "
            "agreed in the HoA. If the landlord insists, require: (a) 6 months minimum "
            "notice, (b) landlord pays all relocation and refitting costs, (c) the "
            "alternative premises must be materially equivalent in size, exposure, and "
            "location within the centre."
        ),
        negotiation_email=(
            "We note that Clause 22.1 of the draft lease introduces a relocation right "
            "that was not contained in the Heads of Agreement and was not agreed during "
            "negotiations. We require this clause to be deleted. If the landlord requires "
            "a relocation right to be included, we are willing to discuss subject to "
            "appropriate protections including adequate notice, landlord-funded relocation "
            "costs, and equivalent premises."
        ),
    ),
    CreepFinding(
        category="outgoings",
        hoa_term="Tenant to pay proportion of building outgoings excluding capital items",
        lease_term="Tenant to pay proportion of all building outgoings including capital "
                   "replacement, building refurbishment reserve, and landlord's management fee",
        severity="high",
        clause_reference="Clause 9.2",
        page_reference=24,
        description="The HoA excluded capital items from outgoings. The final lease "
                    "adds capital replacement and a building refurbishment reserve — both "
                    "capital expenditure items prohibited under applicable retail tenancy "
                    "legislation (Retail Leases Act 1994 (NSW) s.12). A landlord management "
                    "fee was also added with no HoA basis.",
        recommended_action=(
            "Require deletion of capital replacement and building refurbishment reserve "
            "from the outgoings definition — these are prohibited outgoings under "
            "NSW s.12. Also require deletion of the landlord management fee, which was "
            "not agreed in the HoA and is not a recoverable outgoing."
        ),
        negotiation_email=(
            "We note that Clause 9.2 of the draft lease expands the outgoings definition "
            "to include capital replacement costs, a building refurbishment reserve, and a "
            "landlord management fee. These items were not agreed in the Heads of Agreement. "
            "Capital expenditure is also a prohibited outgoing under the Retail Leases Act "
            "1994 (NSW) s.12. We require the outgoings definition to be amended to reflect "
            "the agreed HoA position and exclude these items."
        ),
    ),
    CreepFinding(
        category="bank_guarantee",
        hoa_term="Bank guarantee: 3 months gross rent",
        lease_term="Bank guarantee: 6 months gross rent (including outgoings and GST)",
        severity="medium",
        clause_reference="Clause 7.1",
        page_reference=18,
        description="The HoA specified a 3-month bank guarantee. The final lease doubles "
                    "this to 6 months and adds outgoings and GST to the calculation base — "
                    "materially increasing the security deposit beyond what was negotiated.",
        recommended_action=(
            "Require reinstatement of the 3-month bank guarantee as agreed in the HoA. "
            "Alternatively, agree to step-down provisions: 6 months for years 1–2, "
            "reducing to 3 months for the balance of the term subject to no default."
        ),
        negotiation_email=(
            "Clause 7.1 of the draft lease requires a bank guarantee of 6 months gross "
            "rent including outgoings and GST. The Heads of Agreement specified 3 months "
            "gross rent. We require the bank guarantee to be reduced to the agreed "
            "3-month amount, calculated on base rent only as discussed."
        ),
    ),
    CreepFinding(
        category="assignment",
        hoa_term="Assignment permitted with landlord consent (not to be unreasonably withheld)",
        lease_term="Assignment permitted with landlord consent (not to be unreasonably withheld). "
                   "Assignor to remain liable for all lease obligations for the unexpired term "
                   "as if the lease had not been assigned.",
        severity="medium",
        clause_reference="Clause 15.4",
        page_reference=37,
        description="The HoA permitted assignment on reasonable consent. The final lease "
                    "adds ongoing assignor liability for the full unexpired term post-assignment "
                    "— this was not in the HoA and significantly increases the tenant's risk on exit.",
        recommended_action=(
            "Require deletion of the ongoing assignor liability tail. Post-assignment "
            "liability should be limited to a 12-month guarantee period at most. "
            "Alternatively, require that assignor liability is released on any default "
            "by the assignee that the landlord fails to notify within 10 business days."
        ),
        negotiation_email=(
            "Clause 15.4 introduces assignor liability for the full unexpired lease term "
            "post-assignment. This was not included in the Heads of Agreement and substantially "
            "changes the commercial risk profile of any future assignment. We require this "
            "provision to be deleted or, at minimum, limited to a 12-month capped guarantee "
            "period following assignment."
        ),
    ),
    CreepFinding(
        category="demolition",
        hoa_term="NOT IN HOA",
        lease_term="Landlord may terminate lease on 6 months notice if landlord proposes to "
                   "demolish, redevelop, or carry out substantial refurbishment of the building",
        severity="medium",
        clause_reference="Clause 23.2",
        page_reference=54,
        description="No demolition or redevelopment termination right was agreed in the HoA. "
                    "Clause 23.2 adds a landlord termination right on 6 months' notice — "
                    "a material additional risk for a long-term office tenant.",
        recommended_action=(
            "If a demolition clause cannot be deleted, negotiate: (a) minimum 12 months' "
            "notice, (b) express compensation for unamortised fitout costs, (c) equivalent "
            "replacement premises offered at the same rent before termination is exercised, "
            "(d) demolition clause not to apply in the first 3 years of the lease term."
        ),
        negotiation_email=(
            "Clause 23.2 introduces a demolition and redevelopment termination right that "
            "was not agreed in the Heads of Agreement. Given the planned fitout investment "
            "and the length of the proposed term, we require this clause to be deleted. If "
            "the landlord requires a demolition right, we are prepared to discuss subject "
            "to appropriate notice, compensation, and exclusion during the initial term."
        ),
    ),
]


# ── Main entry point ──────────────────────────────────────────────────────────

def run_hoa_diff(
    hoa_pdf_path: str,
    lease_pdf_path: str,
    jurisdiction: str = "NSW",
    progress_callback=None,
) -> DiffResult:
    """
    Compare a Heads of Agreement PDF against a final draft lease PDF.

    Args:
        hoa_pdf_path:    Absolute path to the signed HoA PDF.
        lease_pdf_path:  Absolute path to the final draft lease PDF.
        jurisdiction:    State code — defaults to NSW.
        progress_callback: Optional fn(pct: int, stage: str)

    Returns:
        DiffResult with a list of CreepFinding records.
    """
    dev = is_dev()
    hoa_filename   = Path(hoa_pdf_path).name
    lease_filename = Path(lease_pdf_path).name
    jur = jurisdiction.upper().strip() or "NSW"

    def _prog(pct: int, stage: str):
        if progress_callback:
            progress_callback(pct, stage)
        logger.debug(f"[HoA-diff:{pct}%] {stage}")

    logger.info(
        f"[HoA-diff] mode={'dev' if dev else 'live'} "
        f"hoa={hoa_filename} lease={lease_filename} jurisdiction={jur}"
    )

    if dev:
        return _run_mock(hoa_filename, lease_filename, jur)

    return _run_live(hoa_pdf_path, lease_pdf_path, hoa_filename, lease_filename, jur, _prog)


# ── DEV mock ──────────────────────────────────────────────────────────────────

def _run_mock(hoa_filename: str, lease_filename: str, jurisdiction: str) -> DiffResult:
    """Return a realistic mock DiffResult for DEV mode."""
    logger.info("[HoA-diff] DEV mode — returning mock diff result")
    high   = [f for f in _MOCK_FINDINGS if f.severity == "high"]
    medium = [f for f in _MOCK_FINDINGS if f.severity == "medium"]
    low    = [f for f in _MOCK_FINDINGS if f.severity == "low"]
    return DiffResult(
        hoa_filename=hoa_filename,
        lease_filename=lease_filename,
        jurisdiction=jurisdiction,
        total_findings=len(_MOCK_FINDINGS),
        high_count=len(high),
        medium_count=len(medium),
        low_count=len(low),
        findings=list(_MOCK_FINDINGS),
        pipeline_warnings=["DEV MODE: Mock lease creep findings. Upload real HoA and lease in LIVE mode."],
    )


# ── LIVE pipeline ─────────────────────────────────────────────────────────────

def _run_live(
    hoa_pdf_path: str,
    lease_pdf_path: str,
    hoa_filename: str,
    lease_filename: str,
    jurisdiction: str,
    progress_callback,
) -> DiffResult:
    """Full live pipeline: extract → diff → structured output."""
    import anthropic
    from ingestion.pdf_parser import parse_pdf
    from ingestion.chunker import chunk_document

    _prog = progress_callback
    warnings: list[str] = []

    # 1. Parse both documents
    _prog(5, "Parsing HoA PDF...")
    try:
        hoa_parsed  = parse_pdf(hoa_pdf_path)
    except Exception as e:
        raise ValueError(f"Failed to parse HoA PDF: {e}")

    _prog(15, "Parsing final lease PDF...")
    try:
        lease_parsed = parse_pdf(lease_pdf_path)
    except Exception as e:
        raise ValueError(f"Failed to parse final lease PDF: {e}")

    # 2. Extract full text
    _prog(20, "Extracting text from both documents...")
    hoa_text   = "\n\n".join(p.get("text", "") for p in hoa_parsed.pages)
    lease_text = "\n\n".join(p.get("text", "") for p in lease_parsed.pages)

    # 3. Truncate HoA to 8000 chars — HoAs are typically 2–10 pages
    #    Truncate lease to 16000 chars — focus on commercial/scheduling provisions
    hoa_excerpt   = hoa_text[:8000]
    lease_excerpt = lease_text[:16000]

    # 4. Send to Claude for structured diff
    _prog(30, "Comparing HoA terms against final lease...")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("sk-ant-your"):
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot run live HoA diff")

    model = os.environ.get("SONNET_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = f"""You are an expert Australian commercial lease solicitor and tenant representative.
Your task: compare a Heads of Agreement (HoA) against a final draft lease and identify every
instance of "lease creep" — terms the landlord's solicitors added, altered, or deleted between
the HoA and the final lease that are adverse to the tenant.

Jurisdiction: {jurisdiction}
Focus: commercial impact on the tenant. Ignore purely administrative or formatting differences.

For each discrepancy found, return a JSON object with these fields:
  category:           one of: rent | make_good | relocation | demolition | assignment |
                      outgoings | fitout | bank_guarantee | options | access |
                      liability | new_restriction | other
  hoa_term:           the relevant HoA wording (verbatim excerpt if possible), or "NOT IN HOA"
                      if this clause has no HoA counterpart
  lease_term:         the relevant final lease wording (verbatim excerpt if possible)
  severity:           "high" (material financial or legal risk) | "medium" (commercially significant)
                      | "low" (minor deviation)
  clause_reference:   clause number in the final lease if identifiable (e.g. "Clause 18.3"), else null
  description:        plain-English explanation of the creep and why it matters to the tenant,
                      including estimated financial impact where calculable
  recommended_action: specific change the tenant should request from the landlord
  negotiation_email:  a 3–4 sentence ready-to-copy email paragraph the tenant rep can send
                      to the landlord's agent, citing the HoA position and requesting the change

Return ONLY a JSON array of finding objects. No prose before or after the JSON.
If no discrepancies are found, return an empty array: []"""

    user_prompt = f"""HEADS OF AGREEMENT (first {len(hoa_excerpt)} characters):
---
{hoa_excerpt}
---

FINAL DRAFT LEASE (first {len(lease_excerpt)} characters):
---
{lease_excerpt}
---

Identify all lease creep discrepancies. Return JSON array only."""

    _prog(40, "Analysing discrepancies with AI...")
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            timeout=120.0,
        )
        raw = msg.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        findings_raw: list[dict] = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"[HoA-diff] JSON parse failed: {e} — raw: {raw[:200]}")
        warnings.append(f"AI response could not be parsed as JSON: {e}. Returning empty diff.")
        findings_raw = []
    except Exception as e:
        logger.error(f"[HoA-diff] Claude call failed: {e}")
        raise RuntimeError(f"HoA diff AI analysis failed: {e}")

    _prog(80, "Building diff report...")

    findings = []
    for item in findings_raw:
        try:
            findings.append(CreepFinding(
                category=item.get("category", "other"),
                hoa_term=item.get("hoa_term", ""),
                lease_term=item.get("lease_term", ""),
                severity=item.get("severity", "medium"),
                clause_reference=item.get("clause_reference"),
                page_reference=item.get("page_reference"),
                description=item.get("description", ""),
                recommended_action=item.get("recommended_action", ""),
                negotiation_email=item.get("negotiation_email"),
            ))
        except Exception as e:
            logger.warning(f"[HoA-diff] Skipping malformed finding: {e}")

    high   = [f for f in findings if f.severity == "high"]
    medium = [f for f in findings if f.severity == "medium"]
    low    = [f for f in findings if f.severity == "low"]

    _prog(95, "Assembling result...")
    logger.info(
        f"[HoA-diff] Complete — {len(findings)} findings: "
        f"{len(high)} high, {len(medium)} medium, {len(low)} low"
    )

    return DiffResult(
        hoa_filename=hoa_filename,
        lease_filename=lease_filename,
        jurisdiction=jurisdiction,
        total_findings=len(findings),
        high_count=len(high),
        medium_count=len(medium),
        low_count=len(low),
        findings=findings,
        pipeline_warnings=warnings,
    )
