"""
audit_run_store.py
------------------
Thin Supabase CRUD layer for the audit_run table.
All public functions return plain dicts or None — no Job objects.

Called exclusively from api/jobs.py; nothing in main.py touches this directly.
"""

import os
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

_SYDNEY_TZ = ZoneInfo("Australia/Sydney")

from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_client = None


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
                # Explicit httpx.Timeout — passing an int has a known bug in supabase-py 2.x
                # where the write timeout isn't applied when sending large findings payloads.
                # write=60 gives the 200-400KB findings JSONB room to complete.
                postgrest_client_timeout=httpx.Timeout(
                    connect=5.0, read=30.0, write=60.0, pool=5.0
                )
            ),
        )
    return _client


def _now() -> str:
    return datetime.now(_SYDNEY_TZ).isoformat()


# ── Write operations ──────────────────────────────────────────────────────────

def insert_job(job_id: str, filename: str, jurisdiction: str, tenant_name: str) -> dict:
    """INSERT a new queued job row. Returns the inserted row."""
    row = {
        "job_id": job_id,
        "filename": filename,
        "jurisdiction": jurisdiction,
        "tenant_name": tenant_name,
        "status": "queued",
        "progress": 0,
        "stage": "Queued",
    }
    result = _get_client().table("audit_run").insert(row).execute()
    logger.debug(f"[{job_id}] Inserted audit_run row")
    return result.data[0] if result.data else row


def update_progress(job_id: str, progress: int, stage: str) -> None:
    """UPDATE progress + stage for a job."""
    _get_client().table("audit_run").update({
        "status": "processing",
        "progress": progress,
        "stage": stage,
    }).eq("job_id", job_id).execute()


def mark_complete(
    job_id: str,
    findings: dict,
    stage_timings: Optional[dict] = None,
    stage_costs: Optional[dict] = None,
) -> None:
    """UPDATE job to complete with findings JSON, optional timing data, and optional cost data."""
    payload = {
        "status": "complete",
        "progress": 100,
        "stage": "Complete",
        "findings": findings,
        "completed_at": _now(),
    }
    if stage_timings:
        payload["stage_timings"] = stage_timings
    if stage_costs:
        payload["stage_costs"] = stage_costs
    _get_client().table("audit_run").update(payload).eq("job_id", job_id).execute()
    logger.info(f"[{job_id}] audit_run marked complete")


def mark_complete_minimal(job_id: str) -> None:
    """
    Fallback: mark job complete without findings (findings already saved in-memory).
    Called when mark_complete times out writing the large findings payload.
    The in-memory fallback in jobs.py still holds the result for same-process reads.
    """
    _get_client().table("audit_run").update({
        "status": "complete",
        "progress": 100,
        "stage": "Complete",
        "completed_at": _now(),
    }).eq("job_id", job_id).execute()
    logger.warning(f"[{job_id}] audit_run marked complete (minimal — findings not persisted to DB)")


def mark_failed(job_id: str, error: str) -> None:
    """UPDATE job to failed with error message."""
    _get_client().table("audit_run").update({
        "status": "failed",
        "stage": "Failed",
        "error": error,
        "completed_at": _now(),
    }).eq("job_id", job_id).execute()
    logger.warning(f"[{job_id}] audit_run marked failed: {error}")


def mark_reviewed(job_id: str, notes: str) -> Optional[dict]:
    """SET reviewed_by_human = True. Returns updated row or None."""
    result = _get_client().table("audit_run").update({
        "reviewed_by_human": True,
        "reviewer_notes": notes,
        "reviewed_at": _now(),
    }).eq("job_id", job_id).eq("status", "complete").execute()
    return result.data[0] if result.data else None


def mark_released(job_id: str) -> Optional[dict]:
    """SET released = True. Only works when reviewed_by_human = True."""
    result = _get_client().table("audit_run").update({
        "released": True,
        "released_at": _now(),
    }).eq("job_id", job_id).eq("reviewed_by_human", True).execute()
    return result.data[0] if result.data else None


# ── Read operations ───────────────────────────────────────────────────────────

def fetch_job(job_id: str) -> Optional[dict]:
    """SELECT a single job row by PK. Returns dict or None."""
    result = _get_client().table("audit_run").select("*").eq("job_id", job_id).execute()
    return result.data[0] if result.data else None


def fetch_findings(job_id: str) -> Optional[dict]:
    """SELECT only the findings JSONB column (avoids loading full row unnecessarily)."""
    result = _get_client().table("audit_run").select("findings").eq("job_id", job_id).execute()
    if result.data:
        return result.data[0].get("findings")
    return None


def fetch_pending_review() -> list[dict]:
    """SELECT complete-but-unreviewed jobs, newest first."""
    result = (
        _get_client()
        .table("audit_run")
        .select("*")
        .eq("status", "complete")
        .eq("reviewed_by_human", False)
        .order("completed_at", desc=True)
        .execute()
    )
    return result.data or []


def fetch_reviewed() -> list[dict]:
    """SELECT reviewed jobs (released or pending release), newest first."""
    result = (
        _get_client()
        .table("audit_run")
        .select("*")
        .eq("reviewed_by_human", True)
        .order("reviewed_at", desc=True)
        .execute()
    )
    return result.data or []


def fetch_failed() -> list[dict]:
    """SELECT failed jobs, newest first."""
    result = (
        _get_client()
        .table("audit_run")
        .select("*")
        .eq("status", "failed")
        .order("completed_at", desc=True)
        .execute()
    )
    return result.data or []


def fetch_all_recent(limit: int = 20) -> list[dict]:
    """SELECT most recent jobs regardless of status — for debugging."""
    result = (
        _get_client()
        .table("audit_run")
        .select("job_id, filename, jurisdiction, tenant_name, status, progress, stage, error, created_at, completed_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def fetch_active() -> list[dict]:
    """SELECT jobs currently queued or processing — for the kill switch panel.
    Ordered newest-first so a freshly submitted job appears at the top, not buried
    under zombie jobs stuck in 'processing' from a prior server restart.
    """
    result = (
        _get_client()
        .table("audit_run")
        .select("job_id, filename, jurisdiction, tenant_name, status, progress, stage, created_at")
        .in_("status", ["queued", "processing"])
        .order("created_at", desc=True)   # newest first
        .execute()
    )
    return result.data or []


def fetch_recent_failed_brief(limit: int = 8) -> list[dict]:
    """SELECT the most recent failed/cancelled jobs with their error field — for the kill switch error inspector."""
    result = (
        _get_client()
        .table("audit_run")
        .select("job_id, filename, jurisdiction, tenant_name, status, stage, error, created_at, completed_at")
        .in_("status", ["failed", "cancelled"])
        .order("completed_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def mark_cancelled(job_id: str) -> None:
    """Mark a job as cancelled (terminal state — prevents accidental re-processing)."""
    _get_client().table("audit_run").update({
        "status": "cancelled",
        "stage": "Cancelled",
        "error": "Manually cancelled by admin",
        "completed_at": _now(),
    }).eq("job_id", job_id).execute()
    logger.info(f"[{job_id}] audit_run marked cancelled")


def reset_for_retry(job_id: str) -> None:
    """Reset job to queued so a re-dispatch can proceed."""
    _get_client().table("audit_run").update({
        "status": "queued",
        "progress": 0,
        "stage": "Queued",
        "error": None,
        "completed_at": None,
    }).eq("job_id", job_id).execute()
    logger.info(f"[{job_id}] audit_run reset for retry")


def delete_job(job_id: str) -> None:
    """Hard delete the job row and all associated data."""
    _get_client().table("audit_run").delete().eq("job_id", job_id).execute()
    logger.info(f"[{job_id}] audit_run row deleted")