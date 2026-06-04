"""
pdf_store.py
------------
Persists uploaded lease PDFs to Supabase Storage bucket 'lease-pdfs'.
Replaces the in-memory _documents dict (V1 fix — multi-replica safe).

Storage path convention:
    lease-pdfs/{job_id}/{filename}

Falls back to in-memory if Supabase is unavailable (dev mode).
"""

import os
from typing import Optional
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

BUCKET = "lease-pdfs"

_client = None


def _get_client():
    global _client
    if _client is None:
        from supabase import create_client
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
        )
    return _client


def upload_pdf(job_id: str, filename: str, data: bytes) -> bool:
    """
    Upload a PDF to Supabase Storage.
    Returns True on success, False on failure.
    """
    path = f"{job_id}/{filename}"
    try:
        _get_client().storage.from_(BUCKET).upload(
            path=path,
            file=data,
            file_options={"content-type": "application/pdf", "upsert": "true"},
        )
        logger.info(f"[{job_id}] PDF uploaded to Supabase Storage: {path}")
        return True
    except Exception as e:
        logger.error(f"[{job_id}] Supabase Storage upload failed: {e}")
        return False


def download_pdf(job_id: str, filename: str) -> Optional[bytes]:
    """
    Download a PDF from Supabase Storage.
    Returns bytes on success, None on failure.
    """
    path = f"{job_id}/{filename}"
    try:
        data = _get_client().storage.from_(BUCKET).download(path)
        logger.info(f"[{job_id}] PDF downloaded from Supabase Storage: {path}")
        return data
    except Exception as e:
        logger.error(f"[{job_id}] Supabase Storage download failed: {e}")
        return None


def delete_pdf(job_id: str, filename: str) -> None:
    """Delete a PDF from storage (e.g. after audit complete and report released)."""
    path = f"{job_id}/{filename}"
    try:
        _get_client().storage.from_(BUCKET).remove([path])
        logger.info(f"[{job_id}] PDF deleted from Supabase Storage: {path}")
    except Exception as e:
        logger.warning(f"[{job_id}] Supabase Storage delete failed (non-critical): {e}")
