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
import re
import json
import time
import anthropic
from loguru import logger
from dotenv import load_dotenv

# Retry config for Claude API rate limits / overload errors
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 5.0  # seconds; doubles each attempt (5, 10, 20)

load_dotenv()

# Model identifiers -- override via .env: OPUS_MODEL, SONNET_MODEL, HAIKU_MODEL
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

# AQ1: Jurisdiction-specific statute map.
# Used to (a) tell Claude which acts to cite and (b) list acts it must NOT cite.
# Keyed by state code -- values are (primary_acts, prohibited_acts).
_JURISDICTION_STATUTES: dict[str, tuple[list[str], list[str]]] = {
    "WA": (
        [
            "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA)",
            "Property Law Act 1969 (WA)",
            "Transfer of Land Act 1893 (WA)",
            "Planning and Development Act 2005 (WA)",
            "Land Administration Act 1997 (WA)",
            "Land Tax Assessment Act 2002 (WA)",
            "Work Health and Safety Act 2020 (WA)",
            "Fair Trading Act 2010 (WA)",
            "Contaminated Sites Act 2003 (WA)",
            "Liquor Control Act 1988 (WA)",
        ],
        [
            "Retail Leases Act 2003 (VIC)",
            "Retail Leases Act 1994 (NSW)",
            "Retail Shop Leases Act 1994 (QLD)",
            "Retail and Commercial Leases Act 1995 (SA)",
            "Leases (Commercial and Retail) Act 2001 (ACT)",
        ],
    ),
    "VIC": (
        [
            "Retail Leases Act 2003 (VIC)",
            "Property Law Act 1958 (VIC)",
            "Transfer of Land Act 1958 (VIC)",
            "Planning and Environment Act 1987 (VIC)",
            "Land Tax Act 2005 (VIC)",
            "Workplace Safety Legislation Amendment Act 2021 (VIC)",
            "Australian Consumer Law and Fair Trading Act 2012 (VIC)",
        ],
        [
            "Retail Leases Act 1994 (NSW)",
            "Retail Shop Leases Act 1994 (QLD)",
            "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA)",
            "Retail and Commercial Leases Act 1995 (SA)",
        ],
    ),
    "NSW": (
        [
            "Retail Leases Act 1994 (NSW)",
            "Conveyancing Act 1919 (NSW)",
            "Real Property Act 1900 (NSW)",
            "Environmental Planning and Assessment Act 1979 (NSW)",
            "Land Tax Management Act 1956 (NSW)",
            "Work Health and Safety Act 2011 (NSW)",
            "Fair Trading Act 1987 (NSW)",
        ],
        [
            "Retail Leases Act 2003 (VIC)",
            "Retail Shop Leases Act 1994 (QLD)",
            "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA)",
            "Retail and Commercial Leases Act 1995 (SA)",
        ],
    ),
    "QLD": (
        [
            "Retail Shop Leases Act 1994 (QLD)",
            "Property Law Act 1974 (QLD)",
            "Land Title Act 1994 (QLD)",
            "Planning Act 2016 (QLD)",
            "Land Tax Act 2010 (QLD)",
            "Work Health and Safety Act 2011 (QLD)",
            "Fair Trading Act 1989 (QLD)",
        ],
        [
            "Retail Leases Act 1994 (NSW)",
            "Retail Leases Act 2003 (VIC)",
            "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA)",
            "Retail and Commercial Leases Act 1995 (SA)",
        ],
    ),
    "SA": (
        [
            "Retail and Commercial Leases Act 1995 (SA)",
            "Law of Property Act 1936 (SA)",
            "Real Property Act 1886 (SA)",
            "Work Health and Safety Act 2012 (SA)",
            "Fair Trading Act 1987 (SA)",
        ],
        [
            "Retail Leases Act 1994 (NSW)",
            "Retail Leases Act 2003 (VIC)",
            "Retail Shop Leases Act 1994 (QLD)",
            "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA)",
        ],
    ),
    "ACT": (
        [
            "Leases (Commercial and Retail) Act 2001 (ACT)",
            "Civil Law (Property) Act 2006 (ACT)",
            "Land Titles Act 1925 (ACT)",
            "Work Health and Safety Act 2011 (ACT)",
        ],
        [
            "Retail Leases Act 1994 (NSW)",
            "Retail Leases Act 2003 (VIC)",
            "Retail Shop Leases Act 1994 (QLD)",
            "Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA)",
        ],
    ),
    "TAS": (
        [
            "Fair Trading (Code of Practice for Retail Tenancies) Regulations 1998 (TAS)",
            "Conveyancing and Law of Property Act 1884 (TAS)",
            "Land Titles Act 1980 (TAS)",
            "Work Health and Safety Act 2012 (TAS)",
        ],
        [
            "Retail Leases Act 1994 (NSW)",
            "Retail Leases Act 2003 (VIC)",
            "Retail Shop Leases Act 1994 (QLD)",
        ],
    ),
    "NT": (
        [
            "Business Tenancies (Fair Dealings) Act 2003 (NT)",
            "Law of Property Act 2000 (NT)",
            "Land Title Act 2000 (NT)",
            "Work Health and Safety (National Uniform Legislation) Act 2011 (NT)",
        ],
        [
            "Retail Leases Act 1994 (NSW)",
            "Retail Leases Act 2003 (VIC)",
            "Retail Shop Leases Act 1994 (QLD)",
        ],
    ),
}


def _build_jurisdiction_constraint(jurisdiction: str) -> str:
    """
    AQ1: Return a hard constraint block that forces the LLM to cite only
    the correct jurisdiction's statutes and never cite other states' laws.
    """
    jur = jurisdiction.upper()
    entry = _JURISDICTION_STATUTES.get(jur)
    if not entry:
        return (
            f"JURISDICTION: {jur}. Cite only {jur} legislation. "
            "Do NOT cite interstate acts (VIC, NSW, QLD, SA, WA, ACT, TAS, NT) "
            "unless they are Commonwealth Acts that apply nationally."
        )
    primary, prohibited = entry
    primary_list  = "\n".join(f"    + {act}" for act in primary)
    blocked_list  = "\n".join(f"    - {act}" for act in prohibited)
    return "\n".join([
        f"JURISDICTION ENFORCEMENT -- {jur}",
        f"  This lease is governed by {jur} law.",
        "  You MUST ONLY cite the following acts (or Commonwealth acts that apply nationally):",
        primary_list,
        "",
        "  You are STRICTLY PROHIBITED from citing:",
        blocked_list,
        f"  Citing a prohibited act in a {jur} audit is a professional error.",
        f"  If a principle from another state applies, cite the equivalent {jur} provision instead.",
    ])


# -- AG2: Jurisdiction clause-level statute hints ------------------------------
#
# Maps (list_of_trigger_patterns, legislation_ref, hint_text) for WA.
# A hint fires when ANY pattern matches the combined clause_number + clause_text.
# Patterns are tried case-insensitively.
#
# Adding hints for a new state: add a parallel list (e.g. _VIC_CTRS_HINTS)
# and extend _build_clause_statute_hints() to check it.

_WA_CTRS_HINTS: list[tuple[list[str], str, str]] = [
    (
        [r"\b8\.\d+\b", r"capital\s+(cost|expenditure|works?|repair)", r"structural\s+(repair|maintenance)"],
        "CTRS Act (WA) s.11 -- Prohibition on Capital Cost Recovery",
        (
            "Under s.11 of the Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA), "
            "a landlord CANNOT recover capital costs, capital works, or structural repairs "
            "through outgoings or any other mechanism. "
            "Any clause that purports to pass capital expenditure to the tenant is UNLAWFUL. "
            "Flag at HIGH severity if this clause allows capital cost recovery."
        ),
    ),
    (
        [r"\b7\.6\b", r"\bland\s+tax\b"],
        "CTRS Act (WA) s.13 -- Absolute Prohibition on Land Tax Recovery",
        (
            "Under s.13 of the Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA), "
            "it is UNLAWFUL for a landlord to require a tenant to pay land tax, directly "
            "or via outgoings. Any such clause is VOID. "
            "This is an absolute prohibition -- flag at HIGH severity regardless of framing."
        ),
    ),
    (
        [r"\b7\.1\b", r"trading\s+hours?", r"hours\s+of\s+(trade|operation|business)"],
        "CTRS Act (WA) s.14C -- Mandatory Trading Hours Restrictions",
        (
            "Under s.14C of the Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA), "
            "a landlord cannot compel a tenant to trade during hours beyond what was voluntarily "
            "agreed. Flag any clause giving the landlord unilateral power to extend or vary "
            "trading hours -- this is a MEDIUM-HIGH severity issue."
        ),
    ),
    (
        [r"\b12\.\d+\b", r"\bassignment\b", r"\bsubleas(e|ing)\b", r"\bsubletting\b"],
        "CTRS Act (WA) s.22 + PLA (WA) ss.80-82 -- Assignment Protections",
        (
            "Under s.22 of the CTRS Act (WA): landlord cannot unreasonably withhold consent "
            "to assignment, and the outgoing tenant MUST be released from future obligations "
            "on a valid assignment. Under Property Law Act 1969 (WA) ss.80-82: assignment "
            "consent standards and covenant release on transfer apply. "
            "Under PLA s.81: landlord MUST serve formal written notice specifying the breach "
            "and a reasonable cure period before re-entering for default. "
            "Flag clauses allowing refusal without stated reasonable grounds, or that retain "
            "ongoing assignor liability post-assignment."
        ),
    ),
    (
        [r"\b26\.16\b", r"contract(ing)?\s+out", r"\bretail\s+shop\b", r"\bCTRS\b"],
        "CTRS Act (WA) s.27 -- Anti-Contracting-Out",
        (
            "Under s.27 of the Commercial Tenancy (Retail Shops) Agreements Act 1985 (WA), "
            "any lease term purporting to exclude, restrict, or modify the CTRS Act is VOID. "
            "An entire agreement clause broad enough to override statutory rights is "
            "unenforceable to that extent. "
            "If this clause contains language excluding retail tenancy protections, flag at "
            "HIGH severity."
        ),
    ),
    (
        [r"\b26\.1\b", r"quiet\s+enjoyment", r"peaceful\s+(enjoyment|possession)", r"covenant\s+for\s+quiet"],
        "Transfer of Land Act 1893 (WA) s.92(b) -- Implied Quiet Enjoyment Covenant",
        (
            "Under s.92(b) of the Transfer of Land Act 1893 (WA), a covenant of quiet "
            "enjoyment is implied into every registered lease. "
            "If this clause excludes, limits, or modifies quiet enjoyment (e.g. restricting "
            "it to direct acts of the landlord only, excluding agents/contractors, or stating "
            "that the landlord's exercise of rights cannot breach the covenant), flag at "
            "HIGH severity. "
            "Also consider recommending lodgement of a caveat under the TLA to protect the "
            "tenant's leasehold interest if the lease is not registered."
        ),
    ),
]


def _build_clause_statute_hints(
    jurisdiction: str,
    clause_number: str,
    clause_text: str,
) -> str:
    """
    AG2: Return a formatted block of jurisdiction-specific statute hints for
    this clause, based on clause number and/or text keyword matches.

    Returns an empty string if no hints apply (so callers can gate on truthiness).
    """
    jur = jurisdiction.upper()
    if jur != "WA":
        # Architecture is in place; add _VIC_CTRS_HINTS etc. as needed
        return ""

    search_text = f"{clause_number} {clause_text}"
    matched: list[str] = []

    for patterns, ref, hint in _WA_CTRS_HINTS:
        if any(re.search(p, search_text, re.IGNORECASE) for p in patterns):
            matched.append(f"  [{ref}]\n  {hint}")

    if not matched:
        return ""

    header = "WA STATUTORY CONSTRAINTS (mandatory -- check each against this clause):"
    return header + "\n" + "\n\n".join(matched)


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
    schedule_context: str = "",  # AQ2: injected schedule item content
    clause_number: str = "",     # AG2: clause heading/number for statute hint lookup
) -> dict:
    """
    Analyse a single lease clause with grounded RAG context.

    Returns structured JSON with extracted terms and risk flags.
    """
    model = select_model(clause_text)
    client = get_client()

    # AQ1: Build the jurisdiction enforcement block once per call.
    _jur_constraint = _build_jurisdiction_constraint(jurisdiction)

    system_prompt = "\n".join([
        f"You are an expert Australian commercial lease auditor specialising in {jurisdiction} tenancy law.",
        "",
        _jur_constraint,
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
    if cpi_context:
        user_prompt_parts += [
            cpi_context,
            "",
        ]

    # F5: Inject definitive jurisdiction-specific land tax position.
    if land_tax_context:
        user_prompt_parts += [
            land_tax_context,
            "",
        ]

    # AQ2: Inject referenced Schedule items so Claude can cross-check
    # what the lease actually says (e.g. Item 6 = no rent, Item 14 = N/A).
    if schedule_context:
        user_prompt_parts += [
            "REFERENCED SCHEDULE ITEMS (from this lease Schedule -- treat as authoritative):",
            schedule_context,
            "IMPORTANT: Check the schedule items above before flagging any risk in this clause.",
            "If a schedule item shows Not Applicable or overrides the clause default, adjust your finding accordingly.",
            "",
        ]

    # AG2: Inject WA CTRS Act / TLA section-level hints when clause number or
    # text matches a known statutory trigger. Empty string if no match.
    _statute_hints = _build_clause_statute_hints(jurisdiction, clause_number, clause_text)
    if _statute_hints:
        user_prompt_parts += [
            _statute_hints,
            "IMPORTANT: Apply each statutory constraint above before finalising your risk flags.",
            "A breach of any of these provisions must be flagged -- do not omit it.",
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
            usage = response.usage
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
                    f"{err_str[:120]} -- retrying in {delay:.0f}s"
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
    Pass 1: Haiku triage -- identify clause indices that need full Sonnet/Opus analysis.

    Takes a batch of chunks (slice of the full chunk list) and returns a tuple of:
      - list of *absolute* indices (batch_offset + local_idx) to flag for deep analysis
      - usage dict: {"input_tokens": int, "output_tokens": int} for cost tracking
        (zeros on fallback so accumulation is always safe)

    Falls back to flagging all clauses in the batch if the model returns non-JSON,
    so a triage failure degrades gracefully to the original sequential behaviour.

    Args:
        chunks:        Slice of DocumentChunk objects for this batch.
        batch_offset:  Index of chunks[0] in the full clause list.
        jurisdiction:  State code -- used for model context.
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
        "TARGET: Flag roughly 20-35% of clauses (5-9 per 25-clause batch). Be selective -- most leases "
        "have 60-70% boilerplate. If you are flagging more than 40%, re-read the DO NOT FLAG list and "
        "reconsider. A missed important clause is worse than a missed boilerplate clause, so when in "
        "doubt on a HIGH-VALUE topic, flag it -- but do not flag clauses that clearly belong in DO NOT FLAG.\n\n"
        "Return ONLY a JSON array of the clause numbers (integers) that need deep analysis. "
        "Example: [0, 3, 7]\n\n"
        f"CLAUSES:\n{clause_list}\n\n"
        "JSON array of clause numbers only:"
    )

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=512,
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
            f"{len(valid)} flagged ({flag_pct}%) -> {valid} "
            f"[in={usage.input_tokens} out={usage.output_tokens}]"
        )
        return valid, {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
    except Exception as e:
        logger.warning(
            f"Haiku triage FALLBACK (batch_offset={batch_offset}): {e} "
            f"-- flagging all {len(chunks)} clauses (may inflate triage rate)"
        )
        return list(range(batch_offset, batch_offset + len(chunks))), {"input_tokens": 0, "output_tokens": 0}
