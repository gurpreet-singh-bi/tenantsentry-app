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
import time
import yaml
from pathlib import Path
from typing import Callable, Optional
from loguru import logger
from ingestion.pdf_parser import parse_pdf
from ingestion.chunker import chunk_document
from llm.router import analyse_clause, triage_clauses
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
    _t0 = time.perf_counter()
    _timings: dict = {}

    def _ms(start: float) -> int:
        return int((time.perf_counter() - start) * 1000)

    # 1. Parse PDF
    _progress(cb, 5, "Parsing PDF...")
    parsed = parse_pdf(pdf_path)

    # 2. OCR if scanned
    _t_ocr = time.perf_counter()
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
    _timings["ocr_ms"] = _ms(_t_ocr)

    # 3. Chunk into clauses
    _t_chunk = time.perf_counter()
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
    _timings["chunking_ms"] = _ms(_t_chunk)

    # 4. Optional: embed + store (production only)
    _t_embed = time.perf_counter()
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
    _timings["embedding_ms"] = _ms(_t_embed)

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

    # 6. Two-pass clause analysis
    #    Pass 1: Haiku triage — batch 25 clauses per prompt to identify the ~20-40
    #            that carry real risk and need full Sonnet/Opus analysis.
    #    Pass 2: Parallel Sonnet/Opus — ThreadPoolExecutor(max_workers=12) over
    #            flagged clauses only; unflagged get a lightweight stub.
    _t_analysis = time.perf_counter()
    n = len(chunks)
    TRIAGE_BATCH_SIZE = 25
    TRIAGE_WORKERS    = 5    # max parallel Sonnet/Opus calls; 12 hammers rate limits → effectively serial

    # ── Pass 1: Haiku triage ─────────────────────────────────────────────────
    _progress(cb, 48, f"Triaging {n} clauses...")
    _t_triage = time.perf_counter()

    flagged: set[int] = set()
    for batch_start in range(0, n, TRIAGE_BATCH_SIZE):
        batch = chunks[batch_start: batch_start + TRIAGE_BATCH_SIZE]
        flagged.update(triage_clauses(batch, batch_start, jur))

    _timings["triage_ms"] = int((time.perf_counter() - _t_triage) * 1000)
    logger.info(
        f"Triage: {len(flagged)}/{n} clauses flagged for deep analysis "
        f"({_timings['triage_ms']}ms)"
    )

    # ── Pass 2: Parallel deep analysis ───────────────────────────────────────
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    clause_analyses: list = [None] * n
    _completed_count = 0
    _lock = threading.Lock()

    def _analyse_one(idx: int, chunk) -> tuple:
        _t_clause = time.perf_counter()
        clause_heading = chunk.metadata.get("clause_heading", f"Clause {idx + 1}")
        logger.info(f"[clause:{idx}] START '{clause_heading}'")
        clause_text = chunk.content
        if USE_VECTOR_STORE:
            from embedding.embedder import embed_query
            from vector_store.supabase_store import similarity_search
            query_vec = embed_query(clause_text)
            leg_chunks = similarity_search(
                query_embedding=query_vec, top_k=3,
                chunk_type="legislation", jurisdiction=jur,
            )
            all_leg = similarity_search(
                query_embedding=query_vec, top_k=2,
                chunk_type="legislation", jurisdiction=None,
            )
            legislation_context = "\n\n".join(c["content"] for c in leg_chunks + all_leg)
        else:
            legislation_context = ""

        cpi_ctx = _get_cpi_context(chunk, series_override=_cpi_series_used)
        lt_ctx  = _land_tax_context if _is_land_tax_clause(chunk) else ""

        analysis = analyse_clause(
            clause_text=clause_text,
            legislation_context=legislation_context,
            rules_context=rules_context,
            jurisdiction=jur,
            cpi_context=cpi_ctx,
            land_tax_context=lt_ctx,
        )
        _clause_ms = int((time.perf_counter() - _t_clause) * 1000)
        n_flags = len(analysis.get("risk_flags") or [])
        logger.info(
            f"[clause:{idx}] DONE '{clause_heading}' | "
            f"model={analysis.get('_model','?')} flags={n_flags} {_clause_ms}ms"
        )
        return idx, ClauseAnalysis(
            clause_heading=clause_heading,
            clause_text=clause_text,
            clause_type=analysis.get("clause_type"),
            key_terms=analysis.get("key_terms", []),
            risk_flags=analysis.get("risk_flags", []),
            plain_english_summary=analysis.get("plain_english_summary"),
            recommended_action=analysis.get("recommended_action"),
            cpi_index_series=analysis.get("cpi_index_series"),
            error=analysis.get("error"),
        )

    # Stubs for unflagged clauses (no LLM cost)
    for idx, chunk in enumerate(chunks):
        if idx not in flagged:
            clause_analyses[idx] = ClauseAnalysis(
                clause_heading=chunk.metadata.get("clause_heading", f"Clause {idx + 1}"),
                clause_text=chunk.content,
                clause_type="other",
                plain_english_summary="Standard clause — screened by triage, no material risk identified.",
            )

    _progress(cb, 50, f"Analysing {len(flagged)} flagged clauses in parallel...")

    # Guard: discard any out-of-range indices Haiku may hallucinate.
    # chunks[idx] in the dict comprehension raises IndexError if idx >= len(chunks),
    # which propagates out of the comprehension and crashes the entire audit pipeline.
    flagged = {idx for idx in flagged if 0 <= idx < n}

    with ThreadPoolExecutor(max_workers=TRIAGE_WORKERS, thread_name_prefix="ts-clause") as pool:
        future_to_idx = {pool.submit(_analyse_one, idx, chunks[idx]): idx for idx in sorted(flagged)}
        for future in as_completed(future_to_idx):
            try:
                idx, ca = future.result()
            except Exception as e:
                idx = future_to_idx[future]
                ca = ClauseAnalysis(
                    clause_heading=chunks[idx].metadata.get("clause_heading", f"Clause {idx + 1}"),
                    clause_text=chunks[idx].content,
                    error=str(e),
                )
            clause_analyses[idx] = ca
            if ca.cpi_index_series and ca.cpi_index_series != _cpi_series_used:
                _cpi_series_used = ca.cpi_index_series
            with _lock:
                _completed_count += 1
                done = _completed_count
            pct = 50 + int((done / max(len(flagged), 1)) * 40)
            _progress(cb, pct, f"Analysed {done} of {len(flagged)} flagged clauses...")

    _timings["analysis_ms"] = _ms(_t_analysis)

    # 7. Extract critical dates
    _t_dates = time.perf_counter()
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

    _timings["dates_ms"] = _ms(_t_dates)

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

    _timings["total_ms"] = _ms(_t0)

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
        stage_timings=_timings,
    )

    logger.info(
        f"Audit complete — {len(clause_analyses)} clauses, "
        f"{len(all_flags)} flags ({len(high_flags)} high), "
        f"risk={risk_score}/100, dates={len(lease_dates)} | "
        f"triage={_timings.get('triage_ms')}ms "
        f"analysis={_timings.get('analysis_ms')}ms "
        f"total={_timings.get('total_ms')}ms"
    )
    return result
