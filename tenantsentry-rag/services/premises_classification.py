"""
premises_classification.py
---------------------------
AQ-NEW-5: Determine the applicable statute for a commercial lease based on:
  - premises_use:  "retail" | "office" | "industrial" | "mixed" | "other"
  - jurisdiction:  State code — NSW, VIC, QLD, SA, WA, TAS, ACT, NT
  - gla_sqm:       Gross lettable area in sqm (optional — None if unknown)
  - entity_type:   "individual" | "company" | "trust" | "government"

Returns a PremisesClassification dataclass with:
  - applicable_statute:  Full name of the governing act (used in LLM prompts)
  - statute_code:        Short code for DB storage / filtering
  - is_retail:           True if retail tenancies legislation applies
  - classification_note: Plain-English explanation shown in audit report / UI

Without this classification the engine cannot distinguish retail lease protection
(e.g. VIC Retail Leases Act 2003 caps make-good, prohibits land tax pass-through)
from plain commercial leases where those protections do not apply.  A retail tenant
misclassified as commercial loses ALL statutory protections.

State thresholds (area triggers for retail legislation):
  VIC  — Retail Leases Act 2003:                  no GLA cap (area > 1,000 sqm needs ministerial order, but Act still applies to retail shops)
  NSW  — Retail Leases Act 1994:                  no GLA cap
  QLD  — Retail Shop Leases Act 1994:             no GLA cap (applies to any retail shop)
  WA   — Commercial Tenancy (Retail Shops) Agreements Act 1985: no GLA cap for retail shops
  SA   — Retail and Commercial Leases Act 1995:   GLA < 1,000 sqm OR rent < $400k/yr
  ACT  — Leases (Commercial and Retail) Act 2001: no GLA cap
  TAS  — Fair Trading (Code of Practice for Retail Tenancies) Act 1998: code of practice only
  NT   — Business Tenancies (Fair Dealings) Act 2003: no GLA cap

Government tenants are typically excluded from retail legislation in most states.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class PremisesClassification:
    applicable_statute: str      # Full act name — injected into LLM prompts
    statute_code: str            # Short code for DB: "retail_vic" | "commercial_wa" etc.
    is_retail: bool              # True = retail act applies
    classification_note: str     # Plain-English rationale


# ── Per-jurisdiction lookup ──────────────────────────────────────────────────

def _classify_vic(premises_use: str, gla_sqm: Optional[float], entity_type: str) -> PremisesClassification:
    """
    VIC: Retail Leases Act 2003.
    Applies to any retail shop regardless of GLA (ministerial order needed to
    exclude large-format retail > 1,000 sqm, rare in practice).
    Government tenants are excluded.
    """
    if entity_type == "government":
        return PremisesClassification(
            applicable_statute="Commercial Tenancy Law (VIC) — Common Law",
            statute_code="commercial_vic",
            is_retail=False,
            classification_note=(
                "Government tenant — excluded from Retail Leases Act 2003 (VIC). "
                "Lease governed by common law and any applicable Crown lease provisions."
            ),
        )
    if premises_use in ("retail", "mixed"):
        return PremisesClassification(
            applicable_statute="Retail Leases Act 2003 (VIC)",
            statute_code="retail_vic",
            is_retail=True,
            classification_note=(
                "Retail use in VIC — Retail Leases Act 2003 applies. "
                "Key protections: no make-good for fair wear and tear, land tax cannot be passed to tenant, "
                "minimum 5-year term, disclosure statement obligations on landlord."
            ),
        )
    return PremisesClassification(
        applicable_statute="Commercial Tenancy Law (VIC) — Common Law + Property Law Act 1958 (VIC)",
        statute_code="commercial_vic",
        is_retail=False,
        classification_note=(
            "Non-retail use in VIC — Retail Leases Act 2003 does NOT apply. "
            "Lease governed by common law and Property Law Act 1958 (VIC). "
            "Statutory tenant protections are significantly reduced."
        ),
    )


def _classify_nsw(premises_use: str, gla_sqm: Optional[float], entity_type: str) -> PremisesClassification:
    """
    NSW: Retail Leases Act 1994.
    Applies to any retail shop. No GLA threshold.
    Government tenants and certain leases (e.g. > 1,000 sqm in non-retail-shopping-centre) may be excluded.
    """
    if entity_type == "government":
        return PremisesClassification(
            applicable_statute="Commercial Tenancy Law (NSW) — Common Law + Conveyancing Act 1919 (NSW)",
            statute_code="commercial_nsw",
            is_retail=False,
            classification_note=(
                "Government tenant — excluded from Retail Leases Act 1994 (NSW). "
                "Lease governed by Conveyancing Act 1919 (NSW) and common law."
            ),
        )
    if premises_use in ("retail", "mixed"):
        return PremisesClassification(
            applicable_statute="Retail Leases Act 1994 (NSW)",
            statute_code="retail_nsw",
            is_retail=True,
            classification_note=(
                "Retail use in NSW — Retail Leases Act 1994 applies. "
                "Key protections: 5-year minimum term, outgoings disclosure, "
                "compensation for fit-out if landlord terminates, no land tax recovery."
            ),
        )
    return PremisesClassification(
        applicable_statute="Commercial Tenancy Law (NSW) — Common Law + Conveyancing Act 1919 (NSW)",
        statute_code="commercial_nsw",
        is_retail=False,
        classification_note=(
            "Non-retail use in NSW — Retail Leases Act 1994 does NOT apply. "
            "Lease governed by Conveyancing Act 1919 (NSW) and common law."
        ),
    )


def _classify_qld(premises_use: str, gla_sqm: Optional[float], entity_type: str) -> PremisesClassification:
    """
    QLD: Retail Shop Leases Act 1994.
    Applies to any retail shop open to the public. No GLA cap.
    """
    if entity_type == "government":
        return PremisesClassification(
            applicable_statute="Commercial Tenancy Law (QLD) — Common Law + Property Law Act 1974 (QLD)",
            statute_code="commercial_qld",
            is_retail=False,
            classification_note=(
                "Government tenant — excluded from Retail Shop Leases Act 1994 (QLD). "
                "Lease governed by Property Law Act 1974 (QLD) and common law."
            ),
        )
    if premises_use in ("retail", "mixed"):
        return PremisesClassification(
            applicable_statute="Retail Shop Leases Act 1994 (QLD)",
            statute_code="retail_qld",
            is_retail=True,
            classification_note=(
                "Retail use in QLD — Retail Shop Leases Act 1994 applies. "
                "Key protections: disclosure obligations, dispute resolution via QCAT, "
                "assignment rights, renewal options."
            ),
        )
    return PremisesClassification(
        applicable_statute="Commercial Tenancy Law (QLD) — Common Law + Property Law Act 1974 (QLD)",
        statute_code="commercial_qld",
        is_retail=False,
        classification_note=(
            "Non-retail use in QLD — Retail Shop Leases Act 1994 does NOT apply. "
            "Lease governed by Property Law Act 1974 (QLD) and common law."
        ),
    )


def _classify_wa(premises_use: str, gla_sqm: Optional[float], entity_type: str) -> PremisesClassification:
    """
    WA: Commercial Tenancy (Retail Shops) Agreements Act 1985.
    Applies to retail shops. No GLA cap.
    Government tenants and Crown leases are excluded.
    """
    if entity_type == "government":
        return PremisesClassification(
            applicable_statute="Commercial Tenancy Act 1985 (WA) — Common Law + Property Law Act 1969 (WA)",
            statute_code="commercial_wa",
            is_retail=False,
            classification_note=(
                "Government tenant in WA — Commercial Tenancy (Retail Shops) Agreements Act 1985 does NOT apply. "
                "Lease governed by Property Law Act 1969 (WA) and common law. "
                "Crown and local government leases operate under separate statutory frameworks."
            ),
        )
    if premises_use in ("retail", "mixed"):
        return PremisesClassification(
            applicable_statute="Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA)",
            statute_code="retail_wa",
            is_retail=True,
            classification_note=(
                "Retail use in WA — Commercial Tenancy (Retail Shops) Agreements Act 1985 applies. "
                "Key protections: landlord cannot pass capital costs to tenant (s.11), "
                "disclosure obligations, CTRS registration requirements."
            ),
        )
    return PremisesClassification(
        applicable_statute="Commercial Tenancy Act 1985 (WA) — Common Law + Property Law Act 1969 (WA)",
        statute_code="commercial_wa",
        is_retail=False,
        classification_note=(
            "Non-retail use in WA — Commercial Tenancy (Retail Shops) Agreements Act 1985 does NOT apply. "
            "Lease governed by Property Law Act 1969 (WA) and common law."
        ),
    )


def _classify_sa(premises_use: str, gla_sqm: Optional[float], entity_type: str) -> PremisesClassification:
    """
    SA: Retail and Commercial Leases Act 1995.
    Applies to retail shops with GLA < 1,000 sqm OR annual rent < $400,000.
    (Unlike other states, SA RCLA covers both retail and commercial premises.)
    """
    if entity_type == "government":
        return PremisesClassification(
            applicable_statute="Commercial Tenancy Law (SA) — Common Law + Law of Property Act 1936 (SA)",
            statute_code="commercial_sa",
            is_retail=False,
            classification_note=(
                "Government tenant in SA — excluded from Retail and Commercial Leases Act 1995. "
                "Lease governed by Law of Property Act 1936 (SA) and common law."
            ),
        )
    if premises_use in ("retail", "mixed"):
        # GLA threshold check
        if gla_sqm is not None and gla_sqm >= 1000:
            return PremisesClassification(
                applicable_statute="Commercial Tenancy Law (SA) — Common Law + Law of Property Act 1936 (SA)",
                statute_code="commercial_sa",
                is_retail=False,
                classification_note=(
                    f"Retail use in SA but GLA ({gla_sqm:.0f} sqm) ≥ 1,000 sqm threshold — "
                    "Retail and Commercial Leases Act 1995 does NOT apply unless annual rent < $400,000. "
                    "Lease likely governed by common law. Verify annual rent to confirm."
                ),
            )
        return PremisesClassification(
            applicable_statute="Retail and Commercial Leases Act 1995 (SA)",
            statute_code="retail_sa",
            is_retail=True,
            classification_note=(
                "Retail use in SA with GLA < 1,000 sqm — Retail and Commercial Leases Act 1995 applies. "
                "Key protections: disclosure obligations, key money prohibition, assignment rights."
            ),
        )
    # Non-retail in SA — RCLA may still apply if it's a 'commercial shop'
    return PremisesClassification(
        applicable_statute="Commercial Tenancy Law (SA) — Common Law + Law of Property Act 1936 (SA)",
        statute_code="commercial_sa",
        is_retail=False,
        classification_note=(
            "Non-retail use in SA — Retail and Commercial Leases Act 1995 does NOT apply. "
            "Lease governed by Law of Property Act 1936 (SA) and common law."
        ),
    )


def _classify_act(premises_use: str, gla_sqm: Optional[float], entity_type: str) -> PremisesClassification:
    """
    ACT: Leases (Commercial and Retail) Act 2001.
    Applies to retail leases regardless of GLA.
    """
    if entity_type == "government":
        return PremisesClassification(
            applicable_statute="Commercial Tenancy Law (ACT) — Common Law + Civil Law (Property) Act 2006 (ACT)",
            statute_code="commercial_act",
            is_retail=False,
            classification_note=(
                "Government tenant in ACT — excluded from Leases (Commercial and Retail) Act 2001. "
                "Lease governed by Civil Law (Property) Act 2006 (ACT) and common law."
            ),
        )
    if premises_use in ("retail", "mixed"):
        return PremisesClassification(
            applicable_statute="Leases (Commercial and Retail) Act 2001 (ACT)",
            statute_code="retail_act",
            is_retail=True,
            classification_note=(
                "Retail use in ACT — Leases (Commercial and Retail) Act 2001 applies. "
                "Key protections: disclosure obligations, security of tenure, dispute resolution via ACAT."
            ),
        )
    return PremisesClassification(
        applicable_statute="Commercial Tenancy Law (ACT) — Common Law + Civil Law (Property) Act 2006 (ACT)",
        statute_code="commercial_act",
        is_retail=False,
        classification_note=(
            "Non-retail use in ACT — Leases (Commercial and Retail) Act 2001 does NOT apply. "
            "Lease governed by Civil Law (Property) Act 2006 (ACT) and common law."
        ),
    )


def _classify_tas(premises_use: str, gla_sqm: Optional[float], entity_type: str) -> PremisesClassification:
    """
    TAS: No comprehensive retail leases act.
    Fair Trading (Code of Practice for Retail Tenancies) Act 1998 provides a code of practice
    but is not a full statutory protection regime like other states.
    """
    if premises_use in ("retail", "mixed"):
        return PremisesClassification(
            applicable_statute="Fair Trading (Code of Practice for Retail Tenancies) Act 1998 (TAS) + Common Law",
            statute_code="retail_tas",
            is_retail=True,
            classification_note=(
                "Retail use in TAS — Fair Trading (Code of Practice for Retail Tenancies) Act 1998 applies. "
                "NOTE: TAS has the weakest retail tenant protections of any state — this is a code of practice, "
                "not a full retail leases act. Protections are significantly limited compared to VIC/NSW/QLD. "
                "Lease is primarily governed by common law and the Landlord and Tenant Act 1935 (TAS)."
            ),
        )
    return PremisesClassification(
        applicable_statute="Commercial Tenancy Law (TAS) — Common Law + Landlord and Tenant Act 1935 (TAS)",
        statute_code="commercial_tas",
        is_retail=False,
        classification_note=(
            "Non-retail use in TAS — lease governed by common law and Landlord and Tenant Act 1935 (TAS)."
        ),
    )


def _classify_nt(premises_use: str, gla_sqm: Optional[float], entity_type: str) -> PremisesClassification:
    """
    NT: Business Tenancies (Fair Dealings) Act 2003.
    Applies to retail and certain commercial tenancies.
    """
    if entity_type == "government":
        return PremisesClassification(
            applicable_statute="Commercial Tenancy Law (NT) — Common Law + Law of Property Act 2000 (NT)",
            statute_code="commercial_nt",
            is_retail=False,
            classification_note=(
                "Government tenant in NT — excluded from Business Tenancies (Fair Dealings) Act 2003. "
                "Lease governed by Law of Property Act 2000 (NT) and common law."
            ),
        )
    if premises_use in ("retail", "mixed"):
        return PremisesClassification(
            applicable_statute="Business Tenancies (Fair Dealings) Act 2003 (NT)",
            statute_code="retail_nt",
            is_retail=True,
            classification_note=(
                "Retail use in NT — Business Tenancies (Fair Dealings) Act 2003 applies. "
                "Key protections: disclosure obligations, dispute resolution via NT Civil and Administrative Tribunal."
            ),
        )
    return PremisesClassification(
        applicable_statute="Commercial Tenancy Law (NT) — Common Law + Law of Property Act 2000 (NT)",
        statute_code="commercial_nt",
        is_retail=False,
        classification_note=(
            "Non-retail use in NT — Business Tenancies (Fair Dealings) Act 2003 does NOT apply. "
            "Lease governed by Law of Property Act 2000 (NT) and common law."
        ),
    )


_CLASSIFIERS = {
    "VIC": _classify_vic,
    "NSW": _classify_nsw,
    "QLD": _classify_qld,
    "WA":  _classify_wa,
    "SA":  _classify_sa,
    "ACT": _classify_act,
    "TAS": _classify_tas,
    "NT":  _classify_nt,
}


# ── Public API ───────────────────────────────────────────────────────────────

def classify_premises(
    premises_use: str,
    jurisdiction: str,
    gla_sqm: Optional[float] = None,
    entity_type: str = "company",
) -> PremisesClassification:
    """
    Determine the applicable statute for a lease given premises metadata.

    Args:
        premises_use:  "retail" | "office" | "industrial" | "mixed" | "other"
        jurisdiction:  State code — NSW, VIC, QLD, SA, WA, TAS, ACT, NT
        gla_sqm:       Gross lettable area in sqm (optional — affects SA threshold)
        entity_type:   "individual" | "company" | "trust" | "government"

    Returns:
        PremisesClassification with applicable_statute, statute_code, is_retail,
        and classification_note.
    """
    jur = jurisdiction.upper().strip()
    use = (premises_use or "other").lower().strip()
    etype = (entity_type or "company").lower().strip()

    classifier = _CLASSIFIERS.get(jur)
    if classifier is None:
        return PremisesClassification(
            applicable_statute=f"Commercial Tenancy Law ({jur}) — Common Law",
            statute_code=f"commercial_{jur.lower()}",
            is_retail=False,
            classification_note=(
                f"Unknown jurisdiction '{jur}' — defaulting to common law commercial tenancy. "
                "Verify applicable legislation with a local solicitor."
            ),
        )

    return classifier(use, gla_sqm, etype)


# ── Prompt injection helper ──────────────────────────────────────────────────

def build_statute_prompt_block(classification: PremisesClassification) -> str:
    """
    Build a formatted block to inject into LLM clause analysis prompts.
    Placed immediately after the jurisdiction constraint so the model knows
    exactly which act governs this lease before it analyses any clause.
    """
    retail_flag = "YES — retail tenancy legislation applies" if classification.is_retail else "NO — common law commercial tenancy"
    return "\n".join([
        "PREMISES CLASSIFICATION:",
        f"  Applicable statute:  {classification.applicable_statute}",
        f"  Retail legislation:  {retail_flag}",
        f"  Note: {classification.classification_note}",
        "",
        "When analysing clauses, apply the protections and prohibitions of the above statute.",
        "Do NOT apply retail lease protections if retail legislation does NOT apply to this lease.",
    ])
