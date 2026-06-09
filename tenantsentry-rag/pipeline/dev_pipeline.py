"""
dev_pipeline.py — DEV MODE pipeline (formerly mock_pipeline.py)

mock_pipeline.py
----------------
DEV MODE: Returns deterministic audit results for local development and testing.
Zero external dependencies — no API calls, no Supabase, no billing.
Covers all frontend code paths:
  - High/medium/low risk flags
  - Multiple clause types
  - Risk score gauge
  - PDF report generation

Enable by setting DEV_MODE=true in .env (default for local dev).
Switch at runtime via the DEV/LIVE toggle in the nav or POST /api/admin/mode/toggle.

Fixture lease: Edward Millen Precinct, 15 Hill View Terrace, East Victoria Park WA
  Landlord:  Town of Victoria Park (ABN 77 284 859 739)
  Tenant:    Blackoak Capital – Elizabeth Baillie Pty Ltd (ACN 651 448 583)
  Term:      20 years | Rent: $122,500/yr (Further Term) | Jurisdiction: WA
  Source:    data/fixtures/Edward_Millen_Precinct_Lease.pdf
"""

import time
from pathlib import Path
from typing import Callable, Optional
from output.json_formatter import AuditResult, ClauseAnalysis

ProgressCallback = Optional[Callable[[int, str], None]]

# Fixture lease used when no PDF is uploaded in DEV mode
DEV_FIXTURE_LEASE = Path(__file__).parent.parent / "data" / "fixtures" / "Edward_Millen_Precinct_Lease.pdf"
DEV_FIXTURE_TENANT = "Blackoak Capital – Elizabeth Baillie Pty Ltd"
DEV_FIXTURE_JURISDICTION = "WA"


def _progress(callback: ProgressCallback, pct: int, stage: str) -> None:
    if callback:
        callback(pct, stage)


MOCK_CLAUSES = [
    ClauseAnalysis(
        clause_heading="1.1 DEFINITIONS — Premises & Landlord",
        clause_text=(
            "Land means Lot 9000 on Deposited Plan 41207, being the whole of the land in "
            "Certificate of Title Volume 2992 Folio 139, more commonly known as "
            "15 Hill View Terrace, East Victoria Park, Western Australia. "
            "Landlord means TOWN OF VICTORIA PARK (ABN 77 284 859 739) of "
            "99 Shepperton Road, Victoria Park, Western Australia, 6100. "
            "Premises means that portion of the Land the boundaries of which are outlined "
            "in Annexure A. Permitted Use means Community, Recreational, Civic, Entertainment, "
            "Education, Cultural and Creative Industry, Heritage and Small Scale Production "
            "and any other use agreed to and approved by the Landlord in writing."
        ),
        clause_type="Definitions",
        key_terms=["15 Hill View Terrace", "Town of Victoria Park", "Lot 9000 DP 41207", "Permitted Use — broad community"],
        risk_flags=[],
        plain_english_summary=(
            "The premises are part of the Edward Millen Precinct heritage site at "
            "15 Hill View Terrace, East Victoria Park WA. The landlord is the Town of Victoria Park "
            "(the local council). The permitted use is broad — community, entertainment, cultural, "
            "education and heritage uses are all allowed, which is favourable for the tenant."
        ),
        recommended_action=(
            "Confirm the precise Annexure A boundaries match your intended footprint before signing. "
            "Ensure 'Small Scale Production' is defined clearly if you plan manufacturing or food production. "
            "Note: the broad permitted use is a positive — protect it from being narrowed by special conditions."
        ),
    ),
    ClauseAnalysis(
        clause_heading="3. TERM AND OPTIONS",
        clause_text=(
            "Item 3 — Term: Twenty (20) years. "
            "Item 4 — Commencement Date: As per the Agreement for Lease. "
            "Item 5 — Expiry Date: The date immediately prior to the 20th anniversary of the "
            "Commencement Date. The Tenant has options to renew for Further Terms as specified "
            "in clause 4. Options must be exercised by written notice within the timeframes "
            "prescribed in clause 4."
        ),
        clause_type="Term & Options",
        key_terms=["20 year term", "commencement TBC", "Further Terms", "written notice required"],
        risk_flags=[
            {
                "flag_id": "TERM001",
                "description": "Commencement Date is '[As per the AFL]' — a blank placeholder in this draft. "
                               "The lease cannot be binding until this is filled in. Risk of dispute "
                               "over when obligations begin.",
                "severity": "high",
                "legislation_ref": "Property Law Act 1969 (WA) s.4 — lease must have certain commencement to be enforceable",
            },
            {
                "flag_id": "TERM002",
                "description": "A 20-year term is an exceptionally long commitment. If business circumstances "
                               "change, exit options are limited without landlord consent.",
                "severity": "medium",
                "legislation_ref": None,
            },
        ],
        plain_english_summary=(
            "This is a 20-year lease — an unusually long term for a commercial tenancy. "
            "The start date is not yet filled in (it references a separate Agreement for Lease). "
            "You are locked in for two decades unless you can negotiate an early exit right. "
            "Missing the option exercise window at the end of the initial term means losing renewal rights."
        ),
        recommended_action=(
            "Insist the Commencement Date be specified as a fixed calendar date before signing. "
            "Negotiate a break clause (e.g. at year 10) allowing exit on 12 months notice. "
            "Set calendar alerts for option exercise deadlines well in advance."
        ),
    ),
    ClauseAnalysis(
        clause_heading="5. RENT REVIEW",
        clause_text=(
            "Item 6(a) — During the initial Term no Rent is payable. "
            "Item 6(c) — $122,500.00 as at the Commencement Date, reviewed in accordance with "
            "clause 2 of Schedule 2 on each anniversary of the Commencement Date to the date of "
            "commencement of the First Further Term. "
            "Schedule 2, clause 2 — On each CPI Review Date, Rent will be reviewed to the greater of: "
            "(a) the Rent for the preceding 12 months increased by 1%; or "
            "(b) the Rent for the previous 12 months multiplied by Current CPI divided by Previous CPI, "
            "capped at a 2.5% increase."
        ),
        clause_type="Rent Review",
        key_terms=["$122,500/yr base rent", "rent-free initial term", "CPI review", "1% floor", "2.5% cap"],
        risk_flags=[
            {
                "flag_id": "RF001",
                "description": "The 1% minimum rent increase applies even when CPI is zero or negative, "
                               "meaning rent always rises regardless of economic conditions.",
                "severity": "medium",
                "legislation_ref": None,
            },
            {
                "flag_id": "RF001b",
                "description": "Rent of $122,500/yr only becomes payable from the First Further Term. "
                               "It is unclear whether this is reviewed during the rent-free initial term — "
                               "if so, rent could be materially higher than $122,500 when payments begin.",
                "severity": "high",
                "legislation_ref": None,
            },
        ],
        plain_english_summary=(
            "No rent is payable during the initial 20-year term — a significant concession from the council. "
            "Rent of $122,500/yr kicks in only if you exercise a Further Term option. "
            "Reviews are CPI-linked with a 1% floor and 2.5% cap, which is reasonable. "
            "However, if CPI reviews apply during the rent-free period, the base rent could compound "
            "substantially before your first payment is due."
        ),
        recommended_action=(
            "Clarify in writing whether CPI reviews accumulate during the rent-free initial term. "
            "If so, negotiate that $122,500 is the fixed base rent at the start of the First Further Term, "
            "regardless of any notional reviews during the initial term. "
            "The 2.5% cap and CPI linkage are otherwise tenant-favourable — protect them."
        ),
    ),
    ClauseAnalysis(
        clause_heading="8. MAINTENANCE AND REPAIR",
        clause_text=(
            "8.1 — Tenant to repair and maintain: The Tenant must keep the Premises and the "
            "Landlord's Equipment in good and substantial repair and condition. "
            "8.3 — Preventative and general maintenance: The Tenant must carry out all preventative "
            "and general maintenance in accordance with the Asset Maintenance Plan. "
            "8.4 — Structural repairs: The Tenant is responsible for all structural repairs to the "
            "Building including the roof, external walls, and foundations. "
            "8.5 — Asset Maintenance Plan: The Tenant must comply with the Asset Maintenance Plan "
            "as amended from time to time by the Minister."
        ),
        clause_type="Maintenance & Repair",
        key_terms=["structural repairs — tenant's cost", "Asset Maintenance Plan", "Landlord's Equipment", "roof and foundations"],
        risk_flags=[
            {
                "flag_id": "RF005",
                "description": "Tenant is responsible for structural repairs including roof, external walls, "
                               "and foundations. On a heritage building, structural costs can run into millions. "
                               "This is an extremely onerous obligation.",
                "severity": "high",
                "legislation_ref": "Property Law Act 1969 (WA) — no statutory protection against structural repair obligations in non-retail leases",
            },
            {
                "flag_id": "RF005b",
                "description": "The Asset Maintenance Plan can be amended 'from time to time by the Minister' "
                               "without tenant consent, potentially increasing obligations after signing.",
                "severity": "high",
                "legislation_ref": None,
            },
            {
                "flag_id": "RF005c",
                "description": "Tenant must maintain 'Landlord's Equipment' — the scope of which is defined "
                               "by Item 12 (not shown). This could include major plant and infrastructure.",
                "severity": "medium",
                "legislation_ref": None,
            },
        ],
        plain_english_summary=(
            "You are responsible for ALL repairs including structural ones — roof, walls, foundations. "
            "On a heritage building, this could mean millions in unexpected costs. "
            "The Asset Maintenance Plan (which dictates what you must do) can be changed by the Minister "
            "at any time without your agreement. This is one of the most financially dangerous clauses in the lease."
        ),
        recommended_action=(
            "Negotiate to exclude structural repairs from the tenant's obligations — these should be the landlord's. "
            "At minimum, cap the tenant's structural repair liability per year or per event. "
            "Require that any amendment to the Asset Maintenance Plan requires tenant consent if it "
            "materially increases the tenant's obligations. "
            "Commission a full building condition report before signing to understand your baseline exposure."
        ),
    ),
    ClauseAnalysis(
        clause_heading="9. COMPLY WITH LAWS AND HERITAGE AGREEMENT",
        clause_text=(
            "9.1 — The Tenant must comply with all Laws and the Heritage Agreement in relation to "
            "the Premises. The Tenant must obtain and maintain all approvals, licences, and permits "
            "required for the Permitted Use at its own cost. "
            "The Premises is subject to heritage listing and the Conditional Tenure Conditions "
            "in Transfer O548293. The Tenant must comply with all conditions of the Deed of Agreement "
            "between the State of Western Australia and the Landlord dated 8 September 2020."
        ),
        clause_type="Compliance & Heritage",
        key_terms=["heritage listing", "Conditional Tenure Conditions", "Deed of Agreement", "all approvals at tenant's cost"],
        risk_flags=[
            {
                "flag_id": "RF006",
                "description": "The premises is a heritage site subject to a State-level Deed of Agreement "
                               "and Transfer conditions (O548293) that the tenant must comply with. "
                               "The full scope of these obligations is not visible in the lease itself.",
                "severity": "high",
                "legislation_ref": "Heritage Act 2018 (WA) — heritage-listed buildings carry strict use and alteration constraints",
            },
            {
                "flag_id": "RF006b",
                "description": "All approvals and licences required for the Permitted Use are at the tenant's "
                               "cost with no landlord assistance obligation. Heritage approvals can be "
                               "time-consuming and expensive.",
                "severity": "medium",
                "legislation_ref": None,
            },
        ],
        plain_english_summary=(
            "The Edward Millen Precinct is a heritage site with State-level obligations layered on top of "
            "the lease. You must comply with a Deed of Agreement between the State and the council, "
            "plus Transfer conditions, which are not fully reproduced in the lease. "
            "You must obtain all permits at your own cost. Heritage constraints can significantly limit "
            "how you use, alter, or fit out the space."
        ),
        recommended_action=(
            "Obtain and read the full Deed of Agreement (Annexure C) and Transfer O548293 conditions "
            "before signing — these are binding on you even though they're between the State and the council. "
            "Get heritage planning advice on what alterations and uses are permissible. "
            "Negotiate a landlord warranty that the Permitted Use is currently approved under all heritage obligations."
        ),
    ),
    ClauseAnalysis(
        clause_heading="12. ASSIGNMENT AND SUBLETTING",
        clause_text=(
            "12.2 — The Tenant must not assign this Lease without the prior written consent of the Landlord. "
            "12.3 — The Tenant acknowledges there is no statutory right to assign. "
            "12.6 — Consent to a Major Sublease: The Landlord may impose conditions on consent including "
            "requiring the subtenant to enter into a deed of covenant with the Landlord. "
            "12.8 — No consent required for Minor Sublease: The Tenant may grant a sublease of part of "
            "the Premises for a term not exceeding 3 years without the Landlord's consent, provided the "
            "Landlord is notified within 5 Business Days."
        ),
        clause_type="Assignment",
        key_terms=["no statutory assignment right", "major sublease requires consent", "minor sublease ≤3 years permitted", "deed of covenant"],
        risk_flags=[
            {
                "flag_id": "RF003",
                "description": "The lease expressly excludes any statutory right to assign — unusual and "
                               "more restrictive than standard commercial leases. The landlord (a council) "
                               "can withhold consent on broad grounds.",
                "severity": "high",
                "legislation_ref": "Property Law Act 1969 (WA) s.80 — assignment rights may otherwise apply",
            },
            {
                "flag_id": "RF003b",
                "description": "Major subleases require a deed of covenant from the subtenant directly to "
                               "the council. This adds legal cost and complexity when subleasing to operators.",
                "severity": "medium",
                "legislation_ref": None,
            },
        ],
        plain_english_summary=(
            "You cannot transfer or sell your lease without council approval. "
            "You can sublease parts of the space to operators for up to 3 years without consent "
            "(just notify within 5 days) — this is useful flexibility for short-term activations. "
            "Longer subleases need council consent and require your subtenants to sign a deed directly "
            "with the council, adding friction and legal cost."
        ),
        recommended_action=(
            "Negotiate a reasonable consent standard ('not to be unreasonably withheld or delayed') "
            "and a 30-day deemed approval timeframe. "
            "The minor sublease carve-out (≤3 years, no consent) is valuable — confirm its scope covers "
            "pop-up retail, market stalls, and short-term hirers. "
            "Seek legal advice before entering any major sublease arrangement given the deed of covenant requirement."
        ),
    ),
    ClauseAnalysis(
        clause_heading="8.6 MAKE GOOD",
        clause_text=(
            "8.6 — Make good damage: Upon expiry or earlier termination of this Lease, the Tenant must "
            "at its own cost restore the Premises to the condition required by the Asset Maintenance Plan "
            "and to the reasonable satisfaction of the Landlord, including removal of all Tenant's "
            "fixtures, fittings, and works unless the Landlord directs otherwise in writing."
        ),
        clause_type="Make Good",
        key_terms=["restore to AMP condition", "landlord satisfaction", "remove all fixtures", "landlord may waive in writing"],
        risk_flags=[
            {
                "flag_id": "RF004",
                "description": "Make-good standard is 'condition required by the Asset Maintenance Plan' and "
                               "'reasonable satisfaction of the Landlord' — both subjective and open to dispute "
                               "on a heritage building that will age over 20 years.",
                "severity": "medium",
                "legislation_ref": None,
            },
            {
                "flag_id": "RF004b",
                "description": "Tenant must remove all fixtures and works at end of lease unless landlord "
                               "agrees otherwise in writing. On a heritage fitout this could mean costly "
                               "strip-out of purpose-built improvements.",
                "severity": "medium",
                "legislation_ref": None,
            },
        ],
        plain_english_summary=(
            "At the end of the lease you must restore the premises to the standard required by the "
            "Asset Maintenance Plan and the landlord's satisfaction. You must remove all your fitout "
            "unless the council agrees in writing to let you leave it. "
            "On a 20-year heritage tenancy with significant capital investment, make-good costs could "
            "be substantial. The standard is somewhat subjective."
        ),
        recommended_action=(
            "Commission a detailed condition report at commencement and agree it with the landlord in writing "
            "— this becomes your baseline and protects against unreasonable make-good demands at exit. "
            "Negotiate that improvements made in accordance with the Asset Maintenance Plan are excluded from make-good. "
            "Seek a clause that the landlord must specify required make-good works in writing at least 6 months before expiry."
        ),
    ),
]


def run_dev_audit(
    pdf_path: str,
    jurisdiction: str,
    tenant_name: str = None,
    job_id: str = None,           # accepted but ignored in mock mode
    document_hash: str = None,    # accepted but ignored in mock mode
    progress_callback: ProgressCallback = None,
    additional_docs: list = None, # accepted but ignored in mock mode
    max_pages: int = None,        # accepted but ignored — mock always uses MOCK_CLAUSES
    skip_vector_store: bool = False,  # accepted but ignored — mock never calls vector store
    # AQ-NEW-5: accepted but defaulted to fixture values in dev mode
    premises_use: str = None,
    entity_type: str = None,
    gla_sqm: float = None,
    applicable_statute: str = None,
    statute_code: str = None,
    is_retail_lease: bool = None,
    statute_prompt_block: str = "",
) -> AuditResult:
    """
    DEV MODE: Returns deterministic audit result for testing.
    Uses Edward Millen Precinct lease fixture as the reference document.
    Simulates realistic processing time. Zero external dependencies.
    """
    cb = progress_callback

    _progress(cb, 5, "Parsing PDF...")
    time.sleep(1.2)

    _progress(cb, 20, "Identifying lease clauses...")
    time.sleep(1.0)

    _progress(cb, 35, "Loading risk rules...")
    time.sleep(0.8)

    n = len(MOCK_CLAUSES)
    for idx in range(n):
        pct = 40 + int((idx / n) * 50)
        _progress(cb, pct, f"Analysing clause {idx + 1} of {n}...")
        time.sleep(0.6)

    _progress(cb, 95, "Assembling audit report...")
    time.sleep(0.5)

    all_flags = [f for ca in MOCK_CLAUSES for f in (ca.risk_flags or [])]
    high_flags = [f for f in all_flags if f.get("severity") == "high"]
    medium_flags = [f for f in all_flags if f.get("severity") == "medium"]
    risk_score = min(100, len(high_flags) * 20 + len(medium_flags) * 8 + len(all_flags) * 2)

    filename = Path(pdf_path).name if pdf_path else DEV_FIXTURE_LEASE.name

    # AQ-NEW-5: Dev fixture is Edward Millen Precinct — WA, community/civic use (non-retail),
    # government landlord (Town of Victoria Park), commercial tenancy law applies.
    _premises_use   = premises_use   or