"""
lease_metadata_extractor.py
---------------------------
Extracts key lease metadata (landlord, rent, area, term) from the first few pages
of a commercial lease using Claude Haiku.

Called from audit_pipeline.run_audit() after date extraction.
Results stored as top-level fields on AuditResult:
  landlord_name, base_rent_pa, floor_area_sqm, lease_term_years

Uses Haiku (not Sonnet) — this is a fast, targeted extraction over a small text window.
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
# are almost always in the first 5-8 pages. 8000 chars ≈ 4-5 pages of dense A4.
_METADATA_TEXT_CHARS = 8_000


def extract_lease_metadata(
    lease_text: str,
    jurisdiction: str,
    job_id: Optional[str] = None,
) -> dict:
    """
    Extract key lease metadata from the top of the lease document.

    Args:
        lease_text:   Full concatenated lease text (all pages).
        jurisdiction: State code — NSW, VIC, QLD, WA, etc.
        job_id:       Used for log context only.

    Returns:
        Dict with keys (all Optional):
            landlord_name:    str   — Landlord entity name
            base_rent_pa:     float — Annual base rent in AUD (e.g. 120000.0)
            floor_area_sqm:   float — Lettable area in square metres
            lease_term_years: float — Initial lease term in years
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    mock_mode = os.environ.get("MOCK_MODE", "true").lower() == "true"

    if mock_mode or not api_key or api_key.startswith("sk-ant-your"):
        logger.info(f"[{job_id}] lease_metadata_extractor: MOCK_MODE — returning None")
        return {}

    try:
        return _extract_via_llm(lease_text, jurisdiction, job_id)
    except Exception as e:
        logger.error(f"[{job_id}] lease_metadata_extractor failed (non-fatal): {e}")
        return {}


def _extract_via_llm(lease_text: str, jurisdiction: str, job_id: Optional[str]) -> dict:
    """Call Claude Haiku to extract metadata. Returns partial dict (missing fields omitted)."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    haiku = os.environ.get("HAIKU_MODEL", "claude-haiku-4-5-20251001")

    # Only use the first N chars — all key lease details are in the schedule/cover pages
    text_sample = lease_text[:_METADATA_TEXT_CHARS]

    prompt = f"""You are an expert Australian commercial lease analyst.

Extract the following key facts from the lease text below. These are almost always stated in the Lease Details / Reference Schedule at the top of the document.

Return a JSON object with ONLY the fields you find. Omit any field you are not confident about.

Fields to extract:
- "landlord_name": Full legal name of the landlord/lessor entity (string)
- "base_rent_pa": Annual base rent in Australian dollars as a number only, no $ or commas (e.g. 120000). If stated as monthly, multiply by 12. If stated as weekly, multiply by 52.
- "floor_area_sqm": Net lettable area / floor area in square metres as a number only (e.g. 450.5)
- "lease_term_years": Initial lease term in years as a decimal number (e.g. 5.0 or 3.5 for "3 years 6 months")

Rules:
- Return ONLY a JSON object, no explanation or markdown
- Omit fields you cannot find or are unsure about
- For base_rent_pa: use the BASE rent only, not gross rent including outgoings
- For landlord_name: use the full legal entity name, not a trading name
- If area is given in sqft, convert to sqm (1 sqft = 0.0929 sqm)

Jurisdiction: {jurisdiction}

LEASE TEXT (first section):
{text_sample}

JSON output:"""

    response = client.messages.create(
        model=haiku,
        max_tokens=256,
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

    logger.info(f"[{job_id}] lease_metadata: landlord={result.get('landlord_name')!r} "
                f"rent={result.get('base_rent_pa')} area={result.get('floor_area_sqm')}sqm "
                f"term={result.get('lease_term_years')}yr")
    return result
