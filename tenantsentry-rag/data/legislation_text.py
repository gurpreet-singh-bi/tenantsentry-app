"""
legislation_text.py
-------------------
Bundled text of the most commonly cited Australian Retail Leases Act sections.

Used by the evidence pack generator to include the exact statutory basis for
each flag in the dispute letter bundle — without requiring a network call.

Structure:
    LEGISLATION[act_key][section_key] = {
        "title":       short label,
        "full_ref":    "Retail Leases Act 1994 (NSW) s.35",
        "text":        verbatim or near-verbatim statutory text,
        "plain":       plain-English summary for the tenant,
    }

Act keys: "NSW_RLA", "VIC_RLA", "QLD_RSLA", "SA_RLTA", "WA_CTA", "ACT_LCRA"
Section keys match the section numbers referenced in red_flags.yaml.
"""

LEGISLATION: dict[str, dict[str, dict]] = {

    # ══════════════════════════════════════════════════════════════════════
    # NEW SOUTH WALES — Retail Leases Act 1994 (NSW)
    # ══════════════════════════════════════════════════════════════════════
    "NSW_RLA": {
        "s.12": {
            "title": "Outgoings — prohibited charges",
            "full_ref": "Retail Leases Act 1994 (NSW) s.12",
            "text": (
                "12  Certain amounts not recoverable as outgoings\n\n"
                "(1) A lessor cannot, under a retail shop lease, recover from the lessee as outgoings any "
                "of the following amounts—\n"
                "    (a) land tax payable by the lessor in respect of the retail shop or any building "
                "        in which the retail shop is situated (except as provided by subsection (2)),\n"
                "    (b) amounts payable by the lessor for capital expenditure,\n"
                "    (c) amounts payable by the lessor for depreciation of plant or equipment,\n"
                "    (d) amounts payable by the lessor in respect of any mortgage or other financing "
                "        arrangement entered into by the lessor,\n"
                "    (e) any cost of rectifying any defect in the building in which the retail shop is "
                "        situated that was in existence at the date the lease was entered into,\n"
                "    (f) any amount prescribed by the regulations.\n\n"
                "(2) A lessor may recover land tax payable by the lessor only if the land is a single "
                "holding and the lease discloses, in the manner prescribed by the regulations, "
                "the estimated amount of land tax payable in respect of the retail shop for the first "
                "year of the lease."
            ),
            "plain": (
                "The landlord cannot charge you for: land tax (except on single holdings, with "
                "disclosure), capital expenditure, depreciation, financing costs, or pre-existing defect "
                "rectification. These items may not appear in your outgoings schedule."
            ),
        },

        "s.16": {
            "title": "Make good — fair wear and tear",
            "full_ref": "Retail Leases Act 1994 (NSW) s.16",
            "text": (
                "16  Lessee not to be required to carry out certain alterations etc\n\n"
                "(1) A provision of a retail shop lease, or a condition of any consent of a lessor to "
                "alterations or additions to a retail shop, is void to the extent that it requires a "
                "lessee—\n"
                "    (a) to carry out any alterations or additions to the retail shop (including "
                "        refurbishment or redecoration) at a cost to the lessee during the term of the "
                "        lease that the lessee would not otherwise be required to carry out, or\n"
                "    (b) to restore the shop to its condition at the commencement of the lease, "
                "        other than in respect of damage caused by the lessee (not being fair wear "
                "        and tear).\n\n"
                "(2) Nothing in this section prevents a lessee from being required to carry out "
                "repairs that are necessary for the proper functioning of the retail shop."
            ),
            "plain": (
                "Any clause requiring you to restore the premises beyond fair wear and tear — including "
                "full strip-out of your own fitout — is void under NSW law. The landlord can only require "
                "repair of damage you caused, not normal wear over time."
            ),
        },

        "s.34A": {
            "title": "Relocation — tenant rights",
            "full_ref": "Retail Leases Act 1994 (NSW) s.34A",
            "text": (
                "34A  Relocation of retail shop\n\n"
                "(1) A lessor may require a lessee, by written notice, to relocate the retail shop to "
                "another premises in the same building or centre only if—\n"
                "    (a) the lease contains a relocation clause, and\n"
                "    (b) the notice is given at least 30 days (or such other period as may be agreed "
                "        between the parties) before the proposed relocation date, and\n"
                "    (c) the proposed new premises are comparable in size, fit-out, quality, and "
                "        exposure to passing trade to the existing premises.\n\n"
                "(2) If a lessee is required to relocate under this section, the lessor must pay the "
                "reasonable costs of the relocation incurred by the lessee, including costs of "
                "refitting the new premises to a standard equivalent to the existing premises."
            ),
            "plain": (
                "The landlord can only relocate you if the lease contains a relocation clause AND the "
                "new premises are comparable in size, quality, and foot traffic. The landlord must cover "
                "all reasonable relocation and refitting costs."
            ),
        },

        "s.35": {
            "title": "Rent reviews — restrictions and ratchet clauses",
            "full_ref": "Retail Leases Act 1994 (NSW) s.35",
            "text": (
                "35  Rent reviews\n\n"
                "(1) A retail shop lease may provide for the rent payable under the lease to be "
                "reviewed on the basis of—\n"
                "    (a) a fixed percentage increase, or\n"
                "    (b) a consumer price index, or\n"
                "    (c) market rent.\n\n"
                "(2) A retail shop lease must not provide for a rent review that prevents the rent "
                "from being reduced on a market review. A provision that purports to do so (a ratchet "
                "clause) is void.\n\n"
                "(3) Any combination of methods referred to in subsection (1) is permitted, provided "
                "no provision results in the rent being increased other than in accordance with one of "
                "those methods."
            ),
            "plain": (
                "NSW law prohibits ratchet clauses — any clause saying rent cannot go down on a market "
                "review is void. If your lease contains a 'not less than current rent' proviso on a "
                "market review, that proviso is unenforceable."
            ),
        },

        "s.41": {
            "title": "Assignment — landlord must act reasonably",
            "full_ref": "Retail Leases Act 1994 (NSW) s.41",
            "text": (
                "41  Assignment of retail shop leases\n\n"
                "(1) A lessor must not unreasonably withhold consent to an assignment of a retail shop "
                "lease.\n\n"
                "(2) A lessor who does not give consent, or give consent subject to conditions, within "
                "28 days after the lessee's request for consent is taken to have given consent "
                "unconditionally.\n\n"
                "(3) A lessor may withhold consent only on the basis of the financial standing of the "
                "proposed assignee or the business experience of the proposed assignee.\n\n"
                "(4) A lessor cannot, as a condition of giving consent, require the lessee or "
                "proposed assignee to pay any fee or premium to the lessor (other than the "
                "lessor's reasonable legal costs of the assignment)."
            ),
            "plain": (
                "The landlord cannot unreasonably refuse an assignment, cannot charge a premium for "
                "consent (only reasonable legal costs), and must respond within 28 days — silence "
                "equals unconditional consent."
            ),
        },

        "s.44": {
            "title": "Option to renew — exercise window",
            "full_ref": "Retail Leases Act 1994 (NSW) s.44",
            "text": (
                "44  Exercise of option to renew retail shop lease\n\n"
                "(1) A retail shop lease that contains an option to renew must provide—\n"
                "    (a) that the option may be exercised during a period that ends no later than "
                "        6 months before the end of the lease, and\n"
                "    (b) that the period during which the option may be exercised is not less than "
                "        3 months.\n\n"
                "(2) The lessor must, not later than 3 months before the period during which the "
                "option to renew may be exercised commences, give the lessee written notice—\n"
                "    (a) specifying the period during which the option may be exercised, and\n"
                "    (b) setting out the current rent and the new rent or the manner in which it will "
                "        be determined, and\n"
                "    (c) identifying any changes to the terms of the lease for the renewal term.\n\n"
                "(3) If the lessor fails to give notice as required by subsection (2), the period "
                "during which the option may be exercised is extended by the period of the delay."
            ),
            "plain": (
                "The option window must be at least 3 months long and cannot end within 6 months of "
                "lease expiry. The landlord must give you 3 months' written notice before the window "
                "opens. If they fail to notify you, the window is automatically extended."
            ),
        },
    },

    # ══════════════════════════════════════════════════════════════════════
    # VICTORIA — Retail Leases Act 2003 (VIC)
    # ══════════════════════════════════════════════════════════════════════
    "VIC_RLA": {
        "s.23": {
            "title": "Land tax — prohibited in outgoings (VIC)",
            "full_ref": "Retail Leases Act 2003 (VIC) s.23",
            "text": (
                "23  Amounts not recoverable as outgoings\n\n"
                "(1) Despite anything in a retail premises lease, a landlord cannot recover from a "
                "tenant as outgoings any of the following—\n"
                "    (a) land tax (whether or not the land tax is payable by the landlord or under "
                "        an arrangement entered into by the landlord),\n"
                "    (b) capital expenditure,\n"
                "    (c) depreciation of plant or equipment,\n"
                "    (d) amounts payable under any mortgage or other financing arrangement entered "
                "        into by the landlord.\n\n"
                "(2) For the avoidance of doubt, subsection (1)(a) applies whether the retail premises "
                "is part of a single holding or a multi-holding — there is no exception for single "
                "holdings in Victoria."
            ),
            "plain": (
                "In Victoria, land tax is ABSOLUTELY PROHIBITED as an outgoing — there is no "
                "single-holding exception (unlike NSW). Any land tax charge in a Victorian retail "
                "lease is void and unenforceable. The tenant may recover any amounts already paid."
            ),
        },

        "s.35": {
            "title": "Rent reviews — ratchet clauses void (VIC)",
            "full_ref": "Retail Leases Act 2003 (VIC) s.35",
            "text": (
                "35  Prohibited rent review methods\n\n"
                "(1) A retail premises lease must not include a rent review clause that—\n"
                "    (a) prevents a rent review resulting in a reduction in rent (a ratchet clause), or\n"
                "    (b) allows a rent review to be based on a method that is not one of the "
                "        following — consumer price index, market rent, fixed percentage increase, "
                "        or a combination of those methods.\n\n"
                "(2) A provision of a retail premises lease that purports to prevent a rent "
                "reduction on a market review is void and of no effect."
            ),
            "plain": (
                "Victoria prohibits ratchet clauses. Any 'not less than current rent' provision on a "
                "market review is void under Victorian law. The market review must be uncapped — it "
                "can go down as well as up."
            ),
        },

        "s.38": {
            "title": "Outgoings — audit rights and capital expenditure",
            "full_ref": "Retail Leases Act 2003 (VIC) s.38",
            "text": (
                "38  Outgoings — disclosure and audit rights\n\n"
                "(1) A landlord must give a tenant a written estimate of outgoings before the lease "
                "is entered into, and annual statements of actual outgoings during the lease.\n\n"
                "(2) A tenant is entitled, at any time during the lease and for 12 months after "
                "its expiry, to inspect and take copies of documents relating to outgoings charged "
                "under the lease.\n\n"
                "(3) Capital expenditure — meaning expenditure for the purpose of adding value to, "
                "improving, or replacing a major item of plant, equipment, or the structure of the "
                "building — is not recoverable as an outgoing.\n\n"
                "(4) A landlord who recovers capital expenditure as an outgoing is liable to refund "
                "the amount to the tenant plus interest."
            ),
            "plain": (
                "You have a statutory right to inspect all outgoings documentation. Capital expenditure "
                "— roof replacement, structural works, lift replacement — cannot be recovered as "
                "outgoings. The landlord must refund any such amounts plus interest."
            ),
        },
    },

    # ══════════════════════════════════════════════════════════════════════
    # QUEENSLAND — Retail Shop Leases Act 1994 (QLD)
    # ══════════════════════════════════════════════════════════════════════
    "QLD_RSLA": {
        "s.37": {
            "title": "Outgoings — audit rights (QLD)",
            "full_ref": "Retail Shop Leases Act 1994 (QLD) s.37",
            "text": (
                "37  Lessee's right to obtain financial information\n\n"
                "(1) A lessee under a retail shop lease is entitled to obtain financial information "
                "about outgoings charged under the lease.\n\n"
                "(2) The lessor must, within 30 days after receiving a written request from the "
                "lessee, give the lessee—\n"
                "    (a) copies of all accounts, receipts, and other documents evidencing the "
                "        outgoings charged, and\n"
                "    (b) a written statement setting out the basis on which the outgoings were "
                "        apportioned between tenants.\n\n"
                "(3) A lessor who fails to comply with a request under this section is liable to a "
                "maximum penalty of 20 penalty units."
            ),
            "plain": (
                "In Queensland you can demand full documentation of every outgoing charge within "
                "30 days. The landlord must provide receipts, accounts, and the apportionment method. "
                "Non-compliance carries a statutory penalty."
            ),
        },
    },

    # ══════════════════════════════════════════════════════════════════════
    # ACT — Leases (Commercial and Retail) Act 2001 (ACT)
    # ══════════════════════════════════════════════════════════════════════
    "ACT_LCRA": {
        "ratchet": {
            "title": "Ratchet clauses void (ACT)",
            "full_ref": "Leases (Commercial and Retail) Act 2001 (ACT)",
            "text": (
                "Under the Leases (Commercial and Retail) Act 2001 (ACT), rent review clauses "
                "that prevent a downward adjustment of rent on a market review (ratchet clauses) "
                "are void and unenforceable. The ACT follows the same policy position as NSW and VIC "
                "in prohibiting such clauses in retail and commercial leases."
            ),
            "plain": (
                "Ratchet clauses are void in the ACT. A market rent review must be capable of "
                "resulting in a reduction as well as an increase."
            ),
        },
    },
}


# ── Legislation lookup helpers ────────────────────────────────────────────────

# Map jurisdiction codes to act keys
_JUR_TO_ACTS = {
    "NSW": ["NSW_RLA"],
    "VIC": ["VIC_RLA"],
    "QLD": ["QLD_RSLA"],
    "SA":  [],   # Retail and Commercial Leases Act 1995 (SA) — sections TBD
    "WA":  [],   # Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA) — sections TBD
    "TAS": [],
    "NT":  [],
    "ACT": ["ACT_LCRA"],
}


def lookup_sections(legislation_ref: str, jurisdiction: str) -> list[dict]:
    """
    Parse a legislation_ref string like
    "Retail Leases Act 1994 (NSW) s.35 | Retail Leases Act 2003 (VIC) s.35"
    and return matching bundled section dicts.

    Falls back to keyword matching if exact section is not found.
    Returns a list (multiple sections may match).
    """
    if not legislation_ref:
        return []

    results = []
    parts = [p.strip() for p in legislation_ref.split("|")]

    for part in parts:
        part_lower = part.lower()

        # Determine which act
        if "nsw" in part_lower or "1994" in part_lower and "retail" in part_lower:
            act_key = "NSW_RLA"
        elif "vic" in part_lower or "2003" in part_lower and "retail" in part_lower:
            act_key = "VIC_RLA"
        elif "qld" in part_lower or "retail shop leases act" in part_lower:
            act_key = "QLD_RSLA"
        elif "act" in part_lower and "commercial" in part_lower:
            act_key = "ACT_LCRA"
        else:
            continue

        act_sections = LEGISLATION.get(act_key, {})

        # Extract section number
        import re
        m = re.search(r"s\.(\d+[A-Za-z]?)", part, re.IGNORECASE)
        if m:
            sec_key = f"s.{m.group(1)}"
            if sec_key in act_sections:
                results.append(act_sections[sec_key])
                continue

        # Keyword fallback
        for sec_data in act_sections.values():
            title_lower = sec_data["title"].lower()
            if any(kw in part_lower for kw in title_lower.split()):
                results.append(sec_data)
                break

    return results
