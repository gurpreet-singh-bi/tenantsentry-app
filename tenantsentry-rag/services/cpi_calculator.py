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

import httpx
from loguru import logger

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
        f"  {s['change_pct']:+.2f}%, flag it as an overcharge with this ABS figure as evidence.",
        "╚═════════════════════════════════════════════════════════════════════════╝",
    ]
    return "\n".join(lines)
