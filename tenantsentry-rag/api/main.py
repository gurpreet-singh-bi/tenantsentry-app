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

# ── Path setup ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent   # tenantsentry-rag/
sys.path.insert(0, str(BASE_DIR))

from api.jobs import (
    create_job, get_job, get_job_result, update_job_progress, complete_job, fail_job, JobStatus,
    review_job, release_job, list_pending_review, list_reviewed,
    store_document, get_document,
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


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    """Alpine.js upload/process/results SPA — wired to real audit API."""
    return templates.TemplateResponse("upload.html", {"request": request})


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
# Audit API
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/audit/submit")
async def submit_audit(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Commercial lease PDF"),
    jurisdiction: str = Form(...),
    tenant_name: str = Form(""),
):
    """
    Submit a lease PDF for async audit.
    Returns job_id immediately. Poll /api/audit/status/{job_id} for progress.
    """
    jur = jurisdiction.upper().strip()
    if jur not in VALID_JURISDICTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid jurisdiction. Must be one of: {sorted(VALID_JURISDICTIONS)}"
        )

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    content = await file.read()
    file_size_mb = len(content) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(status_code=413, detail=f"File too large. Maximum size is {MAX_FILE_SIZE_MB}MB.")

    # G2: SHA-256 fingerprint of the raw PDF bytes.
    # Passed to the pipeline so it can skip re-embedding duplicate uploads.
    document_hash = hashlib.sha256(content).hexdigest()
    logger.info(f"Document hash (SHA-256): {document_hash[:16]}… | size: {file_size_mb:.2f}MB")

    job = create_job(
        filename=file.filename,
        jurisdiction=jur,
        tenant_name=tenant_name.strip() or "Unknown",
    )

    logger.info(f"Job {job.job_id} created: {file.filename} | {jur} | {tenant_name}")

    # Persist original PDF for auditor download
    store_document(job.job_id, file.filename, content)

    # V2: dispatch to bounded executor — prevents event loop starvation.
    # _schedule_audit_job is async so BackgroundTasks awaits it; inside it
    # uses run_in_executor to run the blocking pipeline off the event loop.
    background_tasks.add_task(
        _schedule_audit_job,
        job_id=job.job_id,
        pdf_bytes=content,
        filename=file.filename,
        jurisdiction=jur,
        tenant_name=tenant_name.strip() or "Unknown",
        document_hash=document_hash,
    )

    return JSONResponse({
        "job_id": job.job_id,
        "status": "queued",
    })


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
    from api.jobs import _USE_SUPABASE, _jobs_fallback
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
    from api.jobs import _USE_SUPABASE, _jobs_fallback
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


class ReviewRequest(BaseModel):
    notes: str = ""


@app.post("/api/admin/review/{job_id}")
async def admin_review(job_id: str, body: ReviewRequest, _: None = Depends(require_admin)):
    """
    Auditor approves findings. Sets reviewed_by_human = True.
    Does NOT release to tenant — use /release for that.
    """
    job = review_job(job_id, notes=body.notes)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or not yet complete")
    return JSONResponse({"ok": True, "job_id": job_id})