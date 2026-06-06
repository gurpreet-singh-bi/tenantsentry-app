"""
outgoings_engine.py
-------------------
Reconciles parsed outgoings line items against the lease's outgoings clauses.

Two-step process:
  1. Extract lease outgoings rules from already-analysed clause_analyses
     (what the lease allows/excludes for recovery).
  2. Sonnet cross-references each outgoings line item against those rules,
     flagging overcharges, statutory prohibitions, and undisclosed items.

Returns a ReconciliationResult that is merged into the AuditResult and surfaced
in both the tenant report and the auditor portal.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from ingestion.outgoings_parser import ParsedOutgoings, OutgoingsLineItem
from llm.router import get_client, SONNET_MODEL, HAIKU_MODEL


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ReconciliationFinding:
    line_item_description: str
    category: str
    amount_cents: int
    finding_type: str       # "overcharge" | "prohibited" | "undisclosed" | "compliant" | "unclear"
    severity: str           # "high" | "medium" | "low" | "info"
    explanation: str        # plain English for tenant
    legislation_ref: Optional[str] = None
    clause_ref: Optional[str] = None
    disputed_amount_cents: int = 0   # 0 if fully permitted, else the disputed portion


@dataclass
class ReconciliationResult:
    """
    Full reconciliation outcome for a single outgoings/invoice document.
    """
    doc_filename: str
    doc_type: str                   # "outgoings_schedule" | "invoice"
    period_start: Optional[str]
    period_end: Optional[str]
    total_claimed_cents: int        # as stated / computed from line items
    total_disputed_cents: int       # sum of disputed_amount_cents across findings
    findings: list[ReconciliationFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # data quality + engine warnings
    lease_clauses_used: list[str] = field(default_factory=list)  # clause headings referenced
    engine_status: str = "complete"  # "complete" | "partial" | "skipped" | "failed"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_outgoings_lease_rules(clause_analyses: list[dict], jurisdiction: str) -> str:
    """
    Pull the outgoings-relevant clauses from already-analysed results and
    return a compact text block for Sonnet's context window.
    """
    OUTGOINGS_TYPES = {
        "outgoings", "land_tax", "make_good",
    }
    OUTGOINGS_KEYWORDS = {
        "outgoing", "recoverable", "land tax", "rates", "levies",
        "capital", "capex", "management fee", "insurance", "cleaning",
        "maintenance", "administration", "marketing", "water",
    }

    relevant = []
    for ca in clause_analyses:
        if not isinstance(ca, dict):
            continue
        ct = (ca.get("clause_type") or "").lower()
        heading = (ca.get("clause_heading") or "").lower()
        summary = (ca.get("plain_english_summary") or "").lower()
        text_snippet = (ca.get("clause_text") or "")[:600]

        is_outgoings = (
            ct in OUTGOINGS_TYPES
            or any(kw in heading for kw in OUTGOINGS_KEYWORDS)
            or any(kw in summary for kw in OUTGOINGS_KEYWORDS)
        )
        if is_outgoings:
            relevant.append(
                f"[{ca.get('clause_heading','?')}] (type={ct})\n"
                f"Summary: {ca.get('plain_english_summary','')}\n"
                f"Text excerpt: {text_snippet}\n"
                f"Risk flags: {json.dumps(ca.get('risk_flags', []))}"
            )

    if not relevant:
        return (
            f"No specific outgoings clauses were identified in the lease for {jurisdiction}. "
            "Apply the default statutory position for commercial leases in this jurisdiction."
        )

    return "\n\n---\n\n".join(relevant)


_RECON_SYSTEM = """\
You are an expert Australian commercial lease auditor specialising in outgoings reconciliation.
You cross-reference outgoings schedules and invoices against lease terms and statutory rules.
Return ONLY valid JSON — no preamble, no markdown fences.
"""

_RECON_PROMPT = """\
JURISDICTION: {jurisdiction}

LEASE OUTGOINGS CLAUSES (from the lease audit):
{lease_rules}

OUTGOINGS DOCUMENT DETAILS:
- Document type: {doc_type}
- Period: {period}
- Stated total: {stated_total}

LINE ITEMS TO ASSESS:
{line_items_text}

For each line item, determine whether it is:
- "compliant": properly recoverable under the lease and statute
- "overcharge": amount exceeds what is permitted
- "prohibited": this category of charge is not recoverable (e.g. CapEx, land tax in some states)
- "undisclosed": the charge is not mentioned/contemplated in the lease
- "unclear": insufficient information to make a determination

Return a JSON object:
{{
  "findings": [
    {{
      "line_item_description": "verbatim from the doc",
      "category": "same as input",
      "amount_cents": integer,
      "finding_type": "compliant" | "overcharge" | "prohibited" | "undisclosed" | "unclear",
      "severity": "high" | "medium" | "low" | "info",
      "explanation": "1-2 sentence plain-English explanation for the tenant",
      "legislation_ref": "e.g. RLA s.41(2)" or null,
      "clause_ref": "clause heading from the lease" or null,
      "disputed_amount_cents": integer (0 if compliant, else the full or partial disputed amount)
    }}
  ],
  "summary_warnings": ["any overall concerns not tied to a specific line item"],
  "lease_clauses_referenced": ["list of clause headings you drew on"]
}}

IMPORTANT RULES:
- Capital expenditure items (useful life > 5 years) are almost always non-recoverable as outgoings.
- Land tax recoverability depends on jurisdiction and lease type — apply the correct rule for {jurisdiction}.
- Management/administration fees above 10% of gross outgoings are flagged per ACCC guidelines.
- If the lease is silent on a charge type, flag as "undisclosed" not "prohibited" unless statute prohibits it.
- Do NOT guess amounts. If a line item amount is 0 or missing, note it in summary_warnings.
- Be conservative: when in doubt, flag as "unclear" rather than "compliant".
"""


# ── Main engine ───────────────────────────────────────────────────────────────

def run_outgoings_reconciliation(
    parsed_outgoings: ParsedOutgoings,
    doc_filename: str,
    clause_analyses: list[dict],
    jurisdiction: str,
    progress_callback=None,
) -> ReconciliationResult:
    """
    Cross-reference a parsed outgoings document against lease clause analyses.

    Args:
        parsed_outgoings:  Output of outgoings_parser.parse_outgoings_pdf()
        doc_filename:      Original filename (for display)
        clause_analyses:   List of ClauseAnalysis dicts from the lease audit
        jurisdiction:      State code
        progress_callback: Optional fn(pct, stage) — called within the range the
                           caller allocates (typically 70-90 range in overall pipeline)

    Returns:
        ReconciliationResult
    """
    def _cb(stage: str):
        if progress_callback:
            try:
                progress_callback(stage)
            except Exception:
                pass

    result = ReconciliationResult(
        doc_filename=doc_filename,
        doc_type=parsed_outgoings.doc_type,
        period_start=parsed_outgoings.period_start,
        period_end=parsed_outgoings.period_end,
        total_claimed_cents=parsed_outgoings.computed_total_cents or (parsed_outgoings.total_cents or 0),
        total_disputed_cents=0,
        warnings=list(parsed_outgoings.warnings),
    )

    if not parsed_outgoings.line_items:
        result.engine_status = "skipped"
        result.warnings.append(
            "No line items were extracted from this document — reconciliation skipped. "
            "Please check the document is a readable outgoings schedule or invoice."
        )
        return result

    # Build lease rules context
    lease_rules = _extract_outgoings_lease_rules(clause_analyses, jurisdiction)

    # Format line items for prompt
    line_items_text = "\n".join(
        f"{i+1}. [{item.category}] {item.description} — "
        f"${item.amount_cents/100:,.2f} (GST: ${item.gst_cents/100:,.2f})"
        f"{' [POSSIBLE CAPITAL]' if item.is_capital_flag else ''}"
        f"{' [LAND TAX]' if item.is_land_tax_flag else ''}"
        for i, item in enumerate(parsed_outgoings.line_items)
    )

    period_str = " to ".join(filter(None, [parsed_outgoings.period_start, parsed_outgoings.period_end])) or "Unknown"
    stated_total_str = (
        f"${parsed_outgoings.total_cents/100:,.2f}"
        if parsed_outgoings.total_cents
        else "Not stated in document"
    )

    prompt = _RECON_PROMPT.format(
        jurisdiction=jurisdiction,
        lease_rules=lease_rules,
        doc_type=parsed_outgoings.doc_type,
        period=period_str,
        stated_total=stated_total_str,
        line_items_text=line_items_text,
    )

    _cb("Reconciling outgoings against lease clauses...")

    client = get_client()
    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=4096,
            system=_RECON_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            timeout=90.0,
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        result.engine_status = "failed"
        result.warnings.append(f"Reconciliation engine returned non-JSON — findings unavailable. ({e})")
        logger.error(f"outgoings_engine: JSON decode error: {e}")
        return result
    except Exception as e:
        result.engine_status = "failed"
        result.warnings.append(f"Reconciliation engine error: {e}")
        logger.error(f"outgoings_engine: API error: {e}")
        return result

    # Map findings
    disputed_total = 0
    for f in data.get("findings", []):
        try:
            finding = ReconciliationFinding(
                line_item_description=f.get("line_item_description", ""),
                category=f.get("category", "other"),
                amount_cents=int(f.get("amount_cents", 0)),
                finding_type=f.get("finding_type", "unclear"),
                severity=f.get("severity", "medium"),
                explanation=f.get("explanation", ""),
                legislation_ref=f.get("legislation_ref"),
                clause_ref=f.get("clause_ref"),
                disputed_amount_cents=int(f.get("disputed_amount_cents", 0)),
            )
            result.findings.append(finding)
            disputed_total += finding.disputed_amount_cents
        except Exception as parse_err:
            result.warnings.append(f"Could not parse finding for '{f.get('line_item_description','?')}': {parse_err}")

    result.total_disputed_cents = disputed_total

    summary_warnings = data.get("summary_warnings", [])
    if isinstance(summary_warnings, list):
        result.warnings.extend(summary_warnings)

    result.lease_clauses_used = data.get("lease_clauses_referenced", [])
    result.engine_status = "complete" if result.findings else "partial"

    logger.info(
        f"outgoings_engine: {doc_filename} — {len(result.findings)} findings, "
        f"disputed=${disputed_total/100:,.2f}, status={result.engine_status}"
    )
    return result


def reconciliation_result_to_dict(r: ReconciliationResult) -> dict:
    """Serialise to plain dict for JSON storage in job findings."""
    return {
        "doc_filename": r.doc_filename,
        "doc_type": r.doc_type,
        "period_start": r.period_start,
        "period_end": r.period_end,
        "total_claimed_cents": r.total_claimed_cents,
        "total_disputed_cents": r.total_disputed_cents,
        "engine_status": r.engine_status,
        "warnings": r.warnings,
        "lease_clauses_used": r.lease_clauses_used,
        "findings": [
            {
                "line_item_description": f.line_item_description,
                "category": f.category,
                "amount_cents": f.amount_cents,
                "finding_type": f.finding_type,
                "severity": f.severity,
                "explanation": f.explanation,
                "legislation_ref": f.legislation_ref,
                "clause_ref": f.clause_ref,
                "disputed_amount_cents": f.disputed_amount_cents,
            }
            for f in r.findings
        ],
    }
