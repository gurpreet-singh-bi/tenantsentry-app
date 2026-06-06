"""
jobs.py
-------
Job state manager for async audit processing.
Jobs are keyed by UUID and persisted to Supabase audit_run table (G5).
PDFs are persisted to Supabase Storage bucket 'lease-pdfs' (V1).

Falls back to in-memory storage if Supabase is unavailable (dev mode).

Job lifecycle:
    queued → processing → complete → [awaiting_review] → released
                       → failed

Human-in-loop gate (G4):
    On complete, reviewed_by_human = False and released = False.
    Auditor must approve via /admin portal before report is accessible to tenant.
    review_job() sets reviewed_by_human = True + stores reviewer_notes.
    release_job() sets released = True — tenant can now download their report.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional
from enum import Enum
from zoneinfo import ZoneInfo

_SYDNEY_TZ = ZoneInfo("Australia/Sydney")

from loguru import logger

# ── Detect whether Supabase is configured and reachable ──────────────────────
_SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
_SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Placeholder values in .env.example start with "https://your-project"
_LOOKS_LIKE_PLACEHOLDER = "your-project" in _SUPABASE_URL or "your-" in _SUPABASE_KEY

_USE_SUPABASE: bool = False
_store = None

if _SUPABASE_URL and _SUPABASE_KEY and not _LOOKS_LIKE_PLACEHOLDER:
    try:
        import db.audit_run_store as _store
        # Smoke-test: fetch a non-existent row — if the table exists this returns None, not an error
        _store.fetch_job("00000000-0000-0000-0000-000000000000")
        _USE_SUPABASE = True
        logger.info("G5: Jobs persistence → Supabase audit_run table ✓")
    except Exception as e:
        _store = None
        logger.warning(
            f"G5: Supabase unreachable or audit_run table missing ({e}). "
            "Falling back to in-memory store. Run supabase/migration_audit_run.sql to enable persistence."
        )
else:
    reason = "placeholder credentials" if _LOOKS_LIKE_PLACEHOLDER else "SUPABASE_URL/KEY not set"
    logger.warning(f"G5: {reason} — using in-memory job store (dev mode)")


# ── Enums & Job model ─────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


class Job:
    """
    In-memory representation of a job row.
    Constructed from a Supabase row dict or created fresh.
    Result/findings are fetched separately on demand to avoid loading large JSONB on every status poll.
    """

    def __init__(
        self,
        job_id: str,
        filename: str,
        jurisdiction: str,
        tenant_name: str,
        status: str = "queued",
        progress: int = 0,
        stage: str = "Queued",
        error: Optional[str] = None,
        created_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        reviewed_by_human: bool = False,
        reviewer_notes: Optional[str] = None,
        reviewed_at: Optional[str] = None,
        released: bool = False,
        released_at: Optional[str] = None,
        result: Optional[dict] = None,
    ):
        self.job_id = job_id
        self.filename = filename
        self.jurisdiction = jurisdiction
        self.tenant_name = tenant_name
        self.status = JobStatus(status)
        self.progress = progress
        self.stage = stage
        self.error = error
        self.created_at = created_at or datetime.now(_SYDNEY_TZ).isoformat()
        self.completed_at = completed_at
        self.reviewed_by_human = reviewed_by_human
        self.reviewer_notes = reviewer_notes
        self.reviewed_at = reviewed_at
        self.released = released
        self.released_at = released_at
        self.result = result  # populated only when needed (complete_job / get_job with result)

    @classmethod
    def from_row(cls, row: dict, result: Optional[dict] = None) -> "Job":
        """Construct a Job from a Supabase audit_run row dict."""
        return cls(
            job_id=row["job_id"],
            filename=row["filename"],
            jurisdiction=row["jurisdiction"],
            tenant_name=row["tenant_name"],
            status=row.get("status", "queued"),
            progress=row.get("progress", 0),
            stage=row.get("stage", "Queued"),
            error=row.get("error"),
            created_at=row.get("created_at"),
            completed_at=row.get("completed_at"),
            reviewed_by_human=row.get("reviewed_by_human", False),
            reviewer_notes=row.get("reviewer_notes"),
            reviewed_at=row.get("reviewed_at"),
            released=row.get("released", False),
            released_at=row.get("released_at"),
            result=result or row.get("findings"),
        )

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "filename": self.filename,
            "jurisdiction": self.jurisdiction,
            "tenant_name": self.tenant_name,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "error": self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "reviewed_by_human": self.reviewed_by_human,
            "reviewer_notes": self.reviewer_notes,
            "reviewed_at": self.reviewed_at,
            "released": self.released,
            "released_at": self.released_at,
            # result is large — only included in dedicated result endpoint
        }


# ── In-memory fallback (dev only) ────────────────────────────────────────────
_jobs_fallback: dict[str, Job] = {}

# ── In-memory document fallback (dev only — used when Supabase Storage unavailable) ──
_documents: dict[str, dict] = {}   # job_id → { filename, content_type, data: bytes }

# ── Kill-switch: job IDs cancelled mid-flight ─────────────────────────────────
# complete_job() checks this before writing results, ensuring a cancelled job's
# in-flight thread cannot overwrite the cancelled/deleted state in DB or memory.
_cancelled_jobs: set[str] = set()


# ── Public API ────────────────────────────────────────────────────────────────

def _supabase_ok() -> bool:
    """Runtime check — use Supabase only if it was confirmed reachable at startup."""
    return _USE_SUPABASE and _store is not None


def create_job(filename: str, jurisdiction: str, tenant_name: str) -> Job:
    job_id = str(uuid.uuid4())
    job = Job(job_id=job_id, filename=filename, jurisdiction=jurisdiction, tenant_name=tenant_name)
    # Always write to fallback first so we never lose the job on a Supabase error
    _jobs_fallback[job_id] = job
    if _supabase_ok():
        try:
            _store.insert_job(job_id, filename, jurisdiction, tenant_name)
        except Exception as e:
            logger.error(f"[{job_id}] Supabase insert failed, using in-memory: {e}")
    return job


def get_job(job_id: str) -> Optional[Job]:
    in_mem = _jobs_fallback.get(job_id)
    if _supabase_ok():
        try:
            row = _store.fetch_job(job_id)
            if row:
                db_job = Job.from_row(row)
                # Trust in-memory over DB when memory shows a more advanced state.
                # This covers the case where mark_complete timed out writing findings
                # but the in-memory fallback was already updated to 'complete'.
                if (
                    in_mem
                    and in_mem.status == JobStatus.COMPLETE
                    and db_job.status == JobStatus.PROCESSING
                ):
                    logger.warning(
                        f"[{job_id}] DB shows 'processing' but memory shows 'complete' "
                        "— returning in-memory state (mark_complete may have timed out)"
                    )
                    return in_mem
                return db_job
        except Exception as e:
            logger.error(f"[{job_id}] Supabase fetch failed, checking fallback: {e}")
    return in_mem


def update_job_progress(job_id: str, progress: int, stage: str) -> None:
    # Update in-memory first (cheap, always works)
    job = _jobs_fallback.get(job_id)
    if job:
        job.status = JobStatus.PROCESSING
        job.progress = progress
        job.stage = stage
    if _supabase_ok():
        try:
            _store.update_progress(job_id, progress, stage)
        except Exception as e:
            logger.error(f"[{job_id}] Supabase progress update failed: {e}")


_CLAUSE_TEXT_EXCERPT_CHARS = 1500   # chars kept per clause for the auditor excerpt panel


def _strip_clause_text(result: dict) -> dict:
    """
    Return a copy of result with:
      - clause_text truncated to _CLAUSE_TEXT_EXCERPT_CHARS per clause so the
        auditor's "Document Excerpt" panel has something to display, while keeping
        the JSONB payload manageable (~252 KB max for a 168-clause lease vs.
        ~2-5 MB for the full text).
      - stage_timings removed (stored in its own column, not in findings JSONB)
      - stage_costs removed (stored in its own column, not in findings JSONB)
    """
    stripped = {k: v for k, v in result.items() if k not in ("stage_timings", "stage_costs")}
    if "clause_analyses" in stripped and isinstance(stripped["clause_analyses"], list):
        truncated = []
        for ca in stripped["clause_analyses"]:
            if isinstance(ca, dict) and "clause_text" in ca:
                ca = dict(ca)
                text = ca["clause_text"] or ""
                if len(text) > _CLAUSE_TEXT_EXCERPT_CHARS:
                    ca["clause_text"] = text[:_CLAUSE_TEXT_EXCERPT_CHARS] + "…"
            truncated.append(ca)
        stripped["clause_analyses"] = truncated
    return stripped


def complete_job(job_id: str, result: dict) -> None:
    if job_id in _cancelled_jobs:
        logger.warning(f"[{job_id}] complete_job skipped — job was cancelled mid-flight")
        return
    job = _jobs_fallback.get(job_id)
    if job:
        job.status = JobStatus.COMPLETE
        job.progress = 100
        job.stage = "Complete"
        job.result = result  # keep full result in-memory (same process, no storage cost)
        job.completed_at = datetime.now(_SYDNEY_TZ).isoformat()
    if _supabase_ok():
        try:
            stage_timings = result.get("stage_timings") if isinstance(result, dict) else None
            stage_costs   = result.get("stage_costs")   if isinstance(result, dict) else None
            findings = _strip_clause_text(result) if isinstance(result, dict) else result
            _store.mark_complete(job_id, findings, stage_timings=stage_timings, stage_costs=stage_costs)
        except Exception as e:
            logger.error(f"[{job_id}] Supabase complete failed: {e}")
            # Fallback: write status+progress without findings so the DB row
            # exits 'processing' state and status polls don't show a stale stage.
            try:
                _store.mark_complete_minimal(job_id)
            except Exception as e2:
                logger.error(f"[{job_id}] Supabase minimal complete also failed: {e2}")


def fail_job(job_id: str, error: str) -> None:
    job = _jobs_fallback.get(job_id)
    if job:
        job.status = JobStatus.FAILED
        job.stage = "Failed"
        job.error = error
        job.completed_at = datetime.now(_SYDNEY_TZ).isoformat()
    if _supabase_ok():
        try:
            _store.mark_failed(job_id, error)
        except Exception as e:
            logger.error(f"[{job_id}] Supabase fail update failed: {e}")


def review_job(job_id: str, notes: Optional[str] = None) -> Optional[Job]:
    """Auditor approves findings. Sets reviewed_by_human = True."""
    if _supabase_ok():
        try:
            row = _store.mark_reviewed(job_id, notes or "")
            if row:
                # Sync back to fallback
                job = _jobs_fallback.get(job_id)
                if job:
                    job.reviewed_by_human = True
                    job.reviewer_notes = notes or ""
                    job.reviewed_at = row.get("reviewed_at")
                return Job.from_row(row)
        except Exception as e:
            logger.error(f"[{job_id}] Supabase review failed, applying in-memory: {e}")
    job = _jobs_fallback.get(job_id)
    if job and job.status == JobStatus.COMPLETE:
        job.reviewed_by_human = True
        job.reviewer_notes = notes or ""
        job.reviewed_at = datetime.now(_SYDNEY_TZ).isoformat()
    return job


def release_job(job_id: str) -> Optional[Job]:
    """Release report to tenant. Only callable after review."""
    if _supabase_ok():
        try:
            row = _store.mark_released(job_id)
            if row:
                job = _jobs_fallback.get(job_id)
                if job:
                    job.released = True
                    job.released_at = row.get("released_at")
                return Job.from_row(row)
        except Exception as e:
            logger.error(f"[{job_id}] Supabase release failed, applying in-memory: {e}")
    job = _jobs_fallback.get(job_id)
    if job and job.reviewed_by_human:
        job.released = True
        job.released_at = datetime.now(_SYDNEY_TZ).isoformat()
    return job


def cancel_job(job_id: str, action: str = "fail") -> dict:
    """
    Kill switch — three modes:

    "fail"   → Mark job as cancelled (terminal). In-flight thread results are
               silently discarded via _cancelled_jobs guard in complete_job().
    "retry"  → Cancel current run + reset to queued so it can be re-dispatched.
               Caller (main.py) is responsible for re-dispatching if PDF is available.
    "delete" → Hard-delete from memory + Supabase. Irreversible.

    Returns a result dict passed back to the admin API response.
    """
    # Always add to cancelled set first — prevents the running thread from
    # committing its result if complete_job() is called after we return.
    _cancelled_jobs.add(job_id)

    if action == "delete":
        _jobs_fallback.pop(job_id, None)
        _documents.pop(job_id, None)
        if _supabase_ok():
            try:
                _store.delete_job(job_id)
            except Exception as e:
                logger.error(f"[{job_id}] Supabase delete failed: {e}")
        # Remove from cancelled set — job is gone, no need to track
        _cancelled_jobs.discard(job_id)
        logger.info(f"[{job_id}] Job deleted by admin")
        return {"ok": True, "action": "delete"}

    if action == "retry":
        # Step 1: discard from cancelled set so a fresh dispatch can complete_job()
        _cancelled_jobs.discard(job_id)
        # Step 2: reset in-memory state
        job = _jobs_fallback.get(job_id)
        if job:
            job.status = JobStatus.QUEUED
            job.progress = 0
            job.stage = "Queued"
            job.error = None
        # Step 3: reset in Supabase
        if _supabase_ok():
            try:
                _store.reset_for_retry(job_id)
            except Exception as e:
                logger.error(f"[{job_id}] Supabase retry reset failed: {e}")
        # Re-dispatch is handled by main.py (needs pdf_bytes + event loop).
        # We signal whether the PDF is available so main.py can decide.
        has_pdf = job_id in _documents or (
            _supabase_ok()  # assume Supabase Storage has it if bucket is configured
        )
        logger.info(f"[{job_id}] Job reset for retry by admin (pdf_available={has_pdf})")
        return {"ok": True, "action": "retry", "pdf_available": has_pdf}

    # Default: "fail" — mark cancelled (terminal)
    job = _jobs_fallback.get(job_id)
    if job:
        job.status = JobStatus.FAILED
        job.stage = "Cancelled"
        job.error = "Manually cancelled by admin"
        job.completed_at = datetime.now(_SYDNEY_TZ).isoformat()
    if _supabase_ok():
        try:
            _store.mark_cancelled(job_id)
        except Exception as e:
            logger.error(f"[{job_id}] Supabase mark_cancelled failed: {e}")
    logger.info(f"[{job_id}] Job cancelled by admin")
    return {"ok": True, "action": "fail"}


def list_active() -> list[Job]:
    """Return jobs currently queued or processing — for the kill switch panel."""
    active_statuses = {JobStatus.QUEUED, JobStatus.PROCESSING}
    if _supabase_ok():
        try:
            return [Job.from_row(r) for r in _store.fetch_active()]
        except Exception as e:
            logger.error(f"Supabase list_active failed, using fallback: {e}")
    return sorted(
        [j for j in _jobs_fallback.values() if j.status in active_statuses],
        key=lambda j: j.created_at or "",
    )


def list_pending_review() -> list[Job]:
    """Return completed-but-unreviewed jobs, newest first."""
    if _supabase_ok():
        try:
            return [Job.from_row(r) for r in _store.fetch_pending_review()]
        except Exception as e:
            logger.error(f"Supabase list_pending_review failed, using fallback: {e}")
    return sorted(
        [j for j in _jobs_fallback.values() if j.status == JobStatus.COMPLETE and not j.reviewed_by_human],
        key=lambda j: j.completed_at or "",
        reverse=True,
    )


def list_reviewed() -> list[Job]:
    """Return reviewed jobs (released or awaiting release), newest first."""
    if _supabase_ok():
        try:
            return [Job.from_row(r) for r in _store.fetch_reviewed()]
        except Exception as e:
            logger.error(f"Supabase list_reviewed failed, using fallback: {e}")
    return sorted(
        [j for j in _jobs_fallback.values() if j.reviewed_by_human],
        key=lambda j: j.reviewed_at or "",
        reverse=True,
    )


# ── Uploaded-doc metadata (multi-doc support) ────────────────────────────────
# Tracks every file the user submitted alongside a job: lease, outgoings, invoice, amendment.
# Stored in-memory (keyed by job_id) and flushed to Supabase job metadata when available.

_uploaded_docs: dict[str, list[dict]] = {}   # job_id → [ {filename, doc_type, size_bytes, status, warnings} ]


def store_uploaded_doc_meta(
    job_id: str,
    filename: str,
    doc_type: str,          # "lease" | "outgoings" | "invoice" | "amendment" | "other"
    size_bytes: int,
    status: str = "queued", # "queued" | "processing" | "complete" | "failed" | "skipped"
    warnings: list = None,
) -> None:
    """Register a doc that was uploaded for this job."""
    entry = {
        "filename": filename,
        "doc_type": doc_type,
        "size_bytes": size_bytes,
        "status": status,
        "warnings": warnings or [],
    }
    _uploaded_docs.setdefault(job_id, []).append(entry)


def update_uploaded_doc_status(job_id: str, filename: str, status: str, warnings: list = None) -> None:
    """Update processing status of a specific doc within a job."""
    docs = _uploaded_docs.get(job_id, [])
    for doc in docs:
        if doc["filename"] == filename:
            doc["status"] = status
            if warnings is not None:
                doc["warnings"] = warnings
            return


def get_uploaded_docs(job_id: str) -> list[dict]:
    """Return the list of uploaded doc metadata for a job."""
    return _uploaded_docs.get(job_id, [])


# ── Document storage (in-memory) ─────────────────────────────────────────────

def store_document(job_id: str, filename: str, data: bytes, content_type: str = "application/pdf") -> None:
    """
    Persist original uploaded PDF.
    Primary: Supabase Storage bucket 'lease-pdfs' (V1 — multi-replica safe).
    Fallback: in-memory dict (dev mode / Supabase unavailable).
    """
    # Always keep in-memory copy for same-request access speed
    _documents[job_id] = {"filename": filename, "content_type": content_type, "data": data}

    if _supabase_ok():
        try:
            from db.pdf_store import upload_pdf
            upload_pdf(job_id, filename, data)
        except Exception as e:
            logger.error(f"[{job_id}] PDF Storage upload failed, in-memory only: {e}")


def get_document(job_id: str) -> Optional[dict]:
    """
    Retrieve original uploaded PDF.
    Checks in-memory first (fast path), then falls back to Supabase Storage.
    """
    # Fast path — same process has it in memory
    doc = _documents.get(job_id)
    if doc:
        return doc

    # Slow path — different replica or server restarted, fetch from Storage
    if _supabase_ok():
        try:
            from db.pdf_store import download_pdf
            from db.audit_run_store import fetch_job
            row = fetch_job(job_id)
            if not row:
                return None
            filename = row["filename"]
            data = download_pdf(job_id, filename)
            if data:
                doc = {"filename": filename, "content_type": "application/pdf", "data": data}
                _documents[job_id] = doc  # cache locally for subsequent calls
                return doc
        except Exception as e:
            logger.error(f"[{job_id}] PDF Storage download failed: {e}")

    return None


# ── Result accessor (fetches findings from Supabase if needed) ────────────────

def get_job_result(job_id: str) -> Optional[dict]:
    """
    Fetch the full AuditResult findings for a completed job.
    Separated from get_job() to avoid loading large JSONB on every status poll.
    """
    if _supabase_ok():
        try:
            result = _store.fetch_findings(job_id)
            if result is not None:
                return result
        except Exception as e:
            logger.error(f"[{job_id}] Supabase fetch_findings failed, checking fallback: {e}")
    job = _jobs_fallback.get(job_id)
    return job.result if job else None
