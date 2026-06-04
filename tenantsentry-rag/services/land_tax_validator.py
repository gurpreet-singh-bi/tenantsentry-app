"""
land_tax_validator.py
---------------------
F5: Land Tax Validator — all Australian jurisdictions

Provides definitive, jurisdiction-specific land tax rules as a prompt
context block. Claude receives the correct statutory position for the
lease's jurisdiction and is instructed to apply it directly — no
guesswork about which state's law applies or what it says.

This is the same pattern as services/cpi_calculator.py:
  deterministic lookup → format_for_prompt() → inject into router.py

Public API
----------
    get_land_tax_rule(jurisdiction: str) -> dict
    format_for_prompt(rule: dict)        -> str

Rule dict schema
----------------
    jurisdiction:      str    e.g. "VIC"
    prohibition_level: str    "absolute" | "conditional" | "restricted" | "limited"
    legislation:       str    Act name + section
    summary:           str    One-sentence plain-English position
    conditions:        list   Conditions under which land tax IS recoverable (empty if absolute)
    tenant_actions:    list   What the tenant should do / check
    contact:           str    Relevant regulator + phone number
    severity:          str    "high" | "medium"  (for prompt framing)

Prohibition levels
------------------
    absolute:    Land tax is void by statute — no exceptions (VIC, SA)
    conditional: Land tax permitted only if specific conditions met (NSW, QLD, ACT)
    restricted:  Permitted on single-holding basis, itemised (WA)
    limited:     No express prohibition; weak protections (TAS, NT)
"""

from loguru import logger

# ── Per-jurisdiction land tax rules ───────────────────────────────────────────
# This is the single source of truth for F5. Update here when legislation changes.

_LAND_TAX_RULES: dict[str, dict] = {

    "VIC": {
        "jurisdiction": "VIC",
        "prohibition_level": "absolute",
        "legislation": "Retail Leases Act 2003 (VIC) s.23",
        "summary": (
            "Land tax is ABSOLUTELY PROHIBITED as a recoverable outgoing in Victoria. "
            "There is no single-holding exception. Any clause requiring the tenant to "
            "pay land tax is void by statute, regardless of how it is worded."
        ),
        "conditions": [],  # No conditions — absolute prohibition
        "tenant_actions": [
            "Cease paying any land tax component immediately.",
            "Demand a full refund of all land tax paid during the tenancy.",
            "Report to Consumer Affairs Victoria (CAV) if the landlord refuses — 1300 558 181.",
        ],
        "contact": "Consumer Affairs Victoria — 1300 558 181",
        "severity": "high",
    },

    "NSW": {
        "jurisdiction": "NSW",
        "prohibition_level": "conditional",
        "legislation": "Retail Leases Act 1994 (NSW) s.12",
        "summary": (
            "In NSW, land tax is only a lawful outgoing if BOTH conditions are met: "
            "(1) the land is assessed as a SINGLE HOLDING (not aggregated with other properties), "
            "AND (2) the estimated amount is DISCLOSED in the lease. Portfolio-basis or "
            "undisclosed land tax is unlawful."
        ),
        "conditions": [
            "Land must be assessed as a single holding — not aggregated with other properties.",
            "Estimated land tax amount must be disclosed in the lease for the first year.",
        ],
        "tenant_actions": [
            "Request the land tax assessment from the landlord to verify single-holding basis.",
            "Check the lease for disclosed land tax estimate — if absent, the charge is unlawful.",
            "If either condition is not met, cease payment and seek a refund.",
            "Contact NSW Fair Trading — 13 32 20.",
        ],
        "contact": "NSW Fair Trading — 13 32 20",
        "severity": "high",
    },

    "QLD": {
        "jurisdiction": "QLD",
        "prohibition_level": "conditional",
        "legislation": "Retail Shop Leases Act 1994 (QLD) s.22",
        "summary": (
            "In Queensland, land tax is only recoverable if BOTH conditions are met: "
            "(1) it was DISCLOSED in the lessor's disclosure statement BEFORE the lease was signed, "
            "AND (2) it is calculated on a SINGLE-TENANCY basis. Undisclosed or portfolio-basis "
            "land tax cannot be recovered regardless of the lease wording."
        ),
        "conditions": [
            "Land tax must have been disclosed in the lessor's disclosure statement pre-signing.",
            "Land tax must be calculated on a single-tenancy (not portfolio) basis.",
        ],
        "tenant_actions": [
            "Obtain a copy of the lessor's disclosure statement — land tax must appear in it.",
            "If land tax was not disclosed pre-signing, it is not recoverable — demand a refund.",
            "Verify the calculation is on a single-tenancy (not aggregate) basis.",
            "Contact Queensland Office of Fair Trading — 13 74 68.",
        ],
        "contact": "Office of Fair Trading QLD — 13 74 68",
        "severity": "high",
    },

    "SA": {
        "jurisdiction": "SA",
        "prohibition_level": "absolute",
        "legislation": "Retail and Commercial Leases Act 1995 (SA) s.20",
        "summary": (
            "Land tax is PROHIBITED as a recoverable outgoing in South Australian retail leases. "
            "This mirrors the Victorian position — any clause requiring the SA tenant to pay "
            "land tax is void and of no effect."
        ),
        "conditions": [],  # Absolute prohibition
        "tenant_actions": [
            "Cease paying any land tax component immediately.",
            "Demand a refund of all land tax paid during the tenancy.",
            "Contact Consumer and Business Services SA (CBS) — 131 882.",
        ],
        "contact": "Consumer and Business Services SA — 131 882",
        "severity": "high",
    },

    "WA": {
        "jurisdiction": "WA",
        "prohibition_level": "restricted",
        "legislation": "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA) s.13",
        "summary": (
            "In WA, land tax is a RESTRICTED outgoing. It may only be charged on a "
            "SINGLE-HOLDING basis AND must be SEPARATELY ITEMISED in the outgoings schedule. "
            "Bundling land tax into 'rates and taxes' or charging on a portfolio basis "
            "is not recoverable."
        ),
        "conditions": [
            "Calculated on a single-holding basis only.",
            "Separately itemised in the outgoings schedule — not bundled with other charges.",
        ],
        "tenant_actions": [
            "Check the outgoings schedule — land tax must be a separate line item.",
            "Request evidence of single-holding land tax assessment.",
            "If bundled or portfolio-based, the charge is not lawfully recoverable.",
            "Contact Commerce WA — 1300 304 054.",
        ],
        "contact": "Commerce WA — 1300 304 054",
        "severity": "high",
    },

    "ACT": {
        "jurisdiction": "ACT",
        "prohibition_level": "conditional",
        "legislation": "Leases (Commercial and Retail) Act 2001 (ACT) s.28",
        "summary": (
            "The ACT applies restrictions equivalent to NSW: land tax is only recoverable on "
            "a SINGLE-HOLDING basis AND must be DISCLOSED in the lease. Portfolio-basis or "
            "undisclosed land tax is void."
        ),
        "conditions": [
            "Land assessed as single holding — not aggregated with other properties.",
            "Estimated amount disclosed in the lease.",
        ],
        "tenant_actions": [
            "Verify single-holding assessment and lease disclosure.",
            "If either condition is unmet, the charge is unlawful.",
            "Contact ACT Access Canberra — 13 22 81.",
        ],
        "contact": "ACT Access Canberra — 13 22 81",
        "severity": "high",
    },

    "TAS": {
        "jurisdiction": "TAS",
        "prohibition_level": "limited",
        "legislation": "Fair Trading (Code of Practice for Retail Tenancies) Regulations 1998 (TAS)",
        "summary": (
            "Tasmania has weaker statutory protections than mainland states. Land tax is not "
            "expressly prohibited, but the Code requires outgoings to be itemised and "
            "reasonable. Portfolio-basis land tax should be challenged as unreasonable."
        ),
        "conditions": [
            "Outgoings must be itemised in an outgoings schedule.",
            "Charges must be attributable to the specific premises, not the broader portfolio.",
        ],
        "tenant_actions": [
            "Require separate itemisation of land tax in the outgoings schedule.",
            "Challenge any portfolio-basis or aggregate calculation as unreasonable.",
            "Negotiate single-holding basis and disclosure as express lease conditions.",
            "Contact Consumer, Building and Occupational Services TAS — 1300 654 499.",
        ],
        "contact": "CBOS Tasmania — 1300 654 499",
        "severity": "medium",
    },

    "NT": {
        "jurisdiction": "NT",
        "prohibition_level": "limited",
        "legislation": "Business Tenancies (Fair Dealings) Act 2003 (NT)",
        "summary": (
            "The NT has the weakest retail tenancy protections in Australia. There is no "
            "express statutory prohibition on land tax as an outgoing. Protection is primarily "
            "contractual — single-holding basis and itemised disclosure should be negotiated "
            "into every new lease."
        ),
        "conditions": [
            "No express statutory conditions — common law reasonableness applies.",
            "Outgoings must be genuinely attributable to the specific leased premises.",
        ],
        "tenant_actions": [
            "Negotiate single-holding basis and itemised land tax disclosure into the lease.",
            "Challenge portfolio-basis charges under contract law (unreasonable outgoing).",
            "Seek advice from an NT commercial lease solicitor.",
            "Contact NT Consumer Affairs — 1800 019 319.",
        ],
        "contact": "NT Consumer Affairs — 1800 019 319",
        "severity": "medium",
    },
}


# ── Public API ────────────────────────────────────────────────────────────────

def get_land_tax_rule(jurisdiction: str) -> dict:
    """
    Return the land tax rule dict for the given jurisdiction.
    Falls back to a generic advisory if jurisdiction is unknown.
    """
    jur = jurisdiction.upper().strip()
    rule = _LAND_TAX_RULES.get(jur)
    if not rule:
        logger.warning(f"[LandTax] No rule found for jurisdiction '{jur}' — using generic advisory")
        return {
            "jurisdiction": jur,
            "prohibition_level": "limited",
            "legislation": "Applicable state retail tenancy legislation",
            "summary": (
                f"No specific land tax rule is configured for {jur}. Apply general "
                "Australian retail tenancy principles: land tax should be single-holding "
                "basis only, separately itemised, and pre-disclosed."
            ),
            "conditions": [],
            "tenant_actions": [
                "Require itemised land tax disclosure in the lease.",
                "Seek single-holding basis evidence from the landlord.",
            ],
            "contact": "Relevant state retail tenancy authority",
            "severity": "medium",
        }
    return rule


def format_for_prompt(rule: dict) -> str:
    """
    Format a land tax rule into a context block for injection into the Claude prompt.
    Claude applies this definitive statutory position — no guesswork about which
    state's law applies or what it says.

    Returns empty string only if rule is None/empty.
    """
    if not rule:
        return ""

    jur = rule["jurisdiction"]
    level = rule["prohibition_level"]
    severity_label = "HIGH SEVERITY — void by statute" if level == "absolute" else \
                     "HIGH SEVERITY — strict conditions apply" if rule["severity"] == "high" else \
                     "MEDIUM SEVERITY — weaker protections"

    lines = [
        f"╔══ LAND TAX STATUTORY POSITION — {jur} ({'ABSOLUTE PROHIBITION' if level == 'absolute' else level.upper()}) ══╗",
        f"  Legislation:  {rule['legislation']}",
        f"  Severity:     {severity_label}",
        "",
        f"  Position:     {rule['summary']}",
    ]

    if rule["conditions"]:
        lines += ["", "  Conditions for lawful recovery:"]
        for c in rule["conditions"]:
            lines.append(f"    • {c}")

    lines += [
        "",
        "  Tenant actions:",
    ]
    for a in rule["tenant_actions"]:
        lines.append(f"    • {a}")

    lines += [
        "",
        f"  Regulator:    {rule['contact']}",
        "",
        f"  INSTRUCTION TO CLAUDE:",
        f"  Apply the {jur} land tax position above when analysing this clause.",
        f"  Do NOT assume a different jurisdiction's rules apply.",
        f"  If the clause contains land tax obligations that violate the above position,",
        f"  flag it as {rule['severity'].upper()} severity with the legislation ref: {rule['legislation']}",
        "╚══════════════════════════════════════════════════════════════════════════════╝",
    ]

    return "\n".join(lines)
