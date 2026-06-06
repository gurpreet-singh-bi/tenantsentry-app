"""
main.py
-------
TenantSentry.ai — FastAPI application.

Serves:
  - HTML frontend (Jinja2 templates)
  - REST API for async audit jobs
  - Static files (CSS/JS)

Page routes:
  GET /                → redirect to /login (unauthenticated) or /dashboard
  GET /login           → login.html
  GET /signup          → signup.html
  GET /audit           → audit.html  (dark-theme audit flow prototype)
  GET /upload          → upload.html (Alpine.js upload/process/results SPA, wired to API)
  GET /dashboard       → dashboard.html (TODO)

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
# Admin:  admin.tenantsentry.ai  (separate subdomain — see deployment notes)
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
    return {"status": "ok", "service": "TenantSentry.ai", "version": "1.0.0"}


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
    jurisdiction: str = Form(...),
    tenant_name: str = Form(""),
    chat_session_id: str = Form(""),
):
    """
    Submit one or more documents for async audit.

    files[0] must be the primary lease (doc_types[0]=="lease").
    Additional files may be outgoings schedules, invoices, or amendments.
    Returns job_id immediately; poll /api/audit/status/{job_id} for progress.
    """
    jur = jurisdiction.upper().strip()
    if jur not in VALID_JURISDICTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid jurisdiction. Must be one of: {sorted(VALID_JURISDICTIONS)}"
        )

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
        jurisdiction=jur,
        tenant_name=tenant_name.strip() or "Unknown",
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
        jurisdiction=jur,
        tenant_name=tenant_name.strip() or "Unknown",
        document_hash=document_hash,
        additional_docs_data=additional_data,
        submission_warnings=submission_warnings,
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
def download_evidence_pack(job_id: str):
    """
    G6: Download the evidence pack ZIP for all HIGH-severity flags in a released audit.
    Contains: clause excerpts, legislation PDFs, CPI verification, and dispute letter drafts.
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
        zip_path = generate_evidence_packs(result, job_id=job_id)
        if not zip_path:
            raise HTTPException(status_code=404, detail="No HIGH-severity flags — evidence pack not generated.")
        safe_name = job.tenant_name.replace(" ", "_").replace("/", "_")
        filename = f"TenantSentry_EvidencePack_{safe_name}_{job.jurisdiction}.zip"
        return FileResponse(path=zip_path, media_type="application/zip", filename=filename)
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
        zip_path = generate_evidence_packs(result, job_id=job_id)
        if not zip_path:
            raise HTTPException(status_code=404, detail="No HIGH-severity flags found.")
        safe_name = job.tenant_name.replace(" ", "_").replace("/", "_")
        filename = f"TenantSentry_DRAFT_EvidencePack_{safe_name}_{job.jurisdiction}.zip"
        return FileResponse(path=zip_path, media_type="application/zip", filename=filename)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Admin evidence pack failed for {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Evidence pack generation failed.")


# ══════════════════════════════════════════════════════════════════════════════
# Admin portal — human-in-loop review gate (G4)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/admin", response_class=HTMLResponse)
async def admin_portal(request: Request):
    """Auditor portal — auth enforced client-side via cookie/token check."""
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
    failed_rows = fetch_failed() if _USE_SUPABASE else [
        j.to_dict() for j in _jobs_fallback.values() if j.status.value == "failed"
    ]
    return JSONResponse({
        "pending": [j.to_dict() for j in list_pending_review()],
        "reviewed": [j.to_dict() for j in list_reviewed()],
        "failed":  failed_rows,
    })


@app.get("/api/admin/jobs/recent")
async def admin_recent_jobs(_: None = Depends(require_admin)):
    """Last 20 jobs regardless of status — for debugging pipeline failures."""
    from db.audit_run_store import fetch_all_recent
    if _USE_SUPABASE:
        return JSONResponse({"jobs": fetch_all_recent(20)})
    return JSONResponse({"jobs": [j.to_dict() for j in list(_jobs_fallback.values())[-20:]]})


@app.get("/api/admin/result/{job_id}")
async def admin_get_result(job_id: str, _: None = Depends(require_admin)):
    """Full audit result for a job — for display in the reviewer panel."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail="Job not complete")
    return JSONResponse(get_job_result(job_id) or {})


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
        recent_failed = sorted(
            [_with_elapsed(j.to_dict()) for j in _jobs_fallback.values()
             if j.status.value in ("failed", "cancelled")],
            key=lambda x: x.get("completed_at") or "",
            reverse=True,
        )[:8]

    return JSONResponse({"jobs": active, "recent_failed": recent_failed})


class CancelJobRequest(BaseModel):
    action: str = "fail"   # "fail" | "retry" | "delete"


@app.post("/api/admin/cancel/{job_id}")
async def admin_cancel_job(
    job_id: str,
    body: CancelJobRequest,
    _: None = Depends(require_admin),
):
    """
    Kill switch endpoint.

    action="fail"   → Cancel and mark failed. In-flight thread results are discarded.
    action="retry"  → Cancel current run, reset to queued, re-dispatch if PDF available.
    action="delete" → Hard delete — removes from DB and memory entirely.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = cancel_job(job_id, body.action)

    # For "retry": attempt to re-dispatch the pipeline if we still have the PDF.
    if body.action == "retry":
        doc = get_document(job_id)
        if doc:
            # Re-dispatch onto the running event loop — same path as the original upload handler.
            asyncio.ensure_future(
                _schedule_audit_job(
                    job_id=job_id,
                    pdf_bytes=doc["data"],
                    filename=job.filename,
                    jurisdiction=job.jurisdiction,
                    tenant_name=job.tenant_name,
                    document_hash="",
                )
            )
            result["resubmitted"] = True
            logger.info(f"[{job_id}] Re-dispatched audit pipeline after retry cancel")
        else:
            result["resubmitted"] = False
            result["message"] = "PDF no longer in memory — please re-upload the lease to retry."
            logger.warning(f"[{job_id}] Retry requested but PDF not available for re-dispatch")

    return JSONResponse(result)
