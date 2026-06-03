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
"""

import time
from typing import Callable, Optional
from output.json_formatter import AuditResult, ClauseAnalysis

ProgressCallback = Optional[Callable[[int, str], None]]


def _progress(callback: ProgressCallback, pct: int, stage: str) -> None:
    if callback:
        callback(pct, stage)


MOCK_CLAUSES = [
    ClauseAnalysis(
        clause_heading="3. RENT REVIEW",
        clause_text=(
            "3.1 The rent shall be reviewed annually on each anniversary of the "
            "Commencement Date by a fixed increase of 5% per annum, regardless of "
            "any movement in the Consumer Price Index. A ratchet clause applies such "
            "that rent shall not decrease below the rent payable prior to review."
        ),
        clause_type="Rent Review",
        key_terms=["5% fixed increase", "annual review", "ratchet clause", "CPI excluded"],
        risk_flags=[
            {
                "flag_id": "RF001",
                "description": "Fixed 5% annual increase significantly exceeds typical CPI (2–3%). "
                               "Ratchet clause prevents downward adjustment on market review.",
                "severity": "high",
                "legislation_ref": "Retail Leases Act 1994 (NSW) s.35 — ratchet clauses are void",
            }
        ],
        plain_english_summary=(
            "Your rent increases by a fixed 5% every year no matter what happens to inflation or "
            "the market. The ratchet clause means rent can never go down, even if market rents fall. "
            "This is one of the most expensive clauses in this lease."
        ),
        recommended_action=(
            "Negotiate to replace the fixed 5% with CPI or market rent, whichever is lower. "
            "Remove the ratchet clause — it is void under NSW law anyway. "
            "Seek legal advice before signing."
        ),
    ),
    ClauseAnalysis(
        clause_heading="7. MAKE GOOD",
        clause_text=(
            "7.1 Upon expiry or earlier termination of this Lease, the Tenant must at its own cost "
            "restore the Premises to their original condition as at the Commencement Date, including "
            "removal of all Tenant's fixtures, fittings and works, fair wear and tear not excepted. "
            "The Tenant must repaint all surfaces in the Landlord's original colour scheme."
        ),
        clause_type="Make Good",
        key_terms=["original condition", "fair wear and tear not excepted", "repaint", "fixtures removal"],
        risk_flags=[
            {
                "flag_id": "RF004",
                "description": "Make-good obligations exclude fair wear and tear, meaning the tenant "
                               "must restore the premises to brand-new condition regardless of age.",
                "severity": "high",
                "legislation_ref": "Retail Leases Act 1994 (NSW) s.16",
            },
            {
                "flag_id": "RF004b",
                "description": "Mandatory repainting obligation with specific colour scheme creates "
                               "an excessive and costly exit obligation.",
                "severity": "medium",
                "legislation_ref": None,
            }
        ],
        plain_english_summary=(
            "When you leave, you must restore the space to exactly how it was on day one — at your cost. "
            "Fair wear and tear is not excluded, which is unusual and expensive. "
            "You must also repaint everything in the landlord's colours."
        ),
        recommended_action=(
            "Negotiate to add 'fair wear and tear excepted' to the make-good clause. "
            "Commission a condition report at lease commencement. "
            "Consider negotiating a cash settlement option in lieu of physical make-good works."
        ),
    ),
    ClauseAnalysis(
        clause_heading="9. ASSIGNMENT AND SUBLETTING",
        clause_text=(
            "9.1 The Tenant must not assign this Lease or sublet the whole or any part of the "
            "Premises without the prior written consent of the Landlord, which may be withheld "
            "in the Landlord's absolute discretion."
        ),
        clause_type="Assignment",
        key_terms=["assignment prohibited", "absolute discretion", "prior written consent"],
        risk_flags=[
            {
                "flag_id": "RF003",
                "description": "Landlord can withhold assignment consent in 'absolute discretion' "
                               "with no obligation to act reasonably or within a timeframe.",
                "severity": "high",
                "legislation_ref": "Retail Leases Act 1994 (NSW) s.41 — landlord cannot unreasonably withhold consent",
            }
        ],
        plain_english_summary=(
            "You cannot sell your business or sublet the space without landlord approval, "
            "and the landlord can say no for any reason — or no reason at all. "
            "This could trap you in the lease if you want to exit."
        ),
        recommended_action=(
            "Replace 'absolute discretion' with 'not to be unreasonably withheld or delayed'. "
            "Add a 28-day deemed approval clause if no response is received. "
            "Under NSW law the landlord already cannot unreasonably withhold consent, "
            "but getting this explicit in the lease avoids disputes."
        ),
    ),
    ClauseAnalysis(
        clause_heading="12. OUTGOINGS",
        clause_text=(
            "12.1 The Tenant must pay as additional rent its proportionate share of all outgoings "
            "including land tax, council rates, water rates, building insurance, cleaning, security, "
            "management fees, and capital works as determined by the Landlord from time to time. "
            "The Tenant's share is 8.5% of the Net Lettable Area of the building."
        ),
        clause_type="Outgoings",
        key_terms=["8.5% NLA share", "land tax", "capital works", "management fees", "all outgoings"],
        risk_flags=[
            {
                "flag_id": "RF005",
                "description": "Tenant is liable for capital works, which should be the landlord's "
                               "responsibility. This includes roof replacement, structural repairs, "
                               "and new plant and equipment.",
                "severity": "high",
                "legislation_ref": "Retail Leases Act 1994 (NSW) s.12 — capital expenditure excluded from outgoings",
            },
            {
                "flag_id": "RF005b",
                "description": "Land tax is included in outgoings. In NSW this must be calculated "
                               "on a single-holding basis, not the landlord's entire portfolio.",
                "severity": "medium",
                "legislation_ref": "Retail Leases Act 1994 (NSW) s.12",
            },
            {
                "flag_id": "RF005c",
                "description": "Management fees included without a cap — market rate is typically "
                               "5–8% of gross income. Uncapped fees can be excessive.",
                "severity": "medium",
                "legislation_ref": None,
            }
        ],
        plain_english_summary=(
            "You are responsible for 8.5% of all building costs including capital works like "
            "roof repairs and new air conditioning — these should legally be the landlord's cost. "
            "Land tax and uncapped management fees are also included, which are common overcharging areas."
        ),
        recommended_action=(
            "Remove capital works from the outgoings definition. "
            "Ensure land tax is calculated on single-holding basis only. "
            "Cap management fees at 7.5% of gross income. "
            "Request an itemised estimate of outgoings before signing."
        ),
    ),
    ClauseAnalysis(
        clause_heading="14. PERSONAL GUARANTEE",
        clause_text=(
            "14.1 Each director of the Tenant must execute an unlimited personal guarantee "
            "in the form attached as Schedule 3. The guarantee shall remain in force for "
            "the full term of the Lease and any option period."
        ),
        clause_type="Personal Guarantee",
        key_terms=["unlimited guarantee", "all directors", "full term", "option period"],
        risk_flags=[
            {
                "flag_id": "RF007",
                "description": "Unlimited personal guarantees from all directors for the full lease "
                               "term expose individuals to potentially millions in personal liability.",
                "severity": "high",
                "legislation_ref": None,
            }
        ],
        plain_english_summary=(
            "Every director must personally guarantee all lease obligations with no cap and no "
            "time limit. If the company can't pay rent, the landlord can pursue each director "
            "personally for the full remaining lease value."
        ),
        recommended_action=(
            "Negotiate to cap personal guarantee at 6–12 months rent equivalent. "
            "Seek a time-limited guarantee (e.g. expires after year 2 if no default). "
            "Consider a bank guarantee or rental bond as an alternative to personal guarantees."
        ),
    ),
    ClauseAnalysis(
        clause_heading="5. TERM AND OPTIONS",
        clause_text=(
            "5.1 The Term of this Lease is three (3) years commencing on 1 July 2024 "
            "and expiring on 30 June 2027. "
            "5.2 The Tenant has one option to renew for a further term of three (3) years, "
            "exercisable by written notice no earlier than 9 months and no later than "
            "6 months before expiry."
        ),
        clause_type="Term & Options",
        key_terms=["3 year term", "1 x 3 year option", "6–9 month notice window", "1 July 2024"],
        risk_flags=[
            {
                "flag_id": "OPT001",
                "description": "Option exercise window is narrow (3 months). Missing it means "
                               "losing the right to renew and potentially vacating.",
                "severity": "low",
                "legislation_ref": None,
            }
        ],
        plain_english_summary=(
            "You have a 3-year lease with one renewal option for another 3 years. "
            "You must exercise the option between 6 and 9 months before expiry — "
            "that's a 3-month window. Missing this deadline means you lose the right to renew."
        ),
        recommended_action=(
            "Set a calendar reminder for 9 months before expiry (October 2026). "
            "Consider negotiating a longer option exercise window (e.g. 3–9 months). "
            "Confirm the option rent review mechanism separately."
        ),
    ),
    ClauseAnalysis(
        clause_heading="16. DEMOLITION",
        clause_text=(
            "16.1 The Landlord may terminate this Lease on 3 months written notice if the "
            "Landlord intends to demolish or substantially redevelop the building. "
            "No compensation shall be payable to the Tenant in such circumstances."
        ),
        clause_type="Demolition / Redevelopment",
        key_terms=["3 months notice", "demolition", "redevelopment", "no compensation"],
        risk_flags=[
            {
                "flag_id": "RF002",
                "description": "Landlord can terminate with only 3 months notice for demolition "
                               "with zero compensation. Tenant loses fitout investment and business continuity.",
                "severity": "high",
                "legislation_ref": None,
            }
        ],
        plain_english_summary=(
            "The landlord can kick you out with just 3 months notice if they want to redevelop, "
            "and owes you nothing. If you've spent $200k on fitout, you lose it all."
        ),
        recommended_action=(
            "Negotiate minimum 6-month notice period. "
            "Require compensation covering: unamortised fitout costs, relocation costs, "
            "and lost profit for the remaining lease term. "
            "At minimum, ensure the landlord must repay your fitout contribution."
        ),
    ),
    ClauseAnalysis(
        clause_heading="2. DEFINITIONS",
        clause_text=(
            "In this Lease: 'Commencement Date' means 1 July 2024. 'Premises' means "
            "Suite 4, Level 2, 123 George Street, Sydney NSW 2000 comprising approximately "
            "245 square metres. 'Permitted Use' means café and food retail only. "
            "'Landlord' means George Street Holdings Pty Ltd ACN 123 456 789."
        ),
        clause_type="Definitions",
        key_terms=["245 sqm", "Suite 4 Level 2", "café and food retail", "George Street Holdings"],
        risk_flags=[],
        plain_english_summary=(
            "Standard definitions clause. The premises are 245 sqm on Level 2 at 123 George Street, "
            "Sydney. The permitted use is limited to café and food retail only — you cannot change "
            "your business type without a lease variation."
        ),
        recommended_action=(
            "Confirm the 245 sqm figure against the building's NLA certificate. "
            "If you plan to expand your offering (e.g. add retail), negotiate a broader permitted use now."
        ),
    ),
]


def run_dev_audit(
    pdf_path: str,
    jurisdiction: str,
    tenant_name: str = None,
    job_id: str = None,          # accepted but ignored in mock mode
    document_hash: str = None,   # accepted but ignored in mock mode
    progress_callback: ProgressCallback = None,
) -> AuditResult:
    """
    DEV MODE: Returns deterministic audit result for testing.
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

    import os
    from pathlib import Path
    filename = Path(pdf_path).name if pdf_path else "sample_lease.pdf"
    import os
    from pathlib import Path
    filename = Path(pdf_path).name if pdf_path else "sample_lease.pdf"

    return AuditResult(
        tenant_name=tenant_name or "Dev Tenant Pty Ltd",
        jurisdiction=jurisdiction or "NSW",
        filename=filename,
        total_clauses=len(MOCK_CLAUSES),
        risk_score=risk_score,
        clause_analyses=MOCK_CLAUSES,
        all_risk_flags=all_flags,
    )


# Backward-compatibility alias — use run_dev_audit in new code
run_mock_audit = run_dev_audit
