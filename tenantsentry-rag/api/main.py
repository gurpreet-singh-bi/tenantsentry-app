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

import os
import sys
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request, Depends
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
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

MOCK_MODE = os.environ.get("MOCK_MODE", "true").lower() == "true"

if MOCK_MODE:
    from pipeline.mock_pipeline import run_mock_audit as run_audit
    logger.info("🧪 MOCK_MODE enabled — no API calls will be made")
else:
    from pipeline.audit_pipeline import run_audit
    logger.info("🚀 Production mode — real AI pipeline active")

# ── App factory ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("TenantSentry.ai starting up")
    yield
    logger.info("TenantSentry.ai shutting down")


app = FastAPI(
    title="TenantSentry.ai",
    description="AI-powered commercial lease audit for Australian tenants",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

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
# Health
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "TenantSentry.ai", "version": "1.0.0"}


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

    job = create_job(
        filename=file.filename,
        jurisdiction=jur,
        tenant_name=tenant_name.strip() or "Unknown",
    )

    logger.info(f"Job {job.job_id} created: {file.filename} | {jur} | {tenant_name}")

    # Persist original PDF for auditor download
    store_document(job.job_id, file.filename, content)

    background_tasks.add_task(
        _run_audit_job,
        job_id=job.job_id,
        pdf_bytes=content,
        filename=file.filename,
        jurisdiction=jur,
        tenant_name=tenant_name.strip() or "Unknown",
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
        raise HTTPException(status_code=404, detail="Job not found or not complete")
    return JSONResponse({"ok": True, "reviewed_at": job.reviewed_at})


@app.get("/api/admin/document/{job_id}")
async def admin_download_document(job_id: str, _: None = Depends(require_admin)):
    """Download the original uploaded lease PDF."""
    doc = get_document(job_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Original document not available.")
    from fastapi.responses import Response
    return Response(
        content=doc["data"],
        media_type=doc["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


@app.get("/api/admin/report/{job_id}")
async def admin_download_report(job_id: str, _: None = Depends(require_admin)):
    """Auditor PDF download — bypasses release gate (auditor reviews before releasing)."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETE:
        raise HTTPException(status_code=409, detail="Audit not yet complete")
    try:
        from output.report_generator import generate_pdf_report
        report_path = generate_pdf_report(get_job_result(job_id), job_id=job_id)
        safe_name = job.tenant_name.replace(" ", "_").replace("/", "_")
        filename = f"TenantSentry_DRAFT_Audit_{safe_name}_{job.jurisdiction}.pdf"
        return FileResponse(path=report_path, media_type="application/pdf", filename=filename)
    except Exception as e:
        logger.exception(f"Admin report generation failed for {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Report generation failed.")


@app.post("/api/admin/release/{job_id}")
async def admin_release(job_id: str, _: None = Depends(require_admin)):
    """Release report to tenant. Only works after review."""
    job = release_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or not yet reviewed")
    return JSONResponse({"ok": True, "released_at": job.released_at})


# Guard tenant report download behind release flag
# (Overrides the existing /api/audit/report/{job_id} endpoint logic)
@app.get("/api/audit/report/status/{job_id}")
def get_report_release_status(job_id: str):
    """Frontend can poll this to know when a report has been released."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse({
        "released": job.released,
        "reviewed_by_human": job.reviewed_by_human,
    })


# ══════════════════════════════════════════════════════════════════════════════
# Background job runner
# ══════════════════════════════════════════════════════════════════════════════

def _run_audit_job(
    job_id: str,
    pdf_bytes: bytes,
    filename: str,
    jurisdiction: str,
    tenant_name: str,
) -> None:
    """Runs in a background thread. Calls full audit pipeline with progress updates."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        update_job_progress(job_id, 5, "Parsing PDF...")

        result = run_audit(
            pdf_path=tmp_path,
            jurisdiction=jurisdiction,
            tenant_name=tenant_name,
            job_id=job_id,
            progress_callback=lambda pct, stage: update_job_progress(job_id, pct, stage),
        )

        complete_job(job_id, result.model_dump(mode="json"))
        logger.info(f"[{job_id}] Audit complete")

    except NotImplementedError as e:
        fail_job(job_id, str(e))
        logger.warning(f"[{job_id}] Not implemented: {e}")
    except Exception as e:
        fail_job(job_id, f"Audit failed: {str(e)}")
        logger.exception(f"[{job_id}] Audit error: {e}")