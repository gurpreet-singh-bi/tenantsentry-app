# TenantSentry.ai — Development Modes

## Two Modes, Always in Parallel

Every feature must be implemented in **both** modes simultaneously.
No feature ships with only a Live implementation.

---

## DEV Mode (`DEV_MODE=true`)

**Purpose:** Local development, testing, and UI work. Zero cost, zero risk.

| What | How |
|------|-----|
| Pipeline | `pipeline/dev_pipeline.py` — hardcoded NSW lease, 8 clauses, deterministic output |
| Database | In-memory fallback (jobs.py `_jobs_fallback` dict) |
| Embeddings | Skipped entirely |
| Claude API | Not called |
| VoyageAI | Not called |
| ABS API | Returns canned CPI data |
| Dispute letters | Template fallback — no Claude call |
| Speed | ~6s simulated (realistic UX), no real I/O wait |
| Cost | $0 |

**Use for:** All UI development, frontend changes, testing new features, demos, CI.

---

## LIVE Mode (`DEV_MODE=false`)

**Purpose:** Real audits against real leases. Production grade.

| What | How |
|------|-----|
| Pipeline | `pipeline/audit_pipeline.py` — real PDF parse, OCR, chunking |
| Database | Supabase (`audit_run`, `lease_chunks`, `invoice`, `dispute_letter` tables) |
| Embeddings | VoyageAI `voyage-3` model |
| Claude API | Opus for complex clauses, Sonnet for simple fields |
| ABS API | Live CPI data fetch with graceful degradation |
| Dispute letters | Claude Sonnet with jurisdiction-specific prompt |
| Speed | 30s–5min depending on PDF size and clause count |
| Cost | ~$0.10–$0.50 per audit (Claude tokens + VoyageAI embeddings) |

**Use for:** Real lease audits, pre-production testing, benchmarking.

---

## Switching Modes

### At startup (`.env`):
```
DEV_MODE=true   # Dev mode (default for local dev)
DEV_MODE=false  # Live mode (production)
```

### At runtime (UI toggle):
Click the **DEV / LIVE** badge in the top nav. Requires admin token.
Takes effect immediately for all new audit submissions.
In-flight audits complete in the mode they started.

### Via API (admin only):
```
POST /api/admin/mode/toggle
Header: X-Admin-Token: <your-admin-token>
```

### Current mode:
```
GET /api/mode
```

---

## The Dual-Mode Development Contract

### Rule 1: Every feature ships in both modes
When you add a new capability, implement it for both DEV and LIVE before merging.

### Rule 2: Pattern for new modules

```python
from api.mode import is_dev

def my_service(input_data):
    if is_dev():
        return _dev_my_service(input_data)   # deterministic, zero deps
    return _live_my_service(input_data)      # real API/DB call


def _dev_my_service(input_data):
    """
    DEV: Deterministic fake implementation.
    - Returns realistic, structured data covering all code paths
    - No external calls
    - Fast (< 100ms excluding deliberate UX sleep)
    """
    return {"result": "dev_value", "source": "dev"}


def _live_my_service(input_data):
    """
    LIVE: Production implementation.
    - Calls real external APIs / Supabase
    - Must handle failures gracefully (try/except + logged fallback)
    - Never crashes the request on external failure
    """
    try:
        # ... real implementation ...
        return real_result
    except Exception as e:
        logger.error(f"_live_my_service failed: {e}")
        raise
```

### Rule 3: Dev implementations must be realistic
Dev mode is for testing, not just for skipping errors. Dev data must:
- Cover the same frontend code paths as Live
- Return the same shape/schema as Live
- Include edge cases (HIGH flags, empty clauses, CPI flags, etc.)

### Rule 4: Live implementations must degrade gracefully
External services fail. Every Live implementation must:
- Wrap calls in try/except
- Log failures with `logger.error`
- Either raise a meaningful HTTPException or return a safe fallback
- Never silently swallow errors that affect the tenant

---

## File Map

| File | Dev | Live |
|------|-----|------|
| `pipeline/dev_pipeline.py` | ✓ | — |
| `pipeline/audit_pipeline.py` | — | ✓ |
| `api/mode.py` | Mode singleton + toggle logic | |
| `api/main.py` | `_get_pipeline()` routes to correct pipeline | |
| `api/jobs.py` | In-memory fallback | Supabase `audit_run` |
| `db/pdf_store.py` | In-memory `_documents` | Supabase Storage |
| `output/evidence_pack.py` | Template dispute letter | Claude Sonnet letter + live ABS |
| `vector_store/supabase_store.py` | Skipped (no embeddings in dev) | VoyageAI + pgvector |

---

## Adding a New Feature — Checklist

- [ ] Implement `_dev_<feature>()` with deterministic output
- [ ] Implement `_live_<feature>()` with real API/DB calls + error handling
- [ ] Route via `is_dev()` check
- [ ] Test in DEV mode (should work with no credentials)
- [ ] Test in LIVE mode (should work with real credentials)
- [ ] Update this file if the feature adds a new subsystem
