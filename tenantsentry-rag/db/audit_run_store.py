"""
audit_run_store.py
------------------
Thin Supabase CRUD layer for the audit_run table.
All public functions return plain dicts or None — no Job objects.

Called exclusively from api/jobs.py; nothing in main.py touches this directly.
"""

# Defer annotation evaluation so `str | None` (PEP 604) syntax below doesn't
# crash at module-import time on Python 3.9 (this venv) — type.__or__ for
# unions was only added in 3.10. With this import, annotations are stored as
# strings and never evaluated at runtime, so behaviour is unaffected.
from __future__ import annotations

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
        # supabase-py 2.31.0: ClientOptions dataclass is missing 'storage' and
        # 'httpx_client' attrs that Client.__init__ accesses. Patch them post-construction.
        try:
            from supabase_auth import SyncMemoryStorage as _SyncMem
        except ImportError:
            try:
                from gotrue.types import SyncMemoryStorage as _SyncMem  # type: ignore
            except ImportError:
                _SyncMem = None
        opts = ClientOptions(
            # Explicit httpx.Timeout — passing an int has a known bug in supabase-py 2.x
            # where the write timeout isn't applied when sending large findings payloads.
            # write=60 gives the 200-400KB findings JSONB room to complete.
            postgrest_client_timeout=httpx.Timeout(
                connect=5.0, read=30.0, write=60.0, pool=5.0
            )
        )
        if _SyncMem is not None and not hasattr(opts, 'storage'):
            opts.storage = _SyncMem()
        if not hasattr(opts, 'httpx_client'):
            opts.httpx_client = None
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
            options=opts,
        )
    return _client


def _now() -> str:
    return datetime.now(_SYDNEY_TZ).isoformat()


# ── Write operations ──────────────────────────────────────────────────────────

def insert_job(
    job_id: str,
    filename: str,
    jurisdiction: str,
    tenant_name: str,
    source: str = "live",
    # AQ-NEW-5: premises classification fields
    premises_use: Optional[str] = None,
    entity_type: Optional[str] = None,
    gla_sqm: Optional[float] = None,
    applicable_statute: Optional[str] = None,
    statute_code: Optional[str] = None,
    is_retail_lease: Optional[bool] = None,
    # F-PARTNER-LIVE: channel partner attribution
    partner_id: Optional[str] = None,
    client_org_id: Optional[str] = None,
) -> dict:
    """INSERT a new queued job row. Returns the inserted row."""
    row = {
        "job_id": job_id,
        "filename": filename,
        "jurisdiction": jurisdiction,
        "tenant_name": tenant_name,
        "status": "queued",
        "progress": 0,
        "stage": "Queued",
        "source": source,
    }
    # Only include classification fields when provided (columns may not exist on older schemas)
    if premises_use is not None:
        row["premises_use"] = premises_use
    if entity_type is not None:
        row["entity_type"] = entity_type
    if gla_sqm is not None:
        row["gla_sqm"] = gla_sqm
    if applicable_statute is not None:
        row["applicable_statute"] = applicable_statute
    if statute_code is not None:
        row["statute_code"] = statute_code
    if is_retail_lease is not None:
        row["is_retail_lease"] = is_retail_lease
    # F-PARTNER-LIVE: only set when provided — columns may not exist on older schemas
    # (migration 010 adds them; insert still succeeds for tenant-direct audits without them)
    if partner_id is not None:
        row["partner_id"] = partner_id
    if client_org_id is not None:
        row["client_org_id"] = client_org_id
    try:
        result = _get_client().table("audit_run").insert(row).execute()
    except Exception as e:
        # Defensive fallback: if migration 010 hasn't run yet on this environment,
        # the partner_id/client_org_id columns won't exist — retry without them
        # so job creation never hard-fails on a missing optional column.
        if ("partner_id" in row or "client_org_id" in row) and "column" in str(e).lower():
            logger.warning(f"[{job_id}] audit_run insert failed with partner columns ({e}); retrying without them")
            row.pop("partner_id", None)
            row.pop("client_org_id", None)
            result = _get_client().table("audit_run").insert(row).execute()
        else:
            raise
    logger.debug(f"[{job_id}] Inserted audit_run row (source={source}, statute={statute_code}, partner={partner_id})")
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


def fetch_pending_review(source: str = "live") -> list[dict]:
    """SELECT complete-but-unreviewed jobs for the given source (dev/live), newest first.
    Uses `or_` to catch both reviewed_by_human=false AND reviewed_by_human IS NULL
    (PLG funnel uploads before reviewed_by_human column was set explicitly).
    """
    result = (
        _get_client()
        .table("audit_run")
        .select("*")
        .eq("status", "complete")
        .or_("reviewed_by_human.is.null,reviewed_by_human.eq.false")
        .eq("source", source)
        .order("completed_at", desc=True)
        .execute()
    )
    return result.data or []


def fetch_reviewed(source: str = "live") -> list[dict]:
    """SELECT reviewed jobs for the given source (dev/live), newest first."""
    result = (
        _get_client()
        .table("audit_run")
        .select("*")
        .eq("reviewed_by_human", True)
        .eq("source", source)
        .order("reviewed_at", desc=True)
        .execute()
    )
    return result.data or []


def fetch_failed(source: str | None = None) -> list[dict]:
    """SELECT failed jobs, newest first. Filter by source if given."""
    q = (
        _get_client()
        .table("audit_run")
        .select("*")
        .eq("status", "failed")
    )
    if source:
        q = q.eq("source", source)
    result = q.order("completed_at", desc=True).execute()
    return result.data or []


def fetch_all_recent(limit: int = 20, source: str = "live") -> list[dict]:
    """SELECT most recent jobs for the given source (dev/live) — for debugging."""
    result = (
        _get_client()
        .table("audit_run")
        .select("job_id, filename, jurisdiction, tenant_name, status, progress, stage, error, created_at, completed_at, stage_costs, stage_timings, source")
        .eq("source", source)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def fetch_recent_for_partner(partner_id: str, source: str = "live", limit: int = 20) -> list[dict]:
    """F-PARTNER-LIVE: SELECT the most recent jobs submitted by this channel
    partner (audit_run.partner_id), newest first, for the given source
    (dev/live). Backs /api/partners/audits — includes in-progress jobs so the
    Partner Portal can show a live audit as it runs.
    """
    result = (
        _get_client()
        .table("audit_run")
        .select("job_id, filename, jurisdiction, tenant_name, status, progress, stage, "
                "error, created_at, completed_at, reviewed_by_human, released, "
                "source, partner_id, client_org_id")
        .eq("partner_id", partner_id)
        .eq("source", source)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def fetch_active(source: str | None = None) -> list[dict]:
    """SELECT jobs currently queued or processing — for the kill switch panel.
    Ordered newest-first so a freshly submitted job appears at the top, not buried
    under zombie jobs stuck in 'processing' from a prior server restart.
    """
    q = (
        _get_client()
        .table("audit_run")
        .select("job_id, filename, jurisdiction, tenant_name, status, progress, stage, created_at, source")
        .in_("status", ["queued", "processing"])
    )
    if source:
        q = q.eq("source", source)
    result = q.order("created_at", desc=True).execute()
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


def fetch_released_jobs(source: str = "live") -> list[dict]:
    """SELECT all released=True jobs for the given source, newest first."""
    result = (
        _get_client()
        .table("audit_run")
        .select("job_id, filename, jurisdiction, tenant_name, status, progress, stage, "
                "released, released_at, reviewed_by_human, reviewer_notes, reviewed_at, "
                "created_at, completed_at, source, stage_costs, stage_timings")
        .eq("released", True)
        .eq("source", source)
        .order("released_at", desc=True)
        .execute()
    )
    return result.data or []


def fetch_jobs_for_tenant(tenant_id: str, source: str = "live") -> list[dict]:
    """SELECT released jobs for a specific tenant."""
    return fetch_released_jobs(source=source)
