"""
outgoings_parser.py
-------------------
Parses outgoings schedules and invoice PDFs into structured line items.

Uses Haiku (cheap, fast) to extract tabular data from the text of an outgoings
or invoice PDF.  Deliberately NOT using the clause chunker — outgoings docs are
financial schedules, not clause-structured legal text.

Returns a ParsedOutgoings dataclass containing:
  - line_items:   [{category, description, amount_cents, gst_cents, is_capital_flag}]
  - period:       {start, end}  — financial year or billing period
  - total_cents:  sum as stated in the document (for cross-check)
  - warnings:     list of strings surfaced to UI when data quality is low
  - doc_type:     "outgoings_schedule" | "invoice" | "gl_detail" | "unknown"
  - raw_text_excerpt: first 500 chars of parsed text (auditor sanity check)

AQ-NEW-25: General Ledger (GL) support
---------------------------------------
parse_gl_pdf() handles GL transaction detail exports (CSV or PDF format).
Unlike reconciliation summaries (which only show category totals), GL documents
contain individual transaction rows with dates, vendor names, and descriptions.

The GL parser:
  1. Extracts transaction rows via Haiku
  2. Passes each row through services/gl_capex_classifier.py for CapEx detection
  3. Returns ParsedOutgoings with doc_type="gl_detail" and is_capital_flag set on
     every line item that is classified as capital expenditure

This is the forensic auditor's weapon against disguised CapEx — landlords who book
a cooling tower compressor replacement under "repairs & maintenance" are exposed
when the GL detail is ingested and classified.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from ingestion.pdf_parser import parse_pdf
from llm.router import get_client, HAIKU_MODEL


@dataclass
class OutgoingsLineItem:
    category: str           # e.g. "rates", "insurance", "management_fee", "capex", "land_tax"
    description: str        # verbatim from the doc
    amount_cents: int       # AUD cents
    gst_cents: int = 0
    is_capital_flag: bool = False   # Haiku pre-flags suspected capital items
    is_land_tax_flag: bool = False


@dataclass
class ParsedOutgoings:
    line_items: list[OutgoingsLineItem] = field(default_factory=list)
    period_start: Optional[str] = None     # ISO date or free-text
    period_end: Optional[str] = None
    total_cents: Optional[int] = None      # as stated in doc
    computed_total_cents: int = 0          # sum of line_items
    warnings: list[str] = field(default_factory=list)
    doc_type: str = "unknown"              # "outgoings_schedule" | "invoice" | "unknown"
    raw_text_excerpt: str = ""


def _extract_text(pdf_path: str) -> tuple[str, list[str]]:
    """Parse PDF and return (full_text, warnings)."""
    warnings = []
    try:
        parsed = parse_pdf(pdf_path)
    except Exception as e:
        return "", [f"PDF parse error: {e}"]

    pages = parsed.pages
    if not pages:
        return "", ["No pages found in document."]

    full_text = "\n".join(p.get("text", "") for p in pages).strip()
    if not full_text:
        warnings.append("Document appears to be a scanned image — OCR not applied to outgoings docs yet. Line items may be incomplete.")
    elif len(full_text) < 200:
        warnings.append("Very little text extracted — document may be partially scanned or corrupted.")

    return full_text, warnings


_SYSTEM_PROMPT = """\
You are a financial data extraction assistant specialising in Australian commercial property.
Your job is to extract structured line items from outgoings schedules and property invoices.
Return ONLY valid JSON — no preamble, no markdown fences.
"""

_EXTRACT_PROMPT_TEMPLATE = """\
Extract all financial line items from this document.

DOCUMENT TEXT:
{text}

Return a JSON object with this exact schema:
{{
  "doc_type": "outgoings_schedule" | "invoice" | "unknown",
  "period_start": "YYYY-MM-DD or free text or null",
  "period_end":   "YYYY-MM-DD or free text or null",
  "stated_total_aud": number or null,
  "line_items": [
    {{
      "category": one of ["council_rates","water_rates","land_tax","building_insurance",
                          "management_fee","cleaning","security","utilities","capex",
                          "marketing_levy","essential_safety","other"],
      "description": "verbatim label from the document",
      "amount_aud": number,
      "gst_aud": number or 0,
      "is_capital_flag": true if this looks like a capital/one-off asset item (e.g. lift replacement, HVAC replacement),
      "is_land_tax_flag": true if this is a land tax charge
    }}
  ],
  "warnings": ["list any data quality issues, missing totals, illegible sections, etc."]
}}

Rules:
- amount_aud is the net amount (ex-GST) in AUD dollars.
- If GST is included in the stated amount and not broken out, set gst_aud=0 and note it in warnings.
- If a line item is ambiguous between capital and opex, set is_capital_flag=true and note in warnings.
- If the document does not look like an outgoings schedule or invoice, set doc_type="unknown".
- If text is too short or unreadable, return an empty line_items array and explain in warnings.
- Do NOT hallucinate amounts. If a value is illegible or missing, omit the line item and add a warning.
"""


def parse_outgoings_pdf(pdf_path: str) -> ParsedOutgoings:
    """
    Main entry point.  Parse an outgoings schedule or invoice PDF into
    structured line items using Haiku.
    """
    result = ParsedOutgoings()

    # 1. Extract text
    full_text, text_warnings = _extract_text(pdf_path)
    result.warnings.extend(text_warnings)
    result.raw_text_excerpt = full_text[:500]

    if not full_text:
        result.warnings.append("No text could be extracted — outgoings analysis skipped.")
        return result

    # 2. Truncate to ~8k chars to stay within Haiku context comfortably
    text_for_llm = full_text[:8000]
    if len(full_text) > 8000:
        result.warnings.append(
            f"Document text truncated to 8,000 chars for extraction (full length: {len(full_text):,} chars). "
            "Later pages may not be captured."
        )

    # 3. Call Haiku
    client = get_client()
    prompt = _EXTRACT_PROMPT_TEMPLATE.format(text=text_for_llm)

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            timeout=45.0,
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if model slips
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        data = json.loads(raw)
    except json.JSONDecodeError as e:
        result.warnings.append(f"Haiku returned non-JSON — line item extraction failed. ({e})")
        logger.error(f"outgoings_parser: JSON decode error: {e}")
        return result
    except Exception as e:
        result.warnings.append(f"Extraction model error: {e}")
        logger.error(f"outgoings_parser: API error: {e}")
        return result

    # 4. Map response to dataclass
    result.doc_type = data.get("doc_type", "unknown")

    raw_start = data.get("period_start")
    raw_end   = data.get("period_end")
    result.period_start = str(raw_start) if raw_start else None
    result.period_end   = str(raw_end)   if raw_end   else None

    stated_total = data.get("stated_total_aud")
    if stated_total is not None:
        try:
            result.total_cents = int(float(stated_total) * 100)
        except (ValueError, TypeError):
            result.warnings.append("Could not parse stated total amount.")

    llm_warnings = data.get("warnings", [])
    if isinstance(llm_warnings, list):
        result.warnings.extend(llm_warnings)

    running_total = 0
    for item in data.get("line_items", []):
        try:
            amount_cents = int(float(item.get("amount_aud", 0)) * 100)
            gst_cents    = int(float(item.get("gst_aud", 0)) * 100)
            li = OutgoingsLineItem(
                category=item.get("category", "other"),
                description=item.get("description", ""),
                amount_cents=amount_cents,
                gst_cents=gst_cents,
                is_capital_flag=bool(item.get("is_capital_flag", False)),
                is_land_tax_flag=bool(item.get("is_land_tax_flag", False)),
            )
            result.line_items.append(li)
            running_total += amount_cents
        except Exception as parse_err:
            result.warnings.append(f"Skipped malformed line item: {item.get('description','?')} — {parse_err}")

    result.computed_total_cents = running_total

    # 5. Sanity cross-check
    if result.total_cents and result.computed_total_cents:
        diff_aud = abs(result.total_cents - result.computed_total_cents) / 100
        if diff_aud > 1.0:   # >$1 discrepancy
            result.warnings.append(
                f"Computed line-item total (${result.computed_total_cents/100:,.2f}) differs from "
                f"stated total (${result.total_cents/100:,.2f}) by ${diff_aud:,.2f}. "
                "Some line items may have been missed."
            )

    if not result.line_items:
        result.warnings.append("No line items could be extracted. Reconciliation will be skipped.")

    logger.info(
        f"outgoings_parser: doc_type={result.doc_type} items={len(result.line_items)} "
        f"total=${result.computed_total_cents/100:,.2f} warnings={len(result.warnings)}"
    )
    return result


# ── AQ-NEW-25: General Ledger parser ─────────────────────────────────────────

# GL extraction prompt — asks Haiku to pull tabular transaction rows
_GL_SYSTEM = """\
You are a forensic accountant parsing a General Ledger (GL) export for an Australian \
commercial property outgoings audit.
Extract every transaction row from the text below.
Return ONLY valid JSON — no preamble, no markdown fences.
"""

_GL_PROMPT = """\
Extract all transaction rows from this General Ledger export.

For each transaction, return:
  - date: "YYYY-MM-DD" or null
  - description: full transaction description including vendor name if present
  - vendor: vendor/supplier name (extract from description or a separate column) or null
  - amount_aud: positive float (the dollar amount of the expense)
  - gst_aud: GST component as a positive float (0 if not stated)
  - account_code: GL account code string (e.g. "6100", "R&M", "CAPEX") or null

IMPORTANT:
- Include ALL rows, even those with $0 amount (they may indicate journal adjustments).
- Do NOT aggregate rows — return individual transactions.
- If the same description appears multiple times, include each occurrence separately.
- For Excel-style GL exports, every debit row is a separate transaction.
- Return a JSON object: {{"transactions": [...], "period_start": "YYYY-MM-DD"|null,
  "period_end": "YYYY-MM-DD"|null, "warnings": ["..."]}}

GL TEXT:
{text}

JSON only:"""


def parse_gl_pdf(
    pdf_path: str,
    job_id: Optional[str] = None,
) -> "ParsedOutgoings":
    """
    AQ-NEW-25: Parse a General Ledger export (PDF, CSV-as-PDF, or Excel-exported PDF).

    Unlike parse_outgoings_pdf() which handles reconciliation summaries, this function:
    1. Extracts individual transaction rows (not category totals)
    2. Passes each row through gl_capex_classifier to flag CapEx items
    3. Returns ParsedOutgoings with doc_type="gl_detail"

    DEV mode: returns a mock GL with one CapEx item, one OpEx, one unclear.
    LIVE mode: calls Haiku to extract rows, then gl_capex_classifier for CapEx detection.

    Args:
        pdf_path:  Absolute path to the GL PDF/export file.
        job_id:    For log correlation.

    Returns:
        ParsedOutgoings — ready for run_outgoings_reconciliation().
    """
    import os
    from services.gl_capex_classifier import classify_gl_lines, gl_batch_to_line_items

    mock_mode = os.environ.get("MOCK_MODE", "true").lower() == "true"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if mock_mode or not api_key or api_key.startswith("sk-ant-your"):
        logger.info(f"[{job_id}] parse_gl_pdf: MOCK_MODE — returning synthetic GL")
        return _mock_gl_result()

    # 1. Extract text from PDF
    pages = parse_pdf(pdf_path)
    raw_text = "\n".join(p.text for p in pages if p.text)

    if not raw_text.strip():
        result = ParsedOutgoings(doc_type="gl_detail")
        result.warnings.append("GL document returned no extractable text — check PDF quality.")
        result.engine_status = "failed"
        return result

    # GL documents can be very large — use a larger window than reconciliation summaries
    # but still cap to avoid token limits (50k chars covers most GL exports)
    _GL_MAX_CHARS = 50_000
    if len(raw_text) > _GL_MAX_CHARS:
        text_sample = raw_text[:_GL_MAX_CHARS]
        logger.warning(
            f"[{job_id}] parse_gl_pdf: GL document truncated at {_GL_MAX_CHARS} chars. "
            "Consider splitting the GL by financial year before upload."
        )
    else:
        text_sample = raw_text

    # 2. Extract transaction rows via Haiku
    client = get_client()
    prompt = _GL_PROMPT.format(text=text_sample)
    result = ParsedOutgoings(
        doc_type="gl_detail",
        raw_text_excerpt=raw_text[:500],
    )

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=4096,
            system=_GL_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            timeout=60.0,
        )
        raw_json = response.content[0].text.strip()
        raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json)
        raw_json = re.sub(r"\s*```$", "", raw_json)
        data = json.loads(raw_json)
    except Exception as e:
        result.warnings.append(f"GL extraction failed: {e}. No transactions parsed.")
        result.engine_status = "failed"
        logger.error(f"[{job_id}] parse_gl_pdf: extraction error: {e}")
        return result

    result.period_start = data.get("period_start")
    result.period_end = data.get("period_end")
    if isinstance(data.get("warnings"), list):
        result.warnings.extend(data["warnings"])

    transactions = data.get("transactions", [])
    if not transactions:
        result.warnings.append("No transactions extracted from GL. Check document format.")
        return result

    # 3. Build raw line items for classifier
    raw_items = []
    for txn in transactions:
        try:
            amount_cents = int(float(txn.get("amount_aud", 0)) * 100)
            raw_items.append({
                "description": txn.get("description", ""),
                "vendor": txn.get("vendor", ""),
                "amount_cents": amount_cents,
                "account_code": txn.get("account_code", ""),
            })
        except Exception:
            pass

    # 4. CapEx classification via gl_capex_classifier
    logger.info(f"[{job_id}] parse_gl_pdf: {len(raw_items)} transactions → CapEx classifier")
    batch = classify_gl_lines(raw_items, job_id=job_id)

    # 5. Convert back to OutgoingsLineItem format
    classified_items = gl_batch_to_line_items(batch)
    running_total = 0
    for item in classified_items:
        li = OutgoingsLineItem(
            category=item["category"],
            description=item["description"],
            amount_cents=item["amount_cents"],
            gst_cents=item["gst_cents"],
            is_capital_flag=item["is_capital_flag"],
            is_land_tax_flag=item["is_land_tax_flag"],
        )
        result.line_items.append(li)
        running_total += li.amount_cents

    result.computed_total_cents = running_total
    result.warnings.extend(batch.warnings)

    # 6. Add CapEx summary as a warning for the auditor portal
    if batch.capex_lines:
        result.warnings.append(
            f"GL ANALYSIS — CAPITAL EXPENDITURE DETECTED: "
            f"${batch.total_capex_cents/100:,.2f} across {len(batch.capex_lines)} items "
            f"has been flagged as capital expenditure. These items are likely non-recoverable "
            f"as outgoings. See reconciliation findings for details."
        )
    if batch.unclear_lines:
        result.warnings.append(
            f"GL ANALYSIS — {len(batch.unclear_lines)} items could not be classified "
            f"(${batch.total_unclear_cents/100:,.2f} total). Manual review required — "
            f"request original vendor invoices for these transactions."
        )

    logger.info(
        f"[{job_id}] parse_gl_pdf: extracted {len(result.line_items)} items, "
        f"capex=${batch.total_capex_cents/100:,.2f}, "
        f"unclear=${batch.total_unclear_cents/100:,.2f}"
    )
    return result


def _mock_gl_result() -> "ParsedOutgoings":
    """MOCK_MODE GL result — synthetic but realistic."""
    result = ParsedOutgoings(
        doc_type="gl_detail",
        period_start="2024-07-01",
        period_end="2025-06-30",
        raw_text_excerpt="[MOCK] General Ledger — FY2024-25 — Repairs & Maintenance Account",
    )

    mock_items = [
        OutgoingsLineItem(
            category="capex",
            description="Cooling tower compressor replacement — Unit 3B (Baseline Engineering)",
            amount_cents=2_250_000,
            gst_cents=225_000,
            is_capital_flag=True,
            is_land_tax_flag=False,
        ),
        OutgoingsLineItem(
            category="other",
            description="HVAC filter replacement and service — monthly contract (Arctic Air)",
            amount_cents=185_000,
            gst_cents=18_500,
            is_capital_flag=False,
            is_land_tax_flag=False,
        ),
        OutgoingsLineItem(
            category="other",
            description="[UNCLEAR — manual review required] Electrical work — Main switchboard",
            amount_cents=780_000,
            gst_cents=78_000,
            is_capital_flag=False,
            is_land_tax_flag=False,
        ),
        OutgoingsLineItem(
            category="capex",
            description="Lift motor replacement — Level B1 to Level 12 (ThyssenKrupp)",
            amount_cents=8_500_000,
            gst_cents=850_000,
            is_capital_flag=True,
            is_land_tax_flag=False,
        ),
    ]

    result.line_items = mock_items
    result.computed_total_cents = sum(i.amount_cents for i in mock_items)
    result.total_cents = result.computed_total_cents
    result.warnings = [
        "MOCK_MODE: GL analysis is synthetic. Set MOCK_MODE=false for real analysis.",
        "GL ANALYSIS — CAPITAL EXPENDITURE DETECTED: $107,500.00 across 2 items has been "
        "flagged as capital expenditure. These items are likely non-recoverable as outgoings.",
        "GL ANALYSIS — 1 item could not be classified ($7,800.00 total). Manual review required.",
    ]
    return result
