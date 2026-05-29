"""
premium_app.py — TenantSentry Premium UI
---------------------------------------
FastAPI backend + single-page premium UI.
No API keys needed — uses mock analysis.

Run with:
    python premium_app.py

Then open: http://localhost:8000
"""

import sys, os, json, tempfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from datetime import datetime
import uvicorn

sys.path.insert(0, str(Path(__file__).parent))
from ingestion.pdf_parser import parse_pdf
from ingestion.chunker import chunk_document
from mock.analyser import analyse_clause_mock, compute_risk_score

SIGNUPS_FILE = Path(__file__).parent / "signups.json"

app = FastAPI()


# ─────────────────────────────────────────────────────────────────────────────
# API routes
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/analyse")
async def analyse(
    file: UploadFile = File(...),
    jurisdiction: str = Form("NSW"),
    tenant: str = Form(""),
):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        parsed = parse_pdf(tmp_path)
        chunks = chunk_document(parsed.pages, {
            "jurisdiction": jurisdiction,
            "tenant_name": tenant or "Unknown",
            "filename": file.filename,
        })

        analyses = []
        for c in chunks:
            r = analyse_clause_mock(
                clause_heading=c.metadata.get("clause_heading", ""),
                clause_text=c.content,
                jurisdiction=jurisdiction,
            )
            analyses.append({
                "id": len(analyses),
                "heading": r.clause_heading or f"Clause {len(analyses)+1}",
                "type": r.clause_type,
                "text": r.clause_text,
                "key_terms": r.key_terms,
                "risk_flags": r.risk_flags,
                "summary": r.plain_english_summary,
                "action": r.recommended_action,
                "severity": _max_sev(r.risk_flags),
            })

        all_flags = [f for a in analyses for f in a["risk_flags"]]
        score = compute_risk_score(all_flags)

        return JSONResponse({
            "filename": file.filename,
            "jurisdiction": jurisdiction,
            "tenant": tenant or "Unknown",
            "score": score,
            "risk_level": "HIGH" if score >= 60 else "MEDIUM" if score >= 30 else "LOW",
            "clauses": analyses,
            "all_flags": all_flags,
            "counts": {
                "clauses": len(analyses),
                "high": sum(1 for f in all_flags if f["severity"] == "high"),
                "medium": sum(1 for f in all_flags if f["severity"] == "medium"),
                "low": sum(1 for f in all_flags if f["severity"] == "low"),
            },
            "is_scanned": parsed.is_scanned,
        })
    finally:
        os.unlink(tmp_path)


@app.post("/api/signup")
async def signup(data: dict):
    signups = []
    if SIGNUPS_FILE.exists():
        with open(SIGNUPS_FILE) as f:
            signups = json.load(f)
    email = data.get("email", "").strip().lower()
    if any(s["email"].lower() == email for s in signups):
        return JSONResponse({"ok": False, "msg": "already_exists"})
    signups.append({**data, "email": email, "signed_up_at": datetime.utcnow().isoformat()})
    with open(SIGNUPS_FILE, "w") as f:
        json.dump(signups, f, indent=2)
    return JSONResponse({"ok": True})


def _max_sev(flags):
    for s in ("high", "medium", "low"):
        if any(f["severity"] == s for f in flags):
            return s
    return "none"


# ─────────────────────────────────────────────────────────────────────────────
# Frontend HTML
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TenantSentry — Know every risk before you sign</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:        #F7F5F2;
  --surface:   #FFFFFF;
  --border:    #E8E3DC;
  --text:      #1A1614;
  --muted:     #9C8F85;
  --accent:    #C96A1A;
  --accent-lt: #FEF3E8;
  --high:      #DC2626;
  --high-lt:   #FEF2F2;
  --med:       #D97706;
  --med-lt:    #FFFBEB;
  --low:       #16A34A;
  --low-lt:    #F0FDF4;
  --none-lt:   #F9F8F6;
  --shadow:    0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
  --shadow-lg: 0 10px 40px rgba(0,0,0,.10);
  --radius:    12px;
  --font:      'Inter', system-ui, sans-serif;
}

body { font-family: var(--font); background: var(--bg); color: var(--text); min-height: 100vh; }

/* ── UPLOAD PAGE ── */
#upload-page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1.1rem 2.5rem;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.logo { font-size: 1.15rem; font-weight: 800; color: var(--text); display: flex; align-items: center; gap: .5rem; }
.logo-dot { color: var(--accent); }
.nav-btn {
  padding: .45rem 1rem;
  border-radius: 8px;
  font-size: .82rem;
  font-weight: 600;
  cursor: pointer;
  border: 1.5px solid var(--border);
  background: transparent;
  color: var(--text);
  transition: all .15s;
}
.nav-btn:hover { background: var(--bg); }
.nav-btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.nav-btn.primary:hover { opacity: .9; }

.hero {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 4rem 1.5rem;
}
.hero-badge {
  display: inline-flex;
  align-items: center;
  gap: .4rem;
  background: var(--accent-lt);
  color: var(--accent);
  border: 1px solid #FDDCB5;
  border-radius: 999px;
  padding: .3rem .9rem;
  font-size: .75rem;
  font-weight: 600;
  margin-bottom: 1.5rem;
  letter-spacing: .3px;
}
.hero h1 {
  font-size: 2.8rem;
  font-weight: 800;
  text-align: center;
  color: var(--text);
  line-height: 1.15;
  letter-spacing: -.5px;
  max-width: 600px;
  margin-bottom: .9rem;
}
.hero h1 em { color: var(--accent); font-style: normal; }
.hero p {
  color: var(--muted);
  font-size: 1rem;
  text-align: center;
  max-width: 440px;
  line-height: 1.6;
  margin-bottom: 2.5rem;
}

/* Upload card */
.upload-card {
  background: var(--surface);
  border: 1.5px solid var(--border);
  border-radius: 20px;
  padding: 2rem;
  width: 100%;
  max-width: 520px;
  box-shadow: var(--shadow-lg);
}
.field-row { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin-bottom: .8rem; }
.field label { display: block; font-size: .73rem; font-weight: 600; color: var(--muted); margin-bottom: .35rem; text-transform: uppercase; letter-spacing: .5px; }
.field select, .field input {
  width: 100%;
  padding: .6rem .8rem;
  border: 1.5px solid var(--border);
  border-radius: 9px;
  font-family: var(--font);
  font-size: .88rem;
  color: var(--text);
  background: var(--bg);
  outline: none;
  transition: border-color .15s;
}
.field select:focus, .field input:focus { border-color: var(--accent); background: #fff; }

.drop-zone {
  border: 2px dashed var(--border);
  border-radius: 12px;
  padding: 1.8rem 1rem;
  text-align: center;
  cursor: pointer;
  transition: all .2s;
  margin: .8rem 0;
  background: var(--bg);
  position: relative;
}
.drop-zone:hover, .drop-zone.drag-over { border-color: var(--accent); background: var(--accent-lt); }
.drop-zone input[type=file] { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.drop-icon { font-size: 1.8rem; margin-bottom: .5rem; }
.drop-zone p { font-size: .84rem; color: var(--muted); line-height: 1.5; }
.drop-zone p strong { color: var(--accent); }
.drop-selected { font-size: .82rem; font-weight: 600; color: var(--text); margin-top: .4rem; }

.analyse-btn {
  width: 100%;
  padding: .85rem;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 10px;
  font-family: var(--font);
  font-size: .92rem;
  font-weight: 700;
  cursor: pointer;
  transition: opacity .15s, transform .1s;
  margin-top: .4rem;
  letter-spacing: .1px;
}
.analyse-btn:hover { opacity: .92; transform: translateY(-1px); }
.analyse-btn:active { transform: translateY(0); }
.analyse-btn:disabled { opacity: .5; cursor: not-allowed; transform: none; }

.trust-row {
  display: flex;
  gap: 1.5rem;
  justify-content: center;
  margin-top: 2rem;
  flex-wrap: wrap;
}
.trust-item { display: flex; align-items: center; gap: .4rem; font-size: .78rem; color: var(--muted); }
.trust-item svg { width: 14px; height: 14px; }

/* ── PROGRESS ── */
#progress-page { display: none; min-height: 100vh; align-items: center; justify-content: center; flex-direction: column; }
.progress-wrap { text-align: center; }
.progress-wrap h3 { font-size: 1.1rem; font-weight: 700; margin-bottom: .4rem; }
.progress-wrap p { color: var(--muted); font-size: .86rem; margin-bottom: 2rem; }
.progress-track { width: 320px; height: 4px; background: var(--border); border-radius: 999px; overflow: hidden; }
.progress-fill { height: 100%; background: var(--accent); border-radius: 999px; width: 0%; transition: width .4s ease; }

/* ── RESULTS PAGE ── */
#results-page { display: none; flex-direction: column; min-height: 100vh; }

.results-header {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: .9rem 2rem;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 100;
}
.results-logo { font-size: 1rem; font-weight: 800; color: var(--text); }
.results-logo span { color: var(--accent); }
.results-file { font-size: .82rem; font-weight: 600; color: var(--text); }
.results-meta { font-size: .75rem; color: var(--muted); }
.risk-badge {
  padding: .3rem .8rem;
  border-radius: 999px;
  font-size: .72rem;
  font-weight: 700;
  letter-spacing: .5px;
}
.badge-HIGH   { background: var(--high-lt); color: var(--high); }
.badge-MEDIUM { background: var(--med-lt);  color: var(--med);  }
.badge-LOW    { background: var(--low-lt);  color: var(--low);  }

.results-header-actions { margin-left: auto; display: flex; gap: .6rem; align-items: center; }

/* Stats strip */
.stats-strip {
  display: flex;
  gap: 0;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.stat-cell {
  flex: 1;
  padding: .8rem 1.5rem;
  border-right: 1px solid var(--border);
  text-align: center;
}
.stat-cell:last-child { border-right: none; }
.stat-val { font-size: 1.6rem; font-weight: 800; line-height: 1; }
.stat-lbl { font-size: .68rem; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-top: 3px; }

/* Two-pane layout */
.results-body { display: flex; flex: 1; overflow: hidden; height: calc(100vh - 100px); }

/* LEFT PANE */
.clause-pane {
  width: 320px;
  min-width: 320px;
  border-right: 1px solid var(--border);
  background: var(--surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.pane-header { padding: .9rem 1rem .6rem; border-bottom: 1px solid var(--border); }
.pane-title { font-size: .75rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .6px; margin-bottom: .6rem; }

.filter-pills { display: flex; gap: .3rem; flex-wrap: wrap; }
.fpill {
  padding: .25rem .65rem;
  border-radius: 999px;
  font-size: .72rem;
  font-weight: 600;
  cursor: pointer;
  border: 1.5px solid var(--border);
  background: var(--bg);
  color: var(--muted);
  transition: all .15s;
  white-space: nowrap;
}
.fpill:hover { border-color: var(--accent); color: var(--accent); }
.fpill.active { border-color: var(--accent); background: var(--accent); color: #fff; }
.fpill.active-high   { border-color: var(--high); background: var(--high); color: #fff; }
.fpill.active-medium { border-color: var(--med);  background: var(--med);  color: #fff; }
.fpill.active-low    { border-color: var(--low);  background: var(--low);  color: #fff; }

.type-filter {
  width: 100%;
  padding: .45rem .7rem;
  border: 1.5px solid var(--border);
  border-radius: 8px;
  font-family: var(--font);
  font-size: .78rem;
  color: var(--text);
  background: var(--bg);
  outline: none;
  margin-top: .5rem;
}
.type-filter:focus { border-color: var(--accent); }

.clause-count { font-size: .72rem; color: var(--muted); padding: .4rem 1rem .2rem; }

.clause-list { overflow-y: auto; flex: 1; }
.clause-list::-webkit-scrollbar { width: 4px; }
.clause-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

.clause-item {
  padding: .75rem 1rem;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background .12s;
  display: flex;
  align-items: flex-start;
  gap: .6rem;
  position: relative;
}
.clause-item::before {
  content: '';
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 3px;
  border-radius: 0 2px 2px 0;
}
.clause-item.sev-high::before   { background: var(--high); }
.clause-item.sev-medium::before { background: var(--med); }
.clause-item.sev-low::before    { background: var(--low); }
.clause-item.sev-none::before   { background: transparent; }
.clause-item:hover { background: var(--bg); }
.clause-item.active { background: var(--accent-lt); }
.clause-item.active .ci-title { color: var(--accent); }

.ci-icon { font-size: 1rem; margin-top: 1px; flex-shrink: 0; }
.ci-title { font-size: .82rem; font-weight: 600; color: var(--text); line-height: 1.3; }
.ci-type  { font-size: .72rem; color: var(--muted); margin-top: 2px; }
.ci-pill  { display: inline-block; padding: 1px 6px; border-radius: 999px; font-size: .66rem; font-weight: 700; margin-top: 3px; }
.ci-pill-high   { background: var(--high-lt); color: var(--high); }
.ci-pill-medium { background: var(--med-lt);  color: var(--med); }
.ci-pill-low    { background: var(--low-lt);  color: var(--low); }

/* RIGHT PANE */
.detail-pane {
  flex: 1;
  overflow-y: auto;
  padding: 2rem;
  background: var(--bg);
}
.detail-pane::-webkit-scrollbar { width: 5px; }
.detail-pane::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

.detail-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--muted);
  font-size: .88rem;
  text-align: center;
  gap: .5rem;
}
.detail-empty .de-icon { font-size: 2.5rem; }

.detail-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  margin-bottom: 1rem;
}
.detail-card-header {
  padding: 1.2rem 1.4rem;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
}
.dc-title { font-size: 1rem; font-weight: 700; color: var(--text); line-height: 1.3; }
.dc-type  { display: inline-block; margin-top: .3rem; padding: .2rem .7rem; background: var(--accent-lt); color: var(--accent); border-radius: 999px; font-size: .72rem; font-weight: 600; }
.dc-sev   { flex-shrink: 0; }

.detail-card-body { padding: 1.2rem 1.4rem; }
.section-label {
  font-size: .68rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .6px;
  color: var(--muted);
  margin-bottom: .5rem;
}

.clause-raw {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: .8rem 1rem;
  font-size: .81rem;
  color: #6B5E57;
  line-height: 1.7;
  max-height: 180px;
  overflow-y: auto;
  margin-bottom: 1.2rem;
}
.clause-raw::-webkit-scrollbar { width: 3px; }
.clause-raw::-webkit-scrollbar-thumb { background: var(--border); }

.terms-row { display: flex; flex-wrap: wrap; gap: .3rem; margin-bottom: 1.2rem; }
.term-chip {
  background: #F5F3F0;
  color: #5C5047;
  border-radius: 6px;
  padding: .2rem .6rem;
  font-size: .73rem;
  font-weight: 500;
}

.summary-block {
  background: var(--med-lt);
  border-left: 3px solid var(--med);
  border-radius: 0 8px 8px 0;
  padding: .8rem 1rem;
  font-size: .85rem;
  color: #44403C;
  line-height: 1.6;
  margin-bottom: .8rem;
}
.action-block {
  background: var(--low-lt);
  border-left: 3px solid #4ADE80;
  border-radius: 0 8px 8px 0;
  padding: .8rem 1rem;
  font-size: .85rem;
  color: #14532D;
  line-height: 1.6;
  margin-bottom: 1.2rem;
}

/* Flag items */
.flag-item {
  display: flex;
  gap: .8rem;
  padding: .8rem 0;
  border-bottom: 1px solid var(--border);
  align-items: flex-start;
}
.flag-item:last-child { border-bottom: none; }
.flag-sev-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  margin-top: 5px;
  flex-shrink: 0;
}
.dot-high   { background: var(--high); }
.dot-medium { background: var(--med); }
.dot-low    { background: var(--low); }
.flag-id    { font-size: .7rem; font-weight: 700; color: var(--muted); }
.flag-desc  { font-size: .82rem; color: var(--text); margin-top: 2px; line-height: 1.45; }
.flag-legref { font-size: .72rem; color: var(--muted); font-style: italic; margin-top: 3px; }

/* ── RISK GAUGE ── */
.gauge-wrap { display: flex; justify-content: center; margin: 1.5rem 0; }
.gauge-svg { overflow: visible; }

/* ── MODAL ── */
.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,.45);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(4px);
}
.modal-overlay.open { display: flex; }
.modal {
  background: var(--surface);
  border-radius: 20px;
  padding: 2rem;
  width: 100%;
  max-width: 420px;
  box-shadow: var(--shadow-lg);
  animation: slideUp .2s ease;
}
@keyframes slideUp { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
.modal h2 { font-size: 1.3rem; font-weight: 800; margin-bottom: .3rem; }
.modal p  { font-size: .84rem; color: var(--muted); margin-bottom: 1.5rem; line-height: 1.5; }
.modal-field { margin-bottom: .8rem; }
.modal-field label { display: block; font-size: .72rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: .3rem; }
.modal-field input, .modal-field select {
  width: 100%;
  padding: .65rem .85rem;
  border: 1.5px solid var(--border);
  border-radius: 9px;
  font-family: var(--font);
  font-size: .88rem;
  color: var(--text);
  background: var(--bg);
  outline: none;
}
.modal-field input:focus, .modal-field select:focus { border-color: var(--accent); background: #fff; }
.modal-actions { display: flex; gap: .6rem; margin-top: 1.2rem; }
.modal-close { flex: 1; padding: .7rem; border: 1.5px solid var(--border); background: transparent; border-radius: 9px; font-family: var(--font); font-size: .85rem; font-weight: 600; cursor: pointer; color: var(--muted); }
.modal-close:hover { background: var(--bg); }
.modal-submit { flex: 2; padding: .7rem; background: var(--accent); color: #fff; border: none; border-radius: 9px; font-family: var(--font); font-size: .85rem; font-weight: 700; cursor: pointer; }
.modal-submit:hover { opacity: .9; }
.modal-success { text-align: center; padding: 1rem 0; }
.modal-success .ms-icon { font-size: 2.5rem; margin-bottom: .6rem; }
.modal-success h3 { font-size: 1.1rem; font-weight: 700; margin-bottom: .3rem; }
.modal-success p { font-size: .84rem; color: var(--muted); }

/* ── PAYMENT MODAL ── */
.pay-modal {
  background: var(--surface);
  border-radius: 20px;
  padding: 2rem;
  width: 100%;
  max-width: 400px;
  box-shadow: var(--shadow-lg);
  animation: slideUp .2s ease;
}
.pay-modal h2 { font-size: 1.2rem; font-weight: 800; margin-bottom: .25rem; }
.pay-modal .pay-sub { font-size: .83rem; color: var(--muted); margin-bottom: 1.4rem; line-height: 1.5; }
.pay-amount {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: .8rem 1rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 1.2rem;
}
.pay-amount .pa-label { font-size: .78rem; color: var(--muted); }
.pay-amount .pa-price { font-size: 1.4rem; font-weight: 800; color: var(--text); }
.pay-amount .pa-inc   { font-size: .7rem; color: var(--muted); }
.pay-includes {
  margin-bottom: 1.2rem;
  display: flex;
  flex-direction: column;
  gap: .3rem;
}
.pay-includes .pi-row { display: flex; align-items: center; gap: .5rem; font-size: .8rem; color: var(--text); }
.pi-check { color: var(--low); font-weight: 700; font-size: .9rem; }
.card-field {
  margin-bottom: .8rem;
}
.card-field label { display: block; font-size: .7rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: .3rem; }
.card-input {
  width: 100%;
  padding: .65rem .85rem;
  border: 1.5px solid var(--border);
  border-radius: 9px;
  font-family: var(--font);
  font-size: .92rem;
  color: var(--text);
  background: var(--bg);
  outline: none;
  letter-spacing: .5px;
  transition: border-color .15s;
}
.card-input:focus { border-color: var(--accent); background: #fff; }
.card-input.error { border-color: var(--high); }
.card-row { display: grid; grid-template-columns: 1fr 1fr; gap: .6rem; }
.pay-btn {
  width: 100%;
  padding: .85rem;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 10px;
  font-family: var(--font);
  font-size: .92rem;
  font-weight: 700;
  cursor: pointer;
  margin-top: .4rem;
  transition: opacity .15s, transform .1s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: .5rem;
}
.pay-btn:hover { opacity: .9; transform: translateY(-1px); }
.pay-btn:disabled { opacity: .55; cursor: not-allowed; transform: none; }
.pay-secure { display: flex; align-items: center; justify-content: center; gap: .4rem; font-size: .72rem; color: var(--muted); margin-top: .7rem; }
.pay-error  { font-size: .78rem; color: var(--high); margin-top: .4rem; display: none; }
.pay-success { text-align: center; padding: .5rem 0; }
.pay-success .ps-icon { font-size: 2.8rem; margin-bottom: .6rem; }
.pay-success h3 { font-size: 1.15rem; font-weight: 800; margin-bottom: .4rem; }
.pay-success p  { font-size: .84rem; color: var(--muted); margin-bottom: 1.2rem; line-height: 1.55; }
.spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid rgba(255,255,255,.4); border-top-color: #fff; border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* PDF button */
.pdf-btn {
  display: none;
  align-items: center;
  gap: .35rem;
  padding: .38rem .85rem;
  border-radius: 8px;
  border: 1.5px solid var(--border);
  background: var(--surface);
  font-family: var(--font);
  font-size: .78rem;
  font-weight: 600;
  cursor: pointer;
  color: var(--text);
  transition: all .12s;
}
.pdf-btn:hover { border-color: var(--accent); color: var(--accent); }
.pdf-btn.visible { display: flex; }

/* ── LOCK WALL ── */
.lock-wall {
  position: relative;
  margin: 0;
}
.lock-ghost {
  padding: .65rem 1rem;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: flex-start;
  gap: .6rem;
  filter: blur(3.5px);
  pointer-events: none;
  user-select: none;
  opacity: .7;
}
.ghost-line {
  height: 10px;
  border-radius: 6px;
  background: var(--border);
}
.lock-cta {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: linear-gradient(to bottom, rgba(255,255,255,0) 0%, rgba(255,255,255,.97) 30%);
  padding: 1rem;
  text-align: center;
  cursor: pointer;
}
.lock-cta:hover .lock-cta-btn { opacity: .88; transform: translateY(-1px); }
.lock-icon { font-size: 1.5rem; margin-bottom: .4rem; }
.lock-headline { font-size: .88rem; font-weight: 700; color: var(--text); margin-bottom: .25rem; }
.lock-sub { font-size: .75rem; color: var(--muted); line-height: 1.4; margin-bottom: .9rem; max-width: 190px; }
.lock-cta-btn {
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: .5rem 1.1rem;
  font-family: var(--font);
  font-size: .8rem;
  font-weight: 700;
  cursor: pointer;
  transition: all .15s;
}
.free-badge {
  display: inline-block;
  background: var(--accent-lt);
  color: var(--accent);
  border: 1px solid #FDDCB5;
  border-radius: 999px;
  padding: 1px 7px;
  font-size: .67rem;
  font-weight: 700;
  margin-left: .4rem;
  vertical-align: middle;
  letter-spacing: .3px;
}
.locked-detail {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  text-align: center;
  padding: 2rem;
}
.locked-detail .ld-icon { font-size: 2.4rem; margin-bottom: .7rem; }
.locked-detail h3 { font-size: 1.05rem; font-weight: 700; margin-bottom: .4rem; }
.locked-detail p { font-size: .84rem; color: var(--muted); line-height: 1.55; max-width: 300px; margin-bottom: 1.2rem; }
.locked-detail .ld-btn {
  background: var(--accent); color: #fff; border: none; border-radius: 10px;
  padding: .7rem 1.6rem; font-family: var(--font); font-size: .88rem; font-weight: 700;
  cursor: pointer; transition: opacity .15s;
}
.locked-detail .ld-btn:hover { opacity: .88; }

/* ── EMAIL MODAL ── */
.email-modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,.5);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 1100;
  backdrop-filter: blur(4px);
  padding: 1rem;
}
.email-modal-overlay.open { display: flex; }
.email-modal {
  background: var(--surface);
  border-radius: 20px;
  width: 100%;
  max-width: 860px;
  max-height: 92vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 24px 80px rgba(0,0,0,.18);
  animation: slideUp .2s ease;
  overflow: hidden;
}
.email-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1.1rem 1.5rem;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.email-modal-header h2 { font-size: 1rem; font-weight: 700; }
.email-modal-header p  { font-size: .78rem; color: var(--muted); margin-top: 2px; }
.email-modal-body {
  display: grid;
  grid-template-columns: 260px 1fr;
  flex: 1;
  overflow: hidden;
}
.email-config {
  border-right: 1px solid var(--border);
  padding: 1.2rem;
  overflow-y: auto;
  background: var(--bg);
}
.email-config::-webkit-scrollbar { width: 3px; }
.email-config::-webkit-scrollbar-thumb { background: var(--border); }
.email-preview-wrap {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.email-preview-toolbar {
  display: flex;
  align-items: center;
  gap: .5rem;
  padding: .7rem 1rem;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  flex-shrink: 0;
}
.email-preview-toolbar span { font-size: .72rem; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .5px; }
.email-preview-toolbar .ml-auto { margin-left: auto; }
.ep-btn {
  padding: .35rem .8rem;
  border-radius: 7px;
  font-size: .78rem;
  font-weight: 600;
  cursor: pointer;
  border: 1.5px solid var(--border);
  background: var(--surface);
  color: var(--text);
  font-family: var(--font);
  transition: all .12s;
}
.ep-btn:hover { background: var(--bg); border-color: var(--accent); color: var(--accent); }
.ep-btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.ep-btn.primary:hover { opacity: .88; }
.ep-btn.copied { background: var(--low); color: #fff; border-color: var(--low); }
.email-textarea {
  flex: 1;
  resize: none;
  border: none;
  outline: none;
  padding: 1.2rem 1.4rem;
  font-family: 'Georgia', serif;
  font-size: .84rem;
  line-height: 1.8;
  color: var(--text);
  background: var(--surface);
  overflow-y: auto;
}
.email-textarea::-webkit-scrollbar { width: 4px; }
.email-textarea::-webkit-scrollbar-thumb { background: var(--border); }
.ecfg-label { font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .5px; color: var(--muted); margin-bottom: .3rem; display: block; }
.ecfg-input {
  width: 100%;
  padding: .5rem .7rem;
  border: 1.5px solid var(--border);
  border-radius: 8px;
  font-family: var(--font);
  font-size: .82rem;
  color: var(--text);
  background: var(--surface);
  outline: none;
  margin-bottom: .8rem;
}
.ecfg-input:focus { border-color: var(--accent); }
.inv-row { display: flex; gap: .4rem; align-items: center; margin-bottom: .4rem; }
.inv-row input { flex: 1; padding: .4rem .6rem; border: 1.5px solid var(--border); border-radius: 7px; font-family: var(--font); font-size: .8rem; outline: none; }
.inv-row input:focus { border-color: var(--accent); }
.inv-del { background: none; border: none; cursor: pointer; color: var(--muted); font-size: 1rem; padding: 0 .2rem; }
.inv-del:hover { color: var(--high); }
.add-inv-btn { font-size: .75rem; font-weight: 600; color: var(--accent); background: none; border: none; cursor: pointer; padding: .2rem 0; }
.add-inv-btn:hover { opacity: .8; }
.ecfg-section { margin-bottom: 1.2rem; }
.clause-snippet {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: .6rem .8rem;
  font-size: .75rem;
  color: var(--muted);
  line-height: 1.5;
  max-height: 80px;
  overflow: hidden;
  font-style: italic;
  margin-top: .3rem;
}
.email-close-btn {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 1.3rem;
  color: var(--muted);
  line-height: 1;
  padding: 0 .2rem;
}
.email-close-btn:hover { color: var(--text); }

/* ── UTILS ── */
.hidden { display: none !important; }
.flex   { display: flex; }
.items-center { align-items: center; }
.gap-2 { gap: .5rem; }
.ml-auto { margin-left: auto; }
</style>
</head>
<body>

<!-- ── UPLOAD PAGE ────────────────────────────────────────────── -->
<div id="upload-page" style="display:flex; flex-direction:column;">

  <nav class="top-bar">
    <div class="logo">🛡️ Lease<span class="logo-dot">Guard</span></div>
    <div style="display:flex;gap:.6rem;">
      <button class="nav-btn" onclick="openModal()">Sign up</button>
    </div>
  </nav>

  <div class="hero">
    <div class="hero-badge">
      <svg viewBox="0 0 16 16" fill="currentColor" width="12"><path d="M8 1l2.09 4.26L15 6.27l-3.5 3.41.83 4.82L8 12.07l-4.33 2.43.83-4.82L1 6.27l4.91-.71L8 1z"/></svg>
      AI-powered · Australian law · No lawyers needed
    </div>
    <h1>Know every risk <em>before</em><br>you sign</h1>
    <p>Upload your commercial lease and get a clause-by-clause risk analysis in under 60 seconds.</p>

    <div class="upload-card">
      <div class="field-row">
        <div class="field">
          <label>Jurisdiction</label>
          <select id="jurisdiction">
            <option>NSW</option><option>VIC</option><option>QLD</option>
            <option>SA</option><option>WA</option><option>TAS</option>
            <option>ACT</option><option>NT</option>
          </select>
        </div>
        <div class="field">
          <label>Tenant name</label>
          <input id="tenant" type="text" placeholder="e.g. Acme Pty Ltd">
        </div>
      </div>

      <div class="drop-zone" id="drop-zone">
        <input type="file" id="file-input" accept=".pdf" onchange="onFileSelect(this)">
        <div class="drop-icon">📄</div>
        <p><strong>Drop your lease PDF here</strong><br>or click to browse</p>
        <div class="drop-selected hidden" id="file-label"></div>
      </div>

      <button class="analyse-btn" id="analyse-btn" onclick="startAnalysis()" disabled>
        Analyse lease →
      </button>
    </div>

    <div class="trust-row">
      <div class="trust-item">
        <svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 100 14A7 7 0 008 1zm0 1a6 6 0 110 12A6 6 0 018 2zm-.5 3v4l3 1.5.5-.87-2.5-1.25V5h-1z"/></svg>
        Under 60 seconds
      </div>
      <div class="trust-item">
        <svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1l1.5 3 3.5.5-2.5 2.5.6 3.5L8 9 4.9 10.5l.6-3.5L3 4.5 6.5 4z"/></svg>
        Australian legislation
      </div>
      <div class="trust-item">
        <svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1C5.2 1 3 3.2 3 6v1H2v7h12V7h-1V6c0-2.8-2.2-5-5-5zm0 1c2.2 0 4 1.8 4 4v1H4V6c0-2.2 1.8-4 4-4zm0 7a1 1 0 110 2 1 1 0 010-2z"/></svg>
        Runs locally
      </div>
    </div>
  </div>
</div>

<!-- ── PROGRESS PAGE ──────────────────────────────────────────── -->
<div id="progress-page" style="display:none; min-height:100vh; align-items:center; justify-content:center; flex-direction:column;">
  <div class="progress-wrap">
    <div style="font-size:2.5rem;margin-bottom:1rem;">🔍</div>
    <h3>Analysing your lease…</h3>
    <p id="progress-msg">Reading document</p>
    <div class="progress-track"><div class="progress-fill" id="progress-fill"></div></div>
  </div>
</div>

<!-- ── RESULTS PAGE ───────────────────────────────────────────── -->
<div id="results-page" style="display:none; flex-direction:column; min-height:100vh;">

  <!-- Header -->
  <div class="results-header">
    <div class="logo" style="font-size:.95rem;">🛡️ Lease<span style="color:var(--accent);">Guard</span></div>
    <div style="width:1px;height:20px;background:var(--border);"></div>
    <div>
      <div class="results-file" id="r-filename">—</div>
      <div class="results-meta" id="r-meta">—</div>
    </div>
    <span class="risk-badge" id="r-badge">—</span>
    <div class="results-header-actions">
      <button class="pdf-btn" id="pdf-btn" onclick="generatePDF()">
        📄 Download PDF
      </button>
      <button class="nav-btn" onclick="document.getElementById('upload-page').style.display='flex';document.getElementById('results-page').style.display='none';">
        ← New lease
      </button>
      <button class="nav-btn primary" id="header-cta-btn" onclick="openPayModal()">Unlock — A$25</button>
    </div>
  </div>

  <!-- Stats strip -->
  <div class="stats-strip">
    <div class="stat-cell">
      <div class="stat-val" id="s-score" style="color:var(--accent)">—</div>
      <div class="stat-lbl">Risk score</div>
    </div>
    <div class="stat-cell">
      <div class="stat-val" id="s-clauses">—</div>
      <div class="stat-lbl">Clauses</div>
    </div>
    <div class="stat-cell">
      <div class="stat-val" id="s-high" style="color:var(--high)">—</div>
      <div class="stat-lbl">High risk</div>
    </div>
    <div class="stat-cell">
      <div class="stat-val" id="s-med" style="color:var(--med)">—</div>
      <div class="stat-lbl">Medium</div>
    </div>
    <div class="stat-cell">
      <div class="stat-val" id="s-low" style="color:var(--low)">—</div>
      <div class="stat-lbl">Low</div>
    </div>
  </div>

  <!-- Body -->
  <div class="results-body">

    <!-- Left: clause list -->
    <div class="clause-pane">
      <div class="pane-header">
        <div class="pane-title">Clauses</div>
        <div class="filter-pills" id="filter-pills">
          <button class="fpill active" data-sev="all" onclick="setSevFilter('all',this)">All</button>
          <button class="fpill" data-sev="high" onclick="setSevFilter('high',this)">🔴 High</button>
          <button class="fpill" data-sev="medium" onclick="setSevFilter('medium',this)">🟠 Medium</button>
          <button class="fpill" data-sev="low" onclick="setSevFilter('low',this)">🟢 Low</button>
          <button class="fpill" data-sev="none" onclick="setSevFilter('none',this)">✅ Clean</button>
        </div>
        <select class="type-filter" id="type-filter" onchange="renderList()">
          <option value="all">All clause types</option>
        </select>
      </div>
      <div class="clause-count" id="clause-count"></div>
      <div class="clause-list" id="clause-list"></div>
    </div>

    <!-- Right: clause detail -->
    <div class="detail-pane" id="detail-pane">
      <div class="detail-empty">
        <div class="de-icon">👈</div>
        <div>Select a clause to review</div>
      </div>
    </div>

  </div>
</div>

<!-- ── SIGNUP MODAL ───────────────────────────────────────────── -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <div id="modal-form-wrap">
      <h2>🛡️ Get early access</h2>
      <p>Join the waitlist. Be first to know when TenantSentry launches — and help shape the product.</p>
      <div class="modal-field">
        <label>Your name</label>
        <input id="m-name" type="text" placeholder="Garry">
      </div>
      <div class="modal-field">
        <label>Email address</label>
        <input id="m-email" type="email" placeholder="you@example.com">
      </div>
      <div class="modal-field">
        <label>I am a…</label>
        <select id="m-role">
          <option>Commercial tenant</option>
          <option>Business owner</option>
          <option>Tenant's solicitor / advisor</option>
          <option>Property manager</option>
          <option>Other</option>
        </select>
      </div>
      <div id="modal-error" style="color:var(--high);font-size:.8rem;margin-top:.5rem;display:none;"></div>
      <div class="modal-actions">
        <button class="modal-close" onclick="closeModal()">Cancel</button>
        <button class="modal-submit" onclick="submitSignup()">Join waitlist →</button>
      </div>
    </div>
    <div id="modal-success-wrap" class="modal-success hidden">
      <div class="ms-icon">🎉</div>
      <h3>You're on the list!</h3>
      <p id="modal-success-msg"></p>
      <button class="modal-submit" style="margin-top:1.2rem;width:100%;" onclick="closeModal()">Done</button>
    </div>
  </div>
</div>

<!-- ── PAYMENT MODAL ─────────────────────────────────────────── -->
<div class="modal-overlay" id="pay-modal">
  <div class="pay-modal">

    <div id="pay-form-wrap">
      <h2>🛡️ Unlock full report</h2>
      <p class="pay-sub">One-time payment per lease. Instant access — no subscription.</p>

      <div class="pay-amount">
        <div>
          <div class="pa-label">Full Lease Analysis</div>
          <div class="pa-inc" id="pay-filename-label">1 document</div>
        </div>
        <div style="text-align:right;">
          <div class="pa-price">A$25.00</div>
          <div class="pa-inc">inc. GST</div>
        </div>
      </div>

      <div class="pay-includes">
        <div class="pi-row"><span class="pi-check">✓</span> All clauses analysed with risk flags</div>
        <div class="pi-row"><span class="pi-check">✓</span> Write-to-landlord emails for every issue</div>
        <div class="pi-row"><span class="pi-check">✓</span> Downloadable PDF audit report</div>
        <div class="pi-row"><span class="pi-check">✓</span> Legislation references cited throughout</div>
      </div>

      <div class="card-field">
        <label>Card number</label>
        <input class="card-input" id="card-number" placeholder="1234 5678 9012 3456"
               maxlength="19" oninput="formatCardNumber(this)" autocomplete="cc-number">
      </div>
      <div class="card-row">
        <div class="card-field">
          <label>Expiry</label>
          <input class="card-input" id="card-expiry" placeholder="MM / YY"
                 maxlength="7" oninput="formatExpiry(this)" autocomplete="cc-exp">
        </div>
        <div class="card-field">
          <label>CVC</label>
          <input class="card-input" id="card-cvc" placeholder="123"
                 maxlength="3" oninput="this.value=this.value.replace(/\D/g,'')" autocomplete="cc-csc">
        </div>
      </div>
      <div class="card-field">
        <label>Name on card</label>
        <input class="card-input" id="card-name" placeholder="Garry Smith" autocomplete="cc-name">
      </div>

      <div class="pay-error" id="pay-error"></div>

      <button class="pay-btn" id="pay-btn" onclick="processPayment()">
        <span id="pay-btn-text">Pay A$25.00</span>
      </button>

      <div class="pay-secure">
        🔒 &nbsp;256-bit SSL &nbsp;·&nbsp; No card details stored &nbsp;·&nbsp; Powered by Stripe
      </div>
      <div style="text-align:center;margin-top:.8rem;">
        <button onclick="closePayModal()" style="background:none;border:none;font-size:.78rem;color:var(--muted);cursor:pointer;">Cancel</button>
      </div>
    </div>

    <div id="pay-success-wrap" class="pay-success hidden">
      <div class="ps-icon">🎉</div>
      <h3>Payment confirmed!</h3>
      <p>Your full report is now unlocked. All clauses, risk flags, landlord emails and PDF report are ready.</p>
      <button class="pay-btn" onclick="closePayModal()">View full report →</button>
    </div>

  </div>
</div>

<!-- ── EMAIL MODAL ──────────────────────────────────────────── -->
<div class="email-modal-overlay" id="email-modal">
  <div class="email-modal">

    <div class="email-modal-header">
      <div>
        <h2>✉️ Draft landlord email</h2>
        <p>Customise the details, then copy or download the letter.</p>
      </div>
      <button class="email-close-btn" onclick="closeEmailModal()">✕</button>
    </div>

    <div class="email-modal-body">

      <!-- Left: config -->
      <div class="email-config">

        <div class="ecfg-section">
          <label class="ecfg-label">To — Landlord / Agent</label>
          <input class="ecfg-input" id="ec-to-name"  placeholder="e.g. John Smith"        oninput="rebuildEmail()">
          <input class="ecfg-input" id="ec-to-email" placeholder="landlord@example.com"   oninput="rebuildEmail()" style="margin-bottom:0;">
        </div>

        <div class="ecfg-section">
          <label class="ecfg-label">Property address</label>
          <input class="ecfg-input" id="ec-address" placeholder="e.g. Shop 4, 12 King St, Sydney NSW 2000" oninput="rebuildEmail()" style="margin-bottom:0;">
        </div>

        <div class="ecfg-section">
          <label class="ecfg-label">Your name &amp; role</label>
          <input class="ecfg-input" id="ec-from-name"  placeholder="e.g. Garry Smith, Director" oninput="rebuildEmail()">
          <input class="ecfg-input" id="ec-from-phone" placeholder="Phone number (optional)"      oninput="rebuildEmail()" style="margin-bottom:0;">
        </div>

        <div class="ecfg-section">
          <label class="ecfg-label">📎 Invoice / Evidence references</label>
          <div id="inv-list"></div>
          <button class="add-inv-btn" onclick="addInvoiceRow()">+ Add invoice or document</button>
        </div>

        <div class="ecfg-section">
          <label class="ecfg-label">📄 Clause excerpt (attached)</label>
          <div class="clause-snippet" id="ec-clause-snippet">—</div>
        </div>

        <div class="ecfg-section">
          <label class="ecfg-label">Response requested within</label>
          <select class="ecfg-input" id="ec-days" oninput="rebuildEmail()" style="margin-bottom:0;">
            <option value="5">5 business days</option>
            <option value="7" selected>7 business days</option>
            <option value="10">10 business days</option>
            <option value="14">14 business days</option>
          </select>
        </div>

      </div>

      <!-- Right: live email preview -->
      <div class="email-preview-wrap">
        <div class="email-preview-toolbar">
          <span>Email preview</span>
          <div class="ml-auto" style="display:flex;gap:.4rem;">
            <button class="ep-btn" id="copy-btn" onclick="copyEmail()">Copy</button>
            <button class="ep-btn primary" onclick="downloadEmail()">Download .txt</button>
          </div>
        </div>
        <textarea class="email-textarea" id="email-body" spellcheck="true"></textarea>
      </div>

    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let currentData = null;
let selectedClauseId = null;
let sevFilter = 'all';
let isUnlocked = false;
let isPaid = false;
const FREE_LIMIT = 2;   // clauses visible without signup

// ── Drop zone ──────────────────────────────────────────────────────────────
const dz = document.getElementById('drop-zone');
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag-over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f && f.type === 'application/pdf') setFile(f);
});

function onFileSelect(input) { if (input.files[0]) setFile(input.files[0]); }
function setFile(f) {
  document.getElementById('file-label').textContent = '📄 ' + f.name;
  document.getElementById('file-label').classList.remove('hidden');
  document.getElementById('analyse-btn').disabled = false;
  document.getElementById('analyse-btn')._file = f;
}

// ── Analysis ───────────────────────────────────────────────────────────────
async function startAnalysis() {
  const btn = document.getElementById('analyse-btn');
  const file = btn._file;
  if (!file) return;

  show('progress-page');
  hide('upload-page');

  const msgs = ['Reading document…', 'Extracting clauses…', 'Matching legislation…', 'Analysing risks…', 'Finalising report…'];
  let pct = 0;
  const fill = document.getElementById('progress-fill');
  const pmsg = document.getElementById('progress-msg');
  const ticker = setInterval(() => {
    pct = Math.min(pct + 4, 90);
    fill.style.width = pct + '%';
    pmsg.textContent = msgs[Math.floor(pct / 20)] || msgs[4];
  }, 180);

  const fd = new FormData();
  fd.append('file', file);
  fd.append('jurisdiction', document.getElementById('jurisdiction').value);
  fd.append('tenant', document.getElementById('tenant').value);

  try {
    const res = await fetch('/api/analyse', { method: 'POST', body: fd });
    const data = await res.json();
    clearInterval(ticker);
    fill.style.width = '100%';
    await delay(400);
    loadResults(data);
  } catch(e) {
    clearInterval(ticker);
    alert('Analysis failed. Please try again.');
    show('upload-page'); hide('progress-page');
  }
}

function loadResults(data) {
  currentData = data;
  selectedClauseId = null;

  // Header
  document.getElementById('r-filename').textContent = data.filename;
  document.getElementById('r-meta').textContent = data.jurisdiction + ' · ' + (data.tenant || 'Tenant not specified');
  const badge = document.getElementById('r-badge');
  badge.textContent = { HIGH:'🔴 HIGH RISK', MEDIUM:'🟠 MEDIUM RISK', LOW:'🟢 LOW RISK' }[data.risk_level];
  badge.className = 'risk-badge badge-' + data.risk_level;

  // Stats
  const scoreEl = document.getElementById('s-score');
  scoreEl.textContent = data.score;
  scoreEl.style.color = data.risk_level === 'HIGH' ? 'var(--high)' : data.risk_level === 'MEDIUM' ? 'var(--med)' : 'var(--low)';
  document.getElementById('s-clauses').textContent = data.counts.clauses;
  document.getElementById('s-high').textContent = data.counts.high;
  document.getElementById('s-med').textContent = data.counts.medium;
  document.getElementById('s-low').textContent = data.counts.low;

  // Populate type filter
  const types = [...new Set(data.clauses.map(c => c.type))].sort();
  const tf = document.getElementById('type-filter');
  tf.innerHTML = '<option value="all">All clause types</option>' +
    types.map(t => `<option value="${t}">${t}</option>`).join('');

  renderList();
  hide('progress-page');
  showFlex('results-page');

  // Auto-select first high-risk clause
  const firstHigh = data.clauses.find(c => c.severity === 'high');
  if (firstHigh) selectClause(firstHigh.id);
}

// ── Clause list ────────────────────────────────────────────────────────────
function setSevFilter(sev, btn) {
  sevFilter = sev;
  document.querySelectorAll('.fpill').forEach(b => {
    b.className = 'fpill';
    if (b === btn) b.classList.add(sev === 'all' ? 'active' : `active-${sev}`);
  });
  renderList();
}

function renderList() {
  if (!currentData) return;
  const typeVal = document.getElementById('type-filter').value;
  let allFiltered = currentData.clauses;
  if (sevFilter !== 'all') allFiltered = allFiltered.filter(c => c.severity === sevFilter);
  if (typeVal !== 'all') allFiltered = allFiltered.filter(c => c.type === typeVal);

  // Sort: high → medium → low → none so free preview shows worst first
  const sevOrder = { high:0, medium:1, low:2, none:3 };
  allFiltered = [...allFiltered].sort((a,b) => (sevOrder[a.severity]||3) - (sevOrder[b.severity]||3));

  const visible  = isUnlocked ? allFiltered : allFiltered.slice(0, FREE_LIMIT);
  const locked   = isUnlocked ? [] : allFiltered.slice(FREE_LIMIT);

  const countLabel = isUnlocked
    ? allFiltered.length + ' clause' + (allFiltered.length !== 1 ? 's' : '')
    : `${visible.length} of ${allFiltered.length} clauses <span class="free-badge">FREE</span>`;
  document.getElementById('clause-count').innerHTML = countLabel;

  const icons = { high:'⚠️', medium:'🔸', low:'💛', none:'✅' };
  const list = document.getElementById('clause-list');

  if (allFiltered.length === 0) {
    list.innerHTML = '<div style="padding:2rem;text-align:center;color:var(--muted);font-size:.82rem;">No clauses match this filter</div>';
    return;
  }

  // Visible rows
  let html = visible.map(c => `
    <div class="clause-item sev-${c.severity} ${selectedClauseId === c.id ? 'active' : ''}"
         onclick="selectClause(${c.id})">
      <div class="ci-icon">${icons[c.severity] || '📋'}</div>
      <div>
        <div class="ci-title">${esc(c.heading || 'Clause ' + (c.id+1))}</div>
        <div class="ci-type">${esc(c.type)}</div>
        ${c.severity !== 'none' ? `<span class="ci-pill ci-pill-${c.severity}">${c.severity.toUpperCase()}</span>` : ''}
      </div>
    </div>
  `).join('');

  // Lock wall
  if (locked.length > 0) {
    const ghostRows = Math.min(locked.length, 4);
    let ghosts = '';
    for (let i = 0; i < ghostRows; i++) {
      const w1 = 55 + Math.floor(Math.random()*30);
      const w2 = 35 + Math.floor(Math.random()*30);
      ghosts += `
        <div class="lock-ghost">
          <div style="width:18px;height:18px;border-radius:50%;background:var(--border);flex-shrink:0;"></div>
          <div style="flex:1;">
            <div class="ghost-line" style="width:${w1}%;margin-bottom:5px;"></div>
            <div class="ghost-line" style="width:${w2}%;"></div>
          </div>
        </div>`;
    }
    html += `
      <div class="lock-wall">
        ${ghosts}
        <div class="lock-cta" style="cursor:default;">
          <div class="lock-icon">🔒</div>
          <div class="lock-headline">${locked.length} more issue${locked.length>1?'s':''} hidden</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:.5rem;width:100%;max-width:240px;margin-bottom:.4rem;">
            <div onclick="openModal()" style="border:1.5px solid var(--border);border-radius:10px;padding:.6rem .5rem;text-align:center;cursor:pointer;background:white;transition:border-color .15s;"
                 onmouseover="this.style.borderColor='var(--accent)'" onmouseout="this.style.borderColor='var(--border)'">
              <div style="font-size:.7rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:.2rem;">Free</div>
              <div style="font-size:1rem;font-weight:800;color:var(--text);">Sign up</div>
              <div style="font-size:.65rem;color:var(--muted);margin-top:.2rem;">Early access list</div>
            </div>
            <div onclick="openPayModal()" style="border:1.5px solid var(--accent);border-radius:10px;padding:.6rem .5rem;text-align:center;cursor:pointer;background:var(--accent);transition:opacity .15s;"
                 onmouseover="this.style.opacity='.88'" onmouseout="this.style.opacity='1'">
              <div style="font-size:.7rem;font-weight:700;color:rgba(255,255,255,.75);text-transform:uppercase;letter-spacing:.4px;margin-bottom:.2rem;">Full access</div>
              <div style="font-size:1rem;font-weight:800;color:white;">A$25</div>
              <div style="font-size:.65rem;color:rgba(255,255,255,.75);margin-top:.2rem;">This lease only</div>
            </div>
          </div>
        </div>
      </div>`;
  }

  list.innerHTML = html;
}

function selectClause(id) {
  const c = currentData.clauses.find(x => x.id === id);
  if (!c) return;

  // Check if this clause is locked
  const sevOrder = { high:0, medium:1, low:2, none:3 };
  const sorted = [...currentData.clauses].sort((a,b) => (sevOrder[a.severity]||3) - (sevOrder[b.severity]||3));
  const freeIds = new Set(sorted.slice(0, FREE_LIMIT).map(x => x.id));

  if (!isUnlocked && !freeIds.has(id)) {
    // Show locked detail CTA instead
    document.getElementById('detail-pane').innerHTML = `
      <div class="locked-detail">
        <div class="ld-icon">🔒</div>
        <h3>This clause is locked</h3>
        <p>Unlock all ${currentData.clauses.length} clause analyses, risk flags, landlord email drafts, and a PDF audit report.</p>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.7rem;width:100%;max-width:320px;">
          <button class="ld-btn" onclick="openModal()"
            style="background:var(--surface);color:var(--text);border:1.5px solid var(--border);">
            Sign up free
          </button>
          <button class="ld-btn" onclick="openPayModal()">
            Unlock — A$25
          </button>
        </div>
        <div style="font-size:.72rem;color:var(--muted);margin-top:.7rem;">One-time · This lease only · Instant access</div>
      </div>`;
    return;
  }

  selectedClauseId = id;

  // Update list highlight
  document.querySelectorAll('.clause-item').forEach(el => el.classList.remove('active'));
  const items = document.querySelectorAll('.clause-item');
  items.forEach(el => { if (el.onclick.toString().includes(`(${id})`)) el.classList.add('active'); });

  // Render detail
  const sevColor = { high:'var(--high)', medium:'var(--med)', low:'var(--low)', none:'var(--muted)' }[c.severity];
  const sevLabel = { high:'HIGH RISK', medium:'MEDIUM RISK', low:'LOW RISK', none:'No flags' }[c.severity];
  const sevClass = { high:'badge-HIGH', medium:'badge-MEDIUM', low:'badge-LOW', none:'' }[c.severity];

  const flagsHtml = c.risk_flags.length ? c.risk_flags.map(f => `
    <div class="flag-item">
      <div class="flag-sev-dot dot-${f.severity}"></div>
      <div>
        <div class="flag-id">${esc(f.flag_id)}</div>
        <div class="flag-desc">${esc(f.description)}</div>
        ${f.legislation_ref ? `<div class="flag-legref">📖 ${esc(f.legislation_ref)}</div>` : ''}
      </div>
    </div>
  `).join('') : '<div style="color:var(--muted);font-size:.82rem;padding:.5rem 0;">No risk flags detected for this clause.</div>';

  const termsHtml = c.key_terms.length
    ? c.key_terms.map(t => `<span class="term-chip">${esc(t)}</span>`).join('')
    : '<span style="color:var(--muted);font-size:.78rem;">No key terms extracted</span>';

  document.getElementById('detail-pane').innerHTML = `
    <div class="detail-card">
      <div class="detail-card-header">
        <div>
          <div class="dc-title">${esc(c.heading || 'Clause ' + (c.id+1))}</div>
          <span class="dc-type">${esc(c.type)}</span>
        </div>
        <div style="display:flex;align-items:center;gap:.6rem;flex-shrink:0;">
          ${c.severity !== 'none' ? `<span class="risk-badge ${sevClass}" style="white-space:nowrap;">${sevLabel}</span>` : ''}
          <button onclick="openEmailModal(${c.id})"
            style="display:flex;align-items:center;gap:.35rem;padding:.38rem .85rem;
                   border-radius:8px;border:1.5px solid var(--border);background:var(--surface);
                   font-family:var(--font);font-size:.78rem;font-weight:600;cursor:pointer;
                   color:var(--text);white-space:nowrap;transition:all .12s;"
            onmouseover="this.style.borderColor='var(--accent)';this.style.color='var(--accent)'"
            onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text)'">
            ✉️ Write to landlord
          </button>
        </div>
      </div>
      <div class="detail-card-body">
        <div class="section-label">Clause text</div>
        <div class="clause-raw">${esc(c.text.substring(0, 1200))}${c.text.length > 1200 ? '…' : ''}</div>

        <div class="section-label">Key terms</div>
        <div class="terms-row" style="margin-bottom:1.2rem;">${termsHtml}</div>

        <div class="section-label">Plain-English summary</div>
        <div class="summary-block">💬 ${esc(c.summary)}</div>

        <div class="section-label">Recommended action</div>
        <div class="action-block">✅ ${esc(c.action)}</div>

        ${c.risk_flags.length ? `<div class="section-label">Risk flags</div>${flagsHtml}` : ''}
      </div>
    </div>
  `;
}

// ── Payment modal ──────────────────────────────────────────────────────────
function openPayModal() {
  document.getElementById('pay-form-wrap').classList.remove('hidden');
  document.getElementById('pay-success-wrap').classList.add('hidden');
  document.getElementById('pay-error').style.display = 'none';
  if (currentData) {
    document.getElementById('pay-filename-label').textContent = currentData.filename || '1 document';
  }
  document.getElementById('pay-modal').classList.add('open');
}
function closePayModal() {
  document.getElementById('pay-modal').classList.remove('open');
}
document.getElementById('pay-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('pay-modal')) closePayModal();
});

function formatCardNumber(input) {
  let v = input.value.replace(/\D/g, '').substring(0, 16);
  input.value = v.replace(/(.{4})/g, '$1 ').trim();
}
function formatExpiry(input) {
  let v = input.value.replace(/\D/g, '').substring(0, 4);
  if (v.length >= 3) v = v.substring(0,2) + ' / ' + v.substring(2);
  input.value = v;
}

async function processPayment() {
  const num  = document.getElementById('card-number').value.replace(/\s/g,'');
  const exp  = document.getElementById('card-expiry').value.replace(/\s/g,'');
  const cvc  = document.getElementById('card-cvc').value;
  const name = document.getElementById('card-name').value.trim();
  const errEl = document.getElementById('pay-error');
  errEl.style.display = 'none';

  if (num.length < 16)      { showPayErr('Please enter a valid 16-digit card number.'); return; }
  if (exp.length < 5)       { showPayErr('Please enter a valid expiry date (MM/YY).'); return; }
  if (cvc.length < 3)       { showPayErr('Please enter a valid 3-digit CVC.'); return; }
  if (!name)                { showPayErr('Please enter the name on your card.'); return; }

  // Simulate payment processing
  const btn = document.getElementById('pay-btn');
  const btnText = document.getElementById('pay-btn-text');
  btn.disabled = true;
  btnText.innerHTML = '<span class="spinner"></span> Processing…';

  await delay(1800);

  // Mock success (in production, this hits your Stripe backend)
  btn.disabled = false;
  btnText.textContent = 'Pay A$25.00';

  // Unlock everything
  isPaid = true;
  isUnlocked = true;
  renderList();

  // Show PDF button, update header CTA
  document.getElementById('pdf-btn').classList.add('visible');
  const ctaBtn = document.getElementById('header-cta-btn');
  ctaBtn.textContent = '✓ Paid';
  ctaBtn.style.background = 'var(--low)';
  ctaBtn.style.borderColor = 'var(--low)';
  ctaBtn.onclick = null;

  // Show success screen
  document.getElementById('pay-form-wrap').classList.add('hidden');
  document.getElementById('pay-success-wrap').classList.remove('hidden');
}

function showPayErr(msg) {
  const e = document.getElementById('pay-error');
  e.textContent = msg;
  e.style.display = 'block';
}

// ── PDF report ─────────────────────────────────────────────────────────────
function generatePDF() {
  if (!currentData) return;
  const d = currentData;
  const today = new Date().toLocaleDateString('en-AU', { day:'numeric', month:'long', year:'numeric' });
  const riskColor = d.risk_level === 'HIGH' ? '#DC2626' : d.risk_level === 'MEDIUM' ? '#D97706' : '#16A34A';

  const clauseRows = d.clauses.map(c => {
    const sevColor = { high:'#DC2626', medium:'#D97706', low:'#16A34A', none:'#9C8F85' }[c.severity];
    const flagList = c.risk_flags.length
      ? c.risk_flags.map(f => `<li style="margin-bottom:4px;"><strong>${f.flag_id}</strong> — ${f.description}${f.legislation_ref ? `<br><em style="color:#9C8F85;font-size:11px;">📖 ${f.legislation_ref}</em>` : ''}</li>`).join('')
      : '<li style="color:#9C8F85;">No flags detected</li>';

    return `
      <div style="margin-bottom:1.5rem;padding:1rem 1.2rem;border:1px solid #E8E3DC;border-left:4px solid ${sevColor};border-radius:8px;background:#fff;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.6rem;">
          <div>
            <div style="font-weight:700;font-size:14px;color:#1A1614;">${esc(c.heading || 'Clause ' + (c.id+1))}</div>
            <div style="font-size:11px;color:#9C8F85;margin-top:2px;">${esc(c.type)}</div>
          </div>
          ${c.severity !== 'none' ? `<span style="background:${sevColor}18;color:${sevColor};padding:2px 10px;border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap;">${c.severity.toUpperCase()} RISK</span>` : ''}
        </div>
        <div style="background:#F7F5F2;border-radius:6px;padding:.6rem .8rem;font-size:12px;color:#6B5E57;line-height:1.6;margin-bottom:.6rem;max-height:80px;overflow:hidden;">
          ${esc(c.text.substring(0,400))}${c.text.length > 400 ? '…' : ''}
        </div>
        <div style="font-size:12px;color:#44403C;background:#FFFBEB;border-left:3px solid #FBB f24;padding:.5rem .8rem;border-radius:0 6px 6px 0;margin-bottom:.5rem;line-height:1.5;">
          💬 ${esc(c.summary)}
        </div>
        ${c.risk_flags.length ? `<ul style="margin:.4rem 0 0 1.2rem;padding:0;font-size:12px;color:#1A1614;line-height:1.6;">${flagList}</ul>` : ''}
      </div>`;
  }).join('');

  const html = `<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>TenantSentry Audit Report — ${esc(d.filename)}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Helvetica Neue', Arial, sans-serif; color: #1A1614; background: #fff; padding: 2.5rem; font-size: 13px; line-height: 1.6; }
  @media print {
    body { padding: 0; }
    .no-print { display: none !important; }
    @page { margin: 2cm; size: A4; }
  }
</style>
</head>
<body>

<div class="no-print" style="background:#1A1614;color:white;padding:1rem 2rem;display:flex;align-items:center;justify-content:space-between;margin:-2.5rem -2.5rem 2rem;">
  <span style="font-weight:700;">🛡️ TenantSentry — Audit Report Preview</span>
  <button onclick="window.print()" style="background:#D4792A;color:white;border:none;padding:.5rem 1.2rem;border-radius:8px;font-weight:700;cursor:pointer;font-size:13px;">🖨 Print / Save as PDF</button>
</div>

<div style="border-bottom:2px solid #E8E3DC;padding-bottom:1.5rem;margin-bottom:1.5rem;">
  <div style="display:flex;align-items:flex-start;justify-content:space-between;">
    <div>
      <div style="font-size:22px;font-weight:800;color:#1A1614;">🛡️ TenantSentry Audit Report</div>
      <div style="font-size:13px;color:#9C8F85;margin-top:.3rem;">Prepared ${today}</div>
    </div>
    <span style="background:${riskColor}18;color:${riskColor};padding:.3rem 1rem;border-radius:999px;font-size:12px;font-weight:700;">${d.risk_level} RISK</span>
  </div>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1.5rem;">
  <div style="background:#F7F5F2;border-radius:8px;padding:.8rem 1rem;">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#9C8F85;margin-bottom:.2rem;">Document</div>
    <div style="font-weight:700;">${esc(d.filename)}</div>
  </div>
  <div style="background:#F7F5F2;border-radius:8px;padding:.8rem 1rem;">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#9C8F85;margin-bottom:.2rem;">Tenant</div>
    <div style="font-weight:700;">${esc(d.tenant || '—')}</div>
  </div>
  <div style="background:#F7F5F2;border-radius:8px;padding:.8rem 1rem;">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#9C8F85;margin-bottom:.2rem;">Jurisdiction</div>
    <div style="font-weight:700;">${esc(d.jurisdiction)}</div>
  </div>
  <div style="background:#F7F5F2;border-radius:8px;padding:.8rem 1rem;">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#9C8F85;margin-bottom:.2rem;">Risk Score</div>
    <div style="font-weight:800;font-size:18px;color:${riskColor};">${d.score} <span style="font-size:12px;font-weight:400;color:#9C8F85;">/ 100</span></div>
  </div>
</div>

<div style="display:flex;gap:.6rem;margin-bottom:1.8rem;">
  <span style="background:#FEF2F2;color:#DC2626;padding:.25rem .8rem;border-radius:999px;font-size:11px;font-weight:700;">🔴 ${d.counts.high} High risk</span>
  <span style="background:#FFFBEB;color:#D97706;padding:.25rem .8rem;border-radius:999px;font-size:11px;font-weight:700;">🟠 ${d.counts.medium} Medium</span>
  <span style="background:#F0FDF4;color:#16A34A;padding:.25rem .8rem;border-radius:999px;font-size:11px;font-weight:700;">🟢 ${d.counts.low} Low</span>
  <span style="background:#F7F5F2;color:#9C8F85;padding:.25rem .8rem;border-radius:999px;font-size:11px;font-weight:700;">📋 ${d.counts.clauses} Clauses</span>
</div>

<div style="font-size:15px;font-weight:800;margin-bottom:1rem;padding-bottom:.5rem;border-bottom:1px solid #E8E3DC;">Clause-by-Clause Analysis</div>
${clauseRows}

<div style="margin-top:2rem;padding-top:1rem;border-top:1px solid #E8E3DC;font-size:11px;color:#9C8F85;text-align:center;line-height:1.6;">
  Generated by TenantSentry AI · ${today}<br>
  This report does not constitute legal advice. Always consult a qualified solicitor before signing or negotiating a lease.
</div>

</body>
</html>`;

  const w = window.open('', '_blank');
  w.document.write(html);
  w.document.close();
}

// ── Email modal ────────────────────────────────────────────────────────────
let emailClauseId = null;

function openEmailModal(id) {
  emailClauseId = id;
  const c = currentData.clauses.find(x => x.id === id);
  if (!c) return;

  // Pre-fill tenant name from analysis
  const fromName = document.getElementById('ec-from-name');
  if (!fromName.value && currentData.tenant && currentData.tenant !== 'Unknown') {
    fromName.value = currentData.tenant;
  }

  // Show clause snippet
  document.getElementById('ec-clause-snippet').textContent =
    (c.text || '').substring(0, 220) + (c.text.length > 220 ? '…' : '');

  // Seed invoice list if empty
  if (!document.getElementById('inv-list').children.length) addInvoiceRow();

  rebuildEmail();
  document.getElementById('email-modal').classList.add('open');
}

function closeEmailModal() {
  document.getElementById('email-modal').classList.remove('open');
}
document.getElementById('email-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('email-modal')) closeEmailModal();
});

function addInvoiceRow(val = '') {
  const list = document.getElementById('inv-list');
  const row = document.createElement('div');
  row.className = 'inv-row';
  row.innerHTML = `
    <input type="text" placeholder="e.g. Invoice #1042 or Lease schedule A" value="${val}" oninput="rebuildEmail()">
    <button class="inv-del" onclick="this.parentElement.remove();rebuildEmail()">✕</button>
  `;
  list.appendChild(row);
  rebuildEmail();
}

function getInvoices() {
  return Array.from(document.querySelectorAll('#inv-list input'))
    .map(i => i.value.trim()).filter(Boolean);
}

function rebuildEmail() {
  if (emailClauseId === null) return;
  const c = currentData.clauses.find(x => x.id === emailClauseId);
  if (!c) return;

  const toName    = document.getElementById('ec-to-name').value.trim()    || '[Landlord / Agent Name]';
  const address   = document.getElementById('ec-address').value.trim()    || '[Property Address]';
  const fromName  = document.getElementById('ec-from-name').value.trim()  || '[Your Name]';
  const fromPhone = document.getElementById('ec-from-phone').value.trim();
  const days      = document.getElementById('ec-days').value;
  const invoices  = getInvoices();
  const today     = new Date().toLocaleDateString('en-AU', { day:'numeric', month:'long', year:'numeric' });
  const tenant    = currentData.tenant && currentData.tenant !== 'Unknown' ? currentData.tenant : '[Tenant Name]';

  // Build issues list from risk flags
  const issueLines = c.risk_flags.length
    ? c.risk_flags.map(f => {
        let line = `  • ${f.description.trim()}`;
        if (f.legislation_ref) line += `\n    (Ref: ${f.legislation_ref})`;
        return line;
      }).join('\n')
    : `  • ${c.summary}`;

  // Build evidence list
  const evidenceLines = [`  • Screenshot / excerpt of ${c.heading || c.type} clause (see attachment)`];
  invoices.forEach(inv => evidenceLines.push(`  • ${inv}`));

  // Build the email
  const subject = `RE: Concern Regarding ${c.type} Clause — Commercial Lease at ${address}`;

  const body = `${today}

${toName}
${address}

Dear ${toName},

RE: ${c.type} Clause — Commercial Lease at ${address}

I am writing on behalf of ${tenant} regarding the above-referenced commercial lease. \
Following a detailed review of the document, we have identified a concern with the \
${c.heading ? `"${c.heading}"` : c.type} clause that requires clarification and, where appropriate, \
amendment prior to execution.

─────────────────────────────────────────
ISSUE IDENTIFIED
─────────────────────────────────────────

Clause reviewed: ${c.heading || c.type}

${issueLines}

─────────────────────────────────────────
RELEVANT CLAUSE TEXT
─────────────────────────────────────────

"${c.text.substring(0, 500).trim()}${c.text.length > 500 ? '…' : ''}"

(Full clause excerpt is attached to this correspondence for your reference.)

─────────────────────────────────────────
OUR REQUEST
─────────────────────────────────────────

${c.action}

We consider this amendment reasonable and consistent with standard commercial leasing \
practice and, where applicable, the requirements of the relevant tenancy legislation.

─────────────────────────────────────────
ATTACHED EVIDENCE
─────────────────────────────────────────

${evidenceLines.join('\n')}

─────────────────────────────────────────

We respectfully request your written response within ${days} business days of the date of \
this letter. We remain open to a constructive discussion and are committed to reaching an \
agreement that is fair to both parties.

Should you have any questions, please do not hesitate to contact me directly.

Yours sincerely,

${fromName}
${tenant}${fromPhone ? '\n' + fromPhone : ''}

─────────────────────────────────────────
This letter was prepared with the assistance of TenantSentry AI.
It does not constitute legal advice. Please consult a solicitor before taking any action.
─────────────────────────────────────────`;

  document.getElementById('email-body').value = `Subject: ${subject}\n\n${body}`;
}

function copyEmail() {
  const ta = document.getElementById('email-body');
  ta.select();
  navigator.clipboard.writeText(ta.value).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.textContent = '✓ Copied';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
  });
}

function downloadEmail() {
  const c = currentData.clauses.find(x => x.id === emailClauseId);
  const text = document.getElementById('email-body').value;
  const blob = new Blob([text], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `TenantSentry_Letter_${(c?.type || 'Clause').replace(/\s+/g,'_')}.txt`;
  a.click();
}

// ── Signup modal ───────────────────────────────────────────────────────────
function openModal() {
  document.getElementById('modal-form-wrap').classList.remove('hidden');
  document.getElementById('modal-success-wrap').classList.add('hidden');
  document.getElementById('modal-error').style.display = 'none';
  document.getElementById('modal').classList.add('open');
}
function closeModal() { document.getElementById('modal').classList.remove('open'); }
document.getElementById('modal').addEventListener('click', e => { if (e.target === document.getElementById('modal')) closeModal(); });

async function submitSignup() {
  const name  = document.getElementById('m-name').value.trim();
  const email = document.getElementById('m-email').value.trim();
  const role  = document.getElementById('m-role').value;
  const errEl = document.getElementById('modal-error');
  errEl.style.display = 'none';

  if (!name) { showErr('Please enter your name.'); return; }
  if (!email || !email.includes('@')) { showErr('Please enter a valid email.'); return; }

  const res = await fetch('/api/signup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, email, role }),
  });
  const data = await res.json();

  if (data.ok || data.msg === 'already_exists') {
    // Unlock the full report
    isUnlocked = true;
    renderList();

    document.getElementById('modal-form-wrap').classList.add('hidden');
    document.getElementById('modal-success-wrap').classList.remove('hidden');
    if (data.ok) {
      document.getElementById('modal-success-msg').textContent =
        `Full report unlocked. We'll also reach out to ${email} when we launch. Thanks, ${name.split(' ')[0]}!`;
    } else {
      document.getElementById('modal-success-msg').textContent =
        `You're already on the list — full report unlocked. Welcome back, ${name.split(' ')[0]}!`;
    }
    // Auto-select first high-risk clause after unlock
    setTimeout(() => {
      const firstHigh = currentData?.clauses.find(c => c.severity === 'high');
      if (firstHigh && selectedClauseId === null) selectClause(firstHigh.id);
    }, 400);
  }
}
function showErr(msg) {
  const e = document.getElementById('modal-error');
  e.textContent = msg; e.style.display = 'block';
}

// ── Utils ──────────────────────────────────────────────────────────────────
function show(id)    { document.getElementById(id).style.display = 'flex'; }
function showFlex(id){ document.getElementById(id).style.display = 'flex'; }
function hide(id)    { document.getElementById(id).style.display = 'none'; }
function delay(ms)   { return new Promise(r => setTimeout(r, ms)); }
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


if __name__ == "__main__":
    import webbrowser
    print("\n🛡️  TenantSentry starting at http://localhost:8000\n")
    webbrowser.open("http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
