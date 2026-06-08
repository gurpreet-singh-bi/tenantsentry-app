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
import re
import time
import yaml
from pathlib import Path
from typing import Callable, Optional
from loguru import logger
from ingestion.pdf_parser import parse_pdf
from ingestion.chunker import chunk_document
from llm.router import analyse_clause, triage_clauses
from output.json_formatter import AuditResult, ClauseAnalysis, LeaseDate
from utils.cost_tracker import CostAccumulator

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


# ── AQ2: Schedule cross-reference index ──────────────────────────────────────

# Matches "Schedule 1, Item 6" / "Item 14" / "Schedule 1 Item 6" / "Item 6 of Schedule 1"
_SCHEDULE_REF_RE = re.compile(
    r"""
    (?:Schedule\s*(\d+)[,\s]+)?   # optional "Schedule N" prefix
    Item\s*(\d+)                   # "Item N"
    |
    Item\s*(\d+)\s+of\s+Schedule\s*(\d+)  # "Item N of Schedule M"
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Matches item lines inside a schedule chunk, e.g. "Item 6" or "6." at start of line
_ITEM_LINE_RE = re.compile(r"^\s*(?:Item\s*)?(\d+)[.\s]", re.IGNORECASE | re.MULTILINE)


def _build_schedule_index(chunks) -> dict[tuple[int, int], str]:
    """
    AQ2: Parse schedule chunks and build a lookup from (schedule_num, item_num)
    to the item's text content.

    For each chunk whose heading contains "SCHEDULE N", we split its body on
    item boundaries and index each item. Items without an explicit schedule
    number are stored under schedule_num=1 as a fallback.

    Returns:
        dict keyed by (schedule_num, item_num) → item text
    """
    index: dict[tuple[int, int], str] = {}

    for chunk in chunks:
        heading = chunk.metadata.get("clause_heading", "")
        # Only process chunks that are schedule pages
        sched_match = re.search(r"SCHEDULE\s*(\d+)", heading, re.IGNORECASE)
        if not sched_match:
            continue
        sched_num = int(sched_match.group(1))

        body = chunk.content
        # Find all item positions in the body
        item_positions = list(_ITEM_LINE_RE.finditer(body))
        if not item_positions:
            continue

        for i, m in enumerate(item_positions):
            item_num = int(m.group(1))
            start = m.start()
            end = item_positions[i + 1].start() if i + 1 < len(item_positions) else len(body)
            item_text = body[start:end].strip()
            if item_text:
                index[(sched_num, item_num)] = item_text
                logger.debug(f"[AQ2] Indexed Schedule {sched_num} Item {item_num}: {item_text[:60]!r}")

    logger.info(f"[AQ2] Schedule index built: {len(index)} items across {len({k[0] for k in index})} schedule(s)")
    return index


def _get_schedule_context(clause_text: str, schedule_index: dict[tuple[int, int], str]) -> str:
    """
    AQ2: Detect Schedule/Item references in a clause and return the relevant
    schedule item texts as an injected context block.

    Returns empty string if no references are found or index is empty.
    """
    if not schedule_index:
        return ""

    found: list[str] = []
    seen: set[tuple[int, int]] = set()

    for m in _SCHEDULE_REF_RE.finditer(clause_text):
        # Pattern branch 1: "Schedule N, Item M" or plain "Item M"
        if m.group(2) is not None:
            sched_num = int(m.group(1)) if m.group(1) else 1
            item_num  = int(m.group(2))
        # Pattern branch 2: "Item M of Schedule N"
        elif m.group(3) is not None:
            item_num  = int(m.group(3))
            sched_num = int(m.group(4)) if m.group(4) else 1
        else:
            continue

        key = (sched_num, item_num)
        if key in seen:
            continue
        seen.add(key)

        item_text = schedule_index.get(key)
        if item_text:
            found.append(f"Schedule {sched_num}, Item {item_num}:\n{item_text}")
            logger.debug(f"[AQ2] Injecting Schedule {sched_num} Item {item_num} into clause context")
        else:
            # Item referenced but not in index — flag absence so Claude knows to be cautious
            found.append(
                f"Schedule {sched_num}, Item {item_num}: "
                "[Not extracted — check the original document before flagging a risk on this item]"
            )
            logger.debug(f"[AQ2] Schedule {sched_num} Item {item_num} referenced but not in index")

    return "\n\n".join(found)


# ── AQ4: Clause coverage completeness check ──────────────────────────────────

# Matches top-level and sub-clause numbers: "5.1", "26.16", "7", etc.
_CLAUSE_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+[A-Z]", re.MULTILINE)


def _extract_clause_numbers(chunks) -> set[str]:
    """
    AQ4: Extract the set of clause numbers actually extracted by the chunker.
    Each chunk's heading is parsed for a leading clause number.
    """
    numbers: set[str] = set()
    num_re = re.compile(r"^(\d+(?:\.\d+)*)")
    for chunk in chunks:
        heading = chunk.metadata.get("clause_heading", "").strip()
        m = num_re.match(heading)
        if m:
            numbers.add(m.group(1))
    return numbers


def _check_clause_coverage(chunks, full_text: str) -> list[str]:
    """
    AQ4: Compare the clause numbers found in the full document text against
    those actually extracted into chunks. Return a list of warning strings
    for any clause numbers present in the document but missing from chunks.

    Only checks top-level clauses (e.g. "26") and their direct sub-clauses
    (e.g. "26.16") to avoid noise from deep sub-sub-clauses.
    """
    # Numbers from document text
    doc_numbers: set[str] = set()
    for m in _CLAUSE_NUM_RE.finditer(full_text):
        num = m.group(1)
        # Only track top-level (e.g. "26") and one-level-deep (e.g. "26.16")
        parts = num.split(".")
        if len(parts) <= 2:
            doc_numbers.add(num)

    # Numbers from extracted chunks
    extracted = _extract_clause_numbers(chunks)

    missing = doc_numbers - extracted
    if not missing:
        return []

    sorted_missing = sorted(missing, key=lambda x: [int(p) for p in x.split(".")])
    warnings = [
        f"[AQ4] Clause coverage gap: clause {n} appears in the document text but was not "
        f"extracted as a chunk — it may have been missed during chunking. "
        f"Review this clause manually."
        for n in sorted_missing
        if _is_meaningful_gap(n, extracted)
    ]
    if warnings:
        logger.warning(
            f"[AQ4] {len(warnings)} clause coverage gap(s) detected: "
            f"{sorted_missing[:10]}"
        )
    return warnings


def _is_meaningful_gap(clause_num: str, extracted: set[str]) -> bool:
    """
    Filter out spurious gaps: a sub-clause like "26.16" is only a real gap
    if its parent section ("26") exists in this lease, i.e. at least one
    sibling clause ("26.1", "26.2", ...) was successfully extracted.
    Top-level clauses are always considered meaningful.
    """
    parts = clause_num.split(".")
    if len(parts) == 1:
        return True  # top-level always meaningful
    parent = parts[0]
    # Parent section is present if any extracted clause starts with "parent."
    prefix = parent + "."
    return any(e.startswith(prefix) for e in extracted)


def run_audit(
    pdf_path: str,
    jurisdiction: str,
    tenant_name: str = None,
    job_id: str = None,
    document_hash: str = None,
    progress_callback: ProgressCallback = None,
    additional_docs: list[dict] = None,
    max_pages: int = None,
    skip_vector_store: bool = False,
) -> AuditResult:
    """
    Full audit pipeline for a commercial lease PDF, with optional additional docs.

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
        additional_docs:   Optional list of dicts:
                           [{"path": str, "doc_type": str, "filename": str}, ...]
                           Supported doc_types: "outgoings", "invoice", "amendment"
                           Outgoings/invoices run through outgoings_engine after lease analysis.
                           Amendments are noted in warnings (not yet analysed).
        max_pages:         If set, truncate parsed.pages to the first N pages before
                           chunking. Used by the free-check flow (max_pages=5) to limit
                           cost without changing the engine. None = no truncation (full audit).
        skip_vector_store: If True, skip vector store embedding/upsert even when
                           USE_VECTOR_STORE=true. Used for anonymous free checks to avoid
                           polluting the knowledge base with truncated documents.

    Returns:
        AuditResult with clause analyses, risk flags, extracted lease dates,
        and reconciliation_results for any outgoings/invoice docs.
    """
    logger.info(f"Starting audit: {pdf_path} | {jurisdiction} | vector_store={USE_VECTOR_STORE}")
    cb = progress_callback
    jur = jurisdiction.upper()
    _t0 = time.perf_counter()
    _timings: dict = {}
    _costs = CostAccumulator()

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

    # 2b. Free-check truncation: limit to first N pages before chunking.
    # This keeps the real engine intact — only the input is shortened.
    if max_pages is not None and len(parsed.pages) > max_pages:
        logger.info(
            f"[FREE-CHECK] Truncating {len(parsed.pages)} pages → {max_pages} "
            f"(max_pages={max_pages})"
        )
        parsed.pages = parsed.pages[:max_pages]

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

    # AQ2: Build schedule index for cross-reference injection
    _schedule_index = _build_schedule_index(chunks)

    # 4. Optional: embed + store (production only)
    # skip_vector_store=True bypasses this for anonymous free checks — we don't want
    # truncated, unauthenticated documents polluting the knowledge base.
    _t_embed = time.perf_counter()
    if USE_VECTOR_STORE and not skip_vector_store:
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
        batch_indices, batch_usage = triage_clauses(batch, batch_start, jur)
        flagged.update(batch_indices)
        _costs.add_haiku(batch_usage["input_tokens"], batch_usage["output_tokens"])

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
    _sonnet_count = 0
    _opus_count = 0
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

        cpi_ctx      = _get_cpi_context(chunk, series_override=_cpi_series_used)
        lt_ctx       = _land_tax_context if _is_land_tax_clause(chunk) else ""
        # AQ2: Inject schedule items referenced by this clause
        sched_ctx    = _get_schedule_context(clause_text, _schedule_index)

        analysis = analyse_clause(
            clause_text=clause_text,
            legislation_context=legislation_context,
            rules_context=rules_context,
            jurisdiction=jur,
            cpi_context=cpi_ctx,
            land_tax_context=lt_ctx,
            schedule_context=sched_ctx,
        )
        _clause_ms = int((time.perf_counter() - _t_clause) * 1000)
        n_flags = len(analysis.get("risk_flags") or [])
        _model_used    = analysis.get("_model", "")
        _input_tokens  = analysis.get("_input_tokens",  0)
        _output_tokens = analysis.get("_output_tokens", 0)
        logger.info(
            f"[clause:{idx}] DONE '{clause_heading}' | "
            f"model={_model_used} flags={n_flags} {_clause_ms}ms "
            f"[in={_input_tokens} out={_output_tokens}]"
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
        ), _model_used, _input_tokens, _output_tokens

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
            _model_used, _in, _out = "", 0, 0
            try:
                idx, ca, _model_used, _in, _out = future.result()
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
                if "opus" in _model_used.lower():
                    _opus_count += 1
                    _costs.add_opus(_in, _out)
                else:
                    _sonnet_count += 1
                    _costs.add_sonnet(_in, _out)
            pct = 50 + int((done / max(len(flagged), 1)) * 40)
            _progress(cb, pct, f"Analysed {done} of {len(flagged)} flagged clauses...")

    _timings["analysis_ms"] = _ms(_t_analysis)

    # 7. Extract critical dates
    _t_dates = time.perf_counter()
    _progress(cb, 92, "Extracting critical dates and deadlines...")
    # Assemble full_text here so it's available for AQ4 coverage check and
    # metadata extraction below regardless of date extraction success.
    full_text = "\n\n".join(p.get("text", "") for p in parsed.pages)

    # AQ4: Clause coverage completeness check — runs after all analysis,
    # before assembling the result. Warnings are surfaced in pipeline_warnings.
    _coverage_warnings = _check_clause_coverage(chunks, full_text)

    lease_dates: list[LeaseDate] = []
    try:
        from services.date_extractor import extract_dates
        raw_dates = extract_dates(  # full_text already set above
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

    # 8a. Extract key lease metadata (landlord, rent, area) from cover pages — Haiku, fast
    _t_meta = time.perf_counter()
    lease_metadata: dict = {}
    try:
        from services.lease_metadata_extractor import extract_lease_metadata
        lease_metadata = extract_lease_metadata(
            lease_text=full_text,
            jurisdiction=jur,
            job_id=job_id,
        )
    except Exception as e:
        logger.error(f"Lease metadata extraction failed (non-fatal): {e}")
    _timings["metadata_ms"] = _ms(_t_meta)

    # 8b. AQ3: Planning rules engine — fires cross-clause statutory requirements.
    # Runs after metadata extraction so term/area data is available.
    _t_planning = time.perf_counter()
    _planning_findings: list[dict] = []
    try:
        from services.planning_rules_engine import (
            evaluate_planning_rules,
            format_planning_finding_as_warning,
        )
        _planning_findings = evaluate_planning_rules(
            jurisdiction=jur,
            lease_metadata=lease_metadata,
            lease_text=full_text,
        )
        for finding in _planning_findings:
            pipeline_warnings.insert(0, format_planning_finding_as_warning(finding))
        if _planning_findings:
            logger.warning(
                f"[AQ3] {len(_planning_findings)} planning rule finding(s) added to pipeline_warnings"
            )
    except Exception as e:
        logger.error(f"[AQ3] Planning rules engine failed (non-fatal): {e}")
    _timings["planning_ms"] = int((time.perf_counter() - _t_planning) * 1000)

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

    # 8b. Optional: outgoings / invoice reconciliation
    # Runs after lease analysis so we have clause_analyses available for context.
    reconciliation_results: list[dict] = []
    pipeline_warnings: list[str] = list(_coverage_warnings)  # AQ4: seed with coverage gaps

    if additional_docs:
        from ingestion.outgoings_parser import parse_outgoings_pdf
        from pipeline.outgoings_engine import run_outgoings_reconciliation, reconciliation_result_to_dict

        clause_analyses_dicts = [ca.model_dump() for ca in clause_analyses]

        outgoings_docs = [d for d in additional_docs if d.get("doc_type") in ("outgoings", "invoice")]
        amendment_docs = [d for d in additional_docs if d.get("doc_type") == "amendment"]
        other_docs     = [d for d in additional_docs if d.get("doc_type") not in ("outgoings", "invoice", "amendment")]

        if amendment_docs:
            for ad in amendment_docs:
                pipeline_warnings.append(
                    f"Lease amendment '{ad['filename']}' was uploaded but amendment analysis is "
                    "not yet implemented — it has been noted but not cross-referenced against the lease. "
                    "Please review it manually."
                )

        if other_docs:
            for od in other_docs:
                pipeline_warnings.append(
                    f"Document '{od['filename']}' (type: {od.get('doc_type','other')}) was uploaded "
                    "but no analysis engine exists for this document type — it was ignored."
                )

        for i, doc in enumerate(outgoings_docs):
            doc_path     = doc["path"]
            doc_filename = doc["filename"]
            doc_type     = doc["doc_type"]
            base_pct     = 93 + int(i / max(len(outgoings_docs), 1) * 4)  # 93-97%

            _progress(cb, base_pct, f"Parsing {doc_type} document: {doc_filename}...")
            logger.info(f"Running outgoings engine on: {doc_filename} ({doc_type})")

            try:
                parsed_out = parse_outgoings_pdf(doc_path)

                def _recon_cb(stage: str):
                    _progress(cb, base_pct + 1, stage)

                recon = run_outgoings_reconciliation(
                    parsed_outgoings=parsed_out,
                    doc_filename=doc_filename,
                    clause_analyses=clause_analyses_dicts,
                    jurisdiction=jur,
                    progress_callback=_recon_cb,
                )
                reconciliation_results.append(reconciliation_result_to_dict(recon))

            except Exception as e:
                logger.error(f"Outgoings engine failed for {doc_filename}: {e}")
                reconciliation_results.append({
                    "doc_filename": doc_filename,
                    "doc_type": doc_type,
                    "engine_status": "failed",
                    "warnings": [f"Processing failed: {e}. This document was not reconciled."],
                    "findings": [],
                    "total_claimed_cents": 0,
                    "total_disputed_cents": 0,
                })

    _timings["total_ms"] = _ms(_t0)

    result = AuditResult(
        tenant_name=tenant_name or "Unknown",
        jurisdiction=jur,
        filename=parsed.metadata["filename"],
        raw_clause_count=len(chunks),
        haiku_triage_count=n,
        sonnet_analysed_count=_sonnet_count,
        opus_escalated_count=_opus_count,
        total_clauses=len(clause_analyses),
        stage_costs=_costs.to_dict(),
        risk_score=risk_score,
        clause_analyses=clause_analyses,
        all_risk_flags=all_flags,
        lease_dates=lease_dates,
        extracted_rules=extracted_rules,
        stage_timings=_timings,
        reconciliation_results=reconciliation_results,
        pipeline_warnings=pipeline_warnings,
        # Key metadata from reference schedule (None when not found)
        landlord_name=lease_metadata.get("landlord_name"),
        base_rent_pa=lease_metadata.get("base_rent_pa"),
        floor_area_sqm=lease_metadata.get("floor_area_sqm"),
        lease_term_years=lease_metadata.get("lease_term_years"),
    )

    logger.info(
        f"Audit complete — {len(clause_analyses)} clauses, "
        f"{len(all_flags)} flags ({len(high_flags)} high), "
        f"risk={risk_score}/100, dates={len(lease_dates)} | "
        f"recon_docs={len(reconciliation_results)} | "
        f"triage={_timings.get('triage_ms')}ms "
        f"analysis={_timings.get('analysis_ms')}ms "
        f"total={_timings.get('total_ms')}ms"
    )
    return result
