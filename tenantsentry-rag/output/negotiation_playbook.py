"""
negotiation_playbook.py
-----------------------
AQ-NEW-4: Negotiation Playbook Output Module

Extracts and formats all negotiation positions and ready-to-send email templates
from a completed AuditResult, producing a structured playbook the tenant (or their
advisor) can act on immediately.

Data source: negotiation_position and negotiation_email fields from each risk_flag
in clause_analyses. These are populated by router.py for HIGH/VOID/MEDIUM flags.

Public API
----------
    generate_playbook(result: dict) -> PlaybookResult
    playbook_to_dict(pb: PlaybookResult) -> dict
    playbook_to_text(pb: PlaybookResult) -> str      (plain text, no PDF)

DEV/LIVE: This module is pure data transformation — no LLM calls, no external
dependencies. Works transparently in both modes as long as the audit result was
generated with negotiation data (which router.py now always produces).
"""

from dataclasses import dataclass, field
from typing import Optional


# ── Severity ordering for sort ────────────────────────────────────────────────
_SEV_ORDER = {"void": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class PlaybookItem:
    """One actionable negotiation item corresponding to a single risk flag."""
    clause_heading: str
    page_number: Optional[int]
    flag_id: str
    severity: str               # "void" | "high" | "medium"
    confidence: str             # "confirmed" | "probable" | "flag"
    flag_description: str       # What the flag found
    negotiation_position: str   # One-sentence demand
    negotiation_email: str      # Ready-to-send email paragraph
    legislation_ref: Optional[str]
    financial_impact_estimate: Optional[str]
    clause_type: Optional[str]

    @property
    def priority_rank(self) -> int:
        return _SEV_ORDER.get(self.severity.lower(), 3)

    @property
    def source_citation(self) -> str:
        s = self.clause_heading
        if self.page_number:
            s += f", Page {self.page_number}"
        return s


@dataclass
class PlaybookResult:
    """Full negotiation playbook for one audit."""
    tenant_name: str
    jurisdiction: str
    filename: str
    total_flags: int            # All flags across the audit
    items: list[PlaybookItem] = field(default_factory=list)
    summary_stats: dict = field(default_factory=dict)

    @property
    def void_items(self) -> list[PlaybookItem]:
        return [i for i in self.items if i.severity == "void"]

    @property
    def high_items(self) -> list[PlaybookItem]:
        return [i for i in self.items if i.severity == "high"]

    @property
    def medium_items(self) -> list[PlaybookItem]:
        return [i for i in self.items if i.severity == "medium"]

    @property
    def has_items(self) -> bool:
        return bool(self.items)


# ── Core generator ────────────────────────────────────────────────────────────

def generate_playbook(result: dict) -> PlaybookResult:
    """
    Build a PlaybookResult from a serialised AuditResult dict.

    Collects all VOID/HIGH/MEDIUM risk flags that have a negotiation_position
    OR a negotiation_email, sorted by severity then clause order.

    The result dict is the output of AuditResult.model_dump() or equivalent —
    matches the shape returned by the jobs API endpoint.

    Args:
        result: Serialised AuditResult (dict with clause_analyses, all_risk_flags, etc.)

    Returns:
        PlaybookResult — iterate .items for the actionable list
    """
    tenant_name = result.get("tenant_name", "Unknown Tenant")
    jurisdiction = result.get("jurisdiction", "")
    filename     = result.get("filename", "")
    all_flags    = result.get("all_risk_flags", [])

    items: list[PlaybookItem] = []
    seen_flags: set[str] = set()   # dedup by flag_id+clause_heading

    for ca in result.get("clause_analyses", []):
        heading    = ca.get("clause_heading", "Unknown Clause")
        page_num   = ca.get("page_number")
        clause_type = ca.get("clause_type")

        for flag in (ca.get("risk_flags") or []):
            sev = (flag.get("severity") or "low").lower()
            if sev not in ("void", "high", "medium"):
                continue

            # Prefer per-flag negotiation fields (new schema); fall back to clause-level
            position = (
                flag.get("negotiation_position")
                or ca.get("negotiation_position")
                or ""
            )
            email = (
                flag.get("negotiation_email")
                or ca.get("negotiation_email")
                or ""
            )

            # Only include if there is at least one actionable piece of content
            if not position and not email:
                continue

            flag_id   = flag.get("flag_id", "RF?")
            dedup_key = f"{flag_id}::{heading}"
            if dedup_key in seen_flags:
                continue
            seen_flags.add(dedup_key)

            item = PlaybookItem(
                clause_heading=heading,
                page_number=page_num,
                flag_id=flag_id,
                severity=sev,
                confidence=(flag.get("confidence") or "confirmed").lower(),
                flag_description=flag.get("description", ""),
                negotiation_position=position,
                negotiation_email=email,
                legislation_ref=flag.get("legislation_ref"),
                financial_impact_estimate=flag.get("financial_impact_estimate"),
                clause_type=clause_type,
            )
            items.append(item)

    # Sort: void → high → medium, then preserve clause order within severity
    items.sort(key=lambda i: i.priority_rank)

    # Summary stats
    stats = {
        "total_playbook_items": len(items),
        "void_items": sum(1 for i in items if i.severity == "void"),
        "high_items": sum(1 for i in items if i.severity == "high"),
        "medium_items": sum(1 for i in items if i.severity == "medium"),
        "items_with_email_template": sum(1 for i in items if i.negotiation_email),
        "items_with_position": sum(1 for i in items if i.negotiation_position),
        "confirmed_items": sum(1 for i in items if i.confidence == "confirmed"),
        "probable_items": sum(1 for i in items if i.confidence == "probable"),
        "flag_items": sum(1 for i in items if i.confidence == "flag"),
    }

    return PlaybookResult(
        tenant_name=tenant_name,
        jurisdiction=jurisdiction,
        filename=filename,
        total_flags=len(all_flags),
        items=items,
        summary_stats=stats,
    )


def playbook_to_dict(pb: PlaybookResult) -> dict:
    """
    Serialise a PlaybookResult to a plain dict for JSON API responses.

    Returned shape:
    {
      "tenant_name": "...",
      "jurisdiction": "VIC",
      "filename": "...",
      "total_audit_flags": N,
      "summary_stats": {...},
      "playbook_items": [
        {
          "priority": 1,
          "severity": "high",
          "confidence": "confirmed",
          "clause_heading": "...",
          "page_number": 14,
          "flag_id": "RF001",
          "flag_description": "...",
          "legislation_ref": "...",
          "financial_impact_estimate": "~$120k",
          "negotiation_position": "...",
          "negotiation_email": "...",
        },
        ...
      ]
    }
    """
    return {
        "tenant_name": pb.tenant_name,
        "jurisdiction": pb.jurisdiction,
        "filename": pb.filename,
        "total_audit_flags": pb.total_flags,
        "summary_stats": pb.summary_stats,
        "playbook_items": [
            {
                "priority": idx + 1,
                "severity": item.severity,
                "confidence": item.confidence,
                "clause_heading": item.clause_heading,
                "page_number": item.page_number,
                "clause_type": item.clause_type,
                "flag_id": item.flag_id,
                "flag_description": item.flag_description,
                "legislation_ref": item.legislation_ref,
                "financial_impact_estimate": item.financial_impact_estimate,
                "negotiation_position": item.negotiation_position,
                "negotiation_email": item.negotiation_email,
                "source_citation": item.source_citation,
            }
            for idx, item in enumerate(pb.items)
        ],
    }


def playbook_to_text(pb: PlaybookResult) -> str:
    """
    Render a PlaybookResult as plain text for copy-paste or email export.

    Format per item:
    ─────────────────────────────────
    [HIGH RISK] Clause 14 — Make Good
    Flag: RF006 · Page 14
    Exposure: ~$120k–$250k make-good liability
    Legislation: Retail Leases Act 2003 (VIC) s.41

    DEMAND:
    Delete the 'base building condition' requirement and replace with
    standard fair wear and tear carve-out.

    EMAIL TEMPLATE:
    Dear [Landlord's Solicitor],
    ...
    ─────────────────────────────────
    """
    if not pb.has_items:
        return (
            f"NEGOTIATION PLAYBOOK — {pb.tenant_name} ({pb.jurisdiction})\n"
            f"File: {pb.filename}\n\n"
            "No negotiation items identified. All flagged clauses are at LOW severity\n"
            "or do not have specific negotiation positions attached.\n"
        )

    lines = [
        f"NEGOTIATION PLAYBOOK",
        f"Tenant:       {pb.tenant_name}",
        f"Jurisdiction: {pb.jurisdiction}",
        f"File:         {pb.filename}",
        f"Items:        {len(pb.items)} (VOID:{len(pb.void_items)}  "
        f"HIGH:{len(pb.high_items)}  MEDIUM:{len(pb.medium_items)})",
        "",
        "=" * 72,
        "",
    ]

    for idx, item in enumerate(pb.items, 1):
        sev_label = item.severity.upper()
        conf_label = {
            "confirmed": "CONFIRMED",
            "probable":  "PROBABLE — verify with legal advisor",
            "flag":      "VERIFY — do not act without legal advice",
        }.get(item.confidence, item.confidence.upper())

        lines += [
            f"ITEM {idx} of {len(pb.items)}  [{sev_label}]  [{conf_label}]",
            f"Clause:    {item.source_citation}",
            f"Flag:      {item.flag_id}",
        ]
        if item.legislation_ref:
            lines.append(f"Statute:   {item.legislation_ref}")
        if item.financial_impact_estimate:
            lines.append(f"Exposure:  {item.financial_impact_estimate}")
        lines += [
            "",
            f"FINDING:",
            item.flag_description,
            "",
        ]
        if item.negotiation_position:
            lines += [
                "DEMAND:",
                item.negotiation_position,
                "",
            ]
        if item.negotiation_email:
            lines += [
                "EMAIL TEMPLATE:",
                item.negotiation_email,
                "",
            ]
        lines.append("-" * 72)
        lines.append("")

    return "\n".join(lines)


# ── Mock helper (for DEV mode / tests) ────────────────────────────────────────

def mock_playbook_result() -> PlaybookResult:
    """
    DEV mode: returns a realistic synthetic playbook for UI testing.
    Contains one VOID, two HIGH, and one MEDIUM item.
    """
    items = [
        PlaybookItem(
            clause_heading="Clause 3 — Permitted Use",
            page_number=7,
            flag_id="RF_WA002",
            severity="void",
            confidence="confirmed",
            flag_description=(
                "Contracting-out clause purports to exclude the CTRS Act 1985 (WA) entirely. "
                "Under s.27 of the Act, any term purporting to exclude or modify the Act is void."
            ),
            negotiation_position=(
                "Delete clause 3.4(b) in its entirety — any attempt to contract out of the "
                "CTRS Act is void and unenforceable under s.27."
            ),
            negotiation_email=(
                "We refer to Clause 3.4(b) of the Lease. We note that this clause purports to "
                "exclude the operation of the Commercial Tenancy (Retail Shops) Agreements Act 1985 "
                "(WA). We advise that s.27 of the CTRS Act renders any such exclusion void and of "
                "no legal effect. Our client will not execute the Lease with this clause included "
                "and requires its deletion prior to exchange."
            ),
            legislation_ref="CTRS Act 1985 (WA) s.27",
            financial_impact_estimate=None,
            clause_type="other",
        ),
        PlaybookItem(
            clause_heading="Clause 14 — Make Good",
            page_number=28,
            flag_id="RF006",
            severity="high",
            confidence="confirmed",
            flag_description=(
                "Tenant required to restore premises to 'base building condition' on expiry, "
                "with no fair wear and tear exclusion. In a 5-year lease with a $250k fitout, "
                "this obligation could exceed $150k."
            ),
            negotiation_position=(
                "Amend clause 14 to limit make-good to fair wear and tear standard and exclude "
                "any obligation to reinstate tenant-installed partitions, cabling, or floor coverings."
            ),
            negotiation_email=(
                "We refer to Clause 14 of the proposed Lease (Make Good). The current drafting "
                "requires our client to restore the premises to 'base building condition' on expiry "
                "without any carve-out for fair wear and tear. This is uncommercial and inconsistent "
                "with standard practice in the WA commercial market. We request that Clause 14 be "
                "amended to confine the make-good obligation to: (a) removal of tenant-installed "
                "fixtures at the landlord's written election; (b) patching and painting to a "
                "reasonable standard consistent with normal use; and (c) no obligation to reinstate "
                "to base building condition absent specific written agreement at lease commencement."
            ),
            legislation_ref=None,
            financial_impact_estimate="~$80k–$150k make-good liability",
            clause_type="make_good",
        ),
        PlaybookItem(
            clause_heading="Clause 6 — Rent Review",
            page_number=14,
            flag_id="RF002",
            severity="high",
            confidence="confirmed",
            flag_description=(
                "Rent review mechanism includes a ratchet clause preventing any downward adjustment "
                "even where market rent has declined since the previous review date."
            ),
            negotiation_position=(
                "Delete the ratchet provision so that market rent reviews are genuinely bilateral "
                "and may result in an increase or decrease based on market evidence."
            ),
            negotiation_email=(
                "We refer to the rent review mechanism in Clause 6.2 of the Lease. The current "
                "drafting includes a provision that prevents any downward adjustment in rent "
                "following a market rent review ('ratchet clause'). A genuine market rent review "
                "mechanism must allow for rent to increase or decrease in line with market "
                "conditions. We request deletion of the ratchet provision so that future reviews "
                "reflect prevailing market rental values in either direction."
            ),
            legislation_ref=None,
            financial_impact_estimate="~$15k–$40k excess rent over lease term if market softens",
            clause_type="rent_review",
        ),
        PlaybookItem(
            clause_heading="Clause 9 — Outgoings",
            page_number=21,
            flag_id="RF014",
            severity="medium",
            confidence="probable",
            flag_description=(
                "No cap on annual outgoings increases — tenant could face unlimited escalation "
                "in recoverable outgoings without any contractual limit on year-on-year movements."
            ),
            negotiation_position=(
                "Insert an outgoings cap of 5% per annum cumulative so that the tenant's "
                "outgoings exposure is predictable and capped."
            ),
            negotiation_email=(
                "We refer to the outgoings recovery provisions in Clause 9 of the Lease. "
                "The current drafting does not include any cap on the annual increase in "
                "recoverable outgoings charged to our client. We request that Clause 9 be "
                "amended to include a provision capping year-on-year outgoings increases at "
                "5% per annum on a cumulative basis, consistent with market practice for "
                "office and industrial leases in this jurisdiction."
            ),
            legislation_ref=None,
            financial_impact_estimate="~$8k–$20k additional outgoings if uncapped",
            clause_type="outgoings",
        ),
    ]

    stats = {
        "total_playbook_items": len(items),
        "void_items": 1,
        "high_items": 2,
        "medium_items": 1,
        "items_with_email_template": 4,
        "items_with_position": 4,
        "confirmed_items": 3,
        "probable_items": 1,
        "flag_items": 0,
    }

    return PlaybookResult(
        tenant_name="[MOCK] Acme Pty Ltd",
        jurisdiction="WA",
        filename="mock_lease.pdf",
        total_flags=12,
        items=items,
        summary_stats=stats,
    )
