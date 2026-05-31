"""
load_kb.py
----------
One-time script to populate Supabase with:
  1. Red flag rules from rules/red_flags.yaml  (chunk_type='rule')
  2. Australian commercial lease legislation text (chunk_type='legislation')

Run once before launching the app:
    cd tenantsentry-rag
    python scripts/load_kb.py

Then verify in Supabase SQL editor:
    SELECT chunk_type, jurisdiction, COUNT(*) FROM lease_chunks GROUP BY 1, 2;
"""

import sys
import os
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import yaml
from loguru import logger
from embedding.embedder import embed_texts
from vector_store.supabase_store import get_client

# ── Jurisdiction list for legislation loading ─────────────────────────────────
ALL_JURISDICTIONS = ["NSW", "VIC", "QLD", "SA", "WA", "TAS", "ACT", "NT"]

# ── Legislation text ──────────────────────────────────────────────────────────
# Core provisions from Australian state Retail Leases Acts.
# This is the grounding context Claude uses when analysing each clause.
# Expand this as you add more states and more legislation sections.

LEGISLATION = [
    {
        "jurisdiction": "NSW",
        "title": "Retail Leases Act 1994 (NSW) — Outgoings",
        "text": """
Retail Leases Act 1994 (NSW) — Section 12: Outgoings
A retail shop lease must not require a tenant to pay outgoings unless the lease
specifies the outgoings, or each type of outgoing, that the tenant is required to pay
and the basis on which the lessor's estimate of those outgoings is calculated.
Capital expenditure items (structural repairs, roof replacement, plant and equipment
replacement) cannot be included in outgoings recoverable from tenants.
Land tax charged to a tenant must be calculated on a single-holding basis — the
landlord's whole portfolio cannot be used to calculate land tax for a single tenancy.
""",
    },
    {
        "jurisdiction": "NSW",
        "title": "Retail Leases Act 1994 (NSW) — Rent Review",
        "text": """
Retail Leases Act 1994 (NSW) — Section 35: Rent review
Ratchet clauses that prevent rent from decreasing on a market rent review are void.
Market rent reviews must be conducted by a specialist retail valuer if the parties
cannot agree. The tenant has the right to refer a market rent dispute to mediation.
CPI rent reviews must reference the Consumer Price Index for the capital city of the
state in which the premises are located, using the most recently published index number.
""",
    },
    {
        "jurisdiction": "NSW",
        "title": "Retail Leases Act 1994 (NSW) — Make Good",
        "text": """
Retail Leases Act 1994 (NSW) — Section 16: Condition of premises on termination
Make-good obligations must exclude fair wear and tear. A landlord cannot require a
tenant to restore the premises to their original condition if the changes were made
with the landlord's consent. A condition report at lease commencement is recommended
to establish the baseline condition of the premises.
""",
    },
    {
        "jurisdiction": "NSW",
        "title": "Retail Leases Act 1994 (NSW) — Assignment",
        "text": """
Retail Leases Act 1994 (NSW) — Section 41: Assignment of retail shop leases
A landlord cannot unreasonably withhold consent to an assignment of a retail shop lease.
The landlord must respond to an assignment request within 28 days. If no response is
received within 28 days, consent is deemed to have been given. Landlord consent may
be withheld on reasonable grounds such as the proposed assignee's financial capacity
or business experience, but cannot be withheld solely to extract a premium.
""",
    },
    {
        "jurisdiction": "VIC",
        "title": "Retail Leases Act 2003 (VIC) — Land Tax",
        "text": """
Retail Leases Act 2003 (VIC) — Section 23: Land tax
A retail premises lease must not require the tenant to pay land tax. Land tax is
expressly prohibited as a recoverable outgoing in Victoria. Any lease clause requiring
a tenant to pay land tax (directly or as part of outgoings) is void and of no effect.
This is one of the strongest tenant protections in the Australian retail leasing framework.
""",
    },
    {
        "jurisdiction": "VIC",
        "title": "Retail Leases Act 2003 (VIC) — Outgoings",
        "text": """
Retail Leases Act 2003 (VIC) — Outgoings and capital expenditure
Landlords in Victoria cannot recover capital expenditure from tenants through outgoings.
Capital expenditure includes: structural repairs, roof replacement, air conditioning
plant replacement, lifts, and building facade works. Only operational outgoings
(cleaning, security, insurance, general maintenance) are recoverable.
Annual outgoings statements must be provided within 3 months of the end of each year.
""",
    },
    {
        "jurisdiction": "VIC",
        "title": "Retail Leases Act 2003 (VIC) — Rent Review",
        "text": """
Retail Leases Act 2003 (VIC) — Section 35: Rent review
Ratchet clauses are void in Victoria. A rent review cannot result in a rent lower than
the rent payable immediately before the review only if the lease specifies this and it
is a fixed increase review. Market rent reviews allow downward adjustment.
Tenants have the right to dispute rent review calculations through the Victorian Small
Business Commission (VSBC).
""",
    },
    {
        "jurisdiction": "QLD",
        "title": "Retail Shop Leases Act 1994 (QLD) — Outgoings",
        "text": """
Retail Shop Leases Act 1994 (QLD) — Outgoings
Queensland requires landlords to provide an annual registered auditor's report on
outgoings within 1 month of the end of each lease year. Tenants have the right to
inspect outgoings records. Capital expenditure cannot be recovered through outgoings.
Land tax can be charged in Queensland but the landlord must disclose this obligation
before the lease is entered into and provide an estimate of the annual amount.
""",
    },
    {
        "jurisdiction": "QLD",
        "title": "Retail Shop Leases Act 1994 (QLD) — Assignment",
        "text": """
Retail Shop Leases Act 1994 (QLD) — Assignment and subletting
A landlord cannot unreasonably withhold consent to assignment in Queensland.
The landlord must respond within a reasonable time (generally 28 days).
A landlord can require that the assignee demonstrates equivalent or better financial
capacity and business experience as the original tenant.
""",
    },
    {
        "jurisdiction": "ALL",
        "title": "Australian Commercial Lease — Personal Guarantee Best Practice",
        "text": """
Personal guarantees in Australian commercial leases — best practice and risk factors
Personal guarantees expose individual directors to unlimited personal liability for
lease obligations. Best practice is to negotiate a cap on personal guarantees.
A guarantee limited to 6-12 months rent equivalent is generally acceptable to landlords.
Guarantees should be time-limited (e.g. expire after year 2 of the lease) where possible.
For corporate tenants with strong balance sheets, personal guarantees may be avoidable.
The guarantee should be released automatically if the tenant's obligations under the
lease are met for the first 2 years without default.
""",
    },
    {
        "jurisdiction": "ALL",
        "title": "Australian Commercial Lease — Make Good Risk Factors",
        "text": """
Make good clauses in Australian commercial leases — risk assessment
Make good obligations are one of the most significant exit costs for commercial tenants.
Excessive make good requirements can cost tens of thousands of dollars on lease expiry.
Key risk factors:
- Requirement to restore to original condition regardless of improvements made
- No exclusion for fair wear and tear
- Obligation to remove landlord-installed fitout
- No cash settlement option in lieu of physical make good
Best practice: commission a condition report at commencement, ensure fair wear and tear
exclusion is explicit, negotiate the right to offer a cash settlement in lieu of works.
""",
    },
    {
        "jurisdiction": "ALL",
        "title": "Australian Commercial Lease — Rent Review Risk Factors",
        "text": """
Rent review mechanisms in Australian commercial leases — risk assessment
High-risk rent review structures:
- Fixed increases above CPI + 2% (e.g. fixed 5% p.a. in a low-inflation environment)
- Ratchet clauses preventing downward review to market (void in NSW and VIC)
- Compounding CPI reviews (applying CPI to an already-inflated base)
- Market reviews where the landlord selects the valuer
Lower-risk structures:
- CPI reviews capped at actual CPI for the relevant capital city
- Market reviews with neutral valuer selection process
- Hybrid: lesser of CPI or fixed percentage
""",
    },
]


def load_rules_to_supabase() -> int:
    """Load red_flags.yaml rules as 'rule' chunks into Supabase."""
    rules_path = ROOT / "rules" / "red_flags.yaml"
    if not rules_path.exists():
        logger.error(f"Rules file not found: {rules_path}")
        return 0

    with open(rules_path) as f:
        data = yaml.safe_load(f)

    rules = data.get("rules", [])
    logger.info(f"Loading {len(rules)} rules into Supabase...")

    texts = []
    rows = []
    for rule in rules:
        text = (
            f"RISK RULE: {rule['name']}\n"
            f"Severity: {rule['severity']}\n"
            f"Description: {rule['description'].strip()}\n"
            f"Trigger keywords: {', '.join(rule.get('trigger_keywords', []))}\n"
            f"Legislation reference: {rule.get('legislation_ref') or 'Not specified'}\n"
            f"Recommended action: {rule.get('recommended_action', '').strip()}"
        )
        texts.append(text)
        rows.append({
            "content": text,
            "metadata": {
                "rule_id": rule["id"],
                "rule_name": rule["name"],
                "severity": rule["severity"],
            },
            "chunk_type": "rule",
            "jurisdiction": None,
        })

    embeddings = embed_texts(texts, input_type="document")

    client = get_client()
    insert_rows = []
    for i, row in enumerate(rows):
        insert_rows.append({
            "content": row["content"],
            "embedding": embeddings[i],
            "metadata": row["metadata"],
            "chunk_type": row["chunk_type"],
            "jurisdiction": row["jurisdiction"],
        })

    client.table("lease_chunks").insert(insert_rows).execute()
    logger.info(f"✓ Loaded {len(insert_rows)} rules")
    return len(insert_rows)


def load_legislation_to_supabase() -> int:
    """Load legislation text as 'legislation' chunks into Supabase."""
    logger.info(f"Loading {len(LEGISLATION)} legislation chunks into Supabase...")

    texts = [leg["text"].strip() for leg in LEGISLATION]
    embeddings = embed_texts(texts, input_type="document")

    client = get_client()
    insert_rows = []
    for i, leg in enumerate(LEGISLATION):
        insert_rows.append({
            "content": f"{leg['title']}\n\n{leg['text'].strip()}",
            "embedding": embeddings[i],
            "metadata": {"title": leg["title"], "jurisdiction": leg["jurisdiction"]},
            "chunk_type": "legislation",
            "jurisdiction": leg["jurisdiction"] if leg["jurisdiction"] != "ALL" else None,
        })

    client.table("lease_chunks").insert(insert_rows).execute()
    logger.info(f"✓ Loaded {len(insert_rows)} legislation chunks")
    return len(insert_rows)


def verify_load() -> None:
    """Print a summary of what's in the vector store."""
    client = get_client()
    result = client.table("lease_chunks").select("chunk_type, jurisdiction").execute()
    from collections import Counter
    counts = Counter((r["chunk_type"], r["jurisdiction"] or "ALL") for r in result.data)
    logger.info("Vector store contents:")
    for (chunk_type, jurisdiction), count in sorted(counts.items()):
        logger.info(f"  {chunk_type:15s} | {jurisdiction:5s} | {count} chunks")


if __name__ == "__main__":
    logger.info("=== TenantSentry KB Loader ===")
    rules_loaded = load_rules_to_supabase()
    leg_loaded = load_legislation_to_supabase()
    logger.info(f"\nComplete: {rules_loaded} rules + {leg_loaded} legislation chunks loaded")
    verify_load()
