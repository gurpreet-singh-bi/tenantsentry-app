"""
invoice_store.py
----------------
TenantSentry.ai — F14: Invoice CRUD Layer

Thin Supabase CRUD layer for the invoice table.
Mirrors the pattern in db/audit_run_store.py — plain dicts in/out,
no domain objects. Callers (api/main.py) own the business logic.

Dev mode: in-memory dict store — no Supabase required.
Live mode: Supabase postgrest client (same client singleton as audit_run_store).

Public API
----------
    insert_invoice(row: dict) -> dict
    get_invoice(invoice_id: str) -> dict | None
    get_by_hash(job_id: str, pdf_hash: str) -> dict | None
    list_invoices_for_job(job_id: str) -> list[dict]
    update_invoice(invoice_id: str, patch: dict) -> dict | None
    insert_anomaly_flag(row: dict) -> dict
    get_anomaly_flags(job_id: str, dismissed: bool = False) -> list[dict]
    get_anomaly_flags_for_invoice(invoice_id: str) -> list[dict]
    dismiss_anomaly_flag(flag_id: str, note: str = "") -> dict | None
"""

import os
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# ── Supabase client (shared singleton) ───────────────────────────────────────

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
                postgrest_client_timeout=httpx.Timeout(
                    connect=5.0, read=30.0, write=60.0, pool=5.0
                )
            ),
        )
    return _client


def _now() -> str:
    return datetime.now(_SYDNEY_TZ).isoformat()


# ── Dev-mode in-memory store ─────────────────────────────────────────────────

_dev_invoices: dict[str, dict] = {}        # invoice_id → row
_dev_anomaly_flags: dict[str, dict] = {}   # flag_id → row
_dev_id_counter = 0


def _dev_uuid() -> str:
    """Deterministic fake UUID for dev mode."""
    import uuid
    global _dev_id_counter
    _dev_id_counter += 1
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"dev-invoice-{_dev_id_counter}"))


# ── USE_SUPABASE guard (same logic as jobs.py) ────────────────────────────────

def _use_supabase() -> bool:
    return bool(
        os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")
    )


# ── Invoice CRUD ──────────────────────────────────────────────────────────────

def insert_invoice(row: dict) -> dict:
    """
    INSERT a new invoice row. Returns the inserted row (with id + created_at).

    Expected keys:
        job_id, invoice_type, period_start, period_end, amount_cents,
        line_items, pdf_hash, pdf_url, filename,
        reconciliation_result, recon_status
    """
    if _use_supabase():
        try:
            result = _get_client().table("invoice").insert(row).execute()
            inserted = result.data[0] if result.data else row
            logger.info(f"[invoice_store] Inserted invoice {inserted.get('id')} for job {row.get('job_id')}")
            return inserted
        except Exception as e:
            logger.error(f"[invoice_store] insert_invoice failed: {e}")
            raise

    # Dev fallback
    invoice_id = _dev_uuid()
    stored = {**row, "id": invoice_id, "created_at": _now()}
    _dev_invoices[invoice_id] = stored
    logger.debug(f"[DEV] Invoice {invoice_id} stored in-memory")
    return stored


def get_invoice(invoice_id: str) -> Optional[dict]:
    """SELECT a single invoice by PK."""
    if _use_supabase():
        try:
            result = (
                _get_client()
                .table("invoice")
                .select("*")
                .eq("id", invoice_id)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[invoice_store] get_invoice failed: {e}")
            return None

    return _dev_invoices.get(invoice_id)


def get_by_hash(job_id: str, pdf_hash: str) -> Optional[dict]:
    """
    SELECT the invoice for a given job + PDF hash.
    Returns existing row if same PDF was already uploaded — caller can skip re-processing.
    """
    if _use_supabase():
        try:
            result = (
                _get_client()
                .table("invoice")
                .select("*")
                .eq("job_id", job_id)
                .eq("pdf_hash", pdf_hash)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[invoice_store] get_by_hash failed: {e}")
            return None

    # Dev: linear scan
    for inv in _dev_invoices.values():
        if inv.get("job_id") == job_id and inv.get("pdf_hash") == pdf_hash:
            return inv
    return None


def list_invoices_for_job(job_id: str) -> list[dict]:
    """
    SELECT all invoices for a lease, ordered by period_start descending.
    Returns summary fields only (excludes heavy reconciliation_result JSONB).
    """
    if _use_supabase():
        try:
            result = (
                _get_client()
                .table("invoice")
                .select(
                    "id, job_id, invoice_type, period_start, period_end, "
                    "amount_cents, filename, recon_status, "
                    "reconciliation_result->total_disputed_cents, "
                    "created_at"
                )
                .eq("job_id", job_id)
                .order("period_start", desc=True, nullsfirst=False)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"[invoice_store] list_invoices_for_job failed: {e}")
            return []

    return sorted(
        [inv for inv in _dev_invoices.values() if inv.get("job_id") == job_id],
        key=lambda x: x.get("period_start") or "",
        reverse=True,
    )


def update_invoice(invoice_id: str, patch: dict) -> Optional[dict]:
    """
    UPDATE specific columns on an invoice row.
    Used to set recon_status + reconciliation_result after async processing.
    """
    if _use_supabase():
        try:
            result = (
                _get_client()
                .table("invoice")
                .update(patch)
                .eq("id", invoice_id)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[invoice_store] update_invoice failed: {e}")
            return None

    if invoice_id in _dev_invoices:
        _dev_invoices[invoice_id].update(patch)
        return _dev_invoices[invoice_id]
    return None


# ── Anomaly flag CRUD ─────────────────────────────────────────────────────────

def insert_anomaly_flag(row: dict) -> dict:
    """
    INSERT one anomaly flag row. Returns inserted row.

    Expected keys:
        job_id, invoice_id, check_name, category, severity,
        description, expected_cents, actual_cents, delta_pct,
        detection_layer
    """
    if _use_supabase():
        try:
            result = _get_client().table("anomaly_flag").insert(row).execute()
            inserted = result.data[0] if result.data else row
            logger.info(
                f"[invoice_store] Anomaly flag {inserted.get('id')} "
                f"({row.get('severity')}/{row.get('check_name')}) stored"
            )
            return inserted
        except Exception as e:
            logger.error(f"[invoice_store] insert_anomaly_flag failed: {e}")
            raise

    flag_id = _dev_uuid()
    stored = {**row, "id": flag_id, "dismissed": False, "created_at": _now()}
    _dev_anomaly_flags[flag_id] = stored
    logger.debug(f"[DEV] Anomaly flag {flag_id} stored in-memory")
    return stored


def get_anomaly_flags(job_id: str, include_dismissed: bool = False) -> list[dict]:
    """
    SELECT anomaly flags for a lease.
    By default excludes dismissed flags.
    Orders by severity (high first), then created_at desc.
    """
    if _use_supabase():
        try:
            q = (
                _get_client()
                .table("anomaly_flag")
                .select("*")
                .eq("job_id", job_id)
            )
            if not include_dismissed:
                q = q.eq("dismissed", False)
            result = q.order("created_at", desc=True).execute()
            rows = result.data or []
            # Sort: high → medium → low
            _sev_order = {"high": 0, "medium": 1, "low": 2}
            rows.sort(key=lambda r: (_sev_order.get(r.get("severity", "low"), 2), r.get("created_at", "")))
            return rows
        except Exception as e:
            logger.error(f"[invoice_store] get_anomaly_flags failed: {e}")
            return []

    flags = [f for f in _dev_anomaly_flags.values() if f.get("job_id") == job_id]
    if not include_dismissed:
        flags = [f for f in flags if not f.get("dismissed")]
    _sev_order = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: _sev_order.get(f.get("severity", "low"), 2))
    return flags


def get_anomaly_flags_for_invoice(invoice_id: str) -> list[dict]:
    """SELECT all anomaly flags (including dismissed) for a specific invoice."""
    if _use_supabase():
        try:
            result = (
                _get_client()
                .table("anomaly_flag")
                .select("*")
                .eq("invoice_id", invoice_id)
                .order("created_at", desc=True)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"[invoice_store] get_anomaly_flags_for_invoice failed: {e}")
            return []

    return [f for f in _dev_anomaly_flags.values() if f.get("invoice_id") == invoice_id]


def dismiss_anomaly_flag(flag_id: str, note: str = "") -> Optional[dict]:
    """SET dismissed=TRUE on a flag. Returns updated row or None."""
    patch = {
        "dismissed": True,
        "dismissed_at": _now(),
        "dismissed_note": note or None,
    }
    if _use_supabase():
        try:
            result = (
                _get_client()
                .table("anomaly_flag")
                .update(patch)
                .eq("id", flag_id)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[invoice_store] dismiss_anomaly_flag failed: {e}")
            return None

    if flag_id in _dev_anomaly_flags:
        _dev_anomaly_flags[flag_id].update(patch)
        return _dev_anomaly_flags[flag_id]
    return None
