"""
mock/analyser.py
----------------
Simulates LLM analysis using keyword matching against red_flags.yaml.
No API keys required. Real PDF parsing + real chunking + rule-based mock output.

This is the prototype stand-in for llm/router.py.
Replace with real LLM calls when ready for production.
"""

import yaml
import re
from pathlib import Path
from dataclasses import dataclass, field

# Load red flag rules once at import time
RULES_PATH = Path(__file__).parent.parent / "rules" / "red_flags.yaml"
with open(RULES_PATH) as f:
    RED_FLAG_RULES = yaml.safe_load(f)["rules"]

# Map clause keywords → clause type label
CLAUSE_TYPE_MAP = [
    (["definitions", "interpretation", "meaning"], "Definitions & Interpretation"),
    (["rent review", "cpi", "fixed increase", "market review"], "Rent Review"),
    (["make good", "make-good", "reinstate", "original condition"], "Make Good"),
    (["assignment", "subletting", "sublease", "transfer"], "Assignment & Subletting"),
    (["option", "option to renew", "further term"], "Option to Renew"),
    (["demolition", "redevelopment", "vacant possession"], "Demolition / Redevelopment"),
    (["outgoings", "land tax", "rates", "insurance"], "Outgoings"),
    (["fitout", "fit-out", "works", "alterations"], "Fitout & Alterations"),
    (["guarantee", "guarantor", "personal guarantee"], "Personal Guarantee"),
    (["termination", "default", "breach"], "Termination & Default"),
    (["holdover", "overholding", "holding over"], "Holdover / Overholding"),
    (["exclusivity", "exclusive", "competing business"], "Exclusivity"),
    (["rent", "annual rent", "base rent", "monthly"], "Rent & Payment"),
    (["term", "commencement", "expiry", "lease period"], "Lease Term"),
    (["premises", "property", "floor area", "lettable"], "Premises Description"),
    (["dispute", "mediation", "arbitration"], "Dispute Resolution"),
    (["insurance", "public liability", "indemnity"], "Insurance & Indemnity"),
    (["access", "hours", "trading hours"], "Access & Trading Hours"),
    (["parking", "car park", "vehicle"], "Parking"),
    (["signage", "sign", "advertising"], "Signage"),
]

# Plain-English summary templates by clause type
SUMMARY_TEMPLATES = {
    "Rent Review": (
        "This clause sets out how your rent will increase over time. "
        "Carefully check the review method — fixed % increases can significantly exceed market rates. "
        "Watch for ratchet clauses that prevent your rent from going down even if market rents fall."
    ),
    "Make Good": (
        "This clause describes what condition you must return the premises to at the end of your lease. "
        "Obligations can be very costly — get a condition report at the start and negotiate to limit "
        "your liability to fair wear and tear."
    ),
    "Assignment & Subletting": (
        "This clause covers your right to transfer the lease or sublet to another party. "
        "Important if you sell your business or need to exit early. "
        "Ensure landlord consent cannot be unreasonably withheld."
    ),
    "Option to Renew": (
        "This gives you the right to extend your lease for a further term. "
        "Critical for business continuity — check the notice period carefully, missing it forfeits your right."
    ),
    "Demolition / Redevelopment": (
        "This allows the landlord to terminate your lease if they want to demolish or redevelop. "
        "Without proper protections this could force you to vacate with little notice or compensation."
    ),
    "Outgoings": (
        "This clause specifies which building running costs you must contribute to. "
        "Ensure capital expenditure items (roof, structure) are excluded from your liability."
    ),
    "Personal Guarantee": (
        "This requires directors or individuals to personally guarantee the tenant's lease obligations. "
        "This puts your personal assets at risk — negotiate a cap and time limit."
    ),
    "Termination & Default": (
        "This sets out when and how the landlord can end your lease early. "
        "Ensure you have adequate cure periods before termination can occur."
    ),
    "Rent & Payment": (
        "This sets out the rent amount, payment schedule, and any GST obligations. "
        "Verify the figures match what was negotiated and check for any additional charges."
    ),
    "Lease Term": (
        "This defines how long your lease runs, when it starts, and when it expires. "
        "Make sure these dates are correct and align with your business plan."
    ),
    "Holdover / Overholding": (
        "This governs what happens if you stay in the premises after your lease expires. "
        "Some leases charge a penalty rate (150–200% of rent) during holdover — check this carefully."
    ),
}

DEFAULT_SUMMARY = (
    "This clause contains important legal obligations. Review it carefully with your lawyer "
    "and ensure you understand all conditions before signing."
)

RECOMMENDED_ACTIONS = {
    "Rent Review": "Negotiate to cap increases at CPI or market rent (whichever is lower). Remove any ratchet clause.",
    "Make Good": "Commission a condition report at lease start. Limit make-good to fair wear and tear.",
    "Assignment & Subletting": "Add 'not to be unreasonably withheld' and a 28-day deemed approval period.",
    "Option to Renew": "Diarise the option notice deadline. Consider a solicitor reminder service.",
    "Demolition / Redevelopment": "Negotiate minimum 6 months' notice and compensation for fitout + relocation costs.",
    "Outgoings": "Request 3 years of outgoings history. Cap liability to operational costs only.",
    "Personal Guarantee": "Negotiate a cap of 6–12 months rent equivalent with a sunset date.",
    "Termination & Default": "Ensure minimum 14-day cure period for monetary defaults, 30 days for non-monetary.",
    "Holdover / Overholding": "Negotiate holdover at contract rent on a month-to-month basis.",
}

DEFAULT_ACTION = "Have your solicitor review this clause before signing."


@dataclass
class MockClauseAnalysis:
    clause_heading: str
    clause_text: str
    clause_type: str
    key_terms: list[str] = field(default_factory=list)
    risk_flags: list[dict] = field(default_factory=list)
    plain_english_summary: str = ""
    recommended_action: str = ""


def analyse_clause_mock(clause_heading: str, clause_text: str, jurisdiction: str) -> MockClauseAnalysis:
    """
    Keyword-based mock analysis of a single lease clause.
    Matches red flag rules and returns structured output identical
    to what the real LLM router would return.
    """
    combined = (clause_heading + " " + clause_text).lower()

    # Determine clause type
    clause_type = _detect_clause_type(combined)

    # Extract key terms (simple pattern matching)
    key_terms = _extract_key_terms(clause_text)

    # Match red flag rules
    risk_flags = _match_risk_flags(combined, jurisdiction)

    # Generate summary and action
    summary = SUMMARY_TEMPLATES.get(clause_type, DEFAULT_SUMMARY)
    action = RECOMMENDED_ACTIONS.get(clause_type, DEFAULT_ACTION)

    return MockClauseAnalysis(
        clause_heading=clause_heading,
        clause_text=clause_text,
        clause_type=clause_type,
        key_terms=key_terms,
        risk_flags=risk_flags,
        plain_english_summary=summary,
        recommended_action=action,
    )


def _detect_clause_type(text_lower: str) -> str:
    for keywords, label in CLAUSE_TYPE_MAP:
        if any(kw in text_lower for kw in keywords):
            return label
    return "General Clause"


def _extract_key_terms(text: str) -> list[str]:
    """Extract dollar amounts, percentages, dates, and key phrases."""
    terms = []

    # Dollar amounts
    for m in re.findall(r"\$[\d,]+(?:\.\d{2})?(?:\s*(?:per annum|per month|p\.a\.))?", text, re.IGNORECASE):
        terms.append(m.strip())

    # Percentages
    for m in re.findall(r"\d+(?:\.\d+)?%(?:\s*per annum)?", text, re.IGNORECASE):
        terms.append(m.strip())

    # Dates (e.g. 1 July 2024, 01/07/2024)
    for m in re.findall(r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}", text, re.IGNORECASE):
        terms.append(m.strip())

    # Year spans
    for m in re.findall(r"\d+\s+(?:year|years|month|months)", text, re.IGNORECASE):
        terms.append(m.strip())

    # CPI mentions
    if "cpi" in text.lower():
        terms.append("CPI-linked")

    return list(dict.fromkeys(terms))[:8]   # deduplicate, cap at 8


def _match_risk_flags(text_lower: str, jurisdiction: str) -> list[dict]:
    matched = []
    for rule in RED_FLAG_RULES:
        keywords = rule.get("trigger_keywords", [])
        if any(kw.lower() in text_lower for kw in keywords):
            matched.append({
                "flag_id": rule["id"],
                "description": rule["description"].strip(),
                "severity": rule["severity"],
                "legislation_ref": rule.get("legislation_ref"),
                "recommended_action": rule.get("recommended_action", "").strip(),
            })
    return matched


def compute_risk_score(all_flags: list[dict]) -> int:
    """Compute an overall risk score 0–100."""
    high = sum(1 for f in all_flags if f["severity"] == "high")
    medium = sum(1 for f in all_flags if f["severity"] == "medium")
    low = sum(1 for f in all_flags if f["severity"] == "low")
    score = min(100, high * 20 + medium * 8 + low * 3)
    return score
