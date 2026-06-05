"""
chat_query_store.py
-------------------
TenantSentry.ai — Supabase CRUD layer for the chat_query table.

Logs every F-CHAT widget query for:
  - KB gap detection (matched_kb_article_id IS NULL → is_kb_gap = true)
  - Conversion tracking (converted_to_upload flag)
  - Future clustering / pain-point summarisation (deferred — no cluster table yet)

Dual-mode: DEV returns deterministic in-memory stubs; LIVE writes to Supabase.
All public functions return plain dicts or None — no ORM objects.

Called from api/main.py chat endpoint only.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# ── Supabase client (lazy, shared) ────────────────────────────────────────────
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
                postgrest_client_timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
            ),
        )
    return _client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── DEV mode in-memory store ──────────────────────────────────────────────────
# Resets on server restart — intentional for DEV.
_dev_queries: list[dict] = []


# ══════════════════════════════════════════════════════════════════════════════
# Write operations
# ══════════════════════════════════════════════════════════════════════════════

def log_query(
    *,
    session_id: str,
    raw_query: str,
    jurisdiction: Optional[str] = None,
    clause_type: Optional[str] = None,
    matched_kb_article_id: Optional[str] = None,
) -> dict:
    """
    INSERT a new chat_query row.

    DEV:  appends to in-memory list, returns the constructed dict.
    LIVE: inserts to Supabase chat_query table, returns inserted row.

    matched_kb_article_id=None means no KB article matched → is_kb_gap=true.
    """
    from api.mode import is_dev

    row = {
        "query_id":             str(uuid.uuid4()),
        "session_id":           session_id,
        "raw_query":            raw_query,
        "jurisdiction":         jurisdiction,
        "clause_type":          clause_type,
        "matched_kb_article_id": matched_kb_article_id,
        "is_kb_gap":            matched_kb_article_id is None,
        "converted_to_upload":  False,
        "created_at":           _now(),
    }

    if is_dev():
        _dev_queries.append(row)
        logger.debug(
            f"[DEV] chat_query logged: session={session_id[:8]}… "
            f"gap={row['is_kb_gap']} clause={clause_type} jur={jurisdiction}"
        )
        return row

    # LIVE — write to Supabase
    # is_kb_gap is a generated column; omit from INSERT
    insert_row = {k: v for k, v in row.items() if k not in ("is_kb_gap",)}
    try:
        result = _get_client().table("chat_query").insert(insert_row).execute()
        inserted = result.data[0] if result.data else row
        logger.info(
            f"chat_query logged: id={inserted.get('query_id', '')[:8]}… "
            f"gap={row['is_kb_gap']} clause={clause_type} jur={jurisdiction}"
        )
        return inserted
    except Exception as e:
        logger.error(f"chat_query insert failed: {e}")
        # Non-fatal — never let logging failure break the chat response
        return row


def mark_converted(session_id: str) -> int:
    """
    Set converted_to_upload=true for all chat_query rows with this session_id.
    Called when the same session submits a lease via POST /api/audit/submit.

    DEV:  updates in-memory list, returns count of updated rows.
    LIVE: updates Supabase, returns count.
    """
    from api.mode import is_dev

    if is_dev():
        count = 0
        for q in _dev_queries:
            if q["session_id"] == session_id and not q["converted_to_upload"]:
                q["converted_to_upload"] = True
                count += 1
        logger.debug(f"[DEV] mark_converted: session={session_id[:8]}… rows={count}")
        return count

    try:
        result = (
            _get_client()
            .table("chat_query")
            .update({"converted_to_upload": True})
            .eq("session_id", session_id)
            .eq("converted_to_upload", False)
            .execute()
        )
        count = len(result.data or [])
        logger.info(f"mark_converted: session={session_id[:8]}… rows={count}")
        return count
    except Exception as e:
        logger.error(f"mark_converted failed: {e}")
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# Read operations (admin / service role only)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_gap_queries(limit: int = 50) -> list[dict]:
    """
    Return the most recent KB gap queries (matched_kb_article_id IS NULL).
    Used by the admin dashboard to surface unanswered question patterns.

    DEV:  filters in-memory list.
    LIVE: queries Supabase via service key (bypasses RLS SELECT restriction).
    """
    from api.mode import is_dev

    if is_dev():
        gaps = [q for q in _dev_queries if q["is_kb_gap"]]
        return sorted(gaps, key=lambda x: x["created_at"], reverse=True)[:limit]

    try:
        result = (
            _get_client()
            .table("chat_query")
            .select("query_id, session_id, raw_query, jurisdiction, clause_type, created_at, converted_to_upload")
            .eq("is_kb_gap", True)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"fetch_gap_queries failed: {e}")
        return []


def fetch_recent_queries(limit: int = 100) -> list[dict]:
    """
    Return the most recent queries regardless of gap status.
    Used by the admin dashboard overview.

    DEV:  returns in-memory list (newest first).
    LIVE: queries Supabase.
    """
    from api.mode import is_dev

    if is_dev():
        return sorted(_dev_queries, key=lambda x: x["created_at"], reverse=True)[:limit]

    try:
        result = (
            _get_client()
            .table("chat_query")
            .select("query_id, session_id, raw_query, jurisdiction, clause_type, is_kb_gap, converted_to_upload, created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"fetch_recent_queries failed: {e}")
        return []
