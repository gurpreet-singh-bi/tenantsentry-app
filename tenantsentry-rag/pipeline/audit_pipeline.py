"""
audit_pipeline.py
-----------------
End-to-end orchestration: PDF → chunks → retrieval → LLM → structured audit.

Usage:
    from pipeline.audit_pipeline import run_audit
    result = run_audit("path/to/lease.pdf", jurisdiction="NSW")
"""

from loguru import logger
from ingestion.pdf_parser import parse_pdf
from ingestion.chunker import chunk_document
from embedding.embedder import embed_texts, embed_query
from vector_store.supabase_store import upsert_chunks, similarity_search
from llm.router import analyse_clause
from output.json_formatter import AuditResult, ClauseAnalysis


def run_audit(pdf_path: str, jurisdiction: str, tenant_name: str = None) -> AuditResult:
    """
    Full audit pipeline for a commercial lease PDF.

    Args:
        pdf_path: Absolute path to the lease PDF
        jurisdiction: State code — "NSW", "VIC", "QLD", "SA", "WA", "TAS", "ACT", "NT"
        tenant_name: Optional — used in the output report

    Returns:
        AuditResult with all clause analyses and risk flags
    """
    logger.info(f"Starting audit: {pdf_path} | Jurisdiction: {jurisdiction}")

    # ── 1. Parse PDF ────────────────────────────────────────────────────────
    parsed = parse_pdf(pdf_path)

    if parsed.is_scanned:
        logger.warning("Scanned PDF detected — OCR required (see ingestion/ocr_parser.py)")
        # TODO: route to ocr_parser.py, then re-parse
        raise NotImplementedError("OCR pipeline not yet implemented")

    # ── 2. Chunk into clauses ───────────────────────────────────────────────
    doc_metadata = {
        "jurisdiction": jurisdiction,
        "tenant_name": tenant_name or "Unknown",
        "filename": parsed.metadata["filename"],
    }
    chunks = chunk_document(parsed.pages, doc_metadata)

    # ── 3. Embed lease chunks ───────────────────────────────────────────────
    logger.info(f"Embedding {len(chunks)} lease chunks")
    chunk_texts = [c.content for c in chunks]
    embeddings = embed_texts(chunk_texts, input_type="document")

    # ── 4. Store in Supabase ────────────────────────────────────────────────
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

    # ── 5. Analyse each clause ──────────────────────────────────────────────
    clause_analyses = []

    for chunk in chunks:
        clause_text = chunk.content

        # Retrieve relevant legislation + rules for this clause
        query_vec = embed_query(clause_text)

        legislation_chunks = similarity_search(
            query_embedding=query_vec,
            top_k=4,
            chunk_type="legislation",
            jurisdiction=jurisdiction,
        )
        rule_chunks = similarity_search(
            query_embedding=query_vec,
            top_k=3,
            chunk_type="rule",
        )

        legislation_context = "\n\n".join(c["content"] for c in legislation_chunks)
        rules_context = "\n\n".join(c["content"] for c in rule_chunks)

        # Call LLM (routed to Opus or Sonnet based on clause complexity)
        analysis = analyse_clause(
            clause_text=clause_text,
            legislation_context=legislation_context,
            rules_context=rules_context,
            jurisdiction=jurisdiction,
        )

        clause_analyses.append(ClauseAnalysis(
            clause_heading=chunk.metadata.get("clause_heading", "Unknown"),
            clause_text=clause_text,
            **analysis,
        ))

    # ── 6. Assemble result ──────────────────────────────────────────────────
    all_flags = [f for ca in clause_analyses for f in ca.risk_flags]
    high_flags = [f for f in all_flags if f.get("severity") == "high"]
    risk_score = min(100, len(high_flags) * 20 + len(all_flags) * 5)

    result = AuditResult(
        tenant_name=tenant_name or "Unknown",
        jurisdiction=jurisdiction,
        filename=parsed.metadata["filename"],
        total_clauses=len(clause_analyses),
        risk_score=risk_score,
        clause_analyses=clause_analyses,
        all_risk_flags=all_flags,
    )

    logger.info(f"Audit complete — {len(all_flags)} flags, risk score: {risk_score}/100")
    return result
