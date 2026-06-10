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
   