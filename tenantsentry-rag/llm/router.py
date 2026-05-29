"""
router.py
---------
Decides whether to send a clause to Claude Opus (deep reasoning)
or Claude Sonnet (fast extraction).

Routing logic:
  - Complex clauses (rent review, demolition, assignment, etc.) → Opus
  - Simple fields (dates, term, rent amount) → Sonnet
"""

import os
import json
import anthropic
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

OPUS_MODEL = os.environ.get("OPUS_MODEL", "claude-opus-4-6")
SONNET_MODEL = os.environ.get("SONNET_MODEL", "claude-sonnet-4-6")

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
            logger.debug(f"Complex clause detected ('{keyword}') → Opus")
            return OPUS_MODEL
    logger.debug("Simple clause → Sonnet")
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

    system_prompt = f"""You are an expert Australian commercial lease auditor specialising in {jurisdiction} tenancy law.

You will be given:
1. A clause from a commercial lease
2. Relevant sections from the {jurisdiction} Retail Leases Act and related legislation
3. Known risk flag rules for this type of clause

Your job is to:
- Extract the key terms from the clause
- Identify any risks, unfair terms, or non-compliance with legislation
- Provide a plain-English summary
- Recommend action for the tenant

Always cite specific legislation sections when flagging risks.
Respond ONLY with valid JSON matching the schema below — no preamble."""

    user_prompt = f"""LEGISLATION CONTEXT:
{legislation_context}

RISK FLAG RULES:
{rules_context}

LEASE CLAUSE:
{clause_text}

Respond with this exact JSON structure:
{{
  "clause_type": "string (e.g. Rent Review, Option to Renew, Make Good)",
  "key_terms": ["list of extracted key terms/dates/amounts"],
  "risk_flags": [
    {{
      "flag_id": "string",
      "description": "string",
      "severity": "high | medium | low",
      "legislation_ref": "string or null"
    }}
  ],
  "plain_english_summary": "string — 2-3 sentences for a non-lawyer tenant",
  "recommended_action": "string — what the tenant should do"
}}"""

    logger.info(f"Analysing clause with {model}")

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
    )

    raw = response.content[0].text.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"LLM returned non-JSON: {raw[:200]}")
        return {"error": "Failed to parse LLM response", "raw": raw}
