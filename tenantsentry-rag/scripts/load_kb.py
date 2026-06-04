"""
load_kb.py
----------
Populates Supabase with:
  1. Red flag rules from rules/red_flags.yaml  (chunk_type='rule')
  2. Australian commercial lease legislation text (chunk_type='legislation')

Versioned + idempotent: each legislation block carries a source_version string.
Re-running only loads versions not yet present in the DB -- safe to run repeatedly.

Usage:
    cd tenantsentry-rag
    python scripts/load_kb.py [--force]

Verify in Supabase SQL editor:
    SELECT chunk_type, jurisdiction, source_version, COUNT(*)
    FROM lease_chunks GROUP BY 1, 2, 3 ORDER BY 1, 2;
"""

import sys
import os
import argparse
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import yaml
from loguru import logger
from embedding.embedder import embed_texts
from vector_store.supabase_store import get_client


LEGISLATION = [

    # NSW
    {
        "jurisdiction": "NSW",
        "source_version": "NSW_RLA1994_2026-06",
        "title": "Retail Leases Act 1994 (NSW) -- Outgoings (s.12)",
        "text": (
            "Retail Leases Act 1994 (NSW) -- Section 12: Outgoings\n"
            "A retail shop lease must not require a tenant to pay outgoings unless the lease "
            "specifies each type of outgoing and the basis on which the estimate is calculated.\n\n"
            "Capital expenditure items cannot be included in recoverable outgoings. Capital "
            "expenditure means: structural repairs, roof replacement, replacement of plant and "
            "equipment (air conditioning, lifts, escalators), building facade works, and other "
            "works that extend the useful life of a capital asset.\n\n"
            "Land tax must be calculated on a single-holding basis -- the landlord's entire "
            "portfolio cannot be used to inflate the tenant's share. Any portfolio-basis clause "
            "is unenforceable.\n\n"
            "Management fees must represent the actual cost of managing the property and cannot "
            "include a profit margin. Fees exceeding 10% of net outgoings are unreasonable.\n\n"
            "Landlord must provide an outgoings estimate before execution and a reconciliation "
            "statement within 3 months of the end of each outgoings year."
        ),
    },
    {
        "jurisdiction": "NSW",
        "source_version": "NSW_RLA1994_2026-06",
        "title": "Retail Leases Act 1994 (NSW) -- Rent Review (s.35)",
        "text": (
            "Retail Leases Act 1994 (NSW) -- Section 35: Rent review\n"
            "Ratchet clauses that prevent rent from decreasing on a market rent review are void "
            "and of no effect in New South Wales.\n\n"
            "Market rent reviews must use a specialist retail valuer if parties cannot agree. "
            "Either party may refer disputes to the NSW Small Business Commissioner for mediation.\n\n"
            "CPI rent reviews must reference the Consumer Price Index All Groups for Sydney, "
            "using the most recently published quarterly index number at the time of review.\n\n"
            "Fixed percentage increases above current CPI are tenant-adverse. Compounding fixed "
            "increases create additional long-term cost exposure.\n\n"
            "Rent cannot be reviewed more frequently than once per 12 months."
        ),
    },
    {
        "jurisdiction": "NSW",
        "source_version": "NSW_RLA1994_2026-06",
        "title": "Retail Leases Act 1994 (NSW) -- Make Good (s.16)",
        "text": (
            "Retail Leases Act 1994 (NSW) -- Section 16: Condition of premises on termination\n"
            "Make-good obligations must exclude fair wear and tear. A landlord cannot require "
            "a tenant to restore premises to original condition if changes were made with the "
            "landlord's written consent.\n\n"
            "A condition report signed by both parties at commencement is the definitive baseline. "
            "Without one, the tenant is not required to restore items that cannot be proven to "
            "have been in better condition at commencement.\n\n"
            "A landlord cannot require removal of landlord-installed fitout at expiry unless "
            "clearly disclosed in the lease."
        ),
    },
    {
        "jurisdiction": "NSW",
        "source_version": "NSW_RLA1994_2026-06",
        "title": "Retail Leases Act 1994 (NSW) -- Assignment (s.41)",
        "text": (
            "Retail Leases Act 1994 (NSW) -- Section 41: Assignment\n"
            "A landlord cannot unreasonably withhold consent to assignment. The landlord must "
            "respond within 28 days. Failure to respond results in deemed consent.\n\n"
            "Reasonable grounds to withhold: assignee lacks financial capacity or business "
            "experience, or intends a non-permitted use.\n\n"
            "Unreasonable grounds: extracting a premium, withholding to re-let at higher rent, "
            "or simply preferring a different tenant."
        ),
    },
    {
        "jurisdiction": "NSW",
        "source_version": "NSW_RLA1994_2026-06",
        "title": "Retail Leases Act 1994 (NSW) -- Option to Renew (s.44)",
        "text": (
            "Retail Leases Act 1994 (NSW) -- Section 44: Options to renew\n"
            "The landlord must give the tenant written notice of the option exercise window "
            "at least 3 months (and no more than 6 months) before the window opens.\n\n"
            "If the landlord fails to give notice, the tenant's right is preserved and the "
            "window is extended accordingly.\n\n"
            "A tenant who exercises an option cannot be refused renewal solely on the basis of "
            "a breach, unless it is a significant unremedied breach at the time of exercise.\n\n"
            "Rent for a renewed term via market review is subject to the same ratchet prohibition."
        ),
    },

    {
        "jurisdiction": "NSW",
        "source_version": "NSW_RLA1994_2026-06-LT",
        "title": "Retail Leases Act 1994 (NSW) -- Land Tax Single-Holding Rule (s.12)",
        "text": (
            "Retail Leases Act 1994 (NSW) -- Section 12: Land tax -- single-holding and disclosure\n\n"
            "Land tax is only a lawful outgoing in a NSW retail lease if BOTH conditions are met:\n"
            "(a) the land is assessed as a single holding -- not aggregated with other properties "
            "owned by the landlord for land tax assessment purposes; and\n"
            "(b) the estimated land tax amount for the first year is disclosed in the lease.\n\n"
            "Portfolio-basis land tax -- where the landlord's total land holdings are used to "
            "calculate a higher aggregate land tax that is then apportioned across tenants -- is "
            "expressly prohibited. Any such clause is unenforceable.\n\n"
            "If the estimated amount is not disclosed in the lease, the landlord cannot recover "
            "land tax at all, regardless of the outgoings clause wording.\n\n"
            "The tenant should request a copy of the land tax assessment to verify single-holding "
            "basis. If the landlord cannot produce one, the charge is unlawful.\n\n"
            "Contact NSW Fair Trading on 13 32 20 for assistance."
        ),
    },

    # VIC
    {
        "jurisdiction": "VIC",
        "source_version": "VIC_RLA2003_2026-06",
        "title": "Retail Leases Act 2003 (VIC) -- Land Tax Prohibition (s.23)",
        "text": (
            "Retail Leases Act 2003 (VIC) -- Section 23: Land tax -- absolute prohibition\n"
            "A retail premises lease must not require the tenant to pay land tax. Land tax is "
            "expressly and absolutely prohibited as a recoverable outgoing in Victoria.\n\n"
            "Any clause requiring a VIC tenant to pay land tax is void and of no effect, "
            "regardless of how it is worded. This prohibition cannot be contracted out of.\n\n"
            "Tenants paying land tax under a VIC lease should cease paying immediately and seek "
            "a refund of all amounts paid."
        ),
    },
    {
        "jurisdiction": "VIC",
        "source_version": "VIC_RLA2003_2026-06",
        "title": "Retail Leases Act 2003 (VIC) -- Outgoings (s.38-45)",
        "text": (
            "Retail Leases Act 2003 (VIC) -- Outgoings\n"
            "Landlords cannot recover capital expenditure through outgoings. Capital expenditure "
            "includes: structural repairs, roof replacement, air conditioning plant replacement, "
            "lifts, and building facade works.\n\n"
            "The landlord must provide a disclosure statement before execution, including an "
            "estimate of outgoings for the first year. If actual outgoings exceed the estimate "
            "by more than 10%, the tenant may seek compensation.\n\n"
            "Annual reconciliation statements must be provided within 3 months of year end. "
            "The tenant has the right to inspect records and require an independent audit if "
            "an error exceeding 5% of the estimated amount is found."
        ),
    },
    {
        "jurisdiction": "VIC",
        "source_version": "VIC_RLA2003_2026-06",
        "title": "Retail Leases Act 2003 (VIC) -- Rent Review (s.35)",
        "text": (
            "Retail Leases Act 2003 (VIC) -- Section 35: Rent review\n"
            "Ratchet clauses are void in Victoria. A rent review cannot result in a rent floor "
            "above passing rent unless it is an explicitly fixed-increase review.\n\n"
            "Tenants may dispute rent reviews through the Victorian Small Business Commission "
            "(VSBC). Mediation is mandatory before any VCAT proceeding.\n\n"
            "CPI reviews must reference the Melbourne CPI All Groups index. Market reviews "
            "require a qualified retail valuer; if parties cannot agree on one, either may "
            "apply to the VSBC to appoint."
        ),
    },
    {
        "jurisdiction": "VIC",
        "source_version": "VIC_RLA2003_2026-06",
        "title": "Retail Leases Act 2003 (VIC) -- Key Money Prohibition",
        "text": (
            "Retail Leases Act 2003 (VIC) -- Key money and goodwill: prohibition\n"
            "A landlord must not require or receive key money as a condition of granting, "
            "renewing, transferring, or consenting to assignment of a retail premises lease.\n\n"
            "Key money includes any payment (however described) that is not rent, a reasonable "
            "security deposit, or a legitimate outgoing under the lease.\n\n"
            "A tenant who has paid key money may recover it from the landlord. A landlord who "
            "receives key money commits an offence under the Act."
        ),
    },

    # QLD
    {
        "jurisdiction": "QLD",
        "source_version": "QLD_RSLA1994_2026-06",
        "title": "Retail Shop Leases Act 1994 (QLD) -- Outgoings (s.37-43)",
        "text": (
            "Retail Shop Leases Act 1994 (QLD) -- Outgoings\n"
            "Queensland requires landlords to provide an annual registered auditor's report on "
            "outgoings within 1 month of the end of each lease year. Tenants have the right to "
            "inspect records and request copies.\n\n"
            "Capital expenditure cannot be recovered through outgoings.\n\n"
            "Land tax can be charged in QLD but only if: (a) disclosed before execution; "
            "(b) an estimate of annual amount was provided; and (c) calculated on a "
            "single-tenancy basis. Failure to disclose pre-execution makes the clause "
            "unenforceable.\n\n"
            "Outgoings must be itemised -- lump-sum estimates without itemisation are "
            "non-compliant and may be unenforceable."
        ),
    },
    {
        "jurisdiction": "QLD",
        "source_version": "QLD_RSLA1994_2026-06",
        "title": "Retail Shop Leases Act 1994 (QLD) -- Assignment and Subletting",
        "text": (
            "Retail Shop Leases Act 1994 (QLD) -- Assignment\n"
            "A landlord cannot unreasonably withhold consent to assignment. The landlord must "
            "respond within a reasonable time (generally 28 days).\n\n"
            "The landlord may require the assignee to demonstrate equivalent or better financial "
            "capacity and business experience.\n\n"
            "The landlord cannot charge a fee beyond recovering reasonable out-of-pocket legal "
            "costs actually incurred."
        ),
    },
    {
        "jurisdiction": "QLD",
        "source_version": "QLD_RSLA1994_2026-06",
        "title": "Retail Shop Leases Act 1994 (QLD) -- Rent Review",
        "text": (
            "Retail Shop Leases Act 1994 (QLD) -- Rent review\n"
            "Queensland has no explicit statutory prohibition on ratchet clauses, but such "
            "clauses may be challenged as unconscionable conduct under the ACL.\n\n"
            "CPI reviews must reference the Brisbane All Groups CPI. Market reviews require an "
            "independent valuer; QCAT may appoint one if parties cannot agree.\n\n"
            "Fixed percentage reviews substantially exceeding CPI over extended periods may "
            "attract scrutiny under the ACL unfair contract terms regime, particularly for "
            "small business leases entered after November 2023."
        ),
    },

    # SA
    {
        "jurisdiction": "SA",
        "source_version": "SA_RCLA1995_2026-06",
        "title": "Retail and Commercial Leases Act 1995 (SA) -- Key Provisions",
        "text": (
            "Retail and Commercial Leases Act 1995 (SA)\n"
            "Applies to retail shop leases where annual rent is below the prescribed threshold "
            "(currently AUD 400,000 p.a.). High-rent leases are governed by general contract law.\n\n"
            "Key protections: capital expenditure excluded from outgoings; land tax recoverable "
            "only if disclosed pre-execution on a single-holding basis; landlord must respond to "
            "assignment requests within 28 days (deemed consent on non-response); Form 1 "
            "disclosure statement required at least 7 days before execution (tenant may rescind "
            "if not provided); disputes handled by SACAT."
        ),
    },
    {
        "jurisdiction": "SA",
        "source_version": "SA_RCLA1995_2026-06-LT",
        "title": "Retail and Commercial Leases Act 1995 (SA) -- Land Tax Prohibition (s.20)",
        "text": (
            "Retail and Commercial Leases Act 1995 (SA) -- Section 20: Land tax prohibited\n\n"
            "Land tax is prohibited as a recoverable outgoing in South Australian retail leases. "
            "This mirrors the Victorian prohibition under the Retail Leases Act 2003 (VIC) s.23.\n\n"
            "Any clause in a SA retail lease requiring the tenant to contribute to or pay land "
            "tax is void and of no effect, regardless of how it is worded or how it is structured "
            "in the outgoings schedule.\n\n"
            "The tenant may refuse to pay land tax charges and is entitled to a refund of all "
            "land tax amounts paid during the tenancy. Contact Consumer and Business Services SA "
            "(CBS) on 131 882 to report non-compliance."
        ),
    },

    # WA
    {
        "jurisdiction": "WA",
        "source_version": "WA_CTRSA1985_2026-06",
        "title": "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA) -- Key Provisions",
        "text": (
            "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA)\n"
            "Applies to retail shops in shopping centres and certain standalone retail premises.\n\n"
            "Key provisions: capital expenditure not recoverable; land tax is a restricted "
            "outgoing — only recoverable on a single-holding basis and must be separately "
            "itemised (not bundled in 'rates and taxes'); no explicit ratchet clause prohibition "
            "but ACL unconscionable conduct provisions apply; landlord must act reasonably on "
            "assignment (28-day deemed consent); disclosure statement required and tenant has "
            "7 business days to rescind after receiving it; disputes handled by SAT."
        ),
    },
    {
        "jurisdiction": "WA",
        "source_version": "WA_CTRSA1985_2026-06-LT",
        "title": "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA) -- Land Tax (s.13)",
        "text": (
            "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA) -- Section 13: "
            "Land tax as restricted outgoing\n\n"
            "Land tax is a restricted outgoing in Western Australia. It may only be charged to "
            "the tenant if:\n"
            "(a) it is calculated on a single-holding basis — assessed as if the retail shop "
            "land is the only land owned by the lessor; and\n"
            "(b) it is separately itemised in the outgoings schedule — it cannot be bundled "
            "within a general 'rates and taxes' line item.\n\n"
            "Land tax calculated on an aggregate or portfolio basis (taking into account other "
            "properties owned by the landlord) is not recoverable under any circumstances.\n\n"
            "If the charge is buried in a lump-sum 'rates and taxes' line without separate "
            "itemisation, it is not lawfully recoverable. Contact Commerce WA on 1300 304 054."
        ),
    },

    # TAS
    {
        "jurisdiction": "TAS",
        "source_version": "TAS_FT_CODE_2026-06",
        "title": "Fair Trading (Code of Practice for Retail Tenancies) Regulations 1998 (TAS)",
        "text": (
            "Fair Trading (Code of Practice for Retail Tenancies) Regulations 1998 (TAS)\n"
            "Tasmania relies on a Code of Practice rather than a dedicated Act. Capital "
            "expenditure excluded from outgoings; land tax may be included if disclosed and "
            "calculated on a single-holding basis; market reviews require an independent valuer; "
            "landlord consent to assignment cannot be unreasonably withheld; disputes handled "
            "by TASCAT.\n\n"
            "Note: Tasmania's protections are generally less comprehensive than NSW, VIC, or QLD. "
            "Tenants should rely more heavily on negotiated lease terms."
        ),
    },
    {
        "jurisdiction": "TAS",
        "source_version": "TAS_FT_CODE_2026-06-LT",
        "title": "Fair Trading (Code of Practice for Retail Tenancies) Regulations 1998 (TAS) -- Land Tax",
        "text": (
            "Tasmania -- Land tax in retail lease outgoings\n\n"
            "Tasmania's retail tenancy code requires outgoings to be itemised, reasonable, and "
            "directly attributable to the retail shop. While the Code does not expressly prohibit "
            "land tax in the same absolute terms as VIC or SA, it requires:\n\n"
            "(a) All outgoings must be itemised in an outgoings schedule;\n"
            "(b) Charges must be directly related to the retail premises and not the landlord's "
            "broader property portfolio;\n"
            "(c) The lessor must provide an annual reconciliation statement.\n\n"
            "Portfolio-basis land tax should be challenged as unreasonable and attributable to "
            "the landlord's broader holdings rather than the specific retail premises.\n\n"
            "Contact Consumer, Building and Occupational Services TAS on 1300 654 499."
        ),
    },

    # ACT
    {
        "jurisdiction": "ACT",
        "source_version": "ACT_LCRA2001_2026-06",
        "title": "Leases (Commercial and Retail) Act 2001 (ACT) -- Key Provisions",
        "text": (
            "Leases (Commercial and Retail) Act 2001 (ACT)\n"
            "One of the more tenant-friendly regimes in Australia, comparable to NSW and VIC.\n\n"
            "Key provisions: capital expenditure not recoverable; land tax not recoverable as an "
            "outgoing; ratchet clauses preventing downward market reviews are void; CPI reviews "
            "reference the Canberra CPI All Groups index; 28-day deemed consent on assignment; "
            "disclosure statement required at least 14 days before execution (longer than other "
            "jurisdictions); disputes handled by ACAT."
        ),
    },
    {
        "jurisdiction": "ACT",
        "source_version": "ACT_LCRA2001_2026-06-LT",
        "title": "Leases (Commercial and Retail) Act 2001 (ACT) -- Land Tax (s.28)",
        "text": (
            "Leases (Commercial and Retail) Act 2001 (ACT) -- Section 28: Land tax restrictions\n\n"
            "In the ACT, land tax is only recoverable as an outgoing if both conditions are met:\n"
            "(a) the land is assessed for land tax as a single holding — not aggregated with other "
            "land owned by the lessor; and\n"
            "(b) the estimated amount of land tax is disclosed in the lease.\n\n"
            "A provision of a lease that purports to require the lessee to pay land tax on a "
            "portfolio or aggregate basis is void. The ACT applies restrictions equivalent to NSW.\n\n"
            "If the landlord has not disclosed the estimated land tax amount in the lease, they "
            "cannot recover it at all — regardless of what the outgoings clause says.\n\n"
            "Contact ACT Access Canberra on 13 22 81 for assistance."
        ),
    },

    # NT
    {
        "jurisdiction": "NT",
        "source_version": "NT_BTFDA2003_2026-06",
        "title": "Business Tenancies (Fair Dealings) Act 2003 (NT) -- Key Provisions",
        "text": (
            "Business Tenancies (Fair Dealings) Act 2003 (NT)\n"
            "Applies to business tenancy agreements in the Northern Territory.\n\n"
            "Key provisions: capital expenditure not recoverable; land tax may be included if "
            "disclosed on a single-holding basis; no explicit ratchet prohibition (ACL applies); "
            "market reviews require an independent valuer; landlord cannot unreasonably withhold "
            "assignment consent; cooling-off period after receiving disclosure statement; "
            "disputes handled by NTCAT.\n\n"
            "Note: The NT Act is less developed than major state Acts. Legal advice is "
            "particularly important for NT leases."
        ),
    },
    {
        "jurisdiction": "NT",
        "source_version": "NT_BTFDA2003_2026-06-LT",
        "title": "Business Tenancies (Fair Dealings) Act 2003 (NT) -- Land Tax",
        "text": (
            "Northern Territory -- Land tax in business tenancy outgoings\n\n"
            "The Business Tenancies (Fair Dealings) Act 2003 (NT) provides limited retail tenancy "
            "protections compared to eastern states. There is no express statutory prohibition on "
            "land tax as an outgoing in the NT.\n\n"
            "However, common law and ACL principles apply:\n"
            "(a) Outgoings must be genuinely attributable to the leased premises;\n"
            "(b) Portfolio-basis charges that inflate the tenant's share beyond the proportionate "
            "cost for the specific premises are challengeable under contract law as unreasonable;\n"
            "(c) The ACL unfair contract terms regime (post November 2023) may apply to "
            "standard-form NT retail leases with small business tenants.\n\n"
            "NT tenants should negotiate single-holding basis and itemised land tax disclosure "
            "as express contractual conditions in any new lease. Contact NT Consumer Affairs on "
            "1800 019 319 for guidance."
        ),
    },

    # QLD -- land tax specific (supplements existing QLD outgoings chunk)
    {
        "jurisdiction": "QLD",
        "source_version": "QLD_RSLA1994_2026-06-LT",
        "title": "Retail Shop Leases Act 1994 (QLD) -- Land Tax Disclosure and Single-Tenancy Basis (s.22)",
        "text": (
            "Retail Shop Leases Act 1994 (QLD) -- Section 22: Land tax\n\n"
            "A lessor under a retail shop lease may recover land tax from the lessee as an "
            "outgoing only if both conditions are satisfied:\n"
            "(a) the amount of land tax payable was disclosed in the lessor's disclosure statement "
            "given to the lessee BEFORE the lease was entered into; and\n"
            "(b) the land tax is calculated on a single-tenancy basis — as if the land on which "
            "the retail shop is situated is the only land owned by the lessor.\n\n"
            "If land tax was not disclosed in the lessor's disclosure statement before signing, "
            "the lessor is not entitled to recover it regardless of what the lease says.\n\n"
            "A lessor who charges land tax on a portfolio or aggregate basis (taking into account "
            "other properties) is in breach of this section. The tenant may withhold payment and "
            "seek a refund. Contact the Office of Fair Trading QLD on 13 74 68."
        ),
    },

    # ALL -- cross-cutting risk guidance
    {
        "jurisdiction": "ALL",
        "source_version": "ALL_BESTPRACTICE_2026-06",
        "title": "Australian Commercial Lease -- Personal Guarantee Risk Factors",
        "text": (
            "Personal guarantees in Australian commercial leases -- risk assessment\n"
            "Personal guarantees expose individual directors to unlimited personal liability.\n\n"
            "High-risk structures: unlimited guarantee covering full term with no monetary cap; "
            "joint and several liability with no contribution rights; guarantee survives insolvency; "
            "no sunset clause; guarantee extends to fitout costs and make-good.\n\n"
            "Best practice: cap at 6-12 months total rent; sunset clause after 24 months of clean "
            "payment; limited to base rent only (excluding make-good and legal costs); automatic "
            "release if corporate tenant net assets exceed 2x annual rent.\n\n"
            "Standard market practice: 6-month guarantee cap with 2-year sunset. Any requirement "
            "beyond this should be flagged as high risk."
        ),
    },
    {
        "jurisdiction": "ALL",
        "source_version": "ALL_BESTPRACTICE_2026-06",
        "title": "Australian Commercial Lease -- Make Good Risk Factors",
        "text": (
            "Make good clauses in Australian commercial leases -- risk assessment\n"
            "Make good is among the most significant exit costs. Excessive requirements can cost "
            "AUD 50,000-500,000+ on expiry.\n\n"
            "High-risk structures: restore to original pre-fitout condition; no fair wear and tear "
            "exclusion; must remove all fitout; no cash settlement option; landlord unilaterally "
            "determines adequacy.\n\n"
            "Best practice: detailed condition report at commencement; explicit fair wear and tear "
            "exclusion; right to offer cash settlement in lieu of works; landlord consent to "
            "alterations waives make-good for those alterations; independent building consultant "
            "to assess if disputed."
        ),
    },
    {
        "jurisdiction": "ALL",
        "source_version": "ALL_BESTPRACTICE_2026-06",
        "title": "Australian Commercial Lease -- Rent Review Risk Factors",
        "text": (
            "Rent review mechanisms in Australian commercial leases -- risk assessment\n\n"
            "High-risk: fixed increases above CPI+2% p.a.; compounding fixed increases; ratchet "
            "clauses (void in NSW, VIC, ACT); market reviews where landlord selects the valuer; "
            "CPI referencing a non-standard index.\n\n"
            "Lower-risk: CPI reviews referencing the correct capital city All Groups index; "
            "lesser of CPI or fixed percentage (e.g. capped at 4%); market reviews with neutral "
            "jointly-appointed valuer.\n\n"
            "ABS CPI series codes: Sydney A2325846C, Melbourne A2325850T, Brisbane A2325854A, "
            "Adelaide A2325858J, Perth A2325862B, Hobart A2325866L, Darwin A2325870F, "
            "Canberra A2325874P."
        ),
    },
    {
        "jurisdiction": "ALL",
        "source_version": "ALL_BESTPRACTICE_2026-06",
        "title": "Australian Consumer Law -- Unfair Contract Terms in Commercial Leases",
        "text": (
            "Australian Consumer Law (ACL) -- Unfair contract terms: application to leases\n"
            "Since November 2023, the UCT regime applies to standard form contracts with small "
            "businesses (turnover < AUD 10M or < 100 employees).\n\n"
            "A term is unfair if it: (a) causes significant imbalance in parties' rights; "
            "(b) is not reasonably necessary to protect the advantaged party's legitimate "
            "interests; and (c) would cause detriment to the other party.\n\n"
            "Lease terms most likely to attract scrutiny: unilateral rent increases with no "
            "ceiling; termination rights heavily weighted to the landlord; unlimited personal "
            "guarantees; outgoings with no cap or audit right; relocation with inadequate "
            "notice; make good requiring full restoration regardless of consent.\n\n"
            "If declared unfair, the term is void. Tenants should flag UCT risk on any "
            "non-negotiated standard-form lease."
        ),
    },
]


def _get_existing_versions(client) -> set:
    """Return set of source_version strings already in the DB."""
    result = client.table("lease_chunks").select("source_version").eq(
        "chunk_type", "legislation"
    ).execute()
    return {r["source_version"] for r in (result.data or []) if r.get("source_version")}


def load_rules_to_supabase(client, force: bool = False) -> int:
    """
    Load red_flags.yaml rules as 'rule' chunks into Supabase.
    Embeds and inserts one rule at a time to avoid VoyageAI batch timeouts.
    --force deletes all existing rule rows before re-inserting (no duplicates).
    """
    rules_path = ROOT / "rules" / "red_flags.yaml"
    if not rules_path.exists():
        logger.error(f"Rules file not found: {rules_path}")
        return 0

    with open(rules_path) as f:
        data = yaml.safe_load(f)
    rules = data.get("rules", [])

    # Check what's already loaded (by source_version, not just count)
    existing_result = client.table("lease_chunks").select("source_version").eq("chunk_type", "rule").execute()
    existing_versions = {r["source_version"] for r in (existing_result.data or []) if r.get("source_version")}

    if force:
        # Wipe all rule rows first so we get a clean slate with no duplicates
        client.table("lease_chunks").delete().eq("chunk_type", "rule").execute()
        logger.info("Deleted existing rule chunks (--force mode)")
        existing_versions = set()

    to_load = [r for r in rules if f"RULES_{r['id']}_2026-06" not in existing_versions]

    if not to_load:
        logger.info(f"All {len(rules)} rules already loaded. Use --force to reload.")
        return 0

    logger.info(f"Loading {len(to_load)} rules one at a time (avoids embedding timeout)...")
    loaded = 0
    for rule in to_load:
        jurs = rule.get("jurisdictions", ["ALL"])
        text = (
            f"RISK RULE: {rule['name']}\n"
            f"Severity: {rule['severity']}\n"
            f"Jurisdictions: {', '.join(jurs)}\n"
            f"Description: {rule['description'].strip()}\n"
            f"Trigger keywords: {', '.join(rule.get('trigger_keywords', []))}\n"
            f"Legislation reference: {rule.get('legislation_ref') or 'Not specified'}\n"
            f"Recommended action: {rule.get('recommended_action', '').strip()}"
        )
        try:
            embeddings = embed_texts([text], input_type="document")
            client.table("lease_chunks").insert({
                "content": text,
                "embedding": embeddings[0],
                "metadata": {
                    "rule_id": rule["id"],
                    "rule_name": rule["name"],
                    "severity": rule["severity"],
                    "jurisdictions": jurs,
                },
                "chunk_type": "rule",
                "jurisdiction": None,
                "source_version": "RULES_{}_2026-06".format(rule["id"]),
            }).execute()
            logger.info(f"  Loaded {rule['id']}: {rule['name']}")
            loaded += 1
        except Exception as e:
            logger.error(f"  FAILED {rule['id']}: {e}")

    logger.info(f"Loaded {loaded}/{len(to_load)} rules")
    return loaded


def load_legislation_to_supabase(client, force: bool = False) -> int:
    """Load legislation chunks. Skips source_versions already present."""
    existing = _get_existing_versions(client)
    to_load = [leg for leg in LEGISLATION if force or leg["source_version"] not in existing]

    if not to_load:
        logger.info(f"All {len(LEGISLATION)} legislation chunks already loaded. Use --force to reload.")
        return 0

    skipped = len(LEGISLATION) - len(to_load)
    if skipped:
        logger.info(f"Skipping {skipped} already-loaded. Loading {len(to_load)} new chunks...")
    else:
        logger.info(f"Loading {len(to_load)} legislation chunks...")

    texts = [f"{leg['title']}\n\n{leg['text'].strip()}" for leg in to_load]
    embeddings = embed_texts(texts, input_type="document")

    insert_rows = []
    for i, leg in enumerate(to_load):
        jur = leg["jurisdiction"]
        insert_rows.append({
            "content": texts[i],
            "embedding": embeddings[i],
            "metadata": {"title": leg["title"], "jurisdiction": jur, "source_version": leg["source_version"]},
            "chunk_type": "legislation",
            "jurisdiction": jur if jur != "ALL" else None,
            "source_version": leg["source_version"],
        })

    client.table("lease_chunks").insert(insert_rows).execute()
    logger.info(f"Loaded {len(insert_rows)} legislation chunks")
    return len(insert_rows)


def verify_load(client) -> None:
    from collections import Counter
    result = client.table("lease_chunks").select("chunk_type, jurisdiction, source_version").execute()
    counts = Counter(
        (r["chunk_type"], r["jurisdiction"] or "ALL", r.get("source_version") or "unversioned")
        for r in result.data
    )
    logger.info("Vector store contents:")
    for (ctype, jur, ver), count in sorted(counts.items()):
        logger.info(f"  {ctype:15s} | {jur:5s} | {ver:35s} | {count} chunks")



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load TenantSentry KB into Supabase")
    parser.add_argument("--force", action="store_true", help="Re-load even if already exists")
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--legislation-only", action="store_true")
    args = parser.parse_args()

    logger.info("=== TenantSentry KB Loader ===")
    client = get_client()
    rules_loaded, leg_loaded = 0, 0

    if not args.legislation_only:
        rules_loaded = load_rules_to_supabase(client, force=args.force)
    if not args.rules_only:
        leg_loaded = load_legislation_to_supabase(client, force=args.force)

    msg = "Complete: {} rules + {} legislation chunks loaded".format(rules_loaded, leg_loaded)
    logger.info(msg)
    verify_load(client)
