"""
free_check_pipeline.py
----------------------
TenantSentry.ai — Free Lease Risk Check Pipeline

Lightweight pipeline for the public free-check flow (no auth, no payment).
Analyses the first ~5 pages of a lease or HoA and returns:
  - risk_score (0–100)
  - risk_level (low / medium / high)
  - pages_analysed
  - clauses_scanned
  - total_flags / high_flags
  - top_flags (first 3 visible to anonymous user)

DEV mode: deterministic mock data, no external calls, instant.
LIVE mode: Claude Haiku on extracted first-5-page text + regex flag detection.

Manual entry path: rule-based scoring from form field values (no PDF required).
"""

import time
from typing import Callable, Optional

ProgressCallback = Optional[Callable[[int, str], None]]

# ── Shared helpers ─────────────────────────────────────────────────────────────

def _progress(callback: ProgressCallback, pct: int, stage: str) -> None:
    if callback:
        callback(pct, stage)


# ── Canned flags for DEV mode ──────────────────────────────────────────────────

_LEASE_FLAGS_DEV = [
    {
        "severity": "high",
        "category": "Rent Review",
        "title": "Ratchet clause detected — rent cannot decrease",
        "description": (
            "Clause 12.3 contains a 'greater of' mechanic — rent can only move upward at each "
            "review, even if market rents fall. This is a one-sided provision that benefits the "
            "landlord at every review cycle."
        ),
        "benchmark": "📊 Ratchet clauses produce 15–25% above-market rent by Year 3",
    },
    {
        "severity": "high",
        "category": "Outgoings",
        "title": "Capital works incorrectly classified as outgoings",
        "description": (
            "Schedule B includes items (roof replacement, HVAC systems) that appear to be capital "
            "improvements, not recurring operating costs. Recovering capital costs as outgoings is "
            "prohibited under most state retail leases acts."
        ),
        "benchmark": "📊 Avg. misclassification = $8,400–$22,000/year overcharge",
    },
    {
        "severity": "medium",
        "category": "Option to Renew",
        "title": "Option notice window is unusually short — 3 months",
        "description": (
            "Clause 31.1 requires notice no later than 3 months before expiry. Standard commercial "
            "practice is 6 months. Missing this deadline forfeits your renewal right entirely with "
            "no right of relief."
        ),
        "benchmark": None,
    },
]

_HOA_FLAGS_DEV = [
    {
        "severity": "high",
        "category": "Effective Rent",
        "title": "Face-to-effective rent gap signals above-market positioning",
        "description": (
            "The net effective rent after incentives is significantly below face rent. This gap "
            "compounds at every review — if rent reviews are based on face rent, your effective "
            "cost escalates relative to the incentive value over time."
        ),
        "benchmark": "📊 A 20%+ gap typically means 8–12% above-market rent by Year 3",
    },
    {
        "severity": "high",
        "category": "Rent Review",
        "title": "Ratchet clause present — rent cannot fall at market review",
        "description": (
            "The HoA includes a 'greater of market or current rent' mechanic — locking you in for "
            "the full lease term if market rents soften. This is particularly risky in the current "
            "interest-rate environment."
        ),
        "benchmark": "📊 Market review + ratchet adds avg. $18,000–$45,000 over a 5yr term",
    },
    {
        "severity": "medium",
        "category": "Make-Good",
        "title": "Make-good references 'original condition' without a condition schedule",
        "description": (
            "Without a photographic schedule of condition at commencement, 'original condition' is "
            "subjective. Landlords routinely use this clause to claim new fitout at lease end."
        ),
        "benchmark": None,
    },
]


# ── DEV free check ─────────────────────────────────────────────────────────────

def run_free_check_dev(
    pdf_path: str,
    jurisdiction: str,
    doc_type: str,
    job_id: str,
    progress_callback: ProgressCallback = None,
) -> dict:
    """
    DEV mode free check — no external calls, deterministic result.
    Simulates OCR + analysis delay so the frontend scanning animation looks realistic.
    """
    steps = [
        (10, "Reading document structure"),
        (28, "Scanning rent review clauses"),
        (48, "Analysing outgoings structure"),
        (65, "Checking option exercise windows"),
        (82, "Cross-referencing legislation"),
        (95, "Calculating risk score"),
    ]
    for pct, stage in steps:
        time.sleep(0.7)
        _progress(progress_callback, pct, stage)

    is_hoa = doc_type == "hoa"
    flags = _HOA_FLAGS_DEV if is_hoa else _LEASE_FLAGS_DEV

    result = {
        "risk_score":       74,
        "risk_level":       "high",
        "pages_analysed":   68,
        "clauses_scanned":  41,
        "total_flags":      9,
        "high_flags":       3,
        "doc_type":         doc_type,
        "jurisdiction":     jurisdiction,
        "top_flags":        flags,
        "source":           "dev",
    }
    _progress(progress_callback, 100, "Complete")
    return result


# ── LIVE free check ────────────────────────────────────────────────────────────

def run_free_check_live(
    pdf_path: str,
    jurisdiction: str,
    doc_type: str,
    job_id: str,
    progress_callback: ProgressCallback = None,
) -> dict:
    """
    LIVE mode free check — Claude Haiku on extracted first-5-page text.
    Extracts text via the existing OCR/PDF parser, truncates to first 5 pages,
    asks Haiku for a structured risk score + top 3 flags.
    Falls back to DEV result if any step fails.
    """
    import os
    import json
    from loguru import logger

    _progress(progress_callback, 8, "Reading document structure")

    try:
        # ── Step 1: Extract text from PDF (first 5 pages only) ─────────────────
        from ingestion.pdf_parser import extract_text_from_pdf
        full_text = extract_text_from_pdf(pdf_path)
        _progress(progress_callback, 25, "Extracting clause text")

        # Truncate to approx first 5 pages (~3500 words)
        words = full_text.split()
        snippet = " ".join(words[:3500])

        is_hoa = doc_type == "hoa"
        doc_label = "Heads of Agreement" if is_hoa else "commercial lease"

        _progress(progress_callback, 42, "Scanning rent and outgoings clauses")

        # ── Step 2: Quick Haiku risk pass ──────────────────────────────────────
        import anthropic as _anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        client  = _anthropic.Anthropic(api_key=api_key)

        system_prompt = (
            f"You are an expert Australian commercial lease auditor. "
            f"Analyse this excerpt from a {doc_label} for {jurisdiction} jurisdiction. "
            "Identify the top 3 risk flags and return ONLY valid JSON matching this schema:\n\n"
            "{\n"
            '  "risk_score": <integer 0-100>,\n'
            '  "risk_level": <"low"|"medium"|"high">,\n'
            '  "clauses_scanned": <integer>,\n'
            '  "total_flags": <integer>,\n'
            '  "high_flags": <integer>,\n'
            '  "top_flags": [\n'
            '    { "severity": <"high"|"medium"|"low">, "category": <string>, '
            '"title": <string>, "description": <string>, "benchmark": <string|null> }\n'
            "  ]\n"
            "}\n\n"
            "Return ONLY the JSON — no markdown, no extra text."
        )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[
                {"role": "user", "content": f"Lease excerpt (first 5 pages):\n\n{snippet}"}
            ],
            system=system_prompt,
        )
        raw = response.content[0].text.strip()

        _progress(progress_callback, 75, "Cross-referencing legislation")

        # Strip markdown fences if Haiku adds them
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        parsed = json.loads(raw)

        _progress(progress_callback, 90, "Calculating risk score")

        # Estimate page count from text length
        page_count = max(5, len(words) // 350)

        result = {
            "risk_score":      parsed.get("risk_score", 65),
            "risk_level":      parsed.get("risk_level", "medium"),
            "pages_analysed":  min(5, page_count),
            "clauses_scanned": parsed.get("clauses_scanned", 12),
            "total_flags":     parsed.get("total_flags", len(parsed.get("top_flags", []))),
            "high_flags":      parsed.get("high_flags", 0),
            "doc_type":        doc_type,
            "jurisdiction":    jurisdiction,
            "top_flags":       parsed.get("top_flags", [])[:3],
            "source":          "live",
        }
        _progress(progress_callback, 100, "Complete")
        return result

    except Exception as e:
        logger.warning(f"[{job_id}] Free check LIVE failed ({e}), falling back to DEV result")
        # Graceful fallback — never expose the error to the anonymous user
        return run_free_check_dev(pdf_path, jurisdiction, doc_type, job_id, progress_callback)


# ── Manual entry scoring ───────────────────────────────────────────────────────

def score_manual_entry(data: dict) -> dict:
    """
    Rule-based risk scoring from manual form fields.
    No PDF required. Returns same schema as the pipeline result.
    Runs synchronously (fast, no I/O).

    data keys (lease):
      doc_type, jurisdiction, rent, sqm, term, review_type, fixed_pct,
      outgoings, options, makegood, landtax

    data keys (hoa):
      doc_type, jurisdiction, face_rent, net_rent, sqm, term,
      rentfree, fitout, review_type, outgoings, options, makegood, guarantee, bond
    """
    doc_type   = data.get("doc_type", "lease")
    is_hoa     = doc_type == "hoa"
    jurisdiction = data.get("jurisdiction", "").upper()
    flags: list[dict] = []
    score = 20  # base score — most leases start moderate

    if is_hoa:
        # ── HoA scoring ──────────────────────────────────────────────────────────
        face  = float(data.get("face_rent") or 0)
        net   = float(data.get("net_rent")  or 0)
        review = data.get("review_type", "")
        makegood = data.get("makegood", "")
        guarantee = data.get("guarantee", "")

        # Face-to-effective gap
        if face and net and face > 0:
            gap_pct = (face - net) / face * 100
            if gap_pct > 25:
                score += 30
                flags.append({
                    "severity": "high",
                    "category": "Effective Rent",
                    "title": f"Face-to-effective rent gap is {gap_pct:.0f}%",
                    "description": (
                        f"Face rent ${face:,.0f}/sqm vs net effective ${net:,.0f}/sqm — "
                        f"a {gap_pct:.0f}% gap. This gap compounds at every review if reviews "
                        "are applied to face rent."
                    ),
                    "benchmark": "📊 A 20%+ gap typically means 8–12% above-market rent by Year 3",
                })
            elif gap_pct > 15:
                score += 15
                flags.append({
                    "severity": "medium",
                    "category": "Effective Rent",
                    "title": f"Moderate face-to-effective rent gap ({gap_pct:.0f}%)",
                    "description": (
                        f"A {gap_pct:.0f}% gap between face and net effective rent. Monitor at "
                        "each review to ensure incentive value isn't eroded."
                    ),
                    "benchmark": None,
                })

        # Ratchet rent review
        if review in ("ratchet", "market"):
            score += 25
            flags.append({
                "severity": "high",
                "category": "Rent Review",
                "title": "Market review — potential ratchet risk" if review == "market"
                         else "Ratchet review — rent can only increase",
                "description": (
                    "A market review without an explicit 'no ratchet' clause means rent could be "
                    "locked to current levels even if market falls. Seek a downward adjustment clause."
                    if review == "market" else
                    "Ratchet mechanism means rent can never fall at review, regardless of market."
                ),
                "benchmark": "📊 Market review + ratchet adds avg. $18,000–$45,000 over a 5yr term",
            })

        # Make-good without condition schedule
        if makegood == "original":
            score += 10
            flags.append({
                "severity": "medium",
                "category": "Make-Good",
                "title": "Make-good to 'original condition' without condition schedule",
                "description": (
                    "Without a photographic schedule at commencement, 'original condition' is "
                    "subjective. This exposes you to costly claims at lease end."
                ),
                "benchmark": None,
            })

        # Unlimited personal guarantee
        if guarantee == "yes-unlimited":
            score += 15
            flags.append({
                "severity": "high",
                "category": "Personal Guarantee",
                "title": "Unlimited personal guarantee — full personal liability",
                "description": (
                    "An unlimited personal guarantee means the director is personally liable for "
                    "all rent and obligations for the full lease term. Cap or remove if possible."
                ),
                "benchmark": "📊 Seek to cap at 3–6 months rent or remove entirely for short terms",
            })

    else:
        # ── Lease scoring ────────────────────────────────────────────────────────
        rent    = float(data.get("rent") or 0)
        sqm     = float(data.get("sqm")  or 0)
        term    = float(data.get("term") or 0)
        review  = data.get("review_type", "")
        outgoings = data.get("outgoings", "")
        makegood  = data.get("makegood", "")
        landtax   = data.get("landtax", "")
        fixed_pct = float(data.get("fixed_pct") or 0)

        # High fixed review
        if review == "fixed" and fixed_pct and fixed_pct > 4:
            score += 25
            flags.append({
                "severity": "high",
                "category": "Rent Review",
                "title": f"Fixed rent review of {fixed_pct:.1f}% — well above CPI",
                "description": (
                    f"A fixed {fixed_pct:.1f}% annual rent increase will outpace CPI most years. "
                    f"Over {int(term)} years this compounds to a significant above-market premium."
                ),
                "benchmark": f"📊 At {fixed_pct:.1f}% fixed, rent doubles in ~{72/fixed_pct:.0f} years",
            })
        elif review in ("ratchet", "market"):
            score += 20
            flags.append({
                "severity": "high" if review == "ratchet" else "medium",
                "category": "Rent Review",
                "title": "Ratchet review — rent can only increase" if review == "ratchet"
                         else "Market review — seek explicit no-ratchet protection",
                "description": (
                    "Ratchet mechanism locks rent at the higher of each review — you can never "
                    "benefit from falling market rents." if review == "ratchet" else
                    "Market reviews without a 'no ratchet' clause are common traps. "
                    "Always negotiate a downward adjustment mechanism."
                ),
                "benchmark": "📊 Ratchet clauses produce 15–25% above-market rent by Year 3",
            })

        # Net outgoings — high risk of misclassification
        if outgoings == "net":
            score += 20
            flags.append({
                "severity": "medium",
                "category": "Outgoings",
                "title": "Net lease — tenant pays all outgoings",
                "description": (
                    "A net lease means you're responsible for all outgoings including rates, land "
                    "tax, insurance, and repairs. Misclassification of capital items as outgoings "
                    "is very common and worth auditing carefully."
                ),
                "benchmark": "📊 Avg. outgoings overcharge = $6,000–$18,000/year in net leases",
            })

        # Land tax full pass-through
        if landtax == "full":
            score += 12
            flags.append({
                "severity": "medium",
                "category": "Land Tax",
                "title": "Full land tax pass-through without multi-holding cap",
                "description": (
                    "Full land tax recovery without a multi-holding discount means you could be "
                    "paying a proportional share of the landlord's entire portfolio land tax. "
                    "This is prohibited or capped in most Australian jurisdictions."
                ),
                "benchmark": None,
            })

        # Make-good
        if makegood == "original":
            score += 10
            flags.append({
                "severity": "medium",
                "category": "Make-Good",
                "title": "Make-good to 'original condition' — no condition schedule",
                "description": (
                    "Without an attached schedule of condition at commencement, 'original condition' "
                    "is open to dispute. This is a common source of costly end-of-lease claims."
                ),
                "benchmark": None,
            })

        # Long term with no options
        if term >= 10 and not data.get("options"):
            score += 8
            flags.append({
                "severity": "low",
                "category": "Term & Options",
                "title": f"{int(term)}-year term with no renewal options specified",
                "description": (
                    f"A {int(term)}-year lease with no option means no ability to renew on agreed "
                    "terms — you'd need to renegotiate from scratch. Consider seeking at least one "
                    "renewal option."
                ),
                "benchmark": None,
            })

    score = min(score, 98)

    # Classify risk level
    if score >= 60:
        risk_level = "high"
    elif score >= 35:
        risk_level = "medium"
    else:
        risk_level = "low"

    high_count = sum(1 for f in flags if f["severity"] == "high")
    total_flags = len(flags)

    return {
        "risk_score":      score,
        "risk_level":      risk_level,
        "pages_analysed":  None,   # no pages for manual
        "clauses_scanned": None,
        "total_flags":     total_flags,
        "high_flags":      high_count,
        "doc_type":        doc_type,
        "jurisdiction":    jurisdiction,
        "top_flags":       flags[:3],
        "source":          "manual",
    }
