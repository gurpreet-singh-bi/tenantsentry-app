"""
main.py
-------
TenantSentry.ai — FastAPI application.

Serves:
  - HTML frontend (Jinja2 templates)
  - REST API for async audit jobs
  - Static files (CSS/JS)

Page routes:
  GET /                    → redirect to /login (unauthenticated) or /dashboard
  GET /login               → login.html
  GET /signup              → signup.html
  GET /audit               → audit.html  (dark-theme audit flow prototype)
  GET /upload              → upload.html (Alpine.js upload/process/results SPA, wired to API)
  GET /dashboard           → dashboard.html  (tenant dashboard)
  GET /auditor             → auditor.html    (auditor QA portal — human-in-loop review)
  GET /admin               → admin.html      (super-admin portal — all portals end-to-end)
  GET /partners/dashboard  → partners_dashboard.html  (channel partner portal)
  GET /invoices            → invoices.html  (F14 invoice upload + F16 anomaly monitor)

Run locally:
    uvicorn api.main:app --reload --port 8000
"""

import asyncio
import hashlib
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import asynccontextmanager

from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request, Depends
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────
# Writes to logs/tenantsentry.log (rotated at 10MB, 7-day retention).
# Triage + per-clause lines land here so you can grep after a run.
_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
logger.add(
    _LOG_DIR / "tenantsentry.log",
    rotation="10 MB",
    retention="7 days",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{line} | {message}",
    enqueue=True,   # async-safe: writes happen off the event loop
)

# ── Path setup ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent   # tenantsentry-rag/
sys.path.insert(0, str(BASE_DIR))

from api.jobs import (
    create_job, get_job, get_job_result, update_job_progress, complete_job, fail_job, JobStatus,
    review_job, release_job, list_pending_review, list_reviewed, list_active, cancel_job,
    store_document, get_document,
    _USE_SUPABASE, _jobs_fallback, _cancelled_jobs,
)

# ── Mode management (DEV / LIVE) ──────────────────────────────────────────────
# Import after load_dotenv() so DEV_MODE env var is already set
from api.mode import Mode, is_dev, is_live, toggle as _toggle_mode, status as _mode_status

# Pipeline is selected dynamically at request time via _get_pipeline()
# so toggling mode at runtime immediately affects new audit submissions.
from pipeline.dev_pipeline  import run_dev_audit
from pipeline.audit_pipeline import run_audit as run_live_audit

def _get_pipeline():
    """Return the correct pipeline function for the current mode."""
    return run_dev_audit if is_dev() else run_live_audit

logger.info(f"TenantSentry.ai pipeline loaded — startup mode: {_mode_status()['mode'].upper()}")

# ── App factory ───────────────────────────────────────────────────────────────
# ── V2: Bounded thread pool for audit jobs ────────────────────────────────────
# FastAPI's default BackgroundTasks shares the anyio thread pool (40 threads).
# Long audits (50-page PDF + OCR + multiple LLM calls) can exhaust it under
# concurrent load, blocking ALL background tasks including health checks.
# Fix: dedicated pool, capped at MAX_CONCURRENT_AUDITS.
# Scale path: replace with Celery + Redis when >10 concurrent users (F-ASYNC2).
MAX_CONCURRENT_AUDITS = 4
_audit_executor = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_AUDITS,
    thread_name_prefix="ts-audit",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── V4: Validate model strings before accepting traffic ───────────────────
    from llm.router import OPUS_MODEL, SONNET_MODEL

    KNOWN_VALID_PREFIXES = ("claude-opus-", "claude-sonnet-", "claude-haiku-", "claude-3-")
    for label, model_str in [("OPUS_MODEL", OPUS_MODEL), ("SONNET_MODEL", SONNET_MODEL)]:
        if not model_str or model_str.startswith("your-") or "placeholder" in model_str.lower():
            logger.error(
                f"V4: {label}='{model_str}' looks like a placeholder. "
                "Set a real Anthropic model ID in .env or audits will fail."
            )
        elif not any(model_str.startswith(p) for p in KNOWN_VALID_PREFIXES):
            logger.warning(
                f"V4: {label}='{model_str}' doesn't match known Claude model patterns. "
                f"Expected one of: {KNOWN_VALID_PREFIXES}"
            )
        else:
            logger.info(f"V4: {label}='{model_str}' ✓")

    # V4: Live API connectivity check — only runs when starting in LIVE mode
    if is_live():
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key or api_key.startswith("sk-ant-your"):
            logger.error(
                "V4: ANTHROPIC_API_KEY is missing or placeholder — "
                "switch to DEV mode or set a real key in .env."
            )
        else:
            try:
                import anthropic as _anthropic
                _client = _anthropic.Anthropic(api_key=api_key)
                _client.messages.create(
                    model=SONNET_MODEL, max_tokens=1,
                    messages=[{"role": "user", "content": "ping"}],
                )
                logger.info(f"V4: API connectivity verified — model '{SONNET_MODEL}' accepted ✓")
            except Exception as e:
                logger.error(
                    f"V4: Startup API check failed (model='{SONNET_MODEL}'): {e}. "
                    "Audits will fail in LIVE mode until resolved."
                )
    else:
        logger.info("DEV mode — skipping API connectivity check")

    logger.info(
        f"TenantSentry.ai starting up | mode={_mode_status()['mode'].upper()} | "
        f"audit_workers={MAX_CONCURRENT_AUDITS}"
    )
    yield

    # ── Shutdown: drain the audit thread pool gracefully ─────────────────────
    logger.info("TenantSentry.ai shutting down — waiting for in-flight audits...")
    _audit_executor.shutdown(wait=True)
    logger.info("Audit executor stopped.")


app = FastAPI(
    title="TenantSentry.ai",
    description="AI-powered commercial lease audit for Australian tenants",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

# ── Mode toggle injection middleware ─────────────────────────────────────────
# Appends <script src="/static/mode-toggle.js"> to every HTML response.
# This is the ONLY place mode toggle code lives — no per-template changes needed.
_MODE_SCRIPT = b'<script src="/static/mode-toggle.js" defer></script>'

class ModeToggleMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            body = body.replace(b"</body>", _MODE_SCRIPT + b"\n</body>", 1)
            return StarletteResponse(
                content=body,
                status_code=response.status_code,
                media_type="text/html",
                headers={
                    k: v for k, v in response.headers.items()
                    if k.lower() not in ("content-length",)
                },
            )
        return response

app.add_middleware(ModeToggleMiddleware)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten in production to your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files & templates ──────────────────────────────────────────────────
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ── Constants ─────────────────────────────────────────────────────────────────
VALID_JURISDICTIONS = {"NSW", "VIC", "QLD", "SA", "WA", "TAS", "ACT", "NT"}
MAX_FILE_SIZE_MB = 50

# ── Admin auth ────────────────────────────────────────────────────────────────
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme-set-in-env")
_bearer = HTTPBearer(auto_error=False)


def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> None:
    """
    Accepts token via:
      - Authorization: Bearer <token>  (API calls)
      - X-Admin-Token: <token>         (browser fetch with custom header)
      - Cookie: admin_token=<token>    (portal page after login)
    """
    token = None
    if credentials:
        token = credentials.credentials
    if not token:
        token = request.headers.get("X-Admin-Token")
    if not token:
        token = request.cookies.get("admin_token")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Partner auth ──────────────────────────────────────────────────────────────
# Dev mode: validates against PARTNER_TOKEN env var (same pattern as admin).
# Live mode: TODO — validate email+password against channel_partner_user table
#            in Supabase, then issue a signed JWT cookie.
#
# Portal: tenantsentry.ai/partners/login
# Auditor: tenantsentry.ai/auditor  (audit QA portal)
# Admin:   tenantsentry.ai/admin    (super-admin, all portals)
# ─────────────────────────────────────────────────────────────────────────────
PARTNER_TOKEN = os.environ.get("PARTNER_TOKEN", "partner-changeme-set-in-env")

# Dev-mode demo credentials  (email → maps to a known partner in DEV seed data)
_PARTNER_DEV_CREDENTIALS = {
    "partner@tenantsentry.ai": "partner1234",
}


def require_partner(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> None:
    """
    Guard for partner-only API routes.
    Accepts token via:
      - Authorization: Bearer <token>
      - X-Partner-Token: <token>
      - Cookie: partner_token=<token>
    """
    token = None
    if credentials:
        token = credentials.credentials
    if not token:
        token = request.headers.get("X-Partner-Token")
    if not token:
        token = request.cookies.get("partner_token")
    if not token or token != PARTNER_TOKEN:
        raise HTTPException(status_code=401, detail="Partner authentication required")


# ══════════════════════════════════════════════════════════════════════════════
# Frontend routes
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Root — redirect to login until auth is wired."""
    return RedirectResponse(url="/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page (dark theme)."""
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    """Signup wizard — Account → Business → Plan+Payment → Verify."""
    return templates.TemplateResponse("signup.html", {"request": request})


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    """Dark-theme audit flow prototype (F1→F2→F4–F6→F9)."""
    return templates.TemplateResponse("audit.html", {"request": request})




@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Tenant dashboard — shown after login (F12)."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ══════════════════════════════════════════════════════════════════════════════
# Channel Partner portal — tenantsentry.ai/partners/*
# Separate login path from tenants: /partners/login vs /login
# Admin subdomain: admin.tenantsentry.ai (handled at infra/Nginx level)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/partners/login", response_class=HTMLResponse)
async def partners_login_page(request: Request):
    """Channel Partner login — invite-only, separate from tenant /login."""
    return templates.TemplateResponse("partners_login.html", {"request": request})


@app.get("/partners/dashboard", response_class=HTMLResponse)
async def partners_dashboard_page(request: Request):
    """Channel Partner portal — multi-client management, white-label delivery."""
    return templates.TemplateResponse("partners_dashboard.html", {"request": request})


@app.post("/api/partners/login")
async def partner_login(request: Request):
    """
    Authenticate a channel partner user.

    DEV mode:  validates against _PARTNER_DEV_CREDENTIALS dict.
    LIVE mode: TODO — validate against channel_partner_user table in Supabase,
               issue JWT, store in httpOnly cookie.

    Returns { ok: true } + sets partner_token cookie on success.
    """
    body = await request.json()
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if is_dev():
        # Dev mode: simple credential check against seed data
        expected_pw = _PARTNER_DEV_CREDENTIALS.get(email)
        if not expected_pw or password != expected_pw:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        token = PARTNER_TOKEN
    else:
        # Live mode: validate against Supabase channel_partner_user table
        # TODO (F-PARTNER-AUTH): replace with real Supabase auth call
        # For now fall back to token check to avoid blocking development
        if email not in _PARTNER_DEV_CREDENTIALS or password != _PARTNER_DEV_CREDENTIALS[email]:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        token = PARTNER_TOKEN

    response = JSONResponse({"ok": True, "mode": "dev" if is_dev() else "live"})
    response.set_cookie(
        key="partner_token",
        value=token,
        httponly=True,
        samesite="strict",
        max_age=86400 * 30,   # 30 days (partners log in daily)
    )
    logger.info(f"Partner login: {email} [{_mode_status()['mode'].upper()}]")
    return response


@app.post("/api/partners/logout")
async def partner_logout():
    """Clear partner session cookie."""
    response = JSONResponse({"ok": True})
    response.delete_cookie("partner_token")
    return response


@app.get("/api/partners/clients")
async def partner_clients(_: None = Depends(require_partner)):
    """
    Return the list of tenant clients managed by this partner.

    DEV mode:  returns seed data from migration 005.
    LIVE mode: queries partner_client + organisation tables for this partner_id.
    """
    if is_dev():
        return JSONResponse({"clients": [
            {"id": "1", "name": "Smith Retail Pty Ltd",   "location": "Sydney CBD, NSW",    "leases": 3, "status": "active",   "savings": 12400},
            {"id": "2", "name": "Green Bean Café Group",  "location": "Melbourne, VIC",     "leases": 5, "status": "review",   "savings": 8750 },
            {"id": "3", "name": "FastFit Gyms Australia", "location": "Brisbane, QLD",      "leases": 8, "status": "active",   "savings": 31200},
            {"id": "4", "name": "Corner Pharmacy Network","location": "Perth, WA",           "leases": 2, "status": "overdue",  "savings": 0    },
            {"id": "5", "name": "Harbour View Dental",    "location": "North Sydney, NSW",  "leases": 1, "status": "active",   "savings": 4100 },
        ]})
    # TODO (F-PARTNER-CLIENTS): query Supabase partner_client table
    raise HTTPException(status_code=501, detail="Live partner clients API not yet implemented")


@app.get("/api/partners/stats")
async def partner_stats(_: None = Depends(require_partner)):
    """Aggregate stats for the partner dashboard header."""
    if is_dev():
        return JSONResponse({
            "total_clients":   5,
            "active_audits":   2,
            "total_savings":   56450,
            "revenue_share":   1694,
        })
    raise HTTPException(status_code=501, detail="Live partner stats API not yet implemented")


# ══════════════════════════════════════════════════════════════════════════════
# Health
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/health")
def health():
    from api.jobs import _supabase_ok
    supabase_state = "connected" if _supabase_ok() else ("mock" if not _USE_SUPABASE else "error")
    return {
        "status": "ok",
        "service": "TenantSentry.ai",
        "version": "1.0.0",
        "supabase": supabase_state,
        "pipeline_mode": "live" if not is_dev() else "dev",
    }


@app.get("/api/mode")
def get_mode():
    """Current runtime mode — DEV or LIVE. Polled by the nav toggle button."""
    return JSONResponse(_mode_status())


@app.post("/api/admin/mode/toggle")
async def toggle_mode():
    """
    Toggle between DEV and LIVE mode at runtime.
    No auth required — developer tool, not a user-facing feature.
    DEV  → zero API calls, deterministic data, free.
    LIVE → real Claude API, Supabase, VoyageAI.
    Takes effect immediately for all new audit submissions.
    """
    new_mode = _toggle_mode()
    logger.info(f"Runtime mode toggled → {new_mode.upper()}")
    return JSONResponse(_mode_status())


# ══════════════════════════════════════════════════════════════════════════════
# F-CHAT: Query logging
# ══════════════════════════════════════════════════════════════════════════════

VALID_CLAUSE_TYPES = {
    "rent_review", "outgoings", "make_good",
    "options", "holding_over", "land_tax", "other",
}


class ChatQueryRequest(BaseModel):
    session_id: str                        # anonymous browser session UUID
    raw_query: str
    jurisdiction: str = ""                 # optional — inferred from query context
    clause_type: str = ""                  # optional — caller can pass if already classified
    matched_kb_article_id: str = ""        # empty string = KB gap


@app.post("/api/chat/query")
async def log_chat_query(body: ChatQueryRequest):
    """
    Log an F-CHAT widget query to chat_query.

    Called by the website chat widget after every user message, passing:
      - session_id: anonymous browser UUID (persisted in sessionStorage)
      - raw_query:  verbatim user text
      - jurisdiction, clause_type: optional — populated when KB search infers them
      - matched_kb_article_id: the article slug returned by vector search,
        or empty string if no article cleared the similarity threshold (KB gap)

    Returns { query_id, is_kb_gap } so the front end can branch on gap state
    (e.g. show "We're working on that — upload your lease for a personalised answer").

    Non-fatal: logging failure never breaks the chat response.
    """
    from db.chat_query_store import log_query

    jur = body.jurisdiction.upper().strip() or None
    if jur and jur not in VALID_JURISDICTIONS:
        jur = None  # silently drop invalid jurisdiction rather than erroring

    clause = body.clause_type.strip().lower() or None
    if clause and clause not in VALID_CLAUSE_TYPES:
        clause = "other"

    matched = body.matched_kb_article_id.strip() or None

    row = log_query(
        session_id=body.session_id,
        raw_query=body.raw_query.strip(),
        jurisdiction=jur,
        clause_type=clause,
        matched_kb_article_id=matched,
    )

    return JSONResponse({
        "ok": True,
        "query_id": row.get("query_id"),
        "is_kb_gap": row.get("is_kb_gap", matched is None),
    })


@app.get("/api/admin/chat/gaps")
async def admin_chat_gaps(limit: int = 50, _: None = Depends(require_admin)):
    """
    Admin: return the most recent KB gap queries (no article matched).
    Used to prioritise new KB article topics.
    """
    from db.chat_query_store import fetch_gap_queries
    return JSONResponse({"gaps": fetch_gap_queries(limit=limit)})


@app.get("/api/admin/chat/queries")
async def admin_chat_queries(limit: int = 100, _: None = Depends(require_admin)):
    """Admin: return recent chat queries regardless of gap status."""
    from db.chat_query_store import fetch_recent_queries
    return JSONResponse({"queries": fetch_recent_queries(limit=limit)})


# ══════════════════════════════════════════════════════════════════════════════
# Audit API
# ══════════════════════════════════════════════════════════════════════════════

VALID_DOC_TYPES = {"lease", "outgoings", "invoice", "amendment", "other"}
VALID_IMG_EXTS  = {".pdf", ".png", ".jpg", ".jpeg"}


@app.post("/api/audit/submit")
async def submit_audit(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(..., description="Lease PDF plus any supporting documents"),
    doc_types: list[str] = Form(..., description="Doc type per file: lease|outgoings|invoice|amendment|other"),
    jurisdiction: str = Form("", description="State code (NSW/VIC/QLD/WA/SA/TAS/ACT/NT). Optional — auto-detected from lease if omitted."),
    tenant_name: str = Form(""),
    chat_session_id: str = Form(""),
    # AQ-NEW-5: Premises classification fields — all optional, auto-detected from lease if omitted
    premises_use: str = Form("", description="retail|office|industrial|mixed|other — auto-detected if blank"),
    entity_type: str = Form("", description="individual|company|trust|government — auto-detected if blank"),
    gla_sqm: Optional[float] = Form(None, description="Gross lettable area in sqm — auto-detected if blank"),
):
    """
    Submit one or more documents for async audit.

    files[0] must be the primary lease (doc_types[0]=="lease").
    Additional files may be outgoings schedules, invoices, or amendments.
    Returns job_id immediately; poll /api/audit/status/{job_id} for progress.

    jurisdiction, premises_use, entity_type, and gla_sqm are all optional.
    If omitted, the pipeline auto-detects them from the lease document (AG1-EARLY).
    """
    jur = jurisdiction.upper().strip() if jurisdiction.strip() else ""
    if jur and jur not in VALID_JURISDICTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid jurisdiction '{jur}'. Must be one of: {sorted(VALID_JURISDICTIONS)}"
        )

    # AQ-NEW-5: Classify premises to determine applicable statute.
    # Only pre-classify at submit time when jurisdiction is already known.
    # When jurisdiction is blank, classification is deferred to the pipeline (AG1-EARLY step).
    _classification = None
    _statute_prompt_block = ""
    if jur:
        from services.premises_classification import classify_premises, build_statute_prompt_block
        _classification = classify_premises(
            premises_use=premises_use or "other",
            jurisdiction=jur,
            gla_sqm=gla_sqm,
            entity_type=entity_type or "company",
        )
        _statute_prompt_block = build_statute_prompt_block(_classification)
        logger.info(
            f"AQ-NEW-5 classification: premises_use={premises_use} entity_type={entity_type} "
            f"gla_sqm={gla_sqm} → statute={_classification.statute_code} "
            f"is_retail={_classification.is_retail}"
        )
    else:
        logger.info("Jurisdiction not provided — classification deferred to pipeline auto-detection (AG1-EARLY)")

    if len(files) != len(doc_types):
        raise HTTPException(
            status_code=400,
            detail=f"files and doc_types must have the same length ({len(files)} vs {len(doc_types)})."
        )

    # Validate doc types
    invalid_types = [dt for dt in doc_types if dt not in VALID_DOC_TYPES]
    if invalid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid doc_type(s): {invalid_types}. Must be one of: {sorted(VALID_DOC_TYPES)}"
        )

    # Identify lease file — must be exactly one
    lease_indices = [i for i, dt in enumerate(doc_types) if dt == "lease"]
    if not lease_indices:
        raise HTTPException(status_code=400, detail="At least one file must have doc_type='lease'.")
    if len(lease_indices) > 1:
        raise HTTPException(status_code=400, detail="Only one file may have doc_type='lease'.")

    lease_idx = lease_indices[0]
    lease_file = files[lease_idx]

    if not lease_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="The lease document must be a PDF file.")

    # Read and validate all files
    file_data: list[dict] = []
    submission_warnings: list[str] = []

    for i, (f, dt) in enumerate(zip(files, doc_types)):
        ext = "." + f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in VALID_IMG_EXTS:
            submission_warnings.append(
                f"'{f.filename}' has unsupported extension '{ext}' — accepted: PDF, JPG, PNG. File skipped."
            )
            continue

        content = await f.read()
        size_mb = len(content) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            submission_warnings.append(
                f"'{f.filename}' exceeds {MAX_FILE_SIZE_MB}MB limit ({size_mb:.1f}MB) — file skipped."
            )
            continue

        file_data.append({
            "filename": f.filename,
            "doc_type": dt,
            "content": content,
            "size_bytes": len(content),
        })

    if not any(fd["doc_type"] == "lease" for fd in file_data):
        raise HTTPException(status_code=400, detail="Lease file could not be read or was too large.")

    lease_data = next(fd for fd in file_data if fd["doc_type"] == "lease")
    additional_data = [fd for fd in file_data if fd["doc_type"] != "lease"]

    document_hash = hashlib.sha256(lease_data["content"]).hexdigest()
    logger.info(
        f"Multi-doc submit: lease={lease_data['filename']} "
        f"additional={[fd['filename'] for fd in additional_data]} | "
        f"jurisdiction={jur} | hash={document_hash[:12]}…"
    )

    job = create_job(
        filename=lease_data["filename"],
        jurisdiction=jur or "",       # empty if auto-detection deferred to pipeline
        tenant_name=tenant_name.strip() or "Unknown",
        # AQ-NEW-5 — populated at submit time only when jurisdiction was provided;
        # otherwise the pipeline fills these in via AG1-EARLY auto-detection
        premises_use=premises_use or None,
        entity_type=entity_type or None,
        gla_sqm=gla_sqm,
        applicable_statute=_classification.applicable_statute if _classification else None,
        statute_code=_classification.statute_code if _classification else None,
        is_retail_lease=_classification.is_retail if _classification else None,
    )

    # Register all uploaded doc metadata on the job immediately (visible in auditor portal)
    from api.jobs import store_uploaded_doc_meta
    for fd in file_data:
        store_uploaded_doc_meta(
            job_id=job.job_id,
            filename=fd["filename"],
            doc_type=fd["doc_type"],
            size_bytes=fd["size_bytes"],
            status="queued",
        )

    logger.info(f"Job {job.job_id} created: {lease_data['filename']} | {jur} | docs={len(file_data)}")

    # F-CHAT attribution
    if chat_session_id.strip():
        try:
            from db.chat_query_store import mark_converted
            n = mark_converted(chat_session_id.strip())
            if n:
                logger.info(f"F-CHAT conversion: session={chat_session_id[:8]}… queries_attributed={n}")
        except Exception as e:
            logger.warning(f"F-CHAT mark_converted failed (non-fatal): {e}")

    # Persist primary lease PDF for auditor download
    store_document(job.job_id, lease_data["filename"], lease_data["content"])

    background_tasks.add_task(
        _schedule_audit_job,
        job_id=job.job_id,
        pdf_bytes=lease_data["content"],
        filename=lease_data["filename"],
        jurisdiction=jur,              # may be "" — pipeline auto-detects via AG1-EARLY
        tenant_name=tenant_name.strip() or "Unknown",
        document_hash=document_hash,
        additional_docs_data=additional_data,
        submission_warnings=submission_warnings,
        # AQ-NEW-5 — may be None if jurisdiction was not provided; pipeline fills them in
        premises_use=premises_use or None,
        entity_type=entity_type or None,
        gla_sqm=gla_sqm,
        applicable_statute=_classification.applicable_statute if _classification else None,
        statute_code=_classification.statute_code if _classification else None,
        is_retail_lease=_classification.is_retail if _classification else None,
        statute_prompt_block=_statute_prompt_block,
    )

    response_payload = {"job_id": job.job_id, "status": "queued", "doc_count": len(file_data)}
    if submission_warnings:
        response_payload["warnings"] = submission_warnings

    return JSONResponse(response_payload)


@app.get("/api/jobs/{job_id}/documents")
async def get_job_documents(job_id: str):
    """Return list of all uploaded documents for a job with their processing status."""
    from api.jobs import get_job as _get_job, get_uploaded_docs
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    docs = get_uploaded_docs(job_id)
    return JSONResponse({"job_id": job_id, "documents": docs})


async def _schedule_audit_job(
    job_id: str,
    pdf_bytes: bytes,
    filename: str,
    jurisdiction: str,
    tenant_name: str,
    document_hash: str,
    additional_docs_data: list[dict] = None,
    submission_warnings: list[str] = None,
    # AQ-NEW-5: premises classification
    premises_use: str = None,
    entity_type: str = None,
    gla_sqm: float = None,
    applicable_statute: str = None,
    statute_code: str = None,
    is_retail_lease: bool = None,
    statute_prompt_block: str = "",
) -> None:
    """
    V2: Async wrapper that dispatches the blocking audit pipeline to the bounded
    thread executor. Writes temp files for all docs, passes additional_docs to
    pipeline for outgoings/invoice reconciliation, then cleans up.
    """
    from api.jobs import update_uploaded_doc_status
    loop = asyncio.get_event_loop()
    pipeline = _get_pipeline()

    def _run():
        tmp_paths_to_clean: list[str] = []

        # Write lease to temp file
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            lease_tmp_path = tmp.name
        tmp_paths_to_clean.append(lease_tmp_path)

        # Write additional docs to temp files
        additional_docs: list[dict] = []
        if additional_docs_data:
            for fd in additional_docs_data:
                ext = "." + fd["filename"].rsplit(".", 1)[-1].lower() if "." in fd["filename"] else ".pdf"
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    tmp.write(fd["content"])
                    tmp_path = tmp.name
                tmp_paths_to_clean.append(tmp_path)
                additional_docs.append({
                    "path": tmp_path,
                    "doc_type": fd["doc_type"],
                    "filename": fd["filename"],
                })
                update_uploaded_doc_status(job_id, fd["filename"], "processing")

        try:
            update_uploaded_doc_status(job_id, filename, "processing")

            result = pipeline(
                pdf_path=lease_tmp_path,
                jurisdiction=jurisdiction,
                tenant_name=tenant_name,
                job_id=job_id,
                document_hash=document_hash,
                progress_callback=lambda pct, stage: update_job_progress(job_id, pct, stage),
                additional_docs=additional_docs if additional_docs else None,
                # AQ-NEW-5: premises classification
                premises_use=premises_use,
                entity_type=entity_type,
                gla_sqm=gla_sqm,
                applicable_statute=applicable_statute,
                statute_code=statute_code,
                is_retail_lease=is_retail_lease,
                statute_prompt_block=statute_prompt_block,
            )

            result_dict = (
                result.model_dump(mode="json") if hasattr(result, "model_dump")
                else (result if isinstance(result, dict) else result.__dict__)
            )

            # Merge any submission-time warnings into pipeline warnings
            if submission_warnings:
                result_dict.setdefault("pipeline_warnings", [])
                result_dict["pipeline_warnings"] = submission_warnings + result_dict["pipeline_warnings"]

            # Update doc statuses from reconciliation results
            update_uploaded_doc_status(job_id, filename, "complete")
            for recon in result_dict.get("reconciliation_results", []):
                doc_fn = recon.get("doc_filename", "")
                recon_status = recon.get("engine_status", "complete")
                recon_warnings = recon.get("warnings", [])
                mapped = "complete" if recon_status == "complete" else (
                    "failed" if recon_status == "failed" else "partial"
                )
                update_uploaded_doc_status(job_id, doc_fn, mapped, recon_warnings)

            complete_job(job_id, result_dict)

        except Exception as e:
            logger.exception(f"[{job_id}] Audit pipeline failed: {e}")
            update_uploaded_doc_status(job_id, filename, "failed")
            for fd in (additional_docs_data or []):
                update_uploaded_doc_status(job_id, fd["filename"], "failed")
            fail_job(job_id, str(e))
        finally:
            for p in tmp_paths_to_clean:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    try:
        await asyncio.wait_for(
            loop.run_in_executor(_audit_executor, _run),
            timeout=3600,
        )
    except asyncio.TimeoutError:
        logger.error(f"[{job_id}] Audit timed out after 3600s — marking failed")
        fail_job(job_id, "Audit timed out after 1 hour")
    except Exception as e:
        logger.exception(f"[{job_id}] Executor dispatch failed: {e}")
        fail_job(job_id, str(e))


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    """Reports library — all released audit reports."""
    return templates.TemplateResponse("reports.html", {"request": request})


@app.get("/invoices", response_class=HTMLResponse)
async def invoices_page(request: Request):
    """Invoice Monitor — F14 upload + F16 anomaly flags per lease audit job."""
    return templates.TemplateResponse("invoices.html", {"request": request})


@app.get("/portal", response_class=HTMLResponse)
async def tenant_portal_page(request: Request):
    """Tenant Portal — Portfolio Dashboard + Lease Shield workspace."""
    return templates.TemplateResponse("tenant_portal.html", {"request": request})


@app.get("/api/reports")
def list_reports():
    """
    Return all complete audit jobs regardless of release status.
    Frontend shows status: released = downloadable, complete = pending admin review, failed = error.
    """
    if _USE_SUPABASE:
        try:
            from db.audit_run_store import _get_client
            result = _get_client().table("audit_run").select(
                "job_id, filename, jurisdiction, tenant_name, status, created_at, completed_at, released_at, released, reviewed_by_human, findings"
            ).in_("status", ["complete", "failed"]).order("completed_at", desc=True).execute()
            # Extract risk_score from findings JSONB for display
            rows = []
            for r in (result.data or []):
                findings = r.pop("findings", None) or {}
                r["risk_score"] = findings.get("risk_score")
                r["total_clauses"] = findings.get("total_clauses")
                rows.append(r)
            return JSONResponse({"reports": rows})
        except Exception as e:
            logger.error(f"Failed to fetch reports: {e}")
    jobs = [j.to_dict() for j in _jobs_fallback.values() if j.status.value in ("complete", "failed")]
    return JSONResponse({"reports": jobs})


# ── G3: Invoice type ─────────────────────────────────────────────────────────

VALID_INVOICE_TYPES = {"estimate", "actuals", "monthly_rent"}


class InvoiceSubmitRequest(BaseModel):
    job_id: str
    invoice_type: str        # G3: 'estimate' | 'actuals' | 'monthly_rent'
    period_start: str = ""   # ISO date YYYY-MM-DD
    period_end: str = ""
    amount_cents: int = 0    # total in AUD cents
    line_items: list = []    # [{name, amount_cents, category}]


@app.post("/api/invoice/submit")
async def submit_invoice(body: InvoiceSubmitRequest):
    """
    G3: Record an invoice against a completed audit job.
    invoice_type drives the correct audit logic:
      'estimate'     → compare against lease's estimated outgoings schedule
      'actuals'      → trigger EOFY reconciliation audit
      'monthly_rent' → feed into ongoing anomaly monitor (F16)
    """
    if body.invoice_type not in VALID_INVOICE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid invoice_type '{body.invoice_type}'. Must be one of: {sorted(VALID_INVOICE_TYPES)}"
        )

    job = get_job(body.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    if _USE_SUPABASE:
        try:
            from db.audit_run_store import _get_client
            row = {
                "job_id":       body.job_id,
                "invoice_type": body.invoice_type,
                "period_start": body.period_start or None,
                "period_end":   body.period_end or None,
                "amount_cents": body.amount_cents or None,
                "line_items":   body.line_items or [],
            }
            result = _get_client().table("invoice").insert(row).execute()
            invoice_id = result.data[0]["id"] if result.data else None
            logger.info(f"Invoice {invoice_id} ({body.invoice_type}) stored for job {body.job_id}")
            return JSONResponse({"ok": True, "invoice_id": invoice_id, "invoice_type": body.invoice_type})
        except Exception as e:
            logger.error(f"Invoice insert failed: {e}")
            raise HTTPException(status_code=500, detail="Failed to store invoice.")

    # Dev mode — acknowledge without persisting
    logger.info(f"[DEV] Invoice received: job={body.job_id} type={body.invoice_type} amount={body.amount_cents}c")
    return JSONResponse({"ok": True, "invoice_id": None, "invoice_type": body.invoice_type})


# ── F14: Ongoing invoice upload ───────────────────────────────────────────────

@app.post("/api/invoice/upload")
async def upload_invoice(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Invoice PDF"),
    job_id: str = Form(..., description="Existing completed audit job ID"),
    invoice_type: str = Form(..., description="'monthly_rent' | 'estimate' | 'actuals'"),
    period_start: str = Form("", description="YYYY-MM-DD"),
    period_end: str = Form("", description="YYYY-MM-DD"),
):
    """
    F14: Upload an ongoing invoice PDF against an existing completed lease audit.

    Pipeline:
      1. Validate job exists and is complete
      2. SHA-256 dedup — return existing invoice_id if same PDF already processed
      3. OCR + parse line items via outgoings_parser
      4. Fetch clause_analyses from the original audit job
      5. Run outgoings_engine reconciliation
      6. Store invoice row with reconciliation_result
      7. Trigger F16 anomaly checks as a background task
      8. Return invoice_id + reconciliation summary

    Dev mode: returns canned mock reconciliation result. No OCR or Supabase calls.
    """
    import hashlib
    import tempfile

    from db.invoice_store import insert_invoice, get_by_hash, update_invoice
    from services.anomaly_monitor import run_anomaly_checks, maybe_send_alert

    if invoice_type not in VALID_INVOICE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid invoice_type. Must be one of: {sorted(VALID_INVOICE_TYPES)}",
        )

    # ── Dev mode path ─────────────────────────────────────────────────────────
    if is_dev():
        mock_recon = {
            "doc_filename": file.filename,
            "doc_type": "invoice",
            "period_start": period_start or None,
            "period_end": period_end or None,
            "total_claimed_cents": 450000,
            "total_disputed_cents": 85000,
            "engine_status": "complete",
            "warnings": [],
            "lease_clauses_used": ["Outgoings — Schedule A", "Outgoings — Management Fee"],
            "findings": [
                {
                    "line_item_description": "Building Management Fee",
                    "category": "management_fee",
                    "amount_cents": 85000,
                    "finding_type": "overcharge",
                    "severity": "high",
                    "explanation": (
                        "Management fee ($850) exceeds the 10% cap of net outgoings "
                        "stipulated in clause 8.3. Maximum permitted: $412.50."
                    ),
                    "legislation_ref": None,
                    "clause_ref": "Clause 8.3",
                    "disputed_amount_cents": 43750,
                },
                {
                    "line_item_description": "General Cleaning",
                    "category": "cleaning",
                    "amount_cents": 120000,
                    "finding_type": "compliant",
                    "severity": "info",
                    "explanation": "Cleaning charge is within lease-permitted outgoings categories.",
                    "legislation_ref": None,
                    "clause_ref": "Schedule A",
                    "disputed_amount_cents": 0,
                },
            ],
        }
        row = {
            "job_id": job_id,
            "invoice_type": invoice_type,
            "period_start": period_start or None,
            "period_end": period_end or None,
            "amount_cents": 450000,
            "line_items": [],
            "pdf_hash": "dev-mock-hash",
            "pdf_url": None,
            "filename": file.filename,
            "reconciliation_result": mock_recon,
            "recon_status": "complete",
        }
        stored = insert_invoice(row)
        invoice_id = stored.get("id")

        def _dev_anomaly_task(jid, iid):
            flags = run_anomaly_checks(jid, iid)
            maybe_send_alert(flags, jid)

        background_tasks.add_task(_dev_anomaly_task, job_id, invoice_id)

        return JSONResponse({
            "ok": True,
            "invoice_id": invoice_id,
            "invoice_type": invoice_type,
            "recon_status": "complete",
            "total_claimed_cents": mock_recon["total_claimed_cents"],
            "total_disputed_cents": mock_recon["total_disputed_cents"],
            "finding_count": len(mock_recon["findings"]),
            "reconciliation_result": mock_recon,
            "mode": "dev",
        })

    # ── Live mode path ────────────────────────────────────────────────────────

    # 1. Validate job
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if getattr(job, "status", None) is not None:
        job_status = job.status.value if hasattr(job.status, "value") else str(job.status)
        if job_status != "complete":
            raise HTTPException(
                status_code=400,
                detail=f"Job '{job_id}' is not complete (status: {job_status}). "
                       "Invoice upload requires a completed lease audit.",
            )

    # 2. Read file + SHA-256 dedup
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()

    existing = get_by_hash(job_id, pdf_hash)
    if existing:
        logger.info(f"[invoice/upload] Duplicate PDF detected — returning existing invoice {existing.get('id')}")
        return JSONResponse({
            "ok": True,
            "invoice_id": existing.get("id"),
            "invoice_type": existing.get("invoice_type"),
            "recon_status": existing.get("recon_status"),
            "total_claimed_cents": existing.get("amount_cents"),
            "total_disputed_cents": (
                (existing.get("reconciliation_result") or {}).get("total_disputed_cents", 0)
            ),
            "finding_count": len(
                ((existing.get("reconciliation_result") or {}).get("findings") or [])
            ),
            "duplicate": True,
        })

    # 3. Write to temp file for OCR
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    invoice_id = None
    try:
        # 4. OCR + parse line items
        from ingestion.outgoings_parser import parse_outgoings_pdf
        parsed = parse_outgoings_pdf(tmp_path)

        # Derive amount_cents from parsed result
        amount_cents = parsed.computed_total_cents or parsed.total_cents or 0

        # 5. Fetch clause_analyses + jurisdiction from original audit
        from db.audit_run_store import fetch_findings
        findings = fetch_findings(job_id) or {}
        clause_analyses = findings.get("clause_analyses") or []
        jurisdiction = findings.get("jurisdiction") or ""

        # 6. Reconcile
        recon_result_dict: dict = {}
        recon_status = "skipped"
        if clause_analyses:
            from pipeline.outgoings_engine import run_outgoings_reconciliation, reconciliation_result_to_dict
            recon = run_outgoings_reconciliation(
                parsed_outgoings=parsed,
                doc_filename=file.filename or "invoice.pdf",
                clause_analyses=clause_analyses,
                jurisdiction=jurisdiction,
            )
            recon_result_dict = reconciliation_result_to_dict(recon)
            recon_status = recon.engine_status
        else:
            recon_status = "skipped"
            recon_result_dict = {
                "doc_filename": file.filename,
                "doc_type": "invoice",
                "engine_status": "skipped",
                "warnings": ["No clause analyses found for this job — reconciliation skipped."],
                "findings": [],
                "total_claimed_cents": amount_cents,
                "total_disputed_cents": 0,
            }

        # 7. Store invoice row
        row = {
            "job_id": job_id,
            "invoice_type": invoice_type,
            "period_start": period_start or parsed.period_start or None,
            "period_end": period_end or parsed.period_end or None,
            "amount_cents": amount_cents,
            "line_items": [
                {
                    "category": li.category,
                    "description": li.description,
                    "amount_cents": li.amount_cents,
                    "gst_cents": li.gst_cents,
                }
                for li in (parsed.line_items or [])
            ],
            "pdf_hash": pdf_hash,
            "pdf_url": None,    # TODO: upload to Supabase Storage and store path
            "filename": file.filename,
            "reconciliation_result": recon_result_dict,
            "recon_status": recon_status,
        }
        stored = insert_invoice(row)
        invoice_id = stored.get("id")

        # 8. Background: anomaly checks + optional email alert
        def _anomaly_task(jid, iid):
            try:
                flags = run_anomaly_checks(jid, iid)
                maybe_send_alert(flags, jid)
            except Exception as exc:
                logger.error(f"[anomaly_task] Background anomaly check failed: {exc}")

        background_tasks.add_task(_anomaly_task, job_id, invoice_id)

        logger.info(
            f"[invoice/upload] Invoice {invoice_id} stored for job {job_id} — "
            f"type={invoice_type} amount={amount_cents}c recon={recon_status} "
            f"findings={len(recon_result_dict.get('findings') or [])}"
        )

        return JSONResponse({
            "ok": True,
            "invoice_id": invoice_id,
            "invoice_type": invoice_type,
            "recon_status": recon_status,
            "total_claimed_cents": amount_cents,
            "total_disputed_cents": recon_result_dict.get("total_disputed_cents", 0),
            "finding_count": len(recon_result_dict.get("findings") or []),
            "reconciliation_result": recon_result_dict,
            "mode": "live",
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[invoice/upload] Failed for job {job_id}: {e}")
        # If we stored the invoice row but reconciliation failed, mark it failed
        if invoice_id:
            try:
                update_invoice(invoice_id, {"recon_status": "failed"})
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Invoice processing failed: {e}")

    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass


@app.get("/api/invoice/history/{job_id}")
async def invoice_history(job_id: str):
    """
    F14: List all invoices filed against a completed lease audit, newest first.

    Returns summary per invoice: type, period, amount, disputed amount,
    finding count, reconciliation status, and anomaly flag count.
    Powers the Invoice History tab in the Audit Centre UI (F11).
    """
    from db.invoice_store import list_invoices_for_job, get_anomaly_flags_for_invoice

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if is_dev():
        # Return deterministic mock history
        return JSONResponse({
            "job_id": job_id,
            "invoices": [
                {
                    "invoice_id": "dev-inv-001",
                    "invoice_type": "monthly_rent",
                    "period_start": "2026-05-01",
                    "period_end": "2026-05-31",
                    "amount_cents": 450000,
                    "total_disputed_cents": 85000,
                    "recon_status": "complete",
                    "finding_count": 2,
                    "anomaly_flag_count": 1,
                    "filename": "may-2026-rent-invoice.pdf",
                },
                {
                    "invoice_id": "dev-inv-000",
                    "invoice_type": "monthly_rent",
                    "period_start": "2026-04-01",
                    "period_end": "2026-04-30",
                    "amount_cents": 440000,
                    "total_disputed_cents": 0,
                    "recon_status": "complete",
                    "finding_count": 0,
                    "anomaly_flag_count": 0,
                    "filename": "apr-2026-rent-invoice.pdf",
                },
            ],
            "total_invoices": 2,
            "total_disputed_cents": 85000,
            "mode": "dev",
        })

    invoices = list_invoices_for_job(job_id)
    result = []
    total_disputed = 0
    for inv in invoices:
        inv_id = str(inv.get("id", ""))
        recon = inv.get("reconciliation_result") or {}
        disputed = recon.get("total_disputed_cents") or 0
        total_disputed += disputed
        anomaly_flags = get_anomaly_flags_for_invoice(inv_id)
        result.append({
            "invoice_id": inv_id,
            "invoice_type": inv.get("invoice_type"),
            "period_start": inv.get("period_start"),
            "period_end": inv.get("period_end"),
            "amount_cents": inv.get("amount_cents"),
            "total_disputed_cents": disputed,
            "recon_status": inv.get("recon_status"),
            "finding_count": len(recon.get("findings") or []),
            "anomaly_flag_count": sum(1 for f in anomaly_flags if not f.get("dismissed")),
            "filename": inv.get("filename"),
        })

    return JSONResponse({
        "job_id": job_id,
        "invoices": result,
        "total_invoices": len(result),
        "total_disputed_cents": total_disputed,
        "mode": "live",
    })


# ── F16: Anomaly monitor endpoints ────────────────────────────────────────────

@app.get("/api/anomalies/{job_id}")
async def get_anomaly_flags(job_id: str, include_dismissed: bool = False):
    """
    F16: Return all undismissed anomaly flags for a lease, ordered by severity.

    Query params:
      include_dismissed=true — include flags the tenant has already dismissed

    Returns flags grouped by severity tier for easy UI rendering.
    """
    from db.invoice_store import get_anomaly_flags as _get_flags

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if is_dev():
        return JSONResponse({
            "job_id": job_id,
            "flags": [
                {
                    "id": "dev-flag-001",
                    "check_name": "category_cost_spike",
                    "category": "management_fee",
                    "severity": "medium",
                    "description": (
                        "Management fee increased by 28.5% compared to the "
                        "average of the last 3 invoices."
                    ),
                    "expected_cents": 125000,
                    "actual_cents": 160600,
                    "delta_pct": 28.5,
                    "detection_layer": "trend",
                    "dismissed": False,
                    "invoice_id": "dev-inv-001",
                    "created_at": "2026-06-01T09:00:00+10:00",
                }
            ],
            "total": 1,
            "high_count": 0,
            "medium_count": 1,
            "low_count": 0,
            "mode": "dev",
        })

    flags = _get_flags(job_id, include_dismissed=include_dismissed)
    return JSONResponse({
        "job_id": job_id,
        "flags": flags,
        "total": len(flags),
        "high_count": sum(1 for f in flags if f.get("severity") == "high"),
        "medium_count": sum(1 for f in flags if f.get("severity") == "medium"),
        "low_count": sum(1 for f in flags if f.get("severity") == "low"),
        "mode": "live",
    })


@app.get("/api/anomalies/{job_id}/latest")
async def get_latest_anomaly_flags(job_id: str):
    """
    F16: Return anomaly flags for the most recently uploaded invoice only.
    Useful for post-upload UI: "here's what we found in this invoice."
    """
    from db.invoice_store import list_invoices_for_job, get_anomaly_flags_for_invoice

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if is_dev():
        return JSONResponse({
            "job_id": job_id,
            "invoice_id": "dev-inv-001",
            "flags": [
                {
                    "id": "dev-flag-001",
                    "check_name": "category_cost_spike",
                    "category": "management_fee",
                    "severity": "medium",
                    "description": "Management fee increased by 28.5% vs trailing average.",
                    "expected_cents": 125000,
                    "actual_cents": 160600,
                    "delta_pct": 28.5,
                    "dismissed": False,
                }
            ],
            "total": 1,
            "mode": "dev",
        })

    invoices = list_invoices_for_job(job_id)
    if not invoices:
        return JSONResponse({"job_id": job_id, "invoice_id": None, "flags": [], "total": 0})

    latest = invoices[0]
    latest_id = str(latest.get("id", ""))
    flags = get_anomaly_flags_for_invoice(latest_id)

    return JSONResponse({
        "job_id": job_id,
        "invoice_id": latest_id,
        "flags": flags,
        "total": len(flags),
        "mode": "live",
    })


class DismissFlagRequest(BaseModel):
    note: str = ""


@app.post("/api/anomalies/{flag_id}/dismiss")
async def dismiss_anomaly_flag(flag_id: str, body: DismissFlagRequest):
    """
    F16: Tenant dismisses an anomaly flag after reviewing it.
    Dismissed flags are excluded from the default anomaly view.
    """
    from db.invoice_store import dismiss_anomaly_flag as _dismiss

    if is_dev():
        return JSONResponse({"ok": True, "flag_id": flag_id, "dismissed": True, "mode": "dev"})

    updated = _dismiss(flag_id, note=body.note)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Flag '{flag_id}' not found.")

    return JSONResponse({"ok": True, "flag_id": flag_id, "dismissed": True, "mode": "live"})


# ── Feedback ──────────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    job_id: str
    rating: int   # 1=accurate, 0=partial, -1=inaccurate
    comment: str = ""


@app.post("/api/feedback")
async def submit_feedback(body: FeedbackRequest):
    """Store tenant feedback on a released audit report."""
    if _USE_SUPABASE:
        try:
            from db.audit_run_store import _get_client
            _get_client().table("audit_run").update({
                "reviewer_notes": f"[TENANT FEEDBACK rating={body.rating}] {body.comment}".strip()
            }).eq("job_id", body.job_id).execute()
        except Exception as e:
            logger.error(f"Feedback save failed: {e}")
    return JSONResponse({"ok": True})


@app.get("/api/audit/dates/{job_id}")
def get_audit_dates(job_id: str):
    """
    Return critical dates extracted for a completed audit.
    Powers the 12-Month Monitoring dashboard.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if _USE_SUPABASE:
        try:
            from db.lease_date_store import fetch_dates_for_job
            dates = fetch_dates_for_job(job_id)
            return JSONResponse({"job_id": job_id, "dates": dates})
        except Exception as e:
            logger.error(f"Failed to fetch lease dates for {job_id}: {e}")
    # Fallback: pull from in-memory findings if Supabase unavailable
    findings = get_job_result(job_id) or {}
    return JSONResponse({"job_id": job_id, "dates": findings.get("lease_dates", [])})


@app.get("/api/audit/status/{job_id}")
def get_audit_status(job_id: str):
    """Poll for job status and progress."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JSONResponse(job.to_dict())


@app.get("/api/audit/result/{job_id}")
def get_audit_result(job_id: str):
    """Get full audit result once job is complete."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail=f"Job not complete. Status: {job.status}")
    return JSONResponse(get_job_result(job_id) or {})


@app.get("/api/audit/report/{job_id}")
def download_report(job_id: str):
    """Download the PDF audit report for a completed job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail="Audit not yet complete.")
    if not job.released:
        raise HTTPException(status_code=403, detail="Report is pending expert review and has not been released yet.")

    try:
        from output.report_generator import generate_pdf_report
        report_path = generate_pdf_report(get_job_result(job_id), job_id=job_id)
        safe_name = job.tenant_name.replace(" ", "_").replace("/", "_")
        filename = f"TenantSentry_Audit_{safe_name}_{job.jurisdiction}.pdf"
        return FileResponse(path=report_path, media_type="application/pdf", filename=filename)
    except Exception as e:
        logger.exception(f"Report generation failed for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Report generation failed.")


@app.get("/api/audit/evidence/{job_id}")
async def download_evidence_pack(job_id: str):
    """
    G6: Download the evidence pack ZIP for all HIGH-severity flags in a released audit.
    Contains: clause excerpts, legislation PDFs, CPI verification, and dispute letter drafts.
    Capped at 10 HIGH flags; runs off the event loop with a 90 s hard timeout.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail="Audit not yet complete.")
    if not job.released:
        raise HTTPException(status_code=403, detail="Report not yet released.")

    try:
        from output.evidence_pack import generate_evidence_packs
        result = get_job_result(job_id)
        if not result:
            raise HTTPException(status_code=404, detail="Audit result not found.")

        HIGH_FLAG_CAP = 10
        result_capped = dict(result)
        clause_analyses = result_capped.get("clause_analyses") or []
        high_seen, capped_clauses = 0, []
        for ca in clause_analyses:
            flags = ca.get("risk_flags") or []
            high_here = [f for f in flags if f.get("severity") == "high"]
            if high_here and high_seen < HIGH_FLAG_CAP:
                remaining = HIGH_FLAG_CAP - high_seen
                trimmed = dict(ca)
                trimmed["risk_flags"] = high_here[:remaining] + [f for f in flags if f.get("severity") != "high"]
                high_seen += len(high_here[:remaining])
                capped_clauses.append(trimmed)
            else:
                capped_clauses.append(ca)
        result_capped["clause_analyses"] = capped_clauses

        loop = asyncio.get_event_loop()
        try:
            zip_path = await asyncio.wait_for(
                loop.run_in_executor(None, generate_evidence_packs, result_capped, job_id),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Evidence pack timed out. Try again.")

        if not zip_path:
            raise HTTPException(status_code=404, detail="No HIGH-severity flags — evidence pack not generated.")
        safe_name = job.tenant_name.replace(" ", "_").replace("/", "_")
        filename = f"TenantSentry_EvidencePack_{safe_name}_{job.jurisdiction}.zip"
        return FileResponse(path=zip_path, media_type="application/zip", filename=filename,
                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Evidence pack generation failed for {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Evidence pack generation failed.")


@app.get("/api/audit/evidence/{job_id}/{flag_id}")
def download_single_evidence_pack(job_id: str, flag_id: str):
    """
    G6: Download the evidence pack ZIP for a single specific flag.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail="Audit not yet complete.")
    if not job.released:
        raise HTTPException(status_code=403, detail="Report not yet released.")

    try:
        from output.evidence_pack import generate_single_evidence_pack
        result = get_job_result(job_id)
        if not result:
            raise HTTPException(status_code=404, detail="Audit result not found.")
        zip_path = generate_single_evidence_pack(result, job_id=job_id, flag_id=flag_id)
        safe_flag = flag_id.replace(" ", "_")
        filename = f"TenantSentry_Evidence_{safe_flag}.zip"
        return FileResponse(path=zip_path, media_type="application/zip", filename=filename)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(f"Single evidence pack failed for {job_id}/{flag_id}: {e}")
        raise HTTPException(status_code=500, detail="Evidence pack generation failed.")


@app.get("/api/admin/report/{job_id}")
async def admin_download_report(job_id: str, _: None = Depends(require_admin)):
    """
    Admin PDF report download — bypasses release gate for auditor review.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail="Audit not yet complete.")

    try:
        from output.report_generator import generate_pdf_report
        result = get_job_result(job_id)
        if not result:
            raise HTTPException(status_code=404, detail="Audit result not found.")
        report_path = generate_pdf_report(result, job_id=job_id)
        safe_name = job.tenant_name.replace(" ", "_").replace("/", "_")
        filename = f"TenantSentry_DRAFT_Audit_{safe_name}_{job.jurisdiction}.pdf"
        return FileResponse(path=report_path, media_type="application/pdf", filename=filename)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Admin report generation failed for {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Report generation failed.")


@app.get("/api/admin/evidence/{job_id}")
async def admin_download_evidence(job_id: str, _: None = Depends(require_admin)):
    """
    G6: Admin evidence pack download — bypasses release gate for auditor review.
    Runs pack generation in a thread pool to avoid blocking the event loop.
    Capped at 10 HIGH flags and hard-limited to 90 s total.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail="Audit not yet complete.")

    try:
        from output.evidence_pack import generate_evidence_packs
        result = get_job_result(job_id)
        if not result:
            raise HTTPException(status_code=404, detail="Audit result not found.")

        # Cap to top 10 HIGH flags so generation completes in reasonable time.
        # Trim the result dict before handing to the pack generator.
        result_capped = dict(result)
        clause_analyses = result_capped.get("clause_analyses") or []
        HIGH_FLAG_CAP = 10
        high_seen = 0
        capped_clauses = []
        for ca in clause_analyses:
            flags = ca.get("risk_flags") or []
            high_flags_here = [f for f in flags if f.get("severity") == "high"]
            if high_flags_here and high_seen < HIGH_FLAG_CAP:
                # Include only up to the cap
                remaining = HIGH_FLAG_CAP - high_seen
                trimmed = dict(ca)
                trimmed["risk_flags"] = high_flags_here[:remaining] + [
                    f for f in flags if f.get("severity") != "high"
                ]
                high_seen += len(high_flags_here[:remaining])
                capped_clauses.append(trimmed)
            else:
                capped_clauses.append(ca)
        result_capped["clause_analyses"] = capped_clauses

        # Run blocking PDF/ZIP generation off the event loop with a hard timeout.
        loop = asyncio.get_event_loop()
        try:
            zip_path = await asyncio.wait_for(
                loop.run_in_executor(None, generate_evidence_packs, result_capped, job_id),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            logger.error(f"Evidence pack timed out for {job_id}")
            raise HTTPException(status_code=504,
                                detail="Evidence pack generation timed out. Try again — it may be cached.")

        if not zip_path:
            raise HTTPException(status_code=404, detail="No HIGH-severity flags found.")

        safe_name = job.tenant_name.replace(" ", "_").replace("/", "_")
        filename = f"TenantSentry_DRAFT_EvidencePack_{safe_name}_{job.jurisdiction}.zip"
        return FileResponse(path=zip_path, media_type="application/zip", filename=filename,
                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Admin evidence pack failed for {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Evidence pack generation failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Admin portal — human-in-loop review gate (G4)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/auditor", response_class=HTMLResponse)
async def auditor_portal(request: Request):
    """Auditor portal — human-in-loop review gate for audit QA."""
    return templates.TemplateResponse("auditor.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse)
async def admin_portal(request: Request):
    """Super-admin portal — end-to-end oversight across all portals."""
    return templates.TemplateResponse("admin.html", {"request": request})


@app.post("/api/admin/login")
async def admin_login(request: Request):
    """Validate admin token; returns set-cookie on success."""
    body = await request.json()
    token = body.get("token", "")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        key="admin_token",
        value=token,
        httponly=True,
        samesite="strict",
        max_age=86400 * 7,  # 7 days
    )
    return response


@app.post("/api/admin/logout")
async def admin_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("admin_token")
    return response


@app.get("/api/admin/stats")
async def admin_stats(_: None = Depends(require_admin)):
    """Dashboard stats for the admin portal."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date()
    pending  = list_pending_review()
    reviewed = list_reviewed()
    released_today = [j for j in reviewed if j.released_at and
                      datetime.fromisoformat(j.released_at).date() == today]
    reviewed_today = [j for j in reviewed if j.reviewed_at and
                      datetime.fromisoformat(j.reviewed_at).date() == today]
    all_jobs = [*pending, *reviewed]
    # Fetch findings for stats — scoped to jobs that are complete
    all_findings = [get_job_result(j.job_id) or {} for j in all_jobs]
    risk_scores = [f.get("risk_score", 0) for f in all_findings if f]
    avg_risk = round(sum(risk_scores) / len(risk_scores)) if risk_scores else 0
    high_flags = sum(
        len([flag for flag in (f.get("all_risk_flags") or []) if flag.get("severity") == "high"])
        for f in all_findings if f
    )
    recent_activity = sorted(
        [j.to_dict() for j in all_jobs],
        key=lambda x: x.get("completed_at") or "",
        reverse=True,
    )[:8]
    return JSONResponse({
        "pending_count": len(pending),
        "reviewed_today": len(reviewed_today),
        "released_today": len(released_today),
        "total_reviewed": len(reviewed),
        "avg_risk_score": avg_risk,
        "total_high_flags": high_flags,
        "recent_activity": recent_activity,
    })


@app.get("/api/admin/queue")
async def admin_queue(_: None = Depends(require_admin)):
    """
    Returns pending-review, recently-reviewed, and failed jobs.
    Pending = status COMPLETE, reviewed_by_human = False.
    Reviewed = reviewed_by_human = True (includes released).
    Failed = status FAILED — visible so they're never silently lost.
    """
    from db.audit_run_store import fetch_failed
    source = "dev" if is_dev() else "live"
    failed_rows = fetch_failed(source=source) if _USE_SUPABASE else [
        j.to_dict() for j in _jobs_fallback.values()
        if j.status.value == "failed" and j.source == source
    ]
    return JSONResponse({
        "pending": [j.to_dict() for j in list_pending_review()],
        "reviewed": [j.to_dict() for j in list_reviewed()],
        "failed":  failed_rows,
    })


@app.get("/api/admin/jobs/recent")
async def admin_recent_jobs(_: None = Depends(require_admin)):
    """Last 20 jobs for the current mode's source (dev/live) — for debugging pipeline failures."""
    from db.audit_run_store import fetch_all_recent
    source = "dev" if is_dev() else "live"
    if _USE_SUPABASE:
        return JSONResponse({"jobs": fetch_all_recent(20, source=source)})
    fallback = [j.to_dict() for j in _jobs_fallback.values() if j.source == source]
    return JSONResponse({"jobs": list(reversed(fallback))[-20:]})


@app.get("/api/admin/result/{job_id}")
async def admin_get_result(job_id: str, _: None = Depends(require_admin)):
    """Full audit result for a job — for display in the reviewer panel.
    Re-attaches stage_costs and stage_timings from the job row (they are stripped
    from the findings JSONB and stored in separate DB columns to keep findings lean).
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail="Job not complete")
    findings = get_job_result(job_id) or {}
    # Re-attach cost + timing data.
    # Primary source: dedicated DB columns (stage_costs / stage_timings on the job row).
    # Fallback: the in-memory job.result dict already embeds them (the DB column may not
    # exist yet in older schemas, or mark_complete may have retried without cost columns).
    stage_costs   = job.stage_costs   or findings.pop("stage_costs",   None)
    stage_timings = job.stage_timings or findings.pop("stage_timings", None)
    if stage_costs:
        findings["stage_costs"]   = stage_costs
    if stage_timings:
        findings["stage_timings"] = stage_timings
    return JSONResponse(findings)


# ── Kill switch ───────────────────────────────────────────────────────────────

@app.get("/api/admin/active")
async def admin_active_jobs(_: None = Depends(require_admin)):
    """
    List jobs currently queued or processing — polled every 5s by the kill switch panel.
    Also returns recently failed/cancelled jobs so the auditor can inspect errors.
    elapsed_seconds is computed server-side to avoid client/server clock skew.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    def _with_elapsed(job_dict: dict) -> dict:
        try:
            created = datetime.fromisoformat(job_dict["created_at"]).astimezone(timezone.utc) if job_dict.get("created_at") else None
            job_dict["elapsed_seconds"] = int((now - created).total_seconds()) if created else None
        except Exception:
            job_dict["elapsed_seconds"] = None
        return job_dict

    # ── Zombie cleanup: jobs stuck as processing for > 90 min ────────────────
    # These are left over from server restarts — the in-flight thread was killed
    # but the Supabase row was never updated. Mark them failed so they leave the
    # Active list and drop into Recent Failures instead.
    ZOMBIE_THRESHOLD_S = 90 * 60
    for j in list_active():
        try:
            created = datetime.fromisoformat(j.created_at).astimezone(timezone.utc) if j.created_at else None
            if created and (now - created).total_seconds() > ZOMBIE_THRESHOLD_S:
                if j.status == JobStatus.PROCESSING and j.job_id not in _cancelled_jobs:
                    logger.warning(f"[{j.job_id}] Zombie job detected (>{ZOMBIE_THRESHOLD_S//60}m as processing) — marking failed")
                    fail_job(j.job_id, f"Auto-expired: stuck as processing for >{ZOMBIE_THRESHOLD_S//60} minutes (server restart likely)")
        except Exception as e:
            logger.error(f"Zombie check failed for {j.job_id}: {e}")

    # Active (queued / processing)
    active = [_with_elapsed(j.to_dict()) for j in list_active()]

    # Recent failures — last 8 failed/cancelled jobs, for error inspection
    if _USE_SUPABASE:
        try:
            from db.audit_run_store import fetch_recent_failed_brief
            recent_failed = [_with_elapsed(r) for r in fetch_recent_failed_brief(limit=8)]
        except Exception as e:
            logger.error(f"fetch_recent_failed_brief failed: {e}")
            recent_failed = []
    else:
        recent_failed = [
            _with_elapsed(j.to_dict())
            for j in list(_jobs_fallback.values())
            if j.status.value in ("failed", "cancelled")
        ][-8:]

    return JSONResponse({
        "active":        active,
        "recent_failed": recent_failed,
    })


# ══════════════════════════════════════════════════════════════════════════════
# Free Lease Risk Check — public, no auth, no payment
# Website CTA: tenantsentry-site/free-lease-check.html
# App route:   /free-check (served as Jinja2 template below)
#
# Pipeline strategy: reuses the real audit pipeline (run_dev_audit / run_live_audit)
# with page truncation (max_pages=5) and skip_vector_store=True.
# One high-quality engine for all surfaces — no separate lightweight pipeline.
# Results logged to free_check_run Supabase table (separate from audit_run).
# ══════════════════════════════════════════════════════════════════════════════

# In-memory store for free-check teaser payloads (keyed by job_id).
# Full AuditResult is logged to Supabase via free_check_store.
_free_check_results: dict = {}

FREE_CHECK_MAX_PAGES = 5   # pages fed into the real pipeline per free teaser


def _slice_for_teaser(result: dict, doc_type: str, pages_analysed: int = FREE_CHECK_MAX_PAGES) -> dict:
    """
    Extract the public teaser payload from a full AuditResult dict.
    Exposes: risk_score, top 3 flags (sorted by severity), headline stats.
    Strips clause_analyses and other large fields — never exposed to anonymous users.
    """
    all_flags  = result.get("all_risk_flags") or []
    high_flags = [f for f in all_flags if f.get("severity") == "high"]

    _sev_order = {"high": 0, "medium": 1, "low": 2}
    top_flags  = sorted(all_flags, key=lambda f: _sev_order.get(f.get("severity", "low"), 2))[:3]

    def _normalise(f: dict) -> dict:
        """Normalise flag shape — LLM flags may omit title/category vs mock flags."""
        return {
            "severity":    f.get("severity", "medium"),
            "category":    f.get("category") or f.get("clause_type") or "Lease Risk",
            "title":       f.get("title") or (f.get("description", "")[:80] + "…"
                           if len(f.get("description", "")) > 80 else f.get("description", "")),
            "description": f.get("description", ""),
            "benchmark":   f.get("benchmark"),
        }

    risk_score = result.get("risk_score", 0)
    risk_level = "high" if risk_score >= 60 else ("medium" if risk_score >= 35 else "low")

    return {
        "risk_score":      risk_score,
        "risk_level":      risk_level,
        "pages_analysed":  pages_analysed,
        "clauses_scanned": result.get("raw_clause_count") or result.get("total_clauses"),
        "total_flags":     len(all_flags),
        "high_flags":      len(high_flags),
        "doc_type":        doc_type,
        "jurisdiction":    result.get("jurisdiction", ""),
        "top_flags":       [_normalise(f) for f in top_flags],
        "source":          result.get("source", "live"),
    }


def _score_manual_entry(data: dict) -> dict:
    """
    Rule-based risk scoring from manual form fields (no PDF required).
    Returns the same teaser payload shape as _slice_for_teaser.
    Runs synchronously — fast, zero I/O.

    data keys (lease): doc_type, jurisdiction, rent, sqm, term, review_type,
      fixed_pct, outgoings, options, makegood, landtax
    data keys (hoa):   doc_type, jurisdiction, face_rent, net_rent, sqm, term,
      rentfree, fitout, review_type, outgoings, options, makegood, guarantee, bond
    """
    doc_type     = data.get("doc_type", "lease")
    is_hoa       = doc_type == "hoa"
    jurisdiction = data.get("jurisdiction", "").upper()
    flags: list[dict] = []
    score = 20  # base — most leases start at moderate risk

    if is_hoa:
        face    = float(data.get("face_rent") or 0)
        net     = float(data.get("net_rent")  or 0)
        review  = data.get("review_type", "")
        makegood  = data.get("makegood", "")
        guarantee = data.get("guarantee", "")

        if face and net and face > 0:
            gap_pct = (face - net) / face * 100
            if gap_pct > 25:
                score += 30
                flags.append({
                    "severity": "high", "category": "Effective Rent",
                    "title": f"Face-to-effective rent gap is {gap_pct:.0f}%",
                    "description": (
                        f"Face rent ${face:,.0f}/sqm vs net effective ${net:,.0f}/sqm — "
                        f"a {gap_pct:.0f}% gap. This gap compounds at every review if reviews "
                        "are applied to face rent."
                    ),
                    "benchmark": "📊 A 20%+ gap typically means 8–12% above-market rent by Year 3",
                })
            elif gap_pct > 15:
                score += 15
                flags.append({
                    "severity": "medium", "category": "Effective Rent",
                    "title": f"Moderate face-to-effective rent gap ({gap_pct:.0f}%)",
                    "description": (
                        f"A {gap_pct:.0f}% gap between face and net effective rent. Monitor at "
                        "each review to ensure incentive value isn't eroded."
                    ),
                    "benchmark": None,
                })

        if review in ("ratchet", "market"):
            score += 25
            flags.append({
                "severity": "high", "category": "Rent Review",
                "title": ("Market review — potential ratchet risk" if review == "market"
                          else "Ratchet review — rent can only increase"),
                "description": (
                    "A market review without an explicit 'no ratchet' clause means rent could be "
                    "locked to current levels even if market falls. Seek a downward adjustment clause."
                    if review == "market" else
                    "Ratchet mechanism means rent can never fall at review, regardless of market."
                ),
                "benchmark": "📊 Market review + ratchet adds avg. $18,000–$45,000 over a 5yr term",
            })

        if makegood == "original":
            score += 10
            flags.append({
                "severity": "medium", "category": "Make-Good",
                "title": "Make-good to 'original condition' without condition schedule",
                "description": (
                    "Without a photographic schedule at commencement, 'original condition' is "
                    "subjective. This exposes you to costly claims at lease end."
                ),
                "benchmark": None,
            })

        if guarantee == "yes-unlimited":
            score += 15
            flags.append({
                "severity": "high", "category": "Personal Guarantee",
                "title": "Unlimited personal guarantee — full personal liability",
                "description": (
                    "An unlimited personal guarantee means the director is personally liable for "
                    "all rent and obligations for the full lease term. Cap or remove if possible."
                ),
                "benchmark": "📊 Seek to cap at 3–6 months rent or remove entirely for short terms",
            })

    else:
        rent      = float(data.get("rent") or 0)
        sqm       = float(data.get("sqm")  or 0)
        term      = float(data.get("term") or 0)
        review    = data.get("review_type", "")
        outgoings = data.get("outgoings", "")
        makegood  = data.get("makegood", "")
        landtax   = data.get("landtax", "")
        fixed_pct = float(data.get("fixed_pct") or 0)

        if review == "fixed" and fixed_pct and fixed_pct > 4:
            score += 25
            flags.append({
                "severity": "high", "category": "Rent Review",
                "title": f"Fixed rent review of {fixed_pct:.1f}% — well above CPI",
                "description": (
                    f"A fixed {fixed_pct:.1f}% annual rent increase will outpace CPI most years. "
                    f"Over {int(term)} years this compounds to a significant above-market premium."
                ),
                "benchmark": f"📊 At {fixed_pct:.1f}% fixed, rent doubles in ~{72/fixed_pct:.0f} years",
            })
        elif review in ("ratchet", "market"):
            score += 20
            flags.append({
                "severity": "high" if review == "ratchet" else "medium",
                "category": "Rent Review",
                "title": ("Ratchet review — rent can only increase" if review == "ratchet"
                          else "Market review — seek explicit no-ratchet protection"),
                "description": (
                    "Ratchet mechanism locks rent at the higher of each review — you can never "
                    "benefit from falling market rents." if review == "ratchet" else
                    "Market reviews without a 'no ratchet' clause are common traps. "
                    "Always negotiate a downward adjustment mechanism."
                ),
                "benchmark": "📊 Ratchet clauses produce 15–25% above-market rent by Year 3",
            })

        if outgoings == "net":
            score += 20
            flags.append({
                "severity": "medium", "category": "Outgoings",
                "title": "Net lease — tenant pays all outgoings",
                "description": (
                    "A net lease means you're responsible for all outgoings including rates, land "
                    "tax, insurance, and repairs. Misclassification of capital items as outgoings "
                    "is very common and worth auditing carefully."
                ),
                "benchmark": "📊 Avg. outgoings overcharge = $6,000–$18,000/year in net leases",
            })

        if landtax == "full":
            score += 12
            flags.append({
                "severity": "medium", "category": "Land Tax",
                "title": "Full land tax pass-through without multi-holding cap",
                "description": (
                    "Full land tax recovery without a multi-holding discount means you could be "
                    "paying a proportional share of the landlord's entire portfolio land tax. "
                    "This is prohibited or capped in most Australian jurisdictions."
                ),
                "benchmark": None,
            })

        if makegood == "original":
            score += 10
            flags.append({
                "severity": "medium", "category": "Make-Good",
                "title": "Make-good to 'original condition' — no condition schedule",
                "description": (
                    "Without an attached schedule of condition at commencement, 'original condition' "
                    "is open to dispute. This is a common source of costly end-of-lease claims."
                ),
                "benchmark": None,
            })

        if term >= 10 and not data.get("options"):
            score += 8
            flags.append({
                "severity": "low", "category": "Term & Options",
                "title": f"{int(term)}-year term with no renewal options specified",
                "description": (
                    f"A {int(term)}-year lease with no option means no ability to renew on agreed "
                    "terms — you'd need to renegotiate from scratch. Consider seeking at least one "
                    "renewal option."
                ),
                "benchmark": None,
            })

    score      = min(score, 98)
    risk_level = "high" if score >= 60 else ("medium" if score >= 35 else "low")
    high_count = sum(1 for f in flags if f["severity"] == "high")

    return {
        "risk_score":      score,
        "risk_level":      risk_level,
        "pages_analysed":  None,
        "clauses_scanned": None,
        "total_flags":     len(flags),
        "high_flags":      high_count,
        "doc_type":        doc_type,
        "jurisdiction":    jurisdiction,
        "top_flags":       flags[:3],
        "source":          "manual",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Human-in-loop gate — Review, Release, Cancel, Document download
# All require admin auth. review_job / release_job / cancel_job live in jobs.py.
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/admin/review/{job_id}")
async def admin_review_job(job_id: str, request: Request, _: None = Depends(require_admin)):
    """
    Auditor approves findings — sets reviewed_by_human=True + stores reviewer notes.
    Body: { notes: str, clauseState?: { [heading]: { status, note } } }
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail="Job must be complete before review")
    body = await request.json()
    notes = (body.get("notes") or "").strip()
    reviewed = review_job(job_id, notes)
    if not reviewed:
        raise HTTPException(status_code=500, detail="Review update failed")
    logger.info(f"[{job_id}] Approved by auditor — notes: {notes[:80] or '(none)'}")
    return JSONResponse(reviewed.to_dict())


@app.post("/api/admin/release/{job_id}")
async def admin_release_job(job_id: str, _: None = Depends(require_admin)):
    """
    Release audit report to tenant — only callable after auditor has reviewed.
    Sets released=True + released_at timestamp; tenant portal can now download report.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.reviewed_by_human:
        raise HTTPException(status_code=409, detail="Job must be reviewed before release")
    released = release_job(job_id)
    if not released:
        raise HTTPException(status_code=500, detail="Release update failed")
    logger.info(f"[{job_id}] Released to tenant by auditor")
    return JSONResponse(released.to_dict())


@app.post("/api/admin/cancel/{job_id}")
async def admin_cancel_job(job_id: str, request: Request, _: None = Depends(require_admin)):
    """
    Kill switch — three actions:
      'fail'   → mark job failed (terminal). In-flight thread discarded.
      'retry'  → reset to queued + re-dispatch if PDF is in storage.
      'delete' → hard-delete from memory + Supabase.
    Body: { action: 'fail' | 'retry' | 'delete' }
    """
    body = await request.json()
    action = body.get("action", "fail")
    if action not in ("fail", "retry", "delete"):
        raise HTTPException(status_code=400, detail="action must be 'fail', 'retry', or 'delete'")

    result = cancel_job(job_id, action)

    # If retry and PDF is available, re-dispatch immediately
    if action == "retry" and result.get("pdf_available"):
        stored_doc = get_document(job_id)
        if stored_doc:
            pdf_bytes = stored_doc.get("pdf_bytes") or stored_doc.get("data")
            jurisdiction = stored_doc.get("jurisdiction", "NSW")
            tenant_name  = stored_doc.get("tenant_name")
            if pdf_bytes:
                import tempfile as _tmpfile
                with _tmpfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                    tmp.write(pdf_bytes)
                    tmp_path = tmp.name
                pipeline_fn = _get_pipeline()

                def _retry_run():
                    try:
                        pipeline_fn(
                            job_id=job_id,
                            pdf_path=tmp_path,
                            jurisdiction=jurisdiction,
                            tenant_name=tenant_name,
                            progress_callback=lambda pct, stage: update_job_progress(job_id, pct, stage),
                        )
                        complete_job(job_id, {})
                    except Exception as exc:
                        fail_job(job_id, str(exc))

                _audit_executor.submit(_retry_run)
                result["resubmitted"] = True
                logger.info(f"[{job_id}] Retry: re-dispatched to {pipeline_fn.__name__}")

    logger.info(f"[{job_id}] Kill switch: action={action}, result={result}")
    return JSONResponse(result)


@app.get("/api/admin/document/{job_id}")
async def admin_get_document(job_id: str, _: None = Depends(require_admin)):
    """
    Download the original uploaded lease PDF for an audit job.
    Used by the auditor portal 'Original Lease PDF' document row.
    Falls back to Supabase Storage if the in-memory buffer was cleared.
    """
    from fastapi.responses import Response as _Resp
    stored = get_document(job_id)
    pdf_bytes = None
    filename = f"lease_{job_id}.pdf"

    if stored:
        pdf_bytes = stored.get("pdf_bytes") or stored.get("data")
        filename  = stored.get("filename", filename)

    if not pdf_bytes and _USE_SUPABASE:
        # V1: try to fetch from Supabase Storage 'lease-pdfs' bucket
        try:
            from db.audit_run_store import fetch_pdf_bytes as _fetch_pdf
            pdf_bytes = _fetch_pdf(job_id)
        except Exception as e:
            logger.warning(f"[{job_id}] Supabase PDF fetch failed: {e}")

    if not pdf_bytes:
        raise HTTPException(
            status_code=404,
            detail="Original PDF not available — in-memory buffer may have been cleared after server restart. "
                   "PDF is preserved in Supabase Storage when USE_SUPABASE=true.",
        )

    return _Resp(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tenant jobs API — feeds the Tenant Portal live mode
# Returns all complete+released jobs visible to the current tenant session.
# Auth: session cookie (tenant_id) — in LIVE mode backed by Supabase.
#       DEV mode: returns all released jobs in the fallback store.
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/tenant/jobs")
async def tenant_jobs(request: Request):
    """
    List audit jobs for the tenant portal.
    DEV:  all complete + released jobs in fallback store (for test visibility).
    LIVE: jobs for the authenticated tenant (filtered by tenant_id cookie).
    Returns summary list — no clause_analyses payload (use /api/audit/result/{id}).
    """
    if is_dev():
        jobs = [
            j.to_dict() for j in _jobs_fallback.values()
            if j.status == JobStatus.COMPLETE
        ]
        return JSONResponse({"jobs": sorted(jobs, key=lambda x: x.get("completed_at") or "", reverse=True)})

    # LIVE: pull released jobs from Supabase (pre-auth: returns all released live jobs)
    # TODO (F-AUTH): filter .eq("tenant_id", tenant_id) once auth column exists
    if _USE_SUPABASE:
        try:
            from db.audit_run_store import fetch_released_jobs
            rows = fetch_released_jobs(source="live")
            return JSONResponse({"jobs": rows})
        except Exception as e:
            logger.error(f"tenant_jobs Supabase query failed: {e}")
    # Fallback: released jobs from in-memory store
    jobs = [
        j.to_dict() for j in _jobs_fallback.values()
        if j.status == JobStatus.COMPLETE and j.released
    ]
    return JSONResponse({"jobs": sorted(jobs, key=lambda x: x.get("completed_at") or "", reverse=True)})


@app.get("/free-check", response_class=HTMLResponse)
async def free_check_page(request: Request):
    """
    Free Lease Risk Check page — served from the backend app at /free-check.
    Uses the same UI as the website version but calls relative API URLs (same-origin).
    """
    return templates.TemplateResponse("free_lease_check.html", {"request": request})


@app.post("/api/free-check/submit")
async def free_check_submit(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    jurisdiction: str = Form(...),
    doc_type: str = Form("lease"),
):
    """
    Submit a PDF for the free risk check.
    Returns job_id immediately; poll /api/audit/status/{job_id} for progress.
    On complete, fetch /api/free-check/result/{job_id} for the teaser payload.

    No auth required. No payment required. Max 50 MB.
    Runs the real audit pipeline truncated to the first 5 pages.
    """
    jur = jurisdiction.upper().strip()
    if jur not in VALID_JURISDICTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid jurisdiction '{jur}'.")

    doc_type = doc_type.lower().strip()
    if doc_type not in ("lease", "hoa"):
        doc_type = "lease"

    if not file.filename.lower().endswith((".pdf", ".docx")):
        raise HTTPException(status_code=400, detail="Only PDF or DOCX files are accepted.")

    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(status_code=400, detail=f"File exceeds {MAX_FILE_SIZE_MB} MB limit.")

    job = create_job(
        filename=file.filename,
        jurisdiction=jur,
        tenant_name="Free Check",
    )

    logger.info(f"[FREE-CHECK] Job {job.job_id} created: {file.filename} | {jur} | {doc_type}")

    background_tasks.add_task(
        _run_free_check_job,
        job_id=job.job_id,
        pdf_bytes=content,
        filename=file.filename,
        jurisdiction=jur,
        doc_type=doc_type,
    )

    return JSONResponse({"job_id": job.job_id, "status": "queued"})


async def _run_free_check_job(
    job_id: str,
    pdf_bytes: bytes,
    filename: str,
    jurisdiction: str,
    doc_type: str,
) -> None:
    """
    Async wrapper — runs the real audit pipeline in the bounded thread pool,
    truncated to FREE_CHECK_MAX_PAGES pages. Logs to free_check_run Supabase table.
    """
    loop = asyncio.get_event_loop()

    def _run():
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        try:
            cb = lambda pct, stage: update_job_progress(job_id, pct, stage)

            if is_dev():
                result_obj = run_dev_audit(
                    pdf_path=tmp_path,
                    jurisdiction=jurisdiction,
                    tenant_name="Free Check",
                    job_id=job_id,
                    progress_callback=cb,
                )
            else:
                result_obj = run_live_audit(
                    pdf_path=tmp_path,
                    jurisdiction=jurisdiction,
                    tenant_name="Free Check",
                    job_id=job_id,
                    max_pages=FREE_CHECK_MAX_PAGES,
                    skip_vector_store=True,
                    progress_callback=cb,
                )

            full_result = (
                result_obj.model_dump(mode="json")
                if hasattr(result_obj, "model_dump")
                else (result_obj if isinstance(result_obj, dict) else result_obj.__dict__)
            )
            full_result["source"] = "dev" if is_dev() else "live"

            pages_analysed = min(FREE_CHECK_MAX_PAGES, len(pdf_bytes) // 3000 + 1)
            teaser = _slice_for_teaser(full_result, doc_type, pages_analysed)
            _free_check_results[job_id] = teaser

            try:
                from db.free_check_store import log_free_check
                log_free_check(
                    job_id=job_id,
                    filename=filename,
                    jurisdiction=jurisdiction,
                    doc_type=doc_type,
                    teaser=teaser,
                    full_result=full_result,
                    pages_analysed=pages_analysed,
                )
            except Exception as _store_err:
                logger.warning(f"[FREE-CHECK][{job_id}] Store logging failed (non-fatal): {_store_err}")

            complete_job(job_id, teaser)

        except Exception as e:
            logger.exception(f"[FREE-CHECK][{job_id}] Pipeline failed: {e}")
            fail_job(job_id, str(e))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    try:
        await asyncio.wait_for(
            loop.run_in_executor(_audit_executor, _run),
            timeout=300,
        )
    except asyncio.TimeoutError:
        logger.error(f"[FREE-CHECK][{job_id}] Timed out after 300s")
        fail_job(job_id, "Free check timed out")
    except Exception as e:
        logger.exception(f"[FREE-CHECK][{job_id}] Executor dispatch failed: {e}")
        fail_job(job_id, str(e))


@app.post("/api/free-check/manual")
async def free_check_manual(request: Request):
    """
    Score a free check from manual form field entry — no PDF required.
    Synchronous (fast rule-based scoring). Returns the teaser payload directly.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    doc_type     = (data.get("doc_type") or "lease").lower().strip()
    jurisdiction = (data.get("jurisdiction") or "").upper().strip()

    if jurisdiction not in VALID_JURISDICTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid jurisdiction '{jurisdiction}'.")

    try:
        result = _score_manual_entry({**data, "doc_type": doc_type, "jurisdiction": jurisdiction})
    except Exception as e:
        logger.warning(f"[FREE-CHECK-MANUAL] Scoring failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to score manual entry.")

    try:
        from db.free_check_store import log_free_check
        log_free_check(
            job_id=f"manual-{id(result)}",
            filename="manual-entry",
            jurisdiction=jurisdiction,
            doc_type=doc_type,
            teaser=result,
            full_result={},
            pages_analysed=None,
        )
    except Exception as _store_err:
        logger.debug(f"[FREE-CHECK-MANUAL] Store logging failed (non-fatal): {_store_err}")

    return JSONResponse(result)


@app.get("/api/free-check/result/{job_id}")
def free_check_result(job_id: str):
    """
    Return the free-check teaser payload for a completed job.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail=f"Job not complete. Status: {job.status}")

    result = _free_check_results.get(job_id) or get_job_result(job_id) or {}
    return JSONResponse(result)


@app.post("/api/free-check/email")
async def free_check_email(request: Request):
    """
    Capture a lead email after the free check teaser.
    Updates the free_check_run row in Supabase with the email + lead_captured_at.
    Returns { ok: true } — never fails visibly to the user.
    """
    try:
        data = await request.json()
        email  = (data.get("email") or "").strip().lower()
        job_id = (data.get("job_id") or "").strip()
        jur    = (data.get("jurisdiction") or "").upper().strip()

        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="Valid email required.")

        logger.info(f"[FREE-CHECK-LEAD] email={email} job_id={job_id} jur={jur}")

        try:
            from db.free_check_store import update_lead
            update_lead(job_id, email)
        except Exception as e:
            logger.warning(f"[FREE-CHECK-LEAD] update_lead failed (non-fatal): {e}")

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[FREE-CHECK-EMAIL] Non-fatal error: {e}")

    return JSONResponse({"ok": True})
