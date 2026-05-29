"""
main.py
-------
FastAPI wrapper around the audit pipeline.
Exposes a single endpoint: POST /audit

Run with:
    uvicorn api.main:app --reload --port 8000

Test with:
    curl -X POST http://localhost:8000/audit \
      -H "X-API-Key: your-key" \
      -F "file=@/path/to/lease.pdf" \
      -F "jurisdiction=NSW" \
      -F "tenant_name=Acme Pty Ltd"
"""

import os
import tempfile
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.security import APIKeyHeader
from fastapi.responses import JSONResponse
from loguru import logger
from pipeline.audit_pipeline import run_audit

app = FastAPI(
    title="TenantSentry.ai Audit API",
    description="AI-powered commercial lease audit for Australian tenants",
    version="0.1.0",
)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key")
VALID_JURISDICTIONS = {"NSW", "VIC", "QLD", "SA", "WA", "TAS", "ACT", "NT"}


def verify_api_key(api_key: str = Depends(API_KEY_HEADER)):
    expected = os.environ.get("API_KEY", "dev-key")
    if api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key


@app.get("/health")
def health():
    return {"status": "ok", "service": "TenantSentry.ai"}


@app.post("/audit")
async def audit_lease(
    file: UploadFile = File(..., description="Commercial lease PDF"),
    jurisdiction: str = Form(..., description="State code: NSW, VIC, QLD, etc."),
    tenant_name: str = Form(None, description="Tenant name for the report"),
    _: str = Depends(verify_api_key),
):
    """
    Submit a lease PDF for AI-powered audit.
    Returns structured JSON with clause-by-clause analysis and risk flags.
    """
    # Validate jurisdiction
    jur = jurisdiction.upper()
    if jur not in VALID_JURISDICTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid jurisdiction '{jurisdiction}'. Must be one of: {sorted(VALID_JURISDICTIONS)}"
        )

    # Validate file type
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    # Save upload to temp file and run audit
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        logger.info(f"Audit request: {file.filename} | {jur} | {tenant_name}")
        result = run_audit(pdf_path=tmp_path, jurisdiction=jur, tenant_name=tenant_name)
        return JSONResponse(content=result.model_dump(mode="json"))

    except NotImplementedError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception(f"Audit failed: {e}")
        raise HTTPException(status_code=500, detail="Audit processing failed")
    finally:
        os.unlink(tmp_path)
