"""
lease_metadata_extractor.py
---------------------------
Extracts key lease metadata (landlord, rent, area, term) from the first few pages
of a commercial lease using Claude Haiku.

Called from audit_pipeline.run_audit() after date extraction.
Results stored as top-level fields on AuditResult:
  landlord_name, base_rent_pa, floor_area_sqm, lease_term_years

Uses Haiku (not Sonnet) -- this is a fast, targeted extraction over a small text window.
Falls back gracefully to None for any field not found.
"""

import os
import json
from typing import Optional
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# Number of chars from the top of the lease to send to the LLM.
# Commercial lease schedules / reference tables (which contain all the key details)
# are almost always in the first 5-8 pages. 8000 chars ~= 4-5 pages of dense A4.
_METADATA_TEXT_CHARS = 8_000

# Valid enum values for normalisation
_VALID_JURISDICTIONS = {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"}
_PREMISES_USE_MAP = {
    "retail": "retail", "shop": "retail", "shopping": "retail", "showroom": "retail",
    "office": "office", "commercial": "office",
    "industrial": "industrial", "warehouse": "industrial", "factory": "industrial",
    "storage": "industrial",
    "mixed": "mixed", "mixed use": "mixed",
    "other": "other", "community": "other", "medical": "other", "childcare": "other",
}
_ENTITY_TYPE_MAP = {
    "company": "company", "pty ltd": "company", "pty. ltd.": "company",
    "corporation": "company", "ltd": "company",
    "individual": "individual", "person": "individual", "sole trader": "individual",
    "trust": "trust", "trustee": "trust",
    "government": "government", "council": "government", "authority": "government",
    "state": "government",
}


def _normalise_premises_use(raw: str) -> str:
    """Map free-text permitted use to our enum. Defaults to 'other'."""
    if not raw:
        return "other"
    lower = raw.strip().lower()
    for key, val in _PREMISES_USE_MAP.items():
        if key in lower:
            return val
    return "other"


def _normalise_entity_type(raw: str) -> str:
    """Map free-text entity description to our enum. Defaults to 'company'."""
    if not raw:
        return "company"
    lower = raw.strip().lower()
    for key, val in _ENTITY_TYPE_MAP.items():
        if key in lower:
            return val
    return "company"


def extract_lease_metadata(
    lease_text: str,
    jurisdiction: str,
    job_id: Optional[str] = None,
) -> dict:
    """
    Extract key lease metadata from the top of the lease document.

    Args:
        lease_text:   Full concatenated lease text (all pages).
        jurisdiction: State code -- NSW, VIC, QLD, WA, etc. Pass "" to auto-detect.
        job_id:       Used for log context only.

    Returns:
        Dict with keys (all Optional):
            landlord_name       -- Landlord entity name
            tenant_name         -- Tenant legal entity name
            base_rent_pa        -- Annual base rent in AUD
            floor_area_sqm      -- Lettable area in square metres
            lease_term_years    -- Initial lease term in years
            state_territory     -- State code (NSW/VIC/QLD/WA/SA/TAS/ACT/NT)
            permitted_use       -- Normalised premises use enum
            permitted_use_raw   -- Raw permitted use text from lease
            tenant_entity_type  -- Normalised entity type enum
            premises_address    -- Premises address / property description
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    mock_mode = os.environ.get("MOCK_MODE", "true").lower() == "true"

    if mock_mode or not api_key or api_key.startswith("sk-ant-your"):
        logger.info(f"[{job_id}] lease_metadata_extractor: MOCK_MODE -- returning empty dict")
        return {}

    try:
        return _extract_via_llm(lease_text, jurisdiction, job_id)
    except Exception as e:
        logger.error(f"[{job_id}] lease_metadata_extractor failed (non-fatal): {e}")
        return {}


def _build_prompt(text_sample: str, jurisdiction: str) -> str:
    jur_hint = jurisdiction if jurisdiction else "unknown -- infer from document"
    lines = [
        "You are an expert Australian commercial lease analyst.",
        "",
        "Extract the following key facts from the lease text below.",
        "These are almost always in the Lease Details / Reference Schedule at the top.",
        "",
        "Return a JSON object with ONLY the fields you find. Omit any field you are not confident about.",
        "",
        "Fields to extract:",
        '- "landlord_name": Full legal name of the landlord/lessor entity (string)',
        '- "tenant_name": Full legal name of the tenant/lessee entity (string)',
        '- "base_rent_pa": Annual base rent in AUD as a number only, no $ or commas (e.g. 120000).',
        '  If stated as monthly multiply by 12; if weekly multiply by 52.',
        '- "floor_area_sqm": Net lettable area in square metres as a number only (e.g. 450.5)',
        '- "lease_term_years": Initial lease term in years as a decimal (e.g. 5.0 or 3.5 for 3yr 6mo)',
        '- "state_territory": AU state/territory code -- one of NSW, VIC, QLD, WA, SA, TAS, ACT, NT.',
        '  Infer from property address, governing law clause, or the act named in the lease.',
        '- "permitted_use": Permitted use of premises as stated in the lease (free text)',
        '- "tenant_entity_type": Legal form of tenant -- one of: "company" (Pty Ltd/Corp),',
        '  "individual" (natural person), "trust" (trustee), "government" (council/authority)',
        '- "premises_address": Full street address or property description of leased premises',
        "",
        "Rules:",
        "- Return ONLY a JSON object, no explanation or markdown",
        "- Omit fields you cannot find or are unsure about",
        "- For base_rent_pa: use the BASE rent only, not gross rent including outgoings",
        "- For landlord_name / tenant_name: use the full legal entity name, not a trading name",
        "- If area is given in sqft, convert to sqm (multiply by 0.0929)",
        '- state_territory hints: "Retail Leases Act 1994" = NSW; "Retail Leases Act 2003" = VIC;',
        '  "Retail Shop Leases Act 1994" = QLD; "Commercial Tenancy Act 1985" = WA',
        "",
        f"Jurisdiction hint: {jur_hint}",
        "",
        "LEASE TEXT (first section):",
        text_sample,
        "",
        "JSON output:",
    ]
    return "\n".join(lines)


def _extract_via_llm(lease_text: str, jurisdiction: str, job_id: Optional[str]) -> dict:
    """Call Claude Haiku to extract metadata. Returns partial dict (missing fields omitted)."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    haiku = os.environ.get("HAIKU_MODEL", "claude-haiku-4-5-20251001")

    text_sample = lease_text[:_METADATA_TEXT_CHARS]
    prompt = _build_prompt(text_sample, jurisdiction)

    response = client.messages.create(
        model=haiku,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    data = json.loads(raw)
    result: dict = {}

    if "landlord_name" in data and isinstance(data["landlord_name"], str):
        result["landlord_name"] = data["landlord_name"].strip()

    if "tenant_name" in data and isinstance(data["tenant_name"], str):
        result["tenant_name"] = data["tenant_name"].strip()

    if "base_rent_pa" in data:
        try:
            result["base_rent_pa"] = float(str(data["base_rent_pa"]).replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            pass

    if "floor_area_sqm" in data:
        try:
            result["floor_area_sqm"] = float(str(data["floor_area_sqm"]).replace(",", ""))
        except (ValueError, TypeError):
            pass

    if "lease_term_years" in data:
        try:
            result["lease_term_years"] = float(data["lease_term_years"])
        except (ValueError, TypeError):
            pass

    raw_state = data.get("state_territory", "")
    if raw_state and isinstance(raw_state, str):
        state_upper = raw_state.strip().upper()
        if state_upper in _VALID_JURISDICTIONS:
            result["state_territory"] = state_upper

    raw_use = data.get("permitted_use", "")
    if raw_use and isinstance(raw_use, str):
        result["permitted_use"] = _normalise_premises_use(raw_use)
        result["permitted_use_raw"] = raw_use.strip()

    raw_entity = data.get("tenant_entity_type", "")
    if raw_entity and isinstance(raw_entity, str):
        result["tenant_entity_type"] = _normalise_entity_type(raw_entity)

    raw_addr = data.get("premises_address", "")
    if raw_addr and isinstance(raw_addr, str):
        result["premises_address"] = raw_addr.strip()

    logger.info(
        "[%s] lease_metadata: landlord=%r tenant=%r state=%r use=%r entity=%r "
        "rent=%s area=%ssqm term=%syr address=%r",
        job_id,
        result.get("landlord_name"),
        result.get("tenant_name"),
        result.get("state_territory"),
        result.get("permitted_use"),
        result.get("tenant_entity_type"),
        result.get("base_rent_pa"),
        result.get("floor_area_sqm"),
        result.get("lease_term_years"),
        result.get("premises_address"),
    )
    return result
