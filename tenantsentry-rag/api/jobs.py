"""
jobs.py
-------
In-memory job state manager for async audit processing.
Jobs are keyed by UUID. State persists for the app lifetime (sufficient for MVP).
For production scale: swap _jobs dict for Supabase 'audit_run' table (G5).

Job lifecycle:
    queued → processing → complete → [awaiting_review] → released
                       → failed

Human-in-loop gate (G4):
    On complete, reviewed_by_human = False and released = False.
    Auditor must approve via /admin portal before report is accessible to tenant.
    review_job() sets reviewed_by_human = True + stores reviewer_notes.
    release_job() sets released = True — tenant can now download their report.
"""

import uuid
from datetime import datetime
from typing import Optional
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


class Job:
    def __init__(self, job_id: str, filename: str, jurisdiction: str, tenant_name: str):
        self.job_id = job_id
        self.filename = filename
        self.jurisdiction = jurisdiction
        self.tenant_name = tenant_name
        self.status = JobStatus.QUEUED
        self.progress = 0           # 0–100
        self.stage = "Queued"       # Human-readable current stage
        self.result = None          # AuditResult dict when complete
        self.error = None           # Error message if failed
        self.created_at = datetime.utcnow().isoformat()
        self.completed_at = None
        # ── Human-in-loop gate (G4) ──────────────────────────────────────────
        self.reviewed_by_human: bool = False   # auditor has approved findings
        self.reviewer_notes: Optional[str] = None
        self.reviewed_at: Optional[str] = None
        self.released: bool = False            # report visible to tenant
        self.released_at: Optional[str] = None

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


# ── In-memory stores ──────────────────────────────────────────────────────────
_jobs: dict[str, Job] = {}
_documents: dict[str, dict] = {}   # job_id → { filename, content_type, data: bytes }


def create_job(filename: str, jurisdiction: str, tenant_name: str) -> Job:
    job_id = str(uuid.uuid4())
    job = Job(job_id=job_id, filename=filename, jurisdiction=jurisdiction, tenant_name=tenant_name)
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


def update_job_progress(job_id: str, progress: int, stage: str) -> None:
    job = _jobs.get(job_id)
    if job:
        job.status = JobStatus.PROCESSING
        job.progress = progress
        job.stage = stage


def complete_job(job_id: str, result: dict) -> None:
    job = _jobs.get(job_id)
    if job:
        job.status = JobStatus.COMPLETE
        job.progress = 100
        job.stage = "Complete"
        job.result = result
        job.completed_at = datetime.utcnow().isoformat()


def fail_job(job_id: str, error: str) -> None:
    job = _jobs.get(job_id)
    if job:
        job.status = JobStatus.FAILED
        job.stage = "Failed"
        job.error = error
        job.completed_at = datetime.utcnow().isoformat()


def review_job(job_id: str, notes: Optional[str] = None) -> Optional[Job]:
    """Auditor approves findings. Sets reviewed_by_human = True."""
    job = _jobs.get(job_id)
    if job and job.status == JobStatus.COMPLETE:
        job.reviewed_by_human = True
        job.reviewer_notes = notes or ""
        job.reviewed_at = datetime.utcnow().isoformat()
    return job


def release_job(job_id: str) -> Optional[Job]:
    """Release report to tenant. Only callable after review."""
    job = _jobs.get(job_id)
    if job and job.reviewed_by_human:
        job.released = True
        job.released_at = datetime.utcnow().isoformat()
    return job


def store_document(job_id: str, filename: str, data: bytes, content_type: str = "application/pdf") -> None:
    """Persist original uploaded document for auditor download."""
    _documents[job_id] = {"filename": filename, "content_type": content_type, "data": data}


def get_document(job_id: str) -> Optional[dict]:
    return _documents.get(job_id)


def list_pending_review() -> list[Job]:
    """Return completed-but-unreviewed jobs, newest first."""
    return sorted(
        [j for j in _jobs.values() if j.status == JobStatus.COMPLETE and not j.reviewed_by_human],
        key=lambda j: j.completed_at or "",
        reverse=True,
    )


def list_reviewed() -> list[Job]:
    """Return reviewed jobs (released or awaiting release), newest first."""
    return sorted(
        [j for j in _jobs.values() if j.reviewed_by_human],
        key=lambda j: j.reviewed_at or "",
        reverse=True,
    )
