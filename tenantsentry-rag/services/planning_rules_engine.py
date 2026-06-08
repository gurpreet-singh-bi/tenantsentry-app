"""
planning_rules_engine.py
-------------------------
AQ3: Structured planning law rules engine.

Fires jurisdiction-specific statutory requirements that cannot be detected
from a single clause in isolation — they require multi-clause synthesis
(e.g. lease type + term length + land description).

The core rule pattern across all Australian jurisdictions:

    "Lease of a PORTION of land (not the whole lot, not a tenancy within a
    building) for a total cumulative term exceeding the jurisdiction threshold
    is deemed a subdivision and requires planning/development approval."

    Threshold by jurisdiction:
        NSW  > 5 yr   Conveyancing Act 1919 (NSW) s.23E + EP&A Act 1979
        SA   > 6 yr   Planning, Development and Infrastructure Act 2016 (SA)
        VIC  > 10 yr  Subdivision Act 1988 (Vic)
        QLD  > 10 yr  Planning Act 2016 (Qld) + Land Title Act 1994 (Qld)
        ACT  > 10 yr  Planning Act 2023 (ACT)
        TAS  > 10 yr  Local Government (Building & Misc. Provisions) Act 1993 (Tas)
        NT   > 12 yr  Planning Act 1999 (NT) s.5
        WA   > 20 yr  Planning and Development Act 2005 (WA) s.136 — lease VOID AB INITIO

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


# ── Shared helpers ─────────────────────────────────────────────────────────────

# Patterns that signal the lease is for a PORTION of land (not a whole lot
# and not a self-contained tenancy within a building).
# Shared across all jurisdiction rules — each state rule calls _is_portion().
_PORTION_PATTERNS = [
    r"portion\s+of\s+(Lot|land|the\s+Land|Crown\s+Land)",
    r"part\s+of\s+Lot\s+\d+",
    r"part\s+of\s+the\s+Land",
    r"designated\s+(area|portion)",
    r"area\s+of\s+approximately",
    r"Item\s*2[^\d]",                               # Schedule 1, Item 2 often describes a portion
    r"exclud(ing|es)\s+.{0,40}(area|portion|land)",
    r"(leased\s+)?premises?\s+being\s+part",
    r"part\s+of\s+(the\s+)?land\s+(described|comprising|known)",
    r"portion\s+of\s+(the\s+)?land\s+(described|comprising|known)",
    r"open\s+(area|land|space)\s+(forming\s+)?part",     # outdoor areas attached to tenancy
    r"car\s+park(ing)?\s+area",                          # separate carparking land often triggers
]

# Patterns that NEGATE a deemed-subdivision finding — indicates a self-contained
# building tenancy (Shop X, Suite X, Level X, Unit X) which is universally exempt
# from deemed-subdivision rules across all states.
_BUILDING_TENANCY_PATTERNS = [
    r"\bShop\s+\d+",
    r"\bSuite\s+\d+",
    r"\bUnit\s+\d+",
    r"\bLevel\s+\d+",
    r"\bFloor\s+\d+",
    r"\bLot\s+\d+\s+on\s+(Strata|Community)\s+Plan",   # strata — already a lot
    r"whole\s+of\s+(the\s+)?(building|premises|floor)",
]


def _is_portion_of_land(meta: dict, text: str) -> bool:
    """
    Return True if the lease appears to be for a portion of a land lot
    (as opposed to a whole lot or a tenancy within a building).
    """
    # If metadata has already flagged this, trust it
    if meta.get("is_portion_of_land"):
        return True

    # If it looks like a self-contained building tenancy, exempt it
    if any(re.search(p, text, re.IGNORECASE) for p in _BUILDING_TENANCY_PATTERNS):
        return False

    return any(re.search(p, text, re.IGNORECASE) for p in _PORTION_PATTERNS)


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


def _make_portion_condition(threshold_years: int):
    """
    Factory: returns a condition function for the standard "portion of land +
    total term > threshold" pattern shared by all jurisdiction rules.

    threshold_years: the year limit above which deemed subdivision is triggered.
    """
    def _condition(meta: dict, text: str) -> tuple[bool, list[str]]:
        reasons: list[str] = []

        # ── Check 1: Portion of land ─────────────────────────────────────────
        if not _is_portion_of_land(meta, text):
            return False, []
        reasons.append("lease appears to be for a portion of a land lot (not a whole lot or building tenancy)")

        # ── Check 2: Total term > threshold ──────────────────────────────────
        total_years = _estimate_total_term_years(meta, text)

        if total_years is None:
            reasons.append(
                f"total lease term (including options) could not be determined — "
                f"if it exceeds {threshold_years} years, planning/development approval is mandatory"
            )
            return True, reasons

        if total_years <= threshold_years:
            return False, []

        reasons.append(
            f"total cumulative term is approximately {total_years} years "
            f"(initial term + option periods), which exceeds the {threshold_years}-year threshold"
        )
        return True, reasons

    return _condition


# ── WA: custom condition (void ab initio — more severe, bespoke logic kept) ───

def _wa_pda_s136_condition(meta: dict, text: str) -> tuple[bool, list[str]]:
    """
    WA PDA 2005 s.136: Lease of a PORTION of land for >20 years total term
    (including options) requires WAPC approval or the lease is VOID AB INITIO.
    """
    reasons: list[str] = []

    # WA-specific portion detection (keeps original patterns plus shared ones)
    wa_extra = [r"Item\s*2[^\d]"]  # already in _PORTION_PATTERNS, kept for explicitness
    is_portion = _is_portion_of_land(meta, text)

    if not is_portion:
        return False, []
    reasons.append("lease appears to be for a portion of a lot (not the entire lot)")

    total_years = _estimate_total_term_years(meta, text)

    if total_years is None:
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


# ── Rule registry ─────────────────────────────────────────────────────────────

_PLANNING_RULES: list[dict] = [

    # ── WA ────────────────────────────────────────────────────────────────────
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

    # ── NSW ───────────────────────────────────────────────────────────────────
    {
        "id": "PR-NSW-001",
        "jurisdiction": "NSW",
        "severity": "high",
        "title": "Development Consent Required — Deemed Subdivision (Conveyancing Act / EP&A Act)",
        "description": (
            "In New South Wales, a lease of PART of a land lot (as distinct from a "
            "self-contained tenancy within a building) with a total term exceeding "
            "5 years (including all option/renewal periods) is deemed a subdivision "
            "under the Conveyancing Act 1919 (NSW). "
            "Carrying out such a subdivision without development consent is a breach "
            "of the Environmental Planning and Assessment Act 1979 (NSW), which can "
            "result in prosecution, fines, and the lease being declared void. "
            "Note: This does NOT apply to retail/commercial tenancies within a building "
            "(e.g. Shop 1, Suite 2) that have a unique street address."
        ),
        "legislation": "Conveyancing Act 1919 (NSW) s.23E; Environmental Planning and Assessment Act 1979 (NSW)",
        "action": (
            "1. Confirm whether the premises are a defined tenancy within a building "
            "   (unique description/street address) — if so, this rule does not apply. "
            "2. If leasing a portion of land (outdoor area, open site, partial lot), "
            "   verify whether development consent for subdivision has been obtained. "
            "3. If consent is absent, negotiate a condition precedent or sunset clause "
            "   in the lease, and require the landlord to obtain consent before settlement. "
            "4. Obtain independent legal advice from a NSW property lawyer."
        ),
        "condition": _make_portion_condition(threshold_years=5),
    },

    # ── SA ────────────────────────────────────────────────────────────────────
    {
        "id": "PR-SA-001",
        "jurisdiction": "SA",
        "severity": "high",
        "title": "Development Approval Required — Long-Term Partial Lease (PDI Act 2016)",
        "description": (
            "Under the Planning, Development and Infrastructure Act 2016 (SA), "
            "a lease conferring the right to occupy PART ONLY of an allotment "
            "for a total term exceeding 6 years (including renewal/extension options) "
            "is defined as a 'development' and requires development approval. "
            "Proceeding without approval exposes both landlord and tenant to "
            "enforcement action, fines, and potential lease voidability."
        ),
        "legislation": "Planning, Development and Infrastructure Act 2016 (SA) s.3 (definition of 'development')",
        "action": (
            "1. Confirm whether the premises are a self-contained building tenancy "
            "   — if so, this rule may not apply. "
            "2. If leasing part of a land allotment (open yard, partial site, etc.), "
            "   verify whether development approval has been granted. "
            "3. If absent, require the landlord to obtain approval as a condition of "
            "   the lease becoming binding, and negotiate a sunset clause. "
            "4. Obtain independent legal advice from a SA property lawyer."
        ),
        "condition": _make_portion_condition(threshold_years=6),
    },

    # ── VIC ───────────────────────────────────────────────────────────────────
    {
        "id": "PR-VIC-001",
        "jurisdiction": "VIC",
        "severity": "high",
        "title": "Subdivision Permit Required — Long-Term Partial Lease (Subdivision Act 1988)",
        "description": (
            "Under the Subdivision Act 1988 (Vic), a lease of PART of a land lot "
            "(including outdoor areas, carparking, open space, or access ways forming "
            "part of the leased premises) with a total term exceeding 10 years "
            "(including all option/renewal periods) is deemed a subdivision requiring "
            "a council subdivision permit. "
            "Consecutive short-term leases designed to circumvent this threshold may "
            "be treated as a single lease under current case law. "
            "Note: A lease wholly contained within a building (no external land component) "
            "is generally exempt."
        ),
        "legislation": "Subdivision Act 1988 (Vic) s.22; Planning and Environment Act 1987 (Vic)",
        "action": (
            "1. Check whether the leased area includes any land component outside the "
            "   building envelope (outdoor dining, storage, carparking, access ways). "
            "2. If so, verify whether a council subdivision permit has been obtained. "
            "3. If not obtained, require the landlord to obtain the permit before the "
            "   lease is binding, and include a sunset clause (90 days). "
            "4. Obtain independent legal advice from a Victorian property lawyer."
        ),
        "condition": _make_portion_condition(threshold_years=10),
    },

    # ── QLD ───────────────────────────────────────────────────────────────────
    {
        "id": "PR-QLD-001",
        "jurisdiction": "QLD",
        "severity": "high",
        "title": "Development Approval Required — Deemed Reconfiguration of Lot (Planning Act 2016)",
        "description": (
            "Under the Planning Act 2016 (Qld) and Land Title Act 1994 (Qld), "
            "a lease of PART of a lot (other than a lease of part of a building) "
            "for a total term exceeding 10 years (including all renewal options) "
            "constitutes a 'reconfiguration of a lot' (subdivision) requiring "
            "development approval from the relevant local government. "
            "The lease cannot be registered at Titles Queensland without this approval, "
            "and proceeding without it risks the lease being unregisterable and unenforceable "
            "against third parties."
        ),
        "legislation": "Planning Act 2016 (Qld) sch.2 (definition of 'reconfiguring a lot'); Land Title Act 1994 (Qld)",
        "action": (
            "1. Confirm whether the lease is for a defined part of a building — "
            "   if entirely within a building, this rule does not apply. "
            "2. If leasing a portion of land, verify whether development approval "
            "   (reconfiguration of a lot) has been obtained. "
            "3. Require the landlord to provide evidence of approval before execution. "
            "4. Include a condition precedent — the lease does not become binding until "
            "   approval is granted. "
            "5. Obtain independent legal advice from a Queensland property lawyer."
        ),
        "condition": _make_portion_condition(threshold_years=10),
    },

    # ── ACT ───────────────────────────────────────────────────────────────────
    {
        "id": "PR-ACT-001",
        "jurisdiction": "ACT",
        "severity": "medium",
        "title": "Development Approval Required — Long-Term Partial Lease (Planning Act 2023)",
        "description": (
            "Under the Planning Act 2023 (ACT), a lease of part of land for a total "
            "term exceeding 10 years (including renewal options) may require development "
            "approval as a subdivision. The ACT's leasehold land system (all ACT land "
            "is held under Crown lease) adds an additional layer — any variation to the "
            "Crown lease permitting the use may also be required. "
            "Tenants should verify both the planning approval status and the Crown lease "
            "conditions before proceeding."
        ),
        "legislation": "Planning Act 2023 (ACT); Land (Planning and Environment) Act 1991 (ACT)",
        "action": (
            "1. Confirm whether the leased area is a self-contained tenancy within a building. "
            "2. If leasing a portion of land (outdoor area, partial site), verify whether "
            "   development approval for subdivision has been obtained. "
            "3. Check that the Crown lease over the land permits the proposed use. "
            "4. If approvals are absent, negotiate a condition precedent in the lease. "
            "5. Obtain independent legal advice from an ACT property lawyer."
        ),
        "condition": _make_portion_condition(threshold_years=10),
    },

    # ── TAS ───────────────────────────────────────────────────────────────────
    {
        "id": "PR-TAS-001",
        "jurisdiction": "TAS",
        "severity": "medium",
        "title": "Subdivision Approval Required — Long-Term Partial Lease (LGBMPA 1993)",
        "description": (
            "Under the Local Government (Building and Miscellaneous Provisions) Act 1993 (Tas), "
            "a lease of land (or part of land) with a total term exceeding 10 years "
            "(including renewal options) is deemed a subdivision of the land. "
            "This requires subdivision approval under the Land Use Planning and "
            "Approvals Act 1993 (Tas) from the relevant council. "
            "Note: proposed 2026 amendments may create exemptions for utility/energy "
            "infrastructure leases, but commercial leases remain subject to this rule."
        ),
        "legislation": "Local Government (Building and Miscellaneous Provisions) Act 1993 (Tas) s.3; Land Use Planning and Approvals Act 1993 (Tas)",
        "action": (
            "1. Verify whether a subdivision approval has been obtained from the "
            "   relevant Tasmanian council. "
            "2. If not, require the landlord to obtain approval as a condition of the "
            "   lease being binding. "
            "3. Include a sunset clause in the lease allowing termination if approval "
            "   is refused within a reasonable period. "
            "4. Obtain independent legal advice from a Tasmanian property lawyer."
        ),
        "condition": _make_portion_condition(threshold_years=10),
    },

    # ── NT ────────────────────────────────────────────────────────────────────
    {
        "id": "PR-NT-001",
        "jurisdiction": "NT",
        "severity": "medium",
        "title": "Development Approval Required — Long-Term Partial Lease (Planning Act 1999 s.5)",
        "description": (
            "Under Section 5 of the Planning Act 1999 (NT), a lease or other right to "
            "use or occupy PART of land for a term exceeding 12 years is taken to "
            "constitute a subdivision of the land, requiring development approval from "
            "the NT Planning Commission. "
            "Leases of the whole of a lot are exempt. "
            "Proceeding without approval may render the lease unregisterable and "
            "unenforceable against third parties."
        ),
        "legislation": "Planning Act 1999 (NT) s.5",
        "action": (
            "1. Confirm whether the lease is for part of a land lot (as opposed to "
            "   the whole lot or a defined tenancy within a building). "
            "2. If leasing a portion of land, verify whether development approval has "
            "   been obtained from the NT Planning Commission. "
            "3. Require the landlord to provide evidence of approval, or include a "
            "   condition precedent before the lease binds. "
            "4. Obtain independent legal advice from an NT property lawyer."
        ),
        "condition": _make_portion_condition(threshold_years=12),
    },

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
