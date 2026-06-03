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
import anthropic
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# Model identifiers — override via .env: OPUS_MODEL, SONNET_MODEL
OPUS_MODEL   = os.environ.get("OPUS_MODEL",   "claude-opus-4-6")
SONNET_MODEL = os.environ.get("SONNET_MODEL", "claude-sonnet-4-6")

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
    ])

    leg_context = legislation_context or "No specific legislation retrieved -- apply your expertise and the risk rules below."

    user_prompt = "\n".join([
        "LEGISLATION CONTEXT (cite when available -- flag even if empty):",
        leg_context,
        "",
        "RISK FLAG RULES (MANDATORY -- check every rule against this clause):",
        rules_context,
        "",
        "LEASE CLAUSE TO ANALYSE:",
        clause_text,
        "",
        "CRITICAL INSTRUCTION: You MUST populate the risk_flags array with individual flag objects for every risk identified.",
        "Do NOT put risk descriptions only in plain_english_summary or recommended_action -- they must ALSO appear as entries in risk_flags.",
        "If you identify 3 risks, risk_flags must have 3 entries. An empty risk_flags array means the clause is completely fair and standard.",
        "",
        "Now analyse the clause above and respond with JSON only -- no preamble, no markdown fences:",
    ])

    logger.info(f"Analysing clause with {model}")

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
        timeout=60.0,
    )

    raw = response.content[0].text.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"LLM returned non-JSON: {raw[:200]}")
        return {"error": "Failed to parse LLM response", "raw": raw}
