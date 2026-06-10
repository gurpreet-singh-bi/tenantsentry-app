"""
planning_rules_engine.py
-------------------------
AQ3 / AG3: Structured planning law rules engine.

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

    WA additional rules:
        PR-WA-002: TLA 1893 s.92(b) — quiet enjoyment excluded/limited + caveat recommendation

    Cross-jurisdictional rules (jurisdiction="ALL"):
        PR-LG-001: Local Government landlord — no-fettering doctrine + dual-role conflict

Public API
----------
    evaluate_planning_rules(
        jurisdiction: str,
        lease_metadata: dict,
        lease_text: str,
    ) -> list[dict]

    Each returned dict is a planning finding:
        rule_id:        str   e.g. "PR-WA-001"
        severity:       str   "void" | "high" | "medium"  # AQ-NEW-23 adds void tier
        title:          str
        description:    str   plain-English explanation
        legislation:    str   Act + section
        action:         str   what the tenant must do
        triggered_by:   list  which facts triggered this rule
"""

import re
from loguru import logger


# ── Shared helpers ─────────────────────────────────────────────────────────────

_PORTION_PATTERNS = [
    r"portion\s+of\s+(Lot|land|the\s+Land|Crown\s+Land)",
    r"part\s+of\s+Lot\s+\d+",
    r"part\s+of\s+the\s+Land",
    r"designated\s+(area|portion)",
    r"area\s+of\s+approximately",
    r"Item\s*2[^\d]",
    r"exclud(ing|es)\s+.{0,40}(area|portion|land)",
    r"(leased\s+)?premises?\s+being\s+part",
    r"part\s+of\s+(the\s+)?land\s+(described|comprising|known)",
    r"portion\s+of\s+(the\s+)?land\s+(described|comprising|known)",
    r"open\s+(area|land|space)\s+(forming\s+)?part",
    r"car\s+park(ing)?\s+area",
]

_BUILDING_TENANCY_PATTERNS = [
    r"\bShop\s+\d+",
    r"\bSuite\s+\d+",
    r"\bUnit\s+\d+",
    r"\bLevel\s+\d+",
    r"\bFloor\s+\d+",
    r"\bLot\s+\d+\s+on\s+(Strata|Community)\s+Plan",
    r"whole\s+of\s+(the\s+)?(building|premises|floor)",
]

# AG3: Local government entity name patterns (all Australian states/territories)
_LOCAL_GOVT_PATTERNS = [
    r"\b(Town|City|Shire|District|Regional|Rural|Municipal|Borough|County)\s+of\s+[A-Z][a-zA-Z\s]{2,40}",
    r"\b[A-Z][a-zA-Z\s]{2,30}(City|Town|Shire|District|Regional|Rural|Municipal)\s+Council\b",
    r"\bCouncil\s+of\s+(the\s+)?[A-Z][a-zA-Z\s]{2,30}",
    r"\bLocal\s+Government\s+of\s+[A-Z][a-zA-Z\s]{2,30}",
    r"\bMunicipality\s+of\s+[A-Z][a-zA-Z\s]{2,30}",
    r"\b[A-Z][a-zA-Z\s]{2,30}\s+Regional\s+Council\b",
    r"\b[A-Z][a-zA-Z\s]{2,30}\s+Shire\s+Council\b",
]


def _is_portion_of_land(meta: dict, text: str) -> bool:
    if meta.get("is_portion_of_land"):
        return True
    if any(re.search(p, text, re.IGNORECASE) for p in _BUILDING_TENANCY_PATTERNS):
        return False
    return any(re.search(p, text, re.IGNORECASE) for p in _PORTION_PATTERNS)


def _estimate_total_term_years(meta: dict, text: str) -> int | None:
    if meta.get("total_term_years"):
        return int(meta["total_term_years"])

    initial = meta.get("lease_term_years")
    options_total = meta.get("options_total_years")
    if initial is not None and options_total is not None:
        return int(initial) + int(options_total)

    initial_match = re.search(
        r"(?:initial\s+term|term\s+of\s+(?:the\s+)?lease)[^0-9]{0,20}(\d+)\s*years?",
        text, re.IGNORECASE,
    )
    option_matches = re.findall(
        r"(\d+)\s+(?:further\s+)?(?:option|term)[s]?\s+of\s+(\d+)\s+years?",
        text, re.IGNORECASE,
    )
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
    def _condition(meta: dict, text: str) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if not _is_portion_of_land(meta, text):
            return False, []
        reasons.append("lease appears to be for a portion of a land lot (not a whole lot or building tenancy)")
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


# ── WA bespoke conditions ─────────────────────────────────────────────────────

def _wa_pda_s136_condition(meta: dict, text: str) -> tuple[bool, list[str]]:
    """WA PDA 2005 s.136: portion of lot, >20yr total term -> void ab initio."""
    reasons: list[str] = []
    if not _is_portion_of_land(meta, text):
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


def _wa_quiet_enjoyment_condition(meta: dict, text: str) -> tuple[bool, list[str]]:
    """AG4 / PR-WA-002: QE covenant excluded/limited, or likely unregistered lease."""
    reasons: list[str] = []
    qe_present = bool(re.search(
        r"quiet\s+enjoyment|peaceful\s+(enjoyment|possession)|covenant\s+for\s+quiet",
        text, re.IGNORECASE,
    ))
    if not qe_present:
        return False, []
    exclusion_patterns = [
        r"does\s+not\s+(extend|apply|include).{0,60}(agent|contractor|repair|work)",
        r"(excluding|except\s+for|other\s+than).{0,60}(direct\s+act|own\s+act)",
        r"exercise\s+of\s+(rights?|powers?).{0,40}(not\s+(constitute|be\s+a?\s+breach)|shall\s+not\s+breach)",
        r"(limit(ed)?|restrict(ed)?|modif(ied|y)).{0,60}quiet\s+enjoyment",
        r"quiet\s+enjoyment.{0,120}(limit(ed)?|restrict(ed)?|modif(ied|y)|exclud)",
        r"no\s+(warranty|representation|guarantee).{0,40}quiet",
        r"(waive[sd]?|waiving).{0,40}quiet\s+enjoyment",
    ]
    is_limited = any(re.search(p, text, re.IGNORECASE) for p in exclusion_patterns)
    likely_unregistered = False
    total_years = _estimate_total_term_years(meta, text)
    if total_years is not None and total_years < 3:
        likely_unregistered = True
    if re.search(r"(not\s+register(ed)?|unregistered\s+lease)", text, re.IGNORECASE):
        likely_unregistered = True
    if not is_limited and not likely_unregistered:
        return False, []
    if is_limited:
        reasons.append(
            "lease contains language that appears to limit or exclude the statutory quiet "
            "enjoyment covenant (TLA 1893 s.92(b)) — e.g. restricting it to direct landlord "
            "acts, carving out contractor works, or stating landlord rights cannot breach it"
        )
    if likely_unregistered:
        reasons.append(
            "lease term suggests the lease may not require registration at Landgate — "
            "lodging a TLA caveat is recommended to protect the tenant's leasehold interest "
            "against subsequent purchasers or mortgagees"
        )
    return True, reasons


# ── Cross-jurisdictional conditions ───────────────────────────────────────────

def _local_govt_landlord_condition(meta: dict, text: str) -> tuple[bool, list[str]]:
    """
    AG3 / PR-LG-001: Fires when the landlord is a local government entity.

    A council wearing two hats — landlord AND planning/regulatory authority —
    creates risks that do not exist with a private landlord:

    1. No-fettering doctrine (common law, all jurisdictions):
       A council CANNOT contractually bind itself not to exercise its statutory
       powers. Any lease clause purporting to make the council promise to
       cooperate with planning approvals, building permits, or development
       applications is legally unenforceable as a "fetter" on its public duty.
       Tenants who rely on such clauses are unprotected.

    2. Dual-role conflict:
       The same entity that signed the lease also determines the planning scheme,
       issues development approvals, and enforces building regulations.
       The council's regulatory decisions during the lease term (rezoning,
       planning scheme amendments, enforcement action) can adversely affect
       the tenant's use without triggering a lease breach.

    3. Zoning and use risk:
       The council controls its own planning scheme and may amend it during
       the lease term in ways that restrict or prohibit the permitted use.
    """
    reasons: list[str] = []

    # Check metadata first
    if meta.get("landlord_is_local_government"):
        reasons.append("metadata flags landlord as a local government entity")
        return True, reasons

    # Scan full text for council entity patterns
    matched_entity: str | None = None
    for p in _LOCAL_GOVT_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            matched_entity = m.group(0).strip()
            break

    if not matched_entity:
        return False, []

    reasons.append(
        f"landlord appears to be a local government entity: '{matched_entity}' — "
        "this triggers the no-fettering doctrine and dual-role risks"
    )

    # Check for cooperation/planning clauses that are at risk
    cooperation_patterns = [
        r"(council|landlord).{0,60}(cooperat|assist|support|facilitate).{0,60}(plan|approv|permit|licen)",
        r"(plan|approv|permit|licen).{0,60}(cooperat|assist|support|not\s+object|not\s+withhold)",
        r"(not\s+object|shall\s+not\s+refuse|shall\s+cooperat).{0,60}(plan|approv|develop|build)",
        r"landlord.{0,60}(grant|issue|sign).{0,60}(consent|approv|permit)",
    ]
    if any(re.search(p, text, re.IGNORECASE) for p in cooperation_patterns):
        reasons.append(
            "lease contains clause(s) requiring the council to cooperate with planning or "
            "building approvals — these are likely UNENFORCEABLE under the no-fettering doctrine"
        )

    return True, reasons


# ── Rule registry ─────────────────────────────────────────────────────────────
#
# jurisdiction="ALL" rules apply to every jurisdiction.
# evaluate_planning_rules() merges ALL rules with jurisdiction-specific rules.

_PLANNING_RULES: list[dict] = [

    # ── Cross-jurisdictional (ALL) ─────────────────────────────────────────────
    {
        "id": "PR-LG-001",
        "jurisdiction": "ALL",
        "severity": "high",
        "title": "Local Government Landlord — No-Fettering Doctrine + Dual-Role Risk",
        "description": (
            "The landlord in this lease appears to be a local government (council) entity. "
            "This creates two significant risks that do not apply to private landlords:\n\n"
            "1. NO-FETTERING DOCTRINE (all jurisdictions, common law): A council CANNOT "
            "contractually bind itself not to exercise its statutory powers. Any clause "
            "requiring the council to cooperate with, not object to, or facilitate planning "
            "approvals, building permits, or development applications is legally UNENFORCEABLE "
            "— it is a 'fetter' on the council's public duty. Tenants who rely on such clauses "
            "for their business plan (e.g. assuming a fitout permit will be granted, or that "
            "the council will approve a DA) are legally unprotected.\n\n"
            "2. DUAL-ROLE CONFLICT: The same entity that signed the lease also determines "
            "the local planning scheme, issues development approvals, and enforces building "
            "regulations. The council's regulatory decisions (rezoning, planning scheme "
            "amendments, enforcement action against the tenant) are made in its capacity as "
            "a statutory authority — not as a landlord — and cannot constitute a lease breach. "
            "The tenant has no contractual remedy if the council rezones the land or changes "
            "permitted uses during the lease term."
        ),
        "legislation": (
            "No-fettering doctrine (common law — Ayr Harbour Trustees v Oswald [1883]; "
            "Ansett Transport Industries v Commonwealth (1977)); "
            "Local Government Act 1995 (WA); Local Government Act 2020 (VIC); "
            "Local Government Act 1993 (NSW/QLD/TAS); Local Government Act 1999 (SA); "
            "Local Government Act 2016 (NT)"
        ),
        "action": (
            "1. Identify every clause where the council (as landlord) makes a promise "
            "   relating to planning, approvals, permits, or regulatory cooperation — "
            "   these are likely unenforceable and should not be relied upon. "
            "2. Do NOT structure your business plan around council cooperation clauses "
            "   (e.g. 'council will support DA application for fitout'). Treat them as "
            "   best-endeavours obligations with no legal remedy if unmet. "
            "3. Conduct independent planning due diligence BEFORE signing: check zoning, "
            "   permitted uses, and any pending planning scheme amendments for the site. "
            "4. Negotiate a condition precedent: lease does not become binding until all "
            "   required planning and building approvals are independently obtained. "
            "5. Request a 'council cooperation' deed executed by the council in its "
            "   capacity as planning authority (separate from the lease) — while still "
            "   no-fettering applies, this creates political accountability. "
            "6. Obtain independent legal advice from a property lawyer experienced in "
            "   government leasing in the relevant jurisdiction."
        ),
        "condition": _local_govt_landlord_condition,
    },

    # ── WA ────────────────────────────────────────────────────────────────────
    {
        "id": "PR-WA-002",
        "jurisdiction": "WA",
        "severity": "high",
        "title": "Quiet Enjoyment Covenant Modified/Excluded — Caveat Recommended (TLA 1893 s.92(b))",
        "description": (
            "Under Section 92(b) of the Transfer of Land Act 1893 (WA), a covenant of "
            "quiet enjoyment is implied into every registered lease. "
            "This lease contains language that EXCLUDES or LIMITS the tenant's right to "
            "quiet enjoyment — for example, by restricting it to direct acts of the landlord "
            "only, carving out contractor or repair works, or stating that the landlord's "
            "exercise of lease rights cannot constitute a breach. "
            "A diminished quiet enjoyment covenant leaves the tenant exposed to disruption "
            "with no remedy. If the lease is not registered on title, the tenant's interest "
            "is also vulnerable to bona fide purchasers — lodging a caveat under the TLA "
            "protects the leasehold interest against subsequent dealings."
        ),
        "legislation": "Transfer of Land Act 1893 (WA) s.92(b)",
        "action": (
            "1. Review the quiet enjoyment clause against full TLA s.92(b) protection. "
            "2. Reject carve-outs limiting the covenant to direct landlord acts only — "
            "   insist it extends to all persons claiming through or under the landlord. "
            "3. Remove any provision that the landlord's exercise of lease rights cannot "
            "   constitute a breach of quiet enjoyment. "
            "4. If the lease is not registered at Landgate, instruct your solicitor to "
            "   lodge a caveat under the TLA to protect your leasehold interest. "
            "5. Obtain independent legal advice from a WA property lawyer."
        ),
        "condition": _wa_quiet_enjoyment_condition,
    },
    {
        "id": "PR-WA-001",
        "jurisdiction": "WA",
        "severity": "void",   # AQ3: PDA s.136 = void ab initio — same tier as VOID statute findings
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
            "2. If not obtained, do NOT commence occupation or fitout until granted. "
            "3. Negotiate a sunset clause (90-120 days) allowing termination with full "
            "   refund of all pre-paid monies if WAPC approval is refused. "
            "4. Ensure the landlord bears primary responsibility for obtaining approval. "
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
            "Note: Does NOT apply to retail/commercial tenancies within a building."
        ),
        "legislation": "Conveyancing Act 1919 (NSW) s.23E; Environmental Planning and Assessment Act 1979 (NSW)",
        "action": (
            "1. Confirm whether the premises are a defined tenancy within a building "
            "   (unique description/street address) — if so, this rule does not apply. "
            "2. If leasing a portion of land, verify whether development consent has been obtained. "
            "3. If absent, negotiate a condition precedent or sunset clause. "
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
            "is defined as a 'development' and requires development approval."
        ),
        "legislation": "Planning, Development and Infrastructure Act 2016 (SA) s.3 (definition of 'development')",
        "action": (
            "1. Confirm whether the premises are a self-contained building tenancy. "
            "2. If leasing part of a land allotment, verify development approval. "
            "3. If absent, require landlord approval as a condition precedent. "
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
            "(including outdoor areas, carparking, open space, or access ways) "
            "with a total term exceeding 10 years (including all option/renewal periods) "
            "is deemed a subdivision requiring a council subdivision permit. "
            "Consecutive short-term leases to circumvent this threshold may be "
            "treated as a single lease under current case law."
        ),
        "legislation": "Subdivision Act 1988 (Vic) s.22; Planning and Environment Act 1987 (Vic)",
        "action": (
            "1. Check whether the leased area includes any land outside the building envelope. "
            "2. If so, verify whether a council subdivision permit has been obtained. "
            "3. If not, require the landlord to obtain it before the lease is binding. "
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
            "constitutes a 'reconfiguration of a lot' requiring development approval. "
            "The lease cannot be registered at Titles Queensland without this approval."
        ),
        "legislation": "Planning Act 2016 (Qld) sch.2 (definition of 'reconfiguring a lot'); Land Title Act 1994 (Qld)",
        "action": (
            "1. Confirm whether the lease is for a defined part of a building. "
            "2. If leasing a portion of land, verify development approval. "
            "3. Require landlord to provide evidence of approval before execution. "
            "4. Obtain independent legal advice from a Queensland property lawyer."
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
            "approval as a subdivision. The ACT Crown lease system adds an additional "
            "layer — any variation to the Crown lease permitting the use may also be required."
        ),
        "legislation": "Planning Act 2023 (ACT); Land (Planning and Environment) Act 1991 (ACT)",
        "action": (
            "1. Confirm whether the leased area is a self-contained building tenancy. "
            "2. If leasing a portion of land, verify development approval and Crown lease conditions. "
            "3. If approvals are absent, negotiate a condition precedent. "
            "4. Obtain independent legal advice from an ACT property lawyer."
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
            "(including renewal options) is deemed a subdivision of the land, requiring "
            "subdivision approval under the Land Use Planning and Approvals Act 1993 (Tas)."
        ),
        "legislation": "Local Government (Building and Miscellaneous Provisions) Act 1993 (Tas) s.3; Land Use Planning and Approvals Act 1993 (Tas)",
        "action": (
            "1. Verify whether a subdivision approval has been obtained from the relevant council. "
            "2. If not, require the landlord to obtain it as a condition of the lease binding. "
            "3. Include a sunset clause allowing termination if approval is refused. "
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
            "the NT Planning Commission."
        ),
        "legislation": "Planning Act 1999 (NT) s.5",
        "action": (
            "1. Confirm whether the lease is for part of a land lot. "
            "2. If so, verify development approval from the NT Planning Commission. "
            "3. Require the landlord to provide evidence of approval, or include a condition precedent. "
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

    Rules with jurisdiction="ALL" are always evaluated regardless of state.
    Rules with a specific jurisdiction code are only evaluated for that state.
    """
    jur = jurisdiction.upper().strip()
    findings: list[dict] = []

    applicable_rules = [
        r for r in _PLANNING_RULES
        if r["jurisdiction"] == jur or r["jurisdiction"] == "ALL"
    ]
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
    """Format a planning finding as a pipeline_warnings string."""
    triggered = "; ".join(finding.get("triggered_by", []))
    return (
        f"[PLANNING LAW — {finding['severity'].upper()}] {finding['title']} | "
        f"Legislation: {finding['legislation']} | "
        f"Triggered by: {triggered} | "
        f"Action required: {finding['action']}"
    )
