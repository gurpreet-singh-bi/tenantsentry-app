"""
audit_pipeline.py
-----------------
End-to-end orchestration: PDF → chunks → LLM analysis → structured audit.

Local dev mode (default): No embeddings, no Supabase.
  Red flag rules are loaded directly from rules/red_flags.yaml and passed
  into every Claude prompt. Fast, zero external dependencies beyond Anthropic API.

Production mode (USE_VECTOR_STORE=true in .env): Full RAG with VoyageAI + Supabase.

Progress stages:
  5%  → Parsing PDF
  10–20% → OCR (scanned PDFs only)
  25% → Chunking document
  50–90% → Analysing clauses (per clause)
  95% → Assembling result
"""

import os
from pathlib import Path
from typing import Callable, Optional
from loguru import logger
from ingestion.pdf_parser import parse_pdf
from ingestion.chunker import chunk_document
from llm.router import analyse_clause
from output.json_formatter import AuditResult, ClauseAnalysis

ProgressCallback = Optional[Callable[[int, str], None]]

USE_VECTOR_STORE = os.environ.get("USE_VECTOR_STORE", "false").lower() == "true"

# ── Load rules once at import time ────────────────────────────────────────────
_RULES_CONTEXT: str = ""

def _load_rules_context() -> str:
    """Load red_flags.yaml as a formatted string for the LLM prompt."""
    global _RULES_CONTEXT
    if _RULES_CONTEXT:
        return _RULES_CONTEXT

    rules_path = Path(__file__).parent.parent / "rules" / "red_flags.yaml"
    if not rules_path.exists():
        logger.warning("red_flags.yaml not found — LLM will run without rules context")
        return ""

    import yaml
    with open(rules_path) as f:
        data = yaml.safe_load(f)

    lines = ["KNOWN RISK FLAG RULES FOR AUSTRALIAN COMMERCIAL LEASES:\n"]
    for rule in data.get("rules", []):
        lines.append(
            f"Rule {rule['id']} [{rule['severity'].upper()}]: {rule['name']}\n"
            f"  Description: {rule['description'].strip()}\n"
            f"  Trigger keywords: {', '.join(rule.get('trigger_keywords', []))}\n"
            f"  Legislation: {rule.get('legislation_ref') or 'N/A'}\n"
            f"  Action: {rule.get('recommended_action', '').strip()}\n"
        )

    _RULES_CONTEXT = "\n".join(lines)
    logger.info(f"Loaded {len(data.get('rules', []))} rules into prompt context")
    return _RULES_CONTEXT


def _progress(callback: ProgressCallback, pct: int, stage: str) -> None:
    if callback:
        callback(pct, stage)
    logger.debug(f"[{pct}%] {stage}")


def run_audit(
    pdf_path: str,
    jurisdiction: str,
    tenant_name: str = None,
    progress_callback: ProgressCallback = None,
) -> AuditResult:
    """
    Full audit pipeline for a commercial lease PDF.

    Args:
        pdf_path: Absolute path to the lease PDF
        jurisdiction: State code — NSW, VIC, QLD, SA, WA, TAS, ACT, NT
        tenant_name: Optional — used in the output report
        progress_callback: Optional fn(pct: int, stage: str)

    Returns:
        AuditResult with all clause analyses and risk flags
    """
    logger.info(f"Starting audit: {pdf_path} | {jurisdiction} | vector_store={USE_VECTOR_STORE}")
    cb = progress_callback

    # ── 1. Parse PDF ────────────────────────────────────────────────────────
    _progress(cb, 5, "Parsing PDF...")
    parsed = parse_pdf(pdf_path)

    # ── 2. OCR if scanned ───────────────────────────────────────────────────
    if parsed.is_scanned:
        _progress(cb, 10, "Scanned PDF detected — running OCR...")
        try:
            from ingestion.ocr_parser import ocr_pdf, is_ocr_available
            if not is_ocr_available():
                raise RuntimeError(
                    "This lease appears to be a scanned PDF. OCR (Tesseract) is not installed. "
                    "Please upload a digital (text-based) PDF version of the lease."
                )
            ocr_pages = ocr_pdf(pdf_path)
            for i, page in enumerate(parsed.pages):
                if page["is_scanned"] and i < len(ocr_pages):
                    page["text"] = ocr_pages[i]["text"]
            _progress(cb, 20, "OCR complete. Chunking document...")
        except RuntimeError as e:
            raise RuntimeError(str(e))
    else:
        _progress(cb, 20, "Chunking document...")

    # ── 3. Chunk into clauses ───────────────────────────────────────────────
    _progress(cb, 25, "Identifying lease clauses...")
    doc_metadata = {
        "jurisdiction": jurisdiction,
        "tenant_name": tenant_name or "Unknown",
        "filename": parsed.metadata["filename"],
    }
    chunks = chunk_document(parsed.pages, doc_metadata)

    if not chunks:
        raise ValueError(
            "No readable text found in this PDF. "
            "Please ensure it is a valid, non-corrupted lease document."
        )

    logger.info(f"Found {len(chunks)} clauses")

    # ── 4. Optional: embed + store (production only) ────────────────────────
    if USE_VECTOR_STORE:
        _progress(cb, 35, f"Embedding {len(chunks)} clauses...")
        from embedding.embedder import embed_texts
        from vector_store.supabase_store import upsert_chunks
        chunk_texts = [c.content for c in chunks]
        embeddings = embed_texts(chunk_texts, input_type="document")
        rows = [
            {
                "content": c.content,
                "embedding": embeddings[i],
                "metadata": c.metadata,
                "chunk_type": "lease",
                "jurisdiction": jurisdiction,
            }
            for i, c in enumerate(chunks)
        ]
        upsert_chunks(rows)
        _progress(cb, 45, "Stored in knowledge base.")

    # ── 5. Load rules context (local mode) ─────────────────────────────────
    rules_context = _load_rules_context()

    # ── 6. Analyse each clause with LLM ────────────────────────────────────
    clause_analyses = []
    n = len(chunks)

    for idx, chunk in enumerate(chunks):
        pct = 50 + int((idx / max(n, 1)) * 40)
        _progress(cb, pct, f"Analysing clause {idx + 1} of {n}...")

        clause_text = chunk.content

        # In production: retrieve legislation via RAG
        # In local mode: pass empty string (LLM uses training knowledge + rules)
        if USE_VECTOR_STORE:
            from embedding.embedder import embed_query
            from vector_store.supabase_store import similarity_search
            query_vec = embed_query(clause_text)
            legislation_chunks = similarity_search(
                query_embedding=query_vec, top_k=4,
                chunk_type="legislation", jurisdiction=jurisdiction,
            )
            legislation_context = "\n\n".join(c["content"] for c in legislation_chunks)
        else:
            legislation_context = ""

        analysis = analyse_clause(
            clause_text=clause_text,
            legislation_context=legislation_context,
            rules_context=rules_context,
            jurisdiction=jurisdiction,
        )

        clause_analyses.append(ClauseAnalysis(
            clause_heading=chunk.metadata.get("clause_heading", f"Clause {idx + 1}"),
            clause_text=clause_text,
            clause_type=analysis.get("clause_type"),
            key_terms=analysis.get("key_terms", []),
            risk_flags=analysis.get("risk_flags", []),
            plain_english_summary=analysis.get("plain_english_summary"),
            recommended_action=analysis.get("recommended_action"),
            error=analysis.get("error"),
        ))

    # ── 7. Assemble result ──────────────────────────────────────────────────
    _progress(cb, 95, "Assembling audit report...")

    all_flags = [f for ca in clause_analyses for f in (ca.risk_flags or [])]
    high_flags = [f for f in all_flags if f.get("severity") == "high"]
    medium_flags = [f for f in all_flags if f.get("severity") == "medium"]
    risk_score = min(100, len(high_flags) * 20 + len(medium_flags) * 8 + len(all_flags) * 2)

    result = AuditResult(
        tenant_name=tenant_name or "Unknown",
        jurisdiction=jurisdiction,
        filename=parsed.metadata["filename"],
        total_clauses=len(clause_analyses),
        risk_score=risk_score,
        clause_analyses=clause_analyses,
        all_risk_flags=all_flags,
    )

    logger.info(
        f"Audit complete — {len(clause_analyses)} clauses, "
        f"{len(all_flags)} flags ({len(high_flags)} high), "
        f"risk score: {risk_score}/100"
    )
    return result
