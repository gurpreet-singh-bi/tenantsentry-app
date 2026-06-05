"""
lease_date_store.py
-------------------
Supabase CRUD for the lease_dates table.

Table populated by services/date_extractor.py after each audit completes.
Read by the alert engine and the tenant dashboard monitoring view.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_client = None

# Valid date_type values — must match CHECK constraint in migration 002
VALID_DATE_TYPES = {
    "lease_commencement",
    "lease_expiry",
    "option_exercise_deadline",
    "rent_review_cpi",
    "rent_review_market",
    "rent_review_fixed",
    "outgoings_reconciliation",
    "rent_free_end",
    "fitout_completion_deadline",
    "demolition_notice_window",
    "bank_guarantee_expiry",
    "make_good_deadline",
    "other",
}


def _get_client():
    global _client
    if _client is None:
        import httpx
        from supabase import create_client
        from supabase.lib.client_options import ClientOptions
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
            options=ClientOptions(
                postgrest_client_timeout=httpx.Timeout(
                    connect=5.0, read=30.0, write=30.0, pool=5.0
                )
            ),
        )
    return _client


# ── Write ─────────────────────────────────────────────────────────────────────

def insert_dates(job_id: str, dates: list[dict]) -> int:
    """
    Insert a batch of extracted dates for a job.

    Each date dict should have:
        date_type        str   — must be in VALID_DATE_TYPES
        date_description str   — plain-English label
        date_value       str | None  — ISO format YYYY-MM-DD, or None if unknown
        clause_reference str | None  — e.g. "Clause 4.2(b)"
        recurrence       str | None  — None | "annual" | "monthly"
        alert_days_before int  — default 90

    Returns number of rows inserted.
    """
    if not dates:
        return 0

    rows = []
    for d in dates:
        dtype = d.get("date_type", "other")
        if dtype not in VALID_DATE_TYPES:
            logger.warning(f"Unknown date_type '{dtype}' — storing as 'other'")
            dtype = "other"

        rows.append({
            "job_id": job_id,
            "date_type": dtype,
            "date_description": d.get("date_description", ""),
            "date_value": d.get("date_value"),          # None OK
            "clause_reference": d.get("clause_reference"),
            "recurrence": d.get("recurrence"),
            "alert_days_before": int(d.get("alert_days_before", 90)),
            "alert_sent": False,
        })

    result = _get_client().table("lease_dates").insert(rows).execute()
    inserted = len(result.data or [])
    logger.info(f"[{job_id}] Inserted {inserted} lease dates")
    return inserted


def mark_alert_sent(date_id: str) -> None:
    """Mark a specific date row as alerted."""
    _get_client().table("lease_dates").update({
        "alert_sent": True,
        "alert_sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", date_id).execute()


# ── Read ──────────────────────────────────────────────────────────────────────

def fetch_dates_for_job(job_id: str) -> list[dict]:
    """Return all lease dates for a given audit job."""
    result = (
        _get_client()
        .table("lease_dates")
        .select("*")
        .eq("job_id", job_id)
        .order("date_value", desc=False, nullsfirst=False)
        .execute()
    )
    return result.data or []


def fetch_upcoming_alerts(days_ahead: int = 90) -> list[dict]:
    """
    Return unsent alerts due within the next `days_ahead` days.
    Used by the alert engine / scheduled task.

    Filters: alert_sent=False, date_value IS NOT NULL,
             date_value <= today + days_ahead
    """
    from datetime import date, timedelta
    cutoff = (date.today() + timedelta(days=days_ahead)).isoformat()
    today = date.today().isoformat()

    result = (
        _get_client()
        .table("lease_dates")
        .select("*, audit_run(tenant_name, jurisdiction, filename)")
        .eq("alert_sent", False)
        .gte("date_value", today)          # not already past
        .lte("date_value", cutoff)         # within window
        .order("date_value")
        .execute()
    )
    return result.data or []
