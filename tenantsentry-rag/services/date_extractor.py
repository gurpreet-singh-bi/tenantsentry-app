"""
date_extractor.py
-----------------
Extracts critical dates from a parsed lease document using Claude Sonnet.

Dates extracted power the 12-Month Monitoring feature:
  - Lease expiry / option exercise deadlines → high-urgency alerts
  - CPI / market rent review dates → financial impact alerts
  - Outgoings reconciliation deadlines → overcharge protection
  - Bank guarantee expiry, make good deadlines, rent-free end → exit risk

Called at the end of audit_pipeline.run_audit(), after clause analysis.
Results persisted to lease_dates table via db/lease_date_store.py.

In MOCK_MODE or when Anthropic key is absent, returns a minimal mock set.
"""

import os
import json
from typing import Optional
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# Date types we ask Claude to extract — must match lease_dates CHECK constraint
DATE_TYPE_DESCRIPTIONS = {
    "lease_commencement":          "When the lease begins",
    "lease_expiry":                "When the lease expires (end of initial term)",
    "option_exercise_deadline":    "Last date to exercise each option to renew",
    "rent_review_cpi":             "Annual or periodic CPI rent review date(s)",
    "rent_review_market":          "Market rent review date(s)",
    "rent_review_fixed":           "Fixed-percentage rent increase date(s)",
    "outgoings_reconciliation":    "Annual outgoings reconciliation / EOFY statement due",
    "rent_free_end":               "End of any rent-free or incentive period",
    "fitout_completion_deadline":  "Deadline for tenant fitout to be completed",
    "demolition_notice_window":    "Any window in which landlord may issue demolition notice",
    "bank_guarantee_expiry":       "Expiry date of any bank guarantee provided",
    "make_good_deadline":          "Deadline for completing make-good works on exit",
}

DATE_TYPES_LIST = "\n".join(
    f'  - "{k}": {v}' for k, v in DATE_TYPE_DESCRIPTIONS.items()
)

# Default alert lead-times by date type (days before the date to fire alert)
DEFAULT_ALERT_DAYS: dict[str, int] = {
    "option_exercise_deadline":    180,   # 6 months — missing this is catastrophic
    "lease_expiry":                180,
    "rent_review_cpi":             60,
    "rent_review_market":          90,
    "rent_review_fixed":           60,
    "outgoings_reconciliation":    30,
    "rent_free_end":               30,
    "fitout_completion_deadline":  30,
    "demolition_notice_window":    90,
    "bank_guarantee_expiry":       90,
    "make_good_deadline":          60,
    "lease_commencement":          0,     # informational only
}


def extract_dates(
    lease_text: str,
    jurisdiction: str,
    job_id: Optional[str] = None,
    persist: bool = True,
) -> list[dict]:
    """
    Extract critical dates from the full concatenated lease text.

    Args:
        lease_text:   Full text of the lease (all pages joined).
        jurisdiction: State code — NSW, VIC, QLD, etc.
        job_id:       If provided and persist=True, saves to Supabase lease_dates.
        persist:      Whether to save results to Supabase (default True).

    Returns:
        List of date dicts:
          {
            date_type, date_description, date_value (ISO str | None),
            clause_reference, recurrence, alert_days_before
          }
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    mock_mode = os.environ.get("MOCK_MODE", "true").lower() == "true"

    if mock_mode or not api_key or api_key.startswith("sk-ant-your"):
        logger.info(f"[{job_id}] date_extractor: MOCK_MODE — returning synthetic dates")
        dates = _mock_dates()
    else:
        dates = _extract_via_llm(lease_text, jurisdiction, job_id)

    if persist and job_id and dates:
        _save_to_supabase(job_id, dates)

    return dates


def _extract_via_llm(lease_text: str, jurisdiction: str, job_id: Optional[str]) -> list[dict]:
    """Call Claude Sonnet to extract dates. Returns list of date dicts."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    sonnet = os.environ.get("SONNET_MODEL", "claude-sonnet-4-6")

    # Truncate very long leases — dates are usually in schedules/key details at the top
    # Keep first 12,000 chars (covers most lease schedules) + last 3,000 (schedules at end)
    if len(lease_text) > 15_000:
        text_sample = lease_text[:12_000] + "\n\n[...middle sections omitted...]\n\n" + lease_text[-3_000:]
    else:
        text_sample = lease_text

    prompt = f"""You are an expert Australian commercial lease analyst specialising in {jurisdiction} tenancy law.

Extract all critical dates and deadlines from the lease text below.

For each date found, return an object with these fields:
  - date_type: one of the types listed below (use "other" if none fit)
  - date_description: plain-English label a non-lawyer tenant can understand (e.g. "Option 1 exercise deadline")
  - date_value: the date in ISO format YYYY-MM-DD, or null if only a relative period is stated
  - clause_reference: the clause number where this date appears (e.g. "Clause 4.2(b)"), or null
  - recurrence: null | "annual" | "monthly" — for repeating review dates
  - notes: any important context (e.g. "Must be exercised in writing by registered post")

Valid date_type values:
{DATE_TYPES_LIST}
  - "other": any other important date not covered above

IMPORTANT INSTRUCTIONS:
- Extract EVERY date and deadline — missing one could cost the tenant significant money.
- If a review happens annually (e.g. "CPI review on each anniversary of the commencement date"),
  set recurrence="annual" and date_value to the FIRST occurrence.
- If a date is expressed as a relative period (e.g. "90 days before expiry"), set date_value=null
  and explain in date_description.
- For option exercise windows, extract the DEADLINE (latest date to exercise), not the open date.
- Respond ONLY with a JSON array — no preamble, no markdown fences.

LEASE TEXT:
{text_sample}

Respond with a JSON array only:"""

    logger.info(f"[{job_id}] Extracting dates via {sonnet}...")
    try:
        response = client.messages.create(
            model=sonnet,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
            timeout=60.0,
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if model adds them despite instructions
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        extracted = json.loads(raw)
        if not isinstance(extracted, list):
            raise ValueError(f"Expected list, got {type(extracted)}")
    except (json.JSONDecodeError, ValueError, Exception) as e:
        logger.error(f"[{job_id}] Date extraction LLM error: {e}")
        return []

    # Normalise and attach default alert_days_before
    dates = []
    for item in extracted:
        dtype = item.get("date_type", "other")
        if dtype not in DATE_TYPE_DESCRIPTIONS and dtype != "other":
            dtype = "other"
        dates.append({
            "date_type":         dtype,
            "date_description":  item.get("date_description", ""),
            "date_value":        _parse_date(item.get("date_value")),
            "clause_reference":  item.get("clause_reference"),
            "recurrence":        item.get("recurrence"),
            "alert_days_before": DEFAULT_ALERT_DAYS.get(dtype, 90),
            "notes":             item.get("notes"),
        })

    logger.info(f"[{job_id}] Extracted {len(dates)} dates")
    return dates


def _parse_date(value) -> Optional[str]:
    """Normalise a date value to ISO YYYY-MM-DD string, or return None."""
    if not value:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "n/a", "unknown", "tbd"):
        return None
    # Try ISO format directly
    try:
        from datetime import date
        date.fromisoformat(s[:10])
        return s[:10]
    except ValueError:
        pass
    # Try common Australian formats
    import re
    for fmt in ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%-d %B %Y"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    logger.warning(f"Could not parse date value: {s!r} — storing as null")
    return None


def _save_to_supabase(job_id: str, dates: list[dict]) -> None:
    """Persist extracted dates to Supabase. Silently skips if Supabase not configured."""
    supabase_url = os.environ.get("SUPABASE_URL", "")
    if not supabase_url:
        logger.debug(f"[{job_id}] No SUPABASE_URL — skipping lease_dates persistence")
        return
    try:
        from db.lease_date_store import insert_dates
        insert_dates(job_id, dates)
    except Exception as e:
        logger.error(f"[{job_id}] Failed to persist lease dates: {e}")


def _mock_dates() -> list[dict]:
    """Synthetic dates returned in MOCK_MODE — realistic but fake."""
    return [
        {
            "date_type": "lease_commencement",
            "date_description": "Lease commencement date",
            "date_value": "2024-03-01",
            "clause_reference": "Item 1",
            "recurrence": None,
            "alert_days_before": 0,
            "notes": "First day of the lease term.",
        },
        {
            "date_type": "lease_expiry",
            "date_description": "Initial lease expiry date",
            "date_value": "2029-02-28",
            "clause_reference": "Item 2",
            "recurrence": None,
            "alert_days_before": 180,
            "notes": "End of the 5-year initial term.",
        },
        {
            "date_type": "option_exercise_deadline",
            "date_description": "Option 1 to renew — exercise deadline",
            "date_value": "2028-08-31",
            "clause_reference": "Clause 4.1",
            "recurrence": None,
            "alert_days_before": 180,
            "notes": "Option must be exercised in writing no later than 6 months before expiry.",
        },
        {
            "date_type": "rent_review_cpi",
            "date_description": "Annual CPI rent review",
            "date_value": "2025-03-01",
            "clause_reference": "Clause 6.2",
            "recurrence": "annual",
            "alert_days_before": 60,
            "notes": "CPI review on each anniversary of commencement.",
        },
        {
            "date_type": "outgoings_reconciliation",
            "date_description": "Annual outgoings reconciliation statement due from landlord",
            "date_value": None,
            "clause_reference": "Clause 9.4",
            "recurrence": "annual",
            "alert_days_before": 30,
            "notes": "Due within 3 months of 30 June each year. Tenant has right to dispute within 60 days of receipt.",
        },
        {
            "date_type": "rent_free_end",
            "date_description": "End of 3-month rent-free incentive period",
            "date_value": "2024-06-01",
            "clause_reference": "Special Condition 2",
            "recurrence": None,
            "alert_days_before": 30,
            "notes": "Full rent commences from this date.",
        },
    ]
