"""
cpi_calculator.py
-----------------
G7: Deterministic CPI Calculator

Fetches CPI index data from the ABS SDMX API and computes rent review
adjustments deterministically in Python.

Claude receives the pre-computed result for *interpretation only* —
it is explicitly instructed not to recalculate. This eliminates
hallucination risk on financial figures, which is the highest
liability exposure in the audit pipeline.

Public API
----------
    get_cpi_snapshot(jurisdiction)           -> dict
    compute_adjustment(base_val, review_val) -> float
    format_for_prompt(snapshot, base_date)   -> str   (inject into Claude prompt)

Snapshot dict schema
--------------------
    ok:              bool
    city:            str
    region_code:     str
    periods:         list[str]    e.g. ["2022-Q3", ..., "2024-Q2"]
    values:          list[float]  matching index numbers
    base_quarter:    str
    latest_quarter:  str
    base_value:      float
    latest_value:    float
    change_pct:      float        (latest/base - 1) × 100, 2dp
    source_url:      str
    error:           str | None   set when ok=False

Notes
-----
- Results are cached per jurisdiction for the lifetime of the process.
  ABS publishes quarterly; a single run never spans a quarter boundary.
- If the ABS API is unreachable, ok=False is returned. The caller should
  omit cpi_context from the prompt (Claude will not receive a figure to
  misuse) and the evidence pack falls back to manual verification steps.
- Uses httpx (sync) — called from thread pool in audit_pipeline, not from
  the async event loop directly.
"""

import math
import httpx
from loguru import logger
from datetime import date as _date

ABS_API_BASE = "https://api.data.abs.gov.au"

# ABS CPI SDMX series key structure:
#   CPI/{Measure}.{Region}.{Index}.{Type}.{Frequency}
#   Measure=1 (CPI), Index=10 (All Groups), Type=50 (index numbers), Freq=Q
_JUR_CPI_REGION: dict[str, tuple[str, str]] = {
    "NSW": ("1",     "Sydney"),
    "VIC": ("2",     "Melbourne"),
    "QLD": ("3",     "Brisbane"),
    "SA":  ("4",     "Adelaide"),
    "WA":  ("5",     "Perth"),
    "TAS": ("6",     "Hobart"),
    "NT":  ("7",     "Darwin"),
    "ACT": ("8",     "Canberra"),
}
_NATIONAL_REGION: tuple[str, str] = ("10001", "Weighted Average of Eight Capital Cities")

# G9: Map series names (as Claude extracts them) to ABS region codes.
# These are the values that may appear in lease.extracted_rules["cpi_index_series"].
_SERIES_NAME_TO_REGION: dict[str, tuple[str, str]] = {
    "sydney":           ("1",     "Sydney"),
    "melbourne":        ("2",     "Melbourne"),
    "brisbane":         ("3",     "Brisbane"),
    "adelaide":         ("4",     "Adelaide"),
    "perth":            ("5",     "Perth"),
    "hobart":           ("6",     "Hobart"),
    "darwin":           ("7",     "Darwin"),
    "canberra":         ("8",     "Canberra"),
    "weighted_average": ("10001", "Weighted Average of Eight Capital Cities"),
    "all_groups":       ("10001", "Weighted Average of Eight Capital Cities"),
}

# Process-level cache — keyed by (jurisdiction, series_override) tuple
_snapshot_cache: dict[tuple[str, str | None], dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def get_cpi_snapshot(
    jurisdiction: str,
    num_quarters: int = 8,
    series_override: str | None = None,
) -> dict:
    """
    Return the last `num_quarters` quarters of All Groups CPI.

    jurisdiction:    State code — NSW, VIC, QLD, etc. Used as fallback when
                     series_override is absent.
    series_override: G9 — ABS series name from lease.extracted_rules["cpi_index_series"].
                     e.g. "sydney", "weighted_average". When provided, overrides the
                     jurisdiction default so we use the exact series the lease specifies.
    num_quarters:    How many trailing quarters to return (default 8 = 2 years).

    Result is cached per (jurisdiction, series_override) pair for the lifetime
    of the process — the ABS API is called at most once per unique combination.

    Returns a snapshot dict (see module docstring for schema).
    """
    jur = jurisdiction.upper()
    cache_key = (jur, series_override)

    if cache_key in _snapshot_cache:
        return _snapshot_cache[cache_key]

    # G9: series_override takes precedence over jurisdiction default
    if series_override and series_override.lower() in _SERIES_NAME_TO_REGION:
        region_code, city = _SERIES_NAME_TO_REGION[series_override.lower()]
    else:
        region_code, city = _JUR_CPI_REGION.get(jur, _NATIONAL_REGION)
    data_key = f"1.{region_code}.10.50.Q"
    url = f"{ABS_API_BASE}/data/CPI/{data_key}"

    try:
        resp = httpx.get(url, params={"format": "jsondata"}, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()

        # Navigate SDMX-JSON envelope
        obs = data["data"]["dataSets"][0]["series"]["0:0:0:0:0"]["observations"]
        time_periods = (
            data["data"]["structure"]["dimensions"]["observation"][0]["values"]
        )

        pairs: list[tuple[str, float]] = []
        for idx_str, vals in obs.items():
            idx = int(idx_str)
            period = time_periods[idx]["id"]   # e.g. "2024-Q2"
            value = float(vals[0])
            pairs.append((period, value))

        pairs.sort(key=lambda x: x[0])
        pairs = pairs[-num_quarters:]

        if len(pairs) < 2:
            raise ValueError("Fewer than 2 data points returned from ABS API")

        base_period, base_value = pairs[0]
        latest_period, latest_value = pairs[-1]
        change_pct = compute_adjustment(base_value, latest_value)

        snapshot = {
            "ok": True,
            "city": city,
            "region_code": region_code,
            "periods": [p for p, _ in pairs],
            "values": [v for _, v in pairs],
            "base_quarter": base_period,
            "latest_quarter": latest_period,
            "base_value": base_value,
            "latest_value": latest_value,
            "change_pct": change_pct,
            "source_url": url,
            "error": None,
        }
        _snapshot_cache[cache_key] = snapshot
        logger.info(
            f"[CPI] {jur} snapshot: {base_period}→{latest_period} "
            f"| {base_value:.1f}→{latest_value:.1f} | {change_pct:+.2f}%"
        )
        return snapshot

    except Exception as exc:
        logger.warning(f"[CPI] ABS fetch failed for {jur} ({url}): {exc}")
        err = {
            "ok": False,
            "city": city,
            "region_code": region_code,
            "periods": [],
            "values": [],
            "base_quarter": None,
            "latest_quarter": None,
            "base_value": None,
            "latest_value": None,
            "change_pct": None,
            "source_url": url,
            "error": str(exc),
        }
        _snapshot_cache[cache_key] = err
        return err


def compute_adjustment(base_value: float, review_value: float) -> float:
    """
    Deterministic percentage change between two CPI index values.
    Formula: (review / base − 1) × 100, rounded to 2 decimal places.
    """
    if base_value <= 0:
        raise ValueError(f"base_value must be positive, got {base_value}")
    return round(((review_value - base_value) / base_value) * 100, 2)


def format_for_prompt(snapshot: dict, lease_base_date: str = "") -> str:
    """
    Format a CPI snapshot into a context block for injection into the
    Claude prompt.

    The block contains explicit instructions that Claude must use this
    verified figure and must NOT recalculate from its training data.

    Returns empty string when snapshot.ok is False — the caller should
    simply omit the cpi_context argument rather than passing empty string,
    but empty string is safe (it adds no content to the prompt).
    """
    if not snapshot.get("ok"):
        return ""

    s = snapshot
    lines = [
        "╔══ VERIFIED ABS CPI DATA — DO NOT RECALCULATE ══════════════════════════╗",
        f"  Source:         Australian Bureau of Statistics SDMX API",
        f"  City/Region:    {s['city']} (region code {s['region_code']})",
        f"  Series:         All Groups CPI — quarterly index numbers",
        f"  Data URL:       {s['source_url']}",
        "",
        f"  Period:         {s['base_quarter']} → {s['latest_quarter']}",
        f"  Base value:     {s['base_value']:.1f}",
        f"  Latest value:   {s['latest_value']:.1f}",
        "",
        f"  Deterministic calculation:",
        f"    ({s['latest_value']:.1f} ÷ {s['base_value']:.1f} − 1) × 100",
        f"    = {s['change_pct']:+.2f}%  ← verified CPI movement over this period",
    ]

    if lease_base_date:
        lines += [
            "",
            f"  Lease base date: {lease_base_date}",
            "  If this date falls within the range above, use the nearest quarter value.",
        ]

    lines += [
        "",
        "  MANDATORY INSTRUCTION TO CLAUDE:",
        f"  Use ONLY {s['change_pct']:+.2f}% as the CPI benchmark when assessing rent reviews.",
        "  Do NOT estimate, guess, or source CPI data from your training knowledge.",
        "  Do NOT recalculate. If the landlord has applied a higher increase than",
        "╚═════════════════════════════════════════════════════════════════════════╝",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# AQ-NEW-13: Point-in-time CPI lookup
# Enables deterministic calculation from a specific LEASE BASE DATE to the
# current (or any specified) review date — not just a trailing 8-quarter window.
# This is essential for leases where the CPI clause specifies "from commencement"
# or "from [specific date]" as the base, rather than the immediately prior period.
# ══════════════════════════════════════════════════════════════════════════════

# Cache for full historical ABS series — keyed by region_code
# Historical series returns up to 40+ quarters; we cache the entire fetch.
_historical_cache: dict[str, dict] = {}

# How many years back to fetch — covers most commercial lease terms
_HISTORY_YEARS = 12


def date_to_quarter(iso_date: str) -> str:
    """
    Convert an ISO date string (YYYY-MM-DD) to an ABS quarter label (e.g. "2021-Q2").

    ABS quarters:
        Q1 = Jan–Mar  (month 1–3)
        Q2 = Apr–Jun  (month 4–6)
        Q3 = Jul–Sep  (month 7–9)
        Q4 = Oct–Dec  (month 10–12)

    Args:
        iso_date: "YYYY-MM-DD" string

    Returns:
        Quarter label string, e.g. "2021-Q2"

    Raises:
        ValueError if the date string is malformed
    """
    d = _date.fromisoformat(str(iso_date).strip()[:10])
    q = math.ceil(d.month / 3)
    return f"{d.year}-Q{q}"


def get_historical_snapshot(
    jurisdiction: str,
    series_override: str | None = None,
    years_back: int = _HISTORY_YEARS,
) -> dict:
    """
    AQ-NEW-13: Fetch a full historical CPI series covering the last `years_back` years.

    This is a superset of get_cpi_snapshot — it fetches more quarters so that
    we can look up CPI values for specific past dates (e.g. lease commencement).

    Returns the same snapshot dict schema as get_cpi_snapshot, but with more
    data points in `periods` and `values`.

    Cached per (jurisdiction, series_override) for the process lifetime.
    """
    jur = jurisdiction.upper()
    cache_key = f"hist::{jur}::{series_override}"
    if cache_key in _historical_cache:
        return _historical_cache[cache_key]

    num_quarters = years_back * 4 + 4  # a few extra for safety

    if series_override and series_override.lower() in _SERIES_NAME_TO_REGION:
        region_code, city = _SERIES_NAME_TO_REGION[series_override.lower()]
    else:
        region_code, city = _JUR_CPI_REGION.get(jur, _NATIONAL_REGION)

    data_key = f"1.{region_code}.10.50.Q"
    url = f"{ABS_API_BASE}/data/CPI/{data_key}"

    try:
        resp = httpx.get(url, params={"format": "jsondata"}, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()

        obs = data["data"]["dataSets"][0]["series"]["0:0:0:0:0"]["observations"]
        time_periods = (
            data["data"]["structure"]["dimensions"]["observation"][0]["values"]
        )
        pairs: list[tuple[str, float]] = []
        for idx_str, vals in obs.items():
            idx = int(idx_str)
            period = time_periods[idx]["id"]
            value = float(vals[0])
            pairs.append((period, value))

        pairs.sort(key=lambda x: x[0])
        # Keep last num_quarters
        pairs = pairs[-num_quarters:]

        if len(pairs) < 4:
            raise ValueError("Insufficient historical data from ABS API")

        base_period, base_value   = pairs[0]
        latest_period, latest_value = pairs[-1]

        snapshot = {
            "ok": True,
            "city": city,
            "region_code": region_code,
            "periods": [p for p, _ in pairs],
            "values":  [v for _, v in pairs],
            "base_quarter":   base_period,
            "latest_quarter": latest_period,
            "base_value":     base_value,
            "latest_value":   latest_value,
            "change_pct":     compute_adjustment(base_value, latest_value),
            "source_url":     url,
            "error":          None,
        }
        _historical_cache[cache_key] = snapshot
        logger.info(
            f"[CPI-HIST] {jur}: {len(pairs)} quarters fetched "
            f"({base_period} → {latest_period})"
        )
        return snapshot

    except Exception as exc:
        logger.warning(f"[CPI-HIST] Historical fetch failed for {jur}: {exc}")
        err = {
            "ok": False, "city": city, "region_code": region_code,
            "periods": [], "values": [], "base_quarter": None,
            "latest_quarter": None, "base_value": None, "latest_value": None,
            "change_pct": None, "source_url": url, "error": str(exc),
        }
        _historical_cache[cache_key] = err
        return err


def get_cpi_at_quarter(
    jurisdiction: str,
    quarter: str,
    series_override: str | None = None,
) -> float | None:
    """
    AQ-NEW-13: Look up the ABS CPI index value for a specific quarter.

    Uses the historical series cache so this is O(1) after the first call.

    Args:
        jurisdiction:    State code (NSW, VIC, QLD, etc.)
        quarter:         ABS quarter label, e.g. "2021-Q2" (use date_to_quarter())
        series_override: Optional series name from lease (e.g. "sydney")

    Returns:
        The CPI index number for that quarter, or None if not available.
    """
    snap = get_historical_snapshot(jurisdiction, series_override)
    if not snap["ok"]:
        return None
    try:
        idx = snap["periods"].index(quarter)
        return snap["values"][idx]
    except ValueError:
        # Quarter not in the cached series — may be too old or too recent
        logger.debug(f"[CPI-HIST] Quarter {quarter!r} not found in {jurisdiction} history")
        return None


def compute_cpi_review(
    jurisdiction: str,
    base_date: str,
    review_date: str,
    series_override: str | None = None,
) -> dict:
    """
    AQ-NEW-13: Deterministic CPI calculation between two specific dates.

    This is the high-precision alternative to get_cpi_snapshot — instead of
    "last 8 quarters", it computes the change from the EXACT lease base date
    (e.g. commencement) to the EXACT review date.

    This is critical for long leases where the lease says "from the Commencement
    Date" rather than "since the last review" — the cumulative effect can be
    significantly different.

    Args:
        jurisdiction:    State code
        base_date:       ISO date string — start of the CPI measurement period
                         (typically lease commencement or last review date)
        review_date:     ISO date string — end of the CPI measurement period
                         (typically the rent review trigger date)
        series_override: Optional series name from lease terms

    Returns dict:
    {
      "ok":             bool,
      "jurisdiction":   str,
      "city":           str,
      "base_date":      str,           # as supplied
      "review_date":    str,           # as supplied
      "base_quarter":   str,           # e.g. "2021-Q2"
      "review_quarter": str,           # e.g. "2024-Q2"
      "base_value":     float | None,
      "review_value":   float | None,
      "change_pct":     float | None,  # deterministic % change
      "new_rent_multiplier": float | None,  # 1 + change_pct/100
      "quarters_elapsed": int,
      "annualised_pct": float | None,  # change_pct / years for comparison
      "source_url":     str,
      "error":          str | None,
    }
    """
    jur = jurisdiction.upper()

    try:
        base_q   = date_to_quarter(base_date)
        review_q = date_to_quarter(review_date)
    except (ValueError, TypeError) as e:
        return {
            "ok": False, "jurisdiction": jur, "city": "",
            "base_date": base_date, "review_date": review_date,
            "base_quarter": None, "review_quarter": None,
            "base_value": None, "review_value": None,
            "change_pct": None, "new_rent_multiplier": None,
            "quarters_elapsed": 0, "annualised_pct": None,
            "source_url": "", "error": f"Date parse error: {e}",
        }

    snap = get_historical_snapshot(jur, series_override)
    city = snap.get("city", jur)
    source_url = snap.get("source_url", "")

    if not snap["ok"]:
        return {
            "ok": False, "jurisdiction": jur, "city": city,
            "base_date": base_date, "review_date": review_date,
            "base_quarter": base_q, "review_quarter": review_q,
            "base_value": None, "review_value": None,
            "change_pct": None, "new_rent_multiplier": None,
            "quarters_elapsed": 0, "annualised_pct": None,
            "source_url": source_url, "error": snap.get("error"),
        }

    base_value   = get_cpi_at_quarter(jur, base_q,   series_override)
    review_value = get_cpi_at_quarter(jur, review_q, series_override)

    if base_value is None or review_value is None:
        # Fall back to nearest available quarter
        periods = snap["periods"]
        values  = snap["values"]
        if not periods:
            return {
                "ok": False, "jurisdiction": jur, "city": city,
                "base_date": base_date, "review_date": review_date,
                "base_quarter": base_q, "review_quarter": review_q,
                "base_value": None, "review_value": None,
                "change_pct": None, "new_rent_multiplier": None,
                "quarters_elapsed": 0, "annualised_pct": None,
                "source_url": source_url, "error": "Requested quarters not in ABS historical series",
            }
        # Find nearest available quarter for each
        def _nearest(target_q: str) -> tuple[str, float]:
            sorted_pairs = sorted(zip(periods, values), key=lambda x: x[0])
            best = min(sorted_pairs, key=lambda x: abs(x[0] < target_q))  # type: ignore[arg-type]
            return best

        if base_value is None:
            base_q, base_value = _nearest(base_q)
        if review_value is None:
            review_q, review_value = _nearest(review_q)

    # Compute elapsed quarters (approximate — ABS quarters are sorted strings)
    try:
        periods = snap["periods"]
        q_from = periods.index(base_q) if base_q in periods else 0
        q_to   = periods.index(review_q) if review_q in periods else len(periods) - 1
        quarters_elapsed = max(0, q_to - q_from)
    except Exception:
        quarters_elapsed = 0

    change_pct = compute_adjustment(base_value, review_value)
    multiplier = round(1 + change_pct / 100, 6)
    years_elapsed = quarters_elapsed / 4 if quarters_elapsed else None
    annualised = round(change_pct / years_elapsed, 2) if years_elapsed else None

    result = {
        "ok":               True,
        "jurisdiction":     jur,
        "city":             city,
        "base_date":        base_date,
        "review_date":      review_date,
        "base_quarter":     base_q,
        "review_quarter":   review_q,
        "base_value":       base_value,
        "review_value":     review_value,
        "change_pct":       change_pct,
        "new_rent_multiplier": multiplier,
        "quarters_elapsed": quarters_elapsed,
        "annualised_pct":   annualised,
        "source_url":       source_url,
        "error":            None,
    }

    logger.info(
        f"[CPI-AQ13] {jur} {base_q}→{review_q}: "
        f"{base_value:.1f}→{review_value:.1f} = {change_pct:+.2f}% "
        f"({quarters_elapsed}q, annualised {annualised:+.2f}%/yr)"
        if annualised else
        f"[CPI-AQ13] {jur} {base_q}→{review_q}: {change_pct:+.2f}%"
    )
    return result


def format_review_for_prompt(review: dict) -> str:
    """
    AQ-NEW-13: Format a compute_cpi_review() result for injection into a Claude prompt.

    Same mandatory-instruction pattern as format_for_prompt() but includes the
    specific base-date-to-review-date calculation rather than a trailing window.

    Returns empty string if review["ok"] is False.
    """
    if not review.get("ok"):
        return ""

    r = review
    box_top = "╔" + "═" * 73 + "╗"
    box_bot = "╚" + "═" * 73 + "╝"
    lines = [
        f"╔══ VERIFIED ABS CPI DATA — POINT-IN-TIME REVIEW — DO NOT RECALCULATE ════╗",
        f"  Source:         Australian Bureau of Statistics SDMX API",
        f"  City/Region:    {r['city']}",
        f"  Series:         All Groups CPI — quarterly index numbers",
        f"  Data URL:       {r['source_url']}",
        "",
        f"  CPI REVIEW PERIOD:",
        f"    Base date:      {r['base_date']}  ({r['base_quarter']})  →  index {r['base_value']:.1f}",
        f"    Review date:    {r['review_date']}  ({r['review_quarter']})  →  index {r['review_value']:.1f}",
        f"    Quarters:       {r['quarters_elapsed']} ({r['quarters_elapsed']/4:.1f} years)",
        "",
        f"  DETERMINISTIC CALCULATION:",
        f"    ({r['review_value']:.1f} ÷ {r['base_value']:.1f} − 1) × 100",
        f"    = {r['change_pct']:+.2f}%  total CPI movement over this period",
    ]
    if r.get("annualised_pct") is not None:
        lines.append(f"    = {r['annualised_pct']:+.2f}%  annualised average")
    lines += [
        f"    Rent multiplier: × {r['new_rent_multiplier']:.6f}",
        "",
        "  MANDATORY INSTRUCTION TO CLAUDE:",
        f"  Use ONLY {r['change_pct']:+.2f}% as the CPI benchmark for this rent review period.",
        f"  The base is {r['base_date']} (NOT the prior year). This is a cumulative calculation.",
        "  Do NOT estimate, guess, or source CPI data from training knowledge.",
        "  If the landlord has applied a higher percentage, flag it as an overcharge.",
        f"{box_bot}",
    ]
    return "\n".join(lines)
