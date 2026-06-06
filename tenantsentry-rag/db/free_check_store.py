"""
free_check_store.py
-------------------
TenantSentry.ai — Supabase persistence for the free_check_run table.

DEV + LIVE dual-mode per the DEVELOPMENT_MODES.md contract.

DEV mode:  in-memory list, zero external deps, deterministic.
LIVE mode: Supabase upserts. Non-fatal — never raises to the caller.

Usage in the free-check job runner:
    from db.free_check_store import log_free_check, update_lead
    from api.mode import is_dev

    free_check_id = log_free_check(job_id, filename, jur, doc_type, teaser, full_result, pages)
    # later, when email is captured:
    update_lead(job_id, email)
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from loguru import logger


# ── In-memory DEV store (for test inspection / local analytics) ───────────────
_dev_store: list[dict] = []


# ── Public interface — delegates to DEV or LIVE based on mode ─────────────────

def log_free_check(
    job_id: str,
    filename: str,
    jurisdiction: str,
    doc_type: str,
    teaser: dict,
    full_result: dict,
    pages_analysed: Optional[int] = None,
) -> Optional[str]:
    """
    Persist a completed free-check run.
    Returns the free_check_id (UUID string) or None if persistence failed.
    Always non-fatal.
    """
    from api.mode import is_dev
    try:
        if is_dev():
            return _log_dev(job_id, filename, jurisdiction, doc_type, teaser, full_result, pages_analysed)
        return _log_live(job_id, filename, jurisdiction, doc_type, teaser, full_result, pages_analysed)
    except Exception as e:
        logger.warning(f"[FREE-CHECK-STORE] log_free_check failed (non-fatal): {e}")
        return None


def update_lead(job_id: str, email: str) -> None:
    """
    Capture the lead email for an existing free_check_run row.
    Called when the user submits their email via the teaser gate.
    Always non-fatal.
    """
    from api.mode import is_dev
    try:
        if is_dev():
            _update_lead_dev(job_id, email)
        else:
            _update_lead_live(job_id, email)
    except Exception as e:
        logger.warning(f"[FREE-CHECK-STORE] update_lead failed (non-fatal): {e}")


# ── DEV implementations ───────────────────────────────────────────────────────

def _log_dev(
    job_id: str,
    filename: str,
    jurisdiction: str,
    doc_type: str,
    teaser: dict,
    full_result: dict,
    pages_analysed: Optional[int],
) -> str:
    free_check_id = str(uuid.uuid4())
    row = _build_row(
        free_check_id, job_id, filename, jurisdiction, doc_type,
        teaser, full_result, pages_analysed,
    )
    _dev_store.append(row)
    logger.info(
        f"[FREE-CHECK-STORE][DEV] Logged free_check_id={free_check_id[:8]}… "
        f"job_id={job_id[:8]}… risk={teaser.get('risk_score')} "
        f"flags={teaser.get('total_flags')} pages={pages_analysed}"
    )
    return free_check_id


def _update_lead_dev(job_id: str, email: str) -> None:
    for row in _dev_store:
        if row.get("job_id") == job_id:
            row["email"] = email
            row["lead_captured_at"] = datetime.now(timezone.utc).isoformat()
            logger.info(f"[FREE-CHECK-STORE][DEV] Lead captured: job_id={job_id[:8]}… email={email}")
            return
    logger.warning(f"[FREE-CHECK-STORE][DEV] update_lead: job_id={job_id} not found")


# ── LIVE implementations ──────────────────────────────────────────────────────

def _log_live(
    job_id: str,
    filename: str,
    jurisdiction: str,
    doc_type: str,
    teaser: dict,
    full_result: dict,
    pages_analysed: Optional[int],
) -> Optional[str]:
    try:
        from db.audit_run_store import _get_client
        free_check_id = str(uuid.uuid4())
        row = _build_row(
            free_check_id, job_id, filename, jurisdiction, doc_type,
            teaser, full_result, pages_analysed,
        )
        _get_client().table("free_check_run").insert(row).execute()
        logger.info(
            f"[FREE-CHECK-STORE][LIVE] Persisted free_check_id={free_check_id[:8]}… "
            f"job_id={job_id[:8]}… risk={teaser.get('risk_score')}"
        )
        return free_check_id
    except Exception as e:
        logger.warning(f"[FREE-CHECK-STORE][LIVE] Supabase insert failed (non-fatal): {e}")
        return None


def _update_lead_live(job_id: str, email: str) -> None:
    try:
        from db.audit_run_store import _get_client
        _get_client().table("free_check_run").update({
            "email":            email,
            "lead_captured_at": datetime.now(timezone.utc).isoformat(),
        }).eq("job_id", job_id).execute()
        logger.info(f"[FREE-CHECK-STORE][LIVE] Lead captured: job_id={job_id[:8]}… email={email}")
    except Exception as e:
        logger.warning(f"[FREE-CHECK-STORE][LIVE] Lead upsert failed (non-fatal): {e}")


# ── Row builder ───────────────────────────────────────────────────────────────

def _build_row(
    free_check_id: str,
    job_id: str,
    filename: str,
    jurisdiction: str,
    doc_type: str,
    teaser: dict,
    full_result: dict,
    pages_analysed: Optional[int],
) -> dict:
    def _safe(v):
        """Ensure a value is JSON-serialisable; return None if empty/unencodeable."""
        if v is None or v == [] or v == {}:
            return None
        try:
            json.dumps(v)
            return v
        except (TypeError, ValueError):
            return str(v)

    jur = jurisdiction.upper()

    return {
        "free_check_id":        free_check_id,
        "job_id":               job_id,
        "filename":             filename,
        "jurisdiction":         jur,
        "doc_type":             doc_type or "lease",
        "source":               teaser.get("source", "live"),
        # Risk summary
        "risk_score":           teaser.get("risk_score"),
        "risk_level":           teaser.get("risk_level"),
        "pages_analysed":       pages_analysed,
        # Pipeline stats from AuditResult
        "raw_clause_count":     full_result.get("raw_clause_count"),
        "haiku_triage_count":   full_result.get("haiku_triage_count"),
        "sonnet_analysed_count": full_result.get("sonnet_analysed_count"),
        "opus_escalated_count": full_result.get("opus_escalated_count"),
        "total_clauses":        full_result.get("total_clauses"),
        "total_flags":          teaser.get("total_flags"),
        "high_flags":           teaser.get("high_flags"),
        # JSONB findings
        "top_flags":            _safe(teaser.get("top_flags")),
        "all_risk_flags":       _safe(full_result.get("all_risk_flags")),
        "extracted_rules":      _safe(full_result.get("extracted_rules")),
        "stage_costs":          _safe(full_result.get("stage_costs")),
        "stage_timings":        _safe(full_result.get("stage_timings")),
        "pipeline_warnings":    _safe(full_result.get("pipeline_warnings")),
        # Timestamps
        "completed_at":         datetime.now(timezone.utc).isoformat(),
    }
