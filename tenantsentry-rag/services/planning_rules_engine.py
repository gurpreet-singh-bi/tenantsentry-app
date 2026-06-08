"""
planning_rules_engine.py
-------------------------
AQ3: Structured planning law rules engine.

Fires jurisdiction-specific statutory requirements that cannot be detected
from a single clause in isolation — they require multi-clause synthesis
(e.g. lease type + term length + land description).

The key WA rule this was built for:

    Planning and Development Act 2005 (WA) s.136 — Deemed Subdivision
    ────────────────────────────────────────────────────────────────────
    A lease of a PORTION of a lot (not the whole lot) for a cumulative
    term exceeding 20 years (including all option periods) is deemed a
    subdivision and requires WAPC approval to be legally valid.
    Without approval, the lease is VOID AB INITIO — illegal from inception.

Public API
----------
    evaluate_planning_rules(
        jurisdiction: str,
        lease_metadata: dict,
        lease_text: str,
    ) -> list[dict]

    Each returned dict is a planning finding:
        rule_id:        str   e.g. "PR-WA-001"
        severity:       str   "critical" | "high" | "medium"
        title:          str
        description:    str   plain-English explanation
        legislation:    str   Act + section
        action:         str   what the tenant must do
        triggered_by:   list  which facts triggered this rule
"""

import re
from loguru import logger


# ── Rule definitions ──────────────────────────────────────────────────────────

# Each rule is a dict with:
#   id, jurisdiction, severity, title, description, legislation, action
#   condition: callable(lease_metadata, lease_text) -> (bool, list[str])
#     Returns (triggered: bool, triggered_by: list of human-readable reasons)

def _wa_pda_s136_condition(meta: dict, text: str) -> tuple[bool, list[str]]:
    """
    WA PDA 2005 s.136: Lease of a PORTION of land for >20 years total term
    (including options) requires WAPC approval or the lease is void.

    Triggers when ALL of:
      1. Jurisdiction is WA
      2. Evidence lease is for a portion of a lot (not the whole lot)
      3. Total cumulative term (initial + options) exceeds 20 years
    """
    reasons: list[str] = []

    # ── Condition 2: Portion of land ─────────────────────────────────────────
    # Signals: "portion of", "part of Lot", "Schedule 1, Item 2" (common in WA
    # municipal leases), "area of approximately", "designated area"
    portion_patterns = [
        r"portion\s+of\s+(Lot|land|the\s+Land|Crown\s+Land)",
        r"part\s+of\s+Lot\s+\d+",
        r"part\s+of\s+the\s+Land",
        r"designated\s+(area|portion)",
        r"area\s+of\s+approximately",
        r"Item\s*2[^\d]",        # Schedule 1, Item 2 often describes a portion
        r"exclud(ing|es)\s+.{0,40}(area|portion|land)",
        r"(leased\s+)?premises?\s+being\s+part",
    ]
    is_portion = any(re.search(p, text, re.IGNORECASE) for p in portion_patterns)

    # Also check metadata — lease_metadata_extractor may have flagged this
    if meta.get("is_portion_of_land"):
        is_portion = True

    if not is_portion:
        return False, []
    reasons.append("lease appears to be for a portion of a lot (not the entire lot)")

    # ── Condition 3: Total term > 20 years ───────────────────────────────────
    # Extract initial term years + option periods from metadata or text
    total_years = _estimate_total_term_years(meta, text)

    if total_years is None:
        # Can't determine term — err on the side of flagging (better safe)
        reasons.append(
            "total lease term (including options) could not be determined — "
            "if it exceeds 20 years, WAPC approval is mandatory"
        )
        return True, reasons

    if total_years <= 20:
        return False, []

    reasons.append(
        f"total cumulative term is approximately {total_years} years "
        f"(initial term + option periods), which exceeds the 20-year threshold"
    )
    return True, reasons


def _estimate_total_term_years(meta: dict, text: str) -> int | None:
    """
    Estimate the total lease term including all option periods.
    Returns None if it cannot be determined.
    """
    # Try metadata first (populated by lease_metadata_extractor)
    if meta.get("total_term_years"):
        return int(meta["total_term_years"])

    initial = meta.get("lease_term_years")
    options_total = meta.get("options_total_years")

    if initial is not None and options_total is not None:
        return int(initial) + int(options_total)

    # Fall back to text parsing
    # Pattern: "initial term of X years" + "further term[s] of Y years" / "N options of Y years"
    initial_match = re.search(
        r"(?:initial\s+term|term\s+of\s+(?:the\s+)?lease)[^0-9]{0,20}(\d+)\s*years?",
        text, re.IGNORECASE,
    )
    # Multiple option patterns: "five (5) further terms of ten (10) years each"
    option_matches = re.findall(
        r"(\d+)\s+(?:further\s+)?(?:option|term)[s]?\s+of\s+(\d+)\s+years?",
        text, re.IGNORECASE,
    )
    # Single option: "a further term of 10 years"
    single_option_matches = re.findall(
        r"(?:further\s+term|option\s+period)[^0-9]{0,20}(\d+)\s*years?",
        text, re.IGNORECASE,
    )

    if initial_match:
        initial_years = int(initial_match.group(1))
    else:
        # Try plain "XX year term"
        plain = re.search(r"(\d+)[\s-]year\s+(?:initial\s+)?term", text, re.IGNORECASE)
        initial_years = int(plain.group(1)) if plain else None

    options_years = 0
    for count_str, length_str in option_matches:
        options_years += int(count_str) * int(length_str)
    for length_str in single_option_matches:
        options_years += int(length_str)

    if initial_years is None and options_years == 0:
        return None

    return (initial_years or 0) + options_years


# ── Rule registry ─────────────────────────────────────────────────────────────

_PLANNING_RULES: list[dict] = [
    {
        "id": "PR-WA-001",
        "jurisdiction": "WA",
        "severity": "critical",
        "title": "WAPC Approval Required — Deemed Subdivision (PDA 2005 s.136)",
        "description": (
            "Under Section 136 of the Planning and Development Act 2005 (WA), "
            "a lease of a PORTION of a lot for a cumulative term exceeding 20 years "
            "(including all option periods) is deemed a subdivision and requires "
            "approval from the Western Australian Planning Commission (WAPC). "
            "Without this approval, the lease is ILLEGAL AND VOID AB INITIO — "
            "meaning it has no legal force from the moment it was signed. "
            "The tenant would have no legal right to occupy the premises and "
            "any fitout or development expenditure would be at total risk."
        ),
        "legislation": "Planning and Development Act 2005 (WA) s.136",
        "action": (
            "1. Verify whether WAPC approval has been obtained or applied for. "
            "2. If not obtained, do NOT commence occupation or fitout until approval is granted. "
            "3. Negotiate a sunset clause (90-120 days) allowing termination with full refund "
            "   of all pre-paid monies if WAPC approval is refused. "
            "4. Ensure the landlord (not the tenant) bears primary responsibility for obtaining "
            "   approval, given the landlord controls the land. "
            "5. Obtain independent legal advice from a WA property lawyer immediately."
        ),
        "condition": _wa_pda_s136_condition,
    },
    # Future rules can be added here following the same pattern:
    # {
    #     "id": "PR-NSW-001",
    #     "jurisdiction": "NSW",
    #     "severity": "high",
    #     "title": "...",
    #     ...
    # }
]


# ── Public API ────────────────────────────────────────────────────────────────

def evaluate_planning_rules(
    jurisdiction: str,
    lease_metadata: dict,
    lease_text: str,
) -> list[dict]:
    """
    Run all planning rules applicable to the given jurisdiction against
    the lease metadata and full text.

    Returns a list of triggered finding dicts, each containing:
        rule_id, severity, title, description, legislation, action, triggered_by
    """
    jur = jurisdiction.upper().strip()
    findings: list[dict] = []

    applicable_rules = [r for r in _PLANNING_RULES if r["jurisdiction"] == jur]
    if not applicable_rules:
        logger.debug(f"[AQ3] No planning rules configured for {jur}")
        return findings

    for rule in applicable_rules:
        try:
            triggered, triggered_by = rule["condition"](lease_metadata, lease_text)
        except Exception as e:
            logger.warning(f"[AQ3] Planning rule {rule['id']} evaluation error (non-fatal): {e}")
            continue

        if triggered:
            finding = {
                "rule_id":      rule["id"],
                "severity":     rule["severity"],
                "title":        rule["title"],
                "description":  rule["description"],
                "legislation":  rule["legislation"],
                "action":       rule["action"],
                "triggered_by": triggered_by,
            }
            findings.append(finding)
            logger.warning(
                f"[AQ3] Planning rule TRIGGERED: {rule['id']} — {rule['title']} "
                f"| triggered_by={triggered_by}"
            )
        else:
            logger.debug(f"[AQ3] Planning rule not triggered: {rule['id']}")

    return findings


def format_planning_finding_as_warning(finding: dict) -> str:
    """
    Format a planning finding as a pipeline_warnings string for inclusion
    in the AuditResult. Uses a clear prefix so the UI can style it distinctly.
    """
    triggered = "; ".join(finding.get("triggered_by", []))
    return (
        f"[PLANNING LAW — {finding['severity'].upper()}] {finding['title']} | "
        f"Legislation: {finding['legislation']} | "
        f"Triggered by: {triggered} | "
        f"Action required: {finding['action']}"
    )
