"""
gl_capex_classifier.py
----------------------
AQ-NEW-25: General Ledger CapEx vs OpEx classifier.

A forensic auditor never accepts a landlord's outgoings reconciliation summary at face
value.  They request the General Ledger (GL) transaction detail and vendor invoices to
detect capital expenditure that has been disguised as "repairs & maintenance" or other
operational line items.

This module:
  1. Maintains a deterministic CapEx keyword taxonomy that flags obvious capital items
     without an LLM call (fast, zero-cost).
  2. For ambiguous descriptions, calls Haiku (cheapest model) for a binary CapEx/OpEx
     determination.
  3. Integrates with outgoings_parser.py — GL documents are parsed as a new doc_type
     "gl_detail" and each line item is pre-classified via this module before the
     outgoings_engine reconciliation pass.

DEV / LIVE mode:
  MOCK_MODE=true  → returns deterministic mock classifications; no API calls made.
  MOCK_MODE=false → uses deterministic taxonomy first; escalates ambiguous items to Haiku.
"""

import os
import re
import json
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# ── CapEx keyword taxonomy ────────────────────────────────────────────────────
#
# Classification logic:
#   STRONG_CAPEX  → immediately classify as capex (no LLM needed)
#   STRONG_OPEX   → immediately classify as opex  (no LLM needed)
#   AMBIGUOUS     → neither list matches → escalate to Haiku
#
# Keyword matching is case-insensitive, word-boundary aware.
# A single STRONG_CAPEX match overrides any STRONG_OPEX match.

STRONG_CAPEX_KEYWORDS: list[str] = [
    # Physical plant replacement
    r"\breplace[sd]?\b",
    r"\breplacement\b",
    r"\binstall(ation|ed|ing)?\b",
    r"\bnew\s+(chiller|boiler|pump|lift|elevator|escalator|compressor|tank|panel|unit|system|roof|hvac|ahu|fcu|cooling\s+tower)\b",
    r"\bupgrade[sd]?\b",
    r"\bupgrading\b",
    r"\brefurbish(ment|ed|ing)?\b",
    r"\brenovat(e|ion|ed|ing)\b",
    r"\breconstruct(ion|ed|ing)?\b",
    r"\brebuild(ing)?\b",
    r"\bchiller\s+(replace|overhaul|rewind|rebuild|upgrade|new)\b",
    r"\bchiller\b.*\breplace\b",
    r"\bcompressor\b.*\b(replace|new|install)\b",
    r"\b(replace|new)\b.*\bcompressor\b",
    r"\bcooling\s+tower\b.*\b(replace|new|rebuild|rewind)\b",
    r"\b(replace|rebuild|new)\b.*\bcooling\s+tower\b",
    r"\blift\s+(replace|modernisa|upgrade|new|motor|controller|cab)\b",
    r"\b(replace|new)\b.*\blift\b",
    r"\belevator\s+(replace|modernisa|upgrade|new)\b",
    r"\bescalator\s+(replace|modernisa|upgrade|new)\b",
    r"\broof\s+(replace|replacement|recover|membrane|new)\b",
    r"\b(replace|new)\b.*\broof\b",
    r"\bboiler\s+(replace|new|install)\b",
    r"\bhvac\s+(replace|upgrade|new|install)\b",
    r"\bair\s+handling\s+unit\b",
    r"\bahu\b.*\b(replace|new|install)\b",
    r"\bfcu\b.*\b(replace|new|install)\b",
    r"\bfire\s+(sprinkler|suppression|detection)\s+(system|replace|install|upgrade|new)\b",
    r"\bstructural\s+(repair|work|steel|beam|column|slab|concrete)\b",
    r"\bfoundation\b",
    r"\bseismic\s+(upgrade|retrofit|strengthen)\b",
    r"\basbestos\s+(remove|abate|remediat)\b",
    r"\bdecontaminat\b",
    r"\bremediat\b",
    r"\bcapital\s+(work|expenditure|improvement|project)\b",
    r"\bcapex\b",
    r"\bfit\s*out\b.*\b(new|installa|tenant)\b",
    r"\btenant\s+improvement\b",
    r"\bgenerator\s+(replace|new|install)\b",
    r"\bswitchboard\s+(replace|upgrade|new)\b",
    r"\btransformer\s+(replace|upgrade|new)\b",
    r"\belectric.*\b(replace|upgrade|rewire)\b",
    r"\bcarpet\s+(replace|new)\b",
    r"\bfloor(ing)?\s+(replace|new|install)\b",
    r"\bpaint.*\b(full|external|facade)\b",
    r"\bfacade\s+(replace|restoration|new)\b",
    r"\bcladding\s+(replace|new)\b",
    r"\bwindow\s+(replace|new|install)\b",
    r"\bdoor\s+(replace|new|automatic)\b",
]

STRONG_OPEX_KEYWORDS: list[str] = [
    # Routine maintenance and running costs — clearly NOT capital
    r"\brepair\b",
    r"\bmaintenance\b",
    r"\bservic(e|ing)\b",
    r"\binspection\b",
    r"\bcleaning\b",
    r"\bwaste\s+(removal|collection|disposal)\b",
    r"\bbin\s+(hire|collection)\b",
    r"\bpest\s+control\b",
    r"\blawn\s+(mowing|care|maintenance)\b",
    r"\bgarden(ing)?\b",
    r"\blandscap(e|ing)\b",
    r"\bsecurity\s+(patrol|monitor|guard|system\s+monitor)\b",
    r"\baccess\s+control\s+(monitor|service)\b",
    r"\bfire\s+(test|inspect|service|monitor|compliance)\b",
    r"\blift\s+(service|inspect|maintain|routine|annual)\b",
    r"\bescalator\s+(service|inspect|maintain|routine|annual)\b",
    r"\belev(ator)?\s+(service|inspect|maintain|routine|annual)\b",
    r"\bchiller\s+(service|inspect|maintain|tune|clean|flush)\b",
    r"\bhvac\s+(service|clean|filter|tune|maintain)\b",
    r"\bcouncil\s+rates?\b",
    r"\bwater\s+rates?\b",
    r"\bsewerage\s+rates?\b",
    r"\bwater\s+(usage|charges?)\b",
    r"\binsurance\s+premium\b",
    r"\bbuilding\s+insurance\b",
    r"\bproperty\s+insurance\b",
    r"\bpublic\s+liability\b",
    r"\bmanagement\s+fee\b",
    r"\badmin(istration)?\s+fee\b",
    r"\belectricity\b",
    r"\bgas\b",
    r"\btelecommunication\b",
    r"\binternet\b",
    r"\btelephon\b",
    r"\baccounting\b",
    r"\bauditing?\b",
    r"\binsurance\s+valuat\b",
]


@dataclass
class GLLineClassification:
    """Classification result for a single GL transaction line."""
    description: str
    amount_cents: int
    classification: str     # "capex" | "opex" | "unclear"
    confidence: str         # "high" | "medium" | "low"
    matched_keyword: Optional[str] = None   # taxonomy keyword that fired (if any)
    llm_reasoning: Optional[str] = None     # Haiku explanation (if LLM was used)
    useful_life_estimate: Optional[str] = None  # e.g. "15-20 years" for a chiller


@dataclass
class GLBatchClassification:
    """Batch classification result for an entire GL document."""
    total_lines: int
    capex_lines: list[GLLineClassification] = field(default_factory=list)
    opex_lines: list[GLLineClassification] = field(default_factory=list)
    unclear_lines: list[GLLineClassification] = field(default_factory=list)
    total_capex_cents: int = 0
    total_opex_cents: int = 0
    total_unclear_cents: int = 0
    engine_status: str = "complete"   # "complete" | "partial" | "mock"
    warnings: list[str] = field(default_factory=list)

    @property
    def capex_summary(self) -> str:
        if not self.capex_lines:
            return "No capital expenditure detected in GL detail."
        total = self.total_capex_cents / 100
        items = "\n".join(
            f"  • {c.description}: ${c.amount_cents/100:,.2f}"
            for c in self.capex_lines
        )
        return f"CAPEX DETECTED — ${total:,.2f} total:\n{items}"


# ── Deterministic classification ──────────────────────────────────────────────

def _classify_deterministic(description: str) -> tuple[str, str, Optional[str]]:
    """
    Apply keyword taxonomy.  Returns (classification, confidence, matched_keyword).
    Caller escalates to LLM when classification == "unclear".
    """
    text = description.strip()

    # CapEx check first — a single strong CapEx signal overrides everything
    for pattern in STRONG_CAPEX_KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE):
            return "capex", "high", pattern

    # OpEx check
    for pattern in STRONG_OPEX_KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE):
            return "opex", "high", pattern

    # Neither list matched — ambiguous
    return "unclear", "low", None


# ── LLM-based disambiguation (Haiku) ─────────────────────────────────────────

_HAIKU_SYSTEM = """\
You are a forensic accountant classifying General Ledger transaction descriptions \
for an Australian commercial property outgoings audit.

Determine whether each transaction is:
- "capex": capital expenditure (extends asset useful life >5 years, not immediately consumable)
- "opex": operational expenditure (routine maintenance, running costs, consumables)
- "unclear": insufficient information to determine

Return ONLY a JSON array with one object per transaction — no preamble, no fences.
Schema: [{"id": 0, "description": "...", "classification": "capex|opex|unclear",
           "confidence": "high|medium|low", "reasoning": "1 sentence"}]
"""

_HAIKU_BATCH_SIZE = 30   # Process up to 30 ambiguous items per Haiku call


def _classify_batch_llm(items: list[dict]) -> list[dict]:
    """
    Call Haiku with a batch of ambiguous GL lines.
    `items` is a list of {"id": int, "description": str}.
    Returns list of classification dicts from the model.
    """
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    haiku = os.environ.get("HAIKU_MODEL", "claude-haiku-4-5-20251001")

    items_text = "\n".join(
        f'{i["id"]}. {i["description"]}'
        for i in items
    )
    prompt = (
        "Classify each GL transaction as 'capex', 'opex', or 'unclear'.\n\n"
        f"TRANSACTIONS:\n{items_text}\n\nJSON array only:"
    )

    try:
        response = client.messages.create(
            model=haiku,
            max_tokens=1024,
            system=_HAIKU_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            timeout=30.0,
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        logger.error(f"gl_capex_classifier: Haiku batch call failed: {e}")
        # On failure, mark everything as unclear — safer than assuming opex
        return [
            {"id": item["id"], "description": item["description"],
             "classification": "unclear", "confidence": "low",
             "reasoning": f"LLM call failed: {e}"}
            for item in items
        ]


# ── Public API ────────────────────────────────────────────────────────────────

def classify_gl_lines(
    line_items: list[dict],   # list of {"description": str, "amount_cents": int}
    job_id: Optional[str] = None,
) -> GLBatchClassification:
    """
    Classify a list of GL transaction line items as CapEx, OpEx, or Unclear.

    Args:
        line_items:   Each dict must have "description" (str) and "amount_cents" (int).
                      Optional: "account_code" (str), "vendor" (str).
        job_id:       For log correlation.

    Returns:
        GLBatchClassification with all items sorted into capex/opex/unclear lists.
    """
    mock_mode = os.environ.get("MOCK_MODE", "true").lower() == "true"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if mock_mode or not api_key or api_key.startswith("sk-ant-your"):
        logger.info(f"[{job_id}] gl_capex_classifier: MOCK_MODE — returning synthetic classifications")
        return _mock_classification(line_items)

    return _classify_live(line_items, job_id)


def _classify_live(line_items: list[dict], job_id: Optional[str]) -> GLBatchClassification:
    """Live mode: deterministic taxonomy first, Haiku for ambiguous items."""
    result = GLBatchClassification(total_lines=len(line_items))
    ambiguous_batch: list[dict] = []   # {id, description, amount_cents}

    # ── Pass 1: deterministic ────────────────────────────────────────────────
    for idx, item in enumerate(line_items):
        desc = str(item.get("description", "")).strip()
        amount = int(item.get("amount_cents", 0))
        vendor = str(item.get("vendor", "")).strip()

        # Combine description + vendor for richer matching
        search_text = f"{desc} {vendor}".strip()

        classification, confidence, matched_kw = _classify_deterministic(search_text)

        gl_item = GLLineClassification(
            description=desc,
            amount_cents=amount,
            classification=classification,
            confidence=confidence,
            matched_keyword=matched_kw,
        )

        if classification == "unclear":
            # Queue for LLM pass
            ambiguous_batch.append({"id": idx, "gl_item": gl_item, "description": search_text})
        elif classification == "capex":
            result.capex_lines.append(gl_item)
            result.total_capex_cents += amount
        else:
            result.opex_lines.append(gl_item)
            result.total_opex_cents += amount

    # ── Pass 2: LLM disambiguation ───────────────────────────────────────────
    if ambiguous_batch:
        logger.info(f"[{job_id}] gl_capex_classifier: {len(ambiguous_batch)} ambiguous items → Haiku")
        llm_input = [{"id": b["id"], "description": b["description"]} for b in ambiguous_batch]

        # Process in batches of _HAIKU_BATCH_SIZE
        llm_results: list[dict] = []
        for i in range(0, len(llm_input), _HAIKU_BATCH_SIZE):
            chunk = llm_input[i:i + _HAIKU_BATCH_SIZE]
            llm_results.extend(_classify_batch_llm(chunk))

        # Build lookup by id
        llm_by_id = {r["id"]: r for r in llm_results}

        for batch_item in ambiguous_batch:
            idx = batch_item["id"]
            gl_item = batch_item["gl_item"]
            llm = llm_by_id.get(idx, {})

            gl_item.classification = llm.get("classification", "unclear")
            gl_item.confidence = llm.get("confidence", "low")
            gl_item.llm_reasoning = llm.get("reasoning")

            if gl_item.classification == "capex":
                result.capex_lines.append(gl_item)
                result.total_capex_cents += gl_item.amount_cents
            elif gl_item.classification == "opex":
                result.opex_lines.append(gl_item)
                result.total_opex_cents += gl_item.amount_cents
            else:
                result.unclear_lines.append(gl_item)
                result.total_unclear_cents += gl_item.amount_cents

    logger.info(
        f"[{job_id}] gl_capex_classifier: "
        f"capex=${result.total_capex_cents/100:,.2f} "
        f"opex=${result.total_opex_cents/100:,.2f} "
        f"unclear=${result.total_unclear_cents/100:,.2f}"
    )
    return result


def _mock_classification(line_items: list[dict]) -> GLBatchClassification:
    """
    MOCK_MODE: return synthetic but realistic classifications.
    Includes one obvious CapEx item (cooling tower compressor replacement),
    one obviously OpEx item (routine maintenance), and one unclear item.
    """
    result = GLBatchClassification(
        total_lines=len(line_items),
        engine_status="mock",
    )

    mock_capex = GLLineClassification(
        description="Cooling tower compressor replacement — Unit 3B (Baseline Engineering)",
        amount_cents=2_250_000,  # $22,500
        classification="capex",
        confidence="high",
        matched_keyword=r"\bcompressor\b.*\b(replace|new|install)\b",
        useful_life_estimate="15-20 years — capital item, non-recoverable as outgoings",
    )
    mock_opex = GLLineClassification(
        description="HVAC filter replacement and service — monthly contract (Arctic Air)",
        amount_cents=185_000,   # $1,850
        classification="opex",
        confidence="high",
        matched_keyword=r"\bhvac\s+(service|clean|filter|tune|maintain)\b",
    )
    mock_unclear = GLLineClassification(
        description="Electrical work — Main switchboard",
        amount_cents=780_000,   # $7,800
        classification="unclear",
        confidence="low",
        llm_reasoning="Description ambiguous — could be routine maintenance or capital upgrade. Request original invoice.",
    )

    result.capex_lines.append(mock_capex)
    result.total_capex_cents = mock_capex.amount_cents
    result.opex_lines.append(mock_opex)
    result.total_opex_cents = mock_opex.amount_cents
    result.unclear_lines.append(mock_unclear)
    result.total_unclear_cents = mock_unclear.amount_cents

    result.warnings.append(
        "MOCK_MODE: GL classifications are synthetic. Set MOCK_MODE=false for real analysis."
    )
    return result


def gl_batch_to_line_items(batch: GLBatchClassification) -> list[dict]:
    """
    Convert a GLBatchClassification into a list of dicts compatible with
    outgoings_parser.OutgoingsLineItem fields, for ingestion into the
    outgoings reconciliation engine.

    CapEx items are flagged with is_capital_flag=True.
    """
    items = []
    for c in batch.capex_lines:
        items.append({
            "category": "capex",
            "description": c.description,
            "amount_cents": c.amount_cents,
            "gst_cents": int(c.amount_cents * 0.1),   # Assume GST-inclusive
            "is_capital_flag": True,
            "is_land_tax_flag": False,
        })
    for c in batch.opex_lines:
        items.append({
            "category": "other",
            "description": c.description,
            "amount_cents": c.amount_cents,
            "gst_cents": int(c.amount_cents * 0.1),
            "is_capital_flag": False,
            "is_land_tax_flag": False,
        })
    for c in batch.unclear_lines:
        items.append({
            "category": "other",
            "description": f"[UNCLEAR — manual review required] {c.description}",
            "amount_cents": c.amount_cents,
            "gst_cents": int(c.amount_cents * 0.1),
            "is_capital_flag": False,
            "is_land_tax_flag": False,
        })
    return items
