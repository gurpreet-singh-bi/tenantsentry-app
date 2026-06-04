"""
audit_pipeline.py
-----------------
End-to-end orchestration: PDF -> chunks -> LLM analysis -> structured audit.

Local dev mode (default): No embeddings, no Supabase.
  Red flag rules are loaded directly from rules/red_flags.yaml and passed
  into every Claude prompt. Fast, zero external dependencies beyond Anthropic API.

Production mode (USE_VECTOR_STORE=true in .env): Full RAG with VoyageAI + Supabase.

Progress stages:
  5%  -> Parsing PDF
  10-20% -> OCR (scanned PDFs only)
  25% -> Chunking document
  35-45% -> Embedding + storing (production only)
  50-90% -> Analysing clauses (per clause)
  92% -> Extracting critical dates
  95% -> Assembling result
"""

import os
import yaml
from pathlib import Path
from typing import Callable, Optional
from loguru import logger
from ingestion.pdf_parser import parse_pdf
from ingestion.chunker import chunk_document
from llm.router import analyse_clause
from output.json_formatter import AuditResult, ClauseAnalysis, LeaseDate

ProgressCallback = Optional[Callable[[int, str], None]]

USE_VECTOR_STORE = os.environ.get("USE_VECTOR_STORE", "false").lower() == "true"

# Rules cache - keyed by jurisdiction so filtered sets are cached separately
_RULES_CACHE: dict[str, str] = {}
_RAW_RULES: list[dict] = []

# F5: Keywords that indicate a clause involves land tax.
_LAND_TAX_KEYWORDS: frozenset[str] = frozenset([
    "land tax", "rates and taxes", "government charges", "outgoings",
    "rates, taxes", "levies", "statutory charges",
])

# G7: Keywords that indicate a clause involves CPI / rent review calculations.
# When matched, the deterministic ABS CPI snapshot is pre-fetched and injected
# into the Claude prompt so Claude interprets figures, never calculates them.
_CPI_CLAUSE_KEYWORDS: frozenset[str] = frozenset([
    "cpi", "consumer price index", "rent review", "market review",
    "fixed increase", "ratchet", "annual increase", "escalation",
    "percentage increase", "index", "inflation",
])


def _load_raw_rules() -> list[dict]:
    """Load red_flags.yaml once into module-level cache."""
    global _RAW_RULES
    if _RAW_RULES:
        return _RAW_RULES
    rules_path = Path(__file__).parent.parent / "rules" / "red_flags.yaml"
    if not rules_path.exists():
        logger.warning("red_flags.yaml not found - LLM will run without rules context")
        return []
    with open(rules_path) as f:
        data = yaml.safe_load(f)
    _RAW_RULES = data.get("rules", [])
    logger.info(f"Loaded {len(_RAW_RULES)} rules from red_flags.yaml")
    return _RAW_RULES


def _rules_apply_to_jurisdiction(rule: dict, jurisdiction: str) -> bool:
    """
    Return True if this rule should be included for the given jurisdiction.

    A rule matches if:
      - jurisdictions field is absent (legacy rules - treat as ALL)
      - jurisdictions contains "ALL"
      - jurisdictions contains the exact state code
    """
    jurs = rule.get("jurisdictions")
    if not jurs:
        return True  # legacy rule with no jurisdictions field - include everywhere
    return "ALL" in jurs or jurisdiction.upper() in jurs


def _build_rules_context(jurisdiction: str) -> str:
    """
    Build the rules prompt string filtered to the given jurisdiction.
    Result is cached per-jurisdiction so the first call per state pays the cost.
    """
    global _RULES_CACHE
    jur_upper = jurisdiction.upper()
    if jur_upper in _RULES_CACHE:
        return _RULES_CACHE[jur_upper]

    rules = _load_raw_rules()
    applicable = [r for r in rules if _rules_apply_to_jurisdiction(r, jur_upper)]

    if not applicable:
        logger.warning(f"No rules found for jurisdiction {jur_upper}")
        _RULES_CACHE[jur_upper] = ""
        return ""

    lines = [f"KNOWN RISK FLAG RULES FOR {jur_upper} COMMERCIAL LEASES:\n"]
    for rule in applicable:
        lines.append(
            f"Rule {rule['id']} [{rule['severity'].upper()}]: {rule['name']}\n"
            f"  Description: {rule['description'].strip()}\n"
            f"  Trigger keywords: {', '.join(rule.get('trigger_keywords', []))}\n"
            f"  Legislation: {rule.get('legislation_ref') or 'N/A'}\n"
            f"  Action: {rule.get('recommended_action', '').strip()}\n"
        )

    context = "\n".join(lines)
    _RULES_CACHE[jur_upper] = context
    logger.info(f"Built rules context for {jur_upper}: {len(applicable)}/{len(rules)} rules applicable")
    return context


def _is_land_tax_clause(chunk) -> bool:
    """Return True if this chunk likely contains a land tax or outgoings clause."""
    combined = " ".join([
        chunk.content,
        chunk.metadata.get("clause_heading", ""),
        chunk.metadata.get("clause_type", ""),
    ]).lower()
    return any(kw in combined for kw in _LAND_TAX_KEYWORDS)


def _is_cpi_clause(chunk) -> bool:
    """Return True if this chunk likely contains a CPI or rent review clause."""
    combined = " ".join([
        chunk.content,
        chunk.metadata.get("clause_heading", ""),
        chunk.metadata.get("clause_type", ""),
    ]).lower()
    return any(kw in combined for kw in _CPI_CLAUSE_KEYWORDS)


def _progress(callback: ProgressCallback, pct: int, stage: str) -> None:
    if callback:
        callback(pct, stage)
    logger.debug(f"[{pct}%] {stage}")


def run_audit(
    pdf_path: str,
    jurisdiction: str,
    tenant_name: str = None,
    job_id: str = None,
    document_hash: str = None,
    progress_callback: ProgressCallback = None,
) -> AuditResult:
    """
    Full audit pipeline for a commercial lease PDF.

    Args:
        pdf_path:          Absolute path to the lease PDF
        jurisdiction:      State code - NSW, VIC, QLD, SA, WA, TAS, ACT, NT
        tenant_name:       Optional - used in the output report
        job_id:            Optional - if provided, lease dates are persisted to Supabase
        document_hash:     SHA-256 hex digest of the raw PDF bytes (G2 dedup).
                           When provided and USE_VECTOR_STORE=true, the pipeline
                           checks whether this document is already in the vector
                           store and skips embedding if so.
        progress_callback: Optional fn(pct: int, stage: str)

    Returns:
        AuditResult with clause analyses, risk flags, and extracted lease dates
    """
    logger.info(f"Starting audit: {pdf_path} | {jurisdiction} | vector_store={USE_VECTOR_STORE}")
    cb = progress_callback
    jur = jurisdiction.upper()

    # 1. Parse PDF
    _progress(cb, 5, "Parsing PDF...")
    parsed = parse_pdf(pdf_path)

    # 2. OCR if scanned
    if parsed.is_scanned:
        _progress(cb, 10, "Scanned PDF detected - running OCR...")
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

    # 3. Chunk into clauses
    _progress(cb, 25, "Identifying lease clauses...")
    doc_metadata = {
        "jurisdiction": jur,
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

    # 4. Optional: embed + store (production only)
    if USE_VECTOR_STORE:
        from vector_store.supabase_store import document_exists, upsert_chunks

        # G2: Skip embedding if this exact PDF was already indexed
        _already_indexed = document_hash and document_exists(document_hash)

        if _already_indexed:
            logger.info(
                f"G2 dedup: document {(document_hash or '')[:12]}… already in vector store "
                f"— skipping {len(chunks)} chunk embeddings"
            )
            _progress(cb, 45, "Document already indexed — skipping embedding.")
        else:
            _progress(cb, 35, f"Embedding {len(chunks)} clauses...")
            from embedding.embedder import embed_texts
            chunk_texts = [c.content for c in chunks]
            embeddings = embed_texts(chunk_texts, input_type="document")
            rows = [
                {
                    "content":      c.content,
                    "embedding":    embeddings[i],
                    "metadata":     c.metadata,
                    # G2: store hash as document_id so future uploads can dedup
                    "document_id":  document_hash or f"job-{job_id}",
                    "chunk_type":   "lease",
                    "jurisdiction": jur,
                }
                for i, c in enumerate(chunks)
            ]
            upsert_chunks(rows)
            _progress(cb, 45, "Stored in knowledge base.")

    # 5. Build jurisdiction-filtered rules context
    rules_context = _build_rules_context(jur)

    # F5: Pre-fetch land tax rule once for this jurisdiction.
    # Injected into every land tax / outgoings clause so Claude applies the
    # correct statutory position without guessing which state's law applies.
    _land_tax_context = ""
    try:
        from services.land_tax_validator import get_land_tax_rule, format_for_prompt as _lt_fmt
        _land_tax_rule = get_land_tax_rule(jur)
        _land_tax_context = _lt_fmt(_land_tax_rule)
        logger.info(f"[F5] Land tax rule loaded for {jur}: {_land_tax_rule['prohibition_level']}")
    except Exception as e:
        logger.warning(f"[F5] Land tax validator load failed (non-fatal): {e}")

    # G7/G9: Pre-fetch CPI snapshot once for this jurisdiction.
    # If a clause later provides cpi_index_series, we re-fetch for that series.
    # Used for any CPI/rent-review clause so Claude interprets, never calculates.
    _cpi_snapshot: dict | None = None
    _cpi_series_used: str | None = None   # G9: track which series is in use

    def _get_cpi_context(chunk, series_override: str | None = None) -> str:
        nonlocal _cpi_snapshot, _cpi_series_used
        if not _is_cpi_clause(chunk):
            return ""
        # G9: re-fetch if a specific series is requested and differs from current
        need_fetch = (
            _cpi_snapshot is None
            or (series_override and series_override != _cpi_series_used)
        )
        if need_fetch:
            try:
                from services.cpi_calculator import get_cpi_snapshot, format_for_prompt
                _cpi_snapshot = get_cpi_snapshot(jur, series_override=series_override)
                _cpi_series_used = series_override
                logger.info(
                    f"[G7] CPI snapshot fetched for {jur} "
                    f"series={series_override or 'jurisdiction default'}: "
                    f"ok={_cpi_snapshot.get('ok')}"
                )
            except Exception as e:
                logger.warning(f"[G7] CPI calculator import/fetch failed (non-fatal): {e}")
                _cpi_snapshot = {"ok": False, "error": str(e)}
        if not _cpi_snapshot.get("ok"):
            return ""
        from services.cpi_calculator import format_for_prompt
        return format_for_prompt(_cpi_snapshot)

    # 6. Analyse each clause with LLM
    clause_analyses = []
    n = len(chunks)

    for idx, chunk in enumerate(chunks):
        pct = 50 + int((idx / max(n, 1)) * 40)
        _progress(cb, pct, f"Analysing clause {idx + 1} of {n}...")

        clause_text = chunk.content

        # In production: retrieve jurisdiction-filtered legislation via RAG
        # In local mode: pass empty string (LLM uses training knowledge + rules)
        if USE_VECTOR_STORE:
            from embedding.embedder import embed_query
            from vector_store.supabase_store import similarity_search
            query_vec = embed_query(clause_text)
            # Retrieve legislation for this jurisdiction AND all-jurisdiction best-practice chunks
            leg_chunks = similarity_search(
                query_embedding=query_vec, top_k=3,
                chunk_type="legislation", jurisdiction=jur,
            )
            all_chunks = similarity_search(
                query_embedding=query_vec, top_k=2,
                chunk_type="legislation", jurisdiction=None,
            )
            legislation_context = "\n\n".join(
                c["content"] for c in leg_chunks + all_chunks
            )
        else:
            legislation_context = ""

        # G7: Inject pre-computed CPI data for rent review / CPI clauses.
        # G9: Use series from a prior clause analysis if available.
        cpi_context = _get_cpi_context(chunk, series_override=_cpi_series_used)

        # F5: Inject jurisdiction-specific land tax statutory position.
        land_tax_context = _land_tax_context if _is_land_tax_clause(chunk) else ""

        analysis = analyse_clause(
            clause_text=clause_text,
            legislation_context=legislation_context,
            rules_context=rules_context,
            jurisdiction=jur,
            cpi_context=cpi_context,
            land_tax_context=land_tax_context,
        )

        # G9: If Claude extracted a cpi_index_series, update for subsequent clauses.
        extracted_series = analysis.get("cpi_index_series")
        if extracted_series and extracted_series != _cpi_series_used:
            logger.info(f"[G9] cpi_index_series extracted: '{extracted_series}' — will use for remaining CPI clauses")
            _cpi_series_used = extracted_series

        clause_analyses.append(ClauseAnalysis(
            clause_heading=chunk.metadata.get("clause_heading", f"Clause {idx + 1}"),
            clause_text=clause_text,
            clause_type=analysis.get("clause_type"),
            key_terms=analysis.get("key_terms", []),
            risk_flags=analysis.get("risk_flags", []),
            plain_english_summary=analysis.get("plain_english_summary"),
            recommended_action=analysis.get("recommended_action"),
            cpi_index_series=extracted_series,
            error=analysis.get("error"),
        ))

    # 7. Extract critical dates
    _progress(cb, 92, "Extracting critical dates and deadlines...")
    lease_dates: list[LeaseDate] = []
    try:
        from services.date_extractor import extract_dates
        full_text = "\n\n".join(p.get("text", "") for p in parsed.pages)
        raw_dates = extract_dates(
            lease_text=full_text,
            jurisdiction=jur,
            job_id=job_id,
            persist=bool(job_id),
        )
        lease_dates = [LeaseDate(**d) for d in raw_dates]
        logger.info(f"Extracted {len(lease_dates)} critical dates")
    except Exception as e:
        logger.error(f"Date extraction failed (non-fatal): {e}")

    # 8. Assemble result
    _progress(cb, 95, "Assembling audit report...")

    all_flags = [f for ca in clause_analyses for f in (ca.risk_flags or [])]
    high_flags = [f for f in all_flags if f.get("severity") == "high"]
    medium_flags = [f for f in all_flags if f.get("severity") == "medium"]
    risk_score = min(100, len(high_flags) * 20 + len(medium_flags) * 8 + len(all_flags) * 2)

    # G9: Aggregate extracted_rules from clause-level analyses.
    # cpi_index_series: use the first non-null value found across all clauses.
    _series = next(
        (ca.cpi_index_series for ca in clause_analyses if ca.cpi_index_series),
        None,
    )
    extracted_rules: dict = {}
    if _series:
        extracted_rules["cpi_index_series"] = _series
    if _cpi_series_used and "cpi_index_series" not in extracted_rules:
        extracted_rules["cpi_index_series"] = _cpi_series_used

    result = AuditResult(
        tenant_name=tenant_name or "Unknown",
        jurisdiction=jur,
        filename=parsed.metadata["filename"],
        total_clauses=len(clause_analyses),
        risk_score=risk_score,
        clause_analyses=clause_analyses,
        all_risk_flags=all_flags,
        lease_dates=lease_dates,
        extracted_rules=extracted_rules,
    )

    logger.info(
        f"Audit complete - {len(clause_analyses)} clauses, "
        f"{len(all_flags)} flags ({len(high_flags)} high), "
        f"risk score: {risk_score}/100, "
        f"{len(lease_dates)} dates extracted"
    )
    return result
