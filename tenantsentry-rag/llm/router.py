"""
router.py
---------
Decides whether to send a clause to Claude Opus (deep reasoning)
or Claude Sonnet (fast extraction).

Routing logic:
  - Complex clauses (rent review, demolition, assignment, etc.) -> Opus
  - Simple fields (dates, term, rent amount) -> Sonnet
"""

import os
import json
import time
import anthropic
from loguru import logger
from dotenv import load_dotenv

# Retry config for Claude API rate limits / overload errors
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 5.0  # seconds; doubles each attempt (5, 10, 20)

load_dotenv()

# Model identifiers — override via .env: OPUS_MODEL, SONNET_MODEL, HAIKU_MODEL
OPUS_MODEL   = os.environ.get("OPUS_MODEL",   "claude-opus-4-6")
SONNET_MODEL = os.environ.get("SONNET_MODEL", "claude-sonnet-4-6")
HAIKU_MODEL  = os.environ.get("HAIKU_MODEL",  "claude-haiku-4-5-20251001")

# Startup guard
_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if not _api_key or _api_key.startswith("sk-ant-your"):
    logger.warning(
        "ANTHROPIC_API_KEY is not set or is still a placeholder. "
        "Real audits will fail. Set MOCK_MODE=true for local dev."
    )

# Keywords that trigger deep reasoning (Opus)
COMPLEX_CLAUSE_KEYWORDS = [
    "rent review", "cpi", "market review",
    "demolition", "redevelopment",
    "assignment", "subletting", "sublease",
    "make good", "make-good", "reinstatement",
    "option to renew", "option period",
    "holdover", "overholding",
    "indemnity", "indemnification",
    "force majeure",
    "exclusivity",
    "fitout", "fit-out",
    "outgoings", "land tax",
    "termination", "default",
]

_client = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def select_model(clause_text: str) -> str:
    """Return the appropriate model name for this clause."""
    text_lower = clause_text.lower()
    for keyword in COMPLEX_CLAUSE_KEYWORDS:
        if keyword in text_lower:
            logger.debug(f"Complex clause detected ('{keyword}') -> Opus")
            return OPUS_MODEL
    logger.debug("Simple clause -> Sonnet")
    return SONNET_MODEL


def analyse_clause(
    clause_text: str,
    legislation_context: str,
    rules_context: str,
    jurisdiction: str,
    cpi_context: str = "",
    land_tax_context: str = "",
) -> dict:
    """
    Analyse a single lease clause with grounded RAG context.

    Returns structured JSON with extracted terms and risk flags.
    """
    model = select_model(clause_text)
    client = get_client()

    system_prompt = "\n".join([
        f"You are an expert Australian commercial lease auditor specialising in {jurisdiction} tenancy law.",
        "",
        "You will be given:",
        "1. A clause from a commercial lease",
        f"2. Relevant sections from the {jurisdiction} Retail Leases Act and related legislation (may be empty)",
        "3. Known risk flag rules for this type of clause",
        "",
        "Your job is to:",
        "- Extract the key terms from the clause",
        "- Identify ANY risks, unfair terms, or tenant-adverse provisions -- even if legislation context is not provided",
        "- Flag issues based on the risk flag rules, your knowledge of Australian commercial lease law, and common negotiating best practice",
        "- Provide a plain-English summary a non-lawyer tenant can act on",
        "- Recommend concrete action for the tenant",
        "",
        "FLAGGING RULES:",
        "- Flag every real risk -- but calibrate severity accurately using the scale below.",
        "- Personal guarantees without a monetary cap or time limit are ALWAYS high severity.",
        "- Ratchet clauses preventing downward rent review are ALWAYS high severity.",
        "- Land tax in outgoings is ALWAYS high severity in VIC; medium severity in NSW/QLD.",
        "- Capital expenditure in outgoings is ALWAYS high severity in any jurisdiction.",
        "- Cite legislation when available, but DO NOT withhold a flag just because legislation context is absent.",
        '- "risk_flags" must be an empty array [] only if the clause is genuinely fair and standard.',
        "",
        "SEVERITY CALIBRATION -- use the right level, not always HIGH:",
        "- HIGH: Direct financial exposure, clear legislative breach, or terms likely unenforceable as written.",
        "  Examples: make-good overriding fair wear and tear, uncapped personal guarantee, rent ratchet, no renewal option on a capital-investment lease.",
        "- MEDIUM: Tenant-adverse terms that are legal but negotiable; missing protections that are not mandatory.",
        "  Examples: short notice periods, broad landlord re-entry rights without cure period, outgoings without audit rights.",
        "- LOW: Minor administrative burdens, suboptimal but industry-standard terms, informational gaps with no direct financial impact.",
        "  Examples: no fitout guide provided, CPI base month not specified, non-material definition gaps.",
        "A realistic audit should have a MIX of HIGH, MEDIUM, and LOW flags -- not everything should be HIGH.",
        "",
        "FINANCIAL QUANTIFICATION:",
        "- Where possible, include a dollar estimate of the financial exposure in the flag description.",
        "- Examples: fitout investment of $150k-$300k would be fully written off at expiry;",
        "  make-good strip-out costs in NSW typically reach $100k-$500k+.",
        "- Use ranges if exact figures are unknown. This helps the tenant understand the real stakes.",
        "",
        "Respond ONLY with valid JSON matching the schema below -- no preamble, no markdown.",
        "",
        "JSON SCHEMA:",
        "{",
        '  "clause_type": "rent_review|outgoings|make_good|options|holding_over|land_tax|guarantee|assignment|other",',
        '  "key_terms": ["..."],',
        '  "risk_flags": [{"flag_id":"RF001","description":"...","severity":"high|medium|low","legislation_ref":"..."}],',
        '  "plain_english_summary": "...",',
        '  "recommended_action": "...",',
        '  "cpi_index_series": "sydney|melbourne|brisbane|adelaide|perth|hobart|darwin|canberra|weighted_average|null"',
        "}",
        "",
        "cpi_index_series: Extract ONLY for rent_review clauses. Set to the city/series the lease specifies",
        "(e.g. 'sydney' if the lease says 'Sydney All Groups CPI', 'weighted_average' if it says",
        "'weighted average of eight capital cities' or is unspecified). Set to null for all other clause types.",
    ])

    leg_context = legislation_context or "No specific legislation retrieved -- apply your expertise and the risk rules below."

    user_prompt_parts = [
        "LEGISLATION CONTEXT (cite when available -- flag even if empty):",
        leg_context,
        "",
        "RISK FLAG RULES (MANDATORY -- check every rule against this clause):",
        rules_context,
        "",
    ]

    # G7: Inject pre-computed ABS CPI data when available.
    # Claude must interpret this figure, not recalculate it.
    if cpi_context:
        user_prompt_parts += [
            cpi_context,
            "",
        ]

    # F5: Inject definitive jurisdiction-specific land tax position.
    # Claude applies the correct statutory rule — no guesswork.
    if land_tax_context:
        user_prompt_parts += [
            land_tax_context,
            "",
        ]

    user_prompt_parts += [
        "LEASE CLAUSE TO ANALYSE:",
        clause_text,
        "",
        "CRITICAL INSTRUCTION: You MUST populate the risk_flags array with individual flag objects for every risk identified.",
        "Do NOT put risk descriptions only in plain_english_summary or recommended_action -- they must ALSO appear as entries in risk_flags.",
        "If you identify 3 risks, risk_flags must have 3 entries. An empty risk_flags array means the clause is completely fair and standard.",
        "",
        "Now analyse the clause above and respond with JSON only -- no preamble, no markdown fences:",
    ]

    user_prompt = "\n".join(user_prompt_parts)

    logger.info(f"Analysing clause with {model}")

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                timeout=60.0,
            )
            raw = response.content[0].text.strip()
            usage = response.usage  # always present on a successful response
            try:
                result = json.loads(raw)
                result["_model"] = model
                result["_input_tokens"]  = usage.input_tokens
                result["_output_tokens"] = usage.output_tokens
                return result
            except json.JSONDecodeError:
                logger.error(f"LLM returned non-JSON: {raw[:200]}")
                return {
                    "error": "Failed to parse LLM response", "raw": raw[:500],
                    "_model": model,
                    "_input_tokens": usage.input_tokens,
                    "_output_tokens": usage.output_tokens,
                }
        except Exception as exc:
            last_exc = exc
            err_str = str(exc)
            is_retryable = any(s in err_str for s in ("429", "529", "overloaded", "rate limit", "rate_limit"))
            if is_retryable and attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"Claude API retryable error (attempt {attempt + 1}/{_MAX_RETRIES}): "
                    f"{err_str[:120]} — retrying in {delay:.0f}s"
                )
                time.sleep(delay)
            else:
                break

    logger.error(f"analyse_clause failed after {_MAX_RETRIES} attempts: {last_exc}")
    return {"error": f"API error after retries: {last_exc}"}


def triage_clauses(
    chunks: list,
    batch_offset: int,
    jurisdiction: str,
) -> tuple[list[int], dict]:
    """
    Pass 1: Haiku triage — identify clause indices that need full Sonnet/Opus analysis.

    Takes a batch of chunks (slice of the full chunk list) and returns a tuple of:
      - list of *absolute* indices (batch_offset + local_idx) to flag for deep analysis
      - usage dict: {"input_tokens": int, "output_tokens": int} for cost tracking
        (zeros on fallback so accumulation is always safe)

    Falls back to flagging all clauses in the batch if the model returns non-JSON,
    so a triage failure degrades gracefully to the original sequential behaviour.

    Args:
        chunks:        Slice of DocumentChunk objects for this batch.
        batch_offset:  Index of chunks[0] in the full clause list.
        jurisdiction:  State code — used for model context.
    """
    client = get_client()

    clause_list = "\n".join(
        f"{batch_offset + i}. [{c.metadata.get('clause_heading', f'Clause {batch_offset + i}')}] "
        f"{c.content[:200].replace(chr(10), ' ')}"
        for i, c in enumerate(chunks)
    )

    prompt = (
        f"You are screening {jurisdiction} commercial lease clauses for deep legal analysis.\n\n"
        "FLAG a clause (include its number) ONLY if it contains ONE OR MORE of these HIGH-VALUE topics:\n"
        "- Rent amount, rent review, CPI escalation, market review, rent ratchet\n"
        "- Outgoings, land tax, rates, levies, capital expenditure\n"
        "- Make-good, reinstatement, fitout obligations\n"
        "- Personal guarantee, indemnity, liability cap\n"
        "- Option to renew, option to purchase, holdover/overholding\n"
        "- Assignment, subletting, change of control\n"
        "- Termination, default, re-entry rights\n"
        "- Demolition, redevelopment, relocation\n"
        "- Exclusivity, permitted use restrictions\n\n"
        "DO NOT FLAG (these are standard boilerplate, skip them):\n"
        "- Definitions, interpretation, headings\n"
        "- Notices and service of documents\n"
        "- Entire agreement, waiver, severability\n"
        "- Governing law, jurisdiction\n"
        "- Counterparts, execution\n"
        "- General repair and maintenance (standard obligations only)\n"
        "- Insurance obligations (standard only, no unusual liability)\n"
        "- Confidentiality (standard)\n\n"
        "TARGET: Flag roughly 20-35% of clauses (5-9 per 25-clause batch). Be selective — most leases "
        "have 60-70% boilerplate. If you are flagging more than 40%, re-read the DO NOT FLAG list and "
        "reconsider. A missed important clause is worse than a missed boilerplate clause, so when in "
        "doubt on a HIGH-VALUE topic, flag it — but do not flag clauses that clearly belong in DO NOT FLAG.\n\n"
        "Return ONLY a JSON array of the clause numbers (integers) that need deep analysis. "
        "Example: [0, 3, 7]\n\n"
        f"CLAUSES:\n{clause_list}\n\n"
        "JSON array of clause numbers only:"
    )

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=512,  # 256 was too tight for large-index batches (e.g. indices 125-149)
            messages=[{"role": "user", "content": prompt}],
            timeout=30.0,
        )
        usage = response.usage
        raw = response.content[0].text.strip()
        indices = json.loads(raw)
        valid = [int(i) for i in indices if isinstance(i, (int, float))]
        flag_pct = round(100 * len(valid) / len(chunks)) if chunks else 0
        logger.info(
            f"Haiku triage batch offset={batch_offset} size={len(chunks)}: "
            f"{len(valid)} flagged ({flag_pct}%) → {valid} "
            f"[in={usage.input_tokens} out={usage.output_tokens}]"
        )
        return valid, {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
    except Exception as e:
        logger.warning(
            f"Haiku triage FALLBACK (batch_offset={batch_offset}): {e} "
            f"— flagging all {len(chunks)} clauses (may inflate triage rate)"
        )
        return list(range(batch_offset, batch_offset + len(chunks))), {"input_tokens": 0, "output_tokens": 0}
