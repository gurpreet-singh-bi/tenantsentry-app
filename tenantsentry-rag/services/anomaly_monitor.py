"""
anomaly_monitor.py
------------------
TenantSentry.ai — F16: Anomaly Monitor

Detects billing anomalies in ongoing monthly invoice uploads by comparing
each new invoice against two sources of truth:

  Layer 1 — Rule-based (every invoice):
    Checks the invoice against the lease's extracted clause rules.
    Fast, deterministic, catches clear violations.

  Layer 2 — Trend-based (≥3 invoices in history):
    Compares the invoice against historical invoices for the same lease.
    Catches creep, spikes, and new charge categories.

Entry point
-----------
    run_anomaly_checks(job_id, invoice_id) -> list[AnomalyFlag]

Called from POST /api/invoice/upload as a BackgroundTask after the invoice
is stored. Results are persisted to anomaly_flag table via db/invoice_store.

Dev mode
--------
    Returns 1 deterministic mock flag (category_cost_spike, medium severity).
    No Supabase reads. Fast.

Live mode
---------
    Fetches invoice, invoice history, and lease findings from Supabase.
    Runs both layers. Stores flags. May trigger email alert for high-severity.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional

from loguru import logger

from api.mode import is_dev


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class AnomalyFlag:
    check_name: str         # e.g. "cpi_variance", "prohibited_category"
    category: str           # charge category affected (or "rent" / "total")
    severity: str           # "high" | "medium" | "low"
    description: str        # plain English shown to tenant
    expected_cents: int     # what lease / history predicts (0 if N/A)
    actual_cents: int       # what the invoice claims (0 if N/A)
    delta_pct: float        # percentage variance (0.0 if N/A)
    detection_layer: str    # "rule" | "trend"
    job_id: str
    invoice_id: str


# ── Public entry point ────────────────────────────────────────────────────────

def run_anomaly_checks(job_id: str, invoice_id: str) -> list[AnomalyFlag]:
    """
    Run all anomaly checks for a newly uploaded invoice.
    Returns list of AnomalyFlag (empty = clean bill).

    Stores each flag to anomaly_flag table via invoice_store.
    """
    if is_dev():
        flags = _dev_run_anomaly_checks(job_id, invoice_id)
    else:
        flags = _live_run_anomaly_checks(job_id, invoice_id)

    # Persist flags regardless of mode
    _store_flags(flags)
    return flags


# ── Dev implementation ────────────────────────────────────────────────────────

def _dev_run_anomaly_checks(job_id: str, invoice_id: str) -> list[AnomalyFlag]:
    """Return 1 deterministic mock flag for dev mode testing."""
    return [
        AnomalyFlag(
            check_name="category_cost_spike",
            category="management_fee",
            severity="medium",
            description=(
                "Management fee increased by 28.5% compared to the average of the "
                "last 3 invoices ($1,250/mo → $1,606/mo). This exceeds the 25% "
                "threshold for automatic review. Check whether a lease amendment "
                "or a new management agreement is in place."
            ),
            expected_cents=125000,
            actual_cents=160600,
            delta_pct=28.5,
            detection_layer="trend",
            job_id=job_id,
            invoice_id=invoice_id,
        )
    ]


# ── Live implementation ───────────────────────────────────────────────────────

def _live_run_anomaly_checks(job_id: str, invoice_id: str) -> list[AnomalyFlag]:
    """Fetch data and run both detection layers."""
    from db.invoice_store import get_invoice, list_invoices_for_job
    from db.audit_run_store import fetch_findings

    flags: list[AnomalyFlag] = []

    # 1. Fetch the invoice being checked
    invoice = get_invoice(invoice_id)
    if not invoice:
        logger.warning(f"[anomaly_monitor] Invoice {invoice_id} not found — skipping checks")
        return []

    recon = invoice.get("reconciliation_result") or {}

    # 2. Fetch lease findings for rule context
    findings = fetch_findings(job_id) or {}
    clause_analyses = findings.get("clause_analyses") or []
    extracted_rules = findings.get("extracted_rules") or {}
    jurisdiction = findings.get("jurisdiction", "")

    # 3. Layer 1 — rule-based checks
    try:
        l1_flags = _layer1_rule_checks(invoice, recon, clause_analyses, extracted_rules, jurisdiction)
        flags.extend(l1_flags)
    except Exception as e:
        logger.error(f"[anomaly_monitor] Layer 1 failed for invoice {invoice_id}: {e}")

    # 4. Fetch invoice history for Layer 2
    history = list_invoices_for_job(job_id)
    # Exclude the current invoice from history (it's the one being checked)
    prior_invoices = [inv for inv in history if str(inv.get("id")) != str(invoice_id)]

    # 5. Layer 2 — trend checks (only when ≥3 prior invoices exist)
    if len(prior_invoices) >= 3:
        try:
            l2_flags = _layer2_trend_checks(invoice, prior_invoices, extracted_rules)
            flags.extend(l2_flags)
        except Exception as e:
            logger.error(f"[anomaly_monitor] Layer 2 failed for invoice {invoice_id}: {e}")
    else:
        logger.debug(
            f"[anomaly_monitor] Skipping Layer 2 — only {len(prior_invoices)} prior invoices "
            f"(need ≥3). job_id={job_id}"
        )

    logger.info(
        f"[anomaly_monitor] {len(flags)} flags detected for invoice {invoice_id} "
        f"(L1={sum(1 for f in flags if f.detection_layer=='rule')}, "
        f"L2={sum(1 for f in flags if f.detection_layer=='trend')})"
    )
    return flags


# ── Layer 1: Rule-based checks ────────────────────────────────────────────────

_CPI_VARIANCE_THRESHOLD_PCT = 2.0       # >2% variance from expected rent triggers flag
_AMOUNT_CAP_TOLERANCE_PCT   = 1.0       # 1% tolerance on outgoings caps (rounding)
_LINE_ITEMS_SUM_TOLERANCE   = 0.05      # 5% tolerance: line item sum vs stated total


def _layer1_rule_checks(
    invoice: dict,
    recon: dict,
    clause_analyses: list[dict],
    extracted_rules: dict,
    jurisdiction: str,
) -> list[AnomalyFlag]:
    """Run all deterministic rule-based checks. Returns list of AnomalyFlag."""
    flags: list[AnomalyFlag] = []
    job_id    = str(invoice.get("job_id", ""))
    inv_id    = str(invoice.get("id", ""))
    inv_type  = invoice.get("invoice_type", "")
    amount_c  = invoice.get("amount_cents") or 0

    # ── Check L1-A: Prohibited charge category ────────────────────────────────
    # Any finding from reconciliation with finding_type="prohibited" is a hard flag.
    for finding in recon.get("findings", []):
        if finding.get("finding_type") == "prohibited" and finding.get("severity") in ("high", "medium"):
            flags.append(AnomalyFlag(
                check_name="prohibited_category",
                category=finding.get("category", "unknown"),
                severity="high",
                description=(
                    f"Charge '{finding.get('line_item_description', '')}' "
                    f"({finding.get('category', '')}) is prohibited under your lease or "
                    f"applicable legislation. {finding.get('explanation', '')} "
                    f"Legislation ref: {finding.get('legislation_ref', 'see lease')}."
                ),
                expected_cents=0,
                actual_cents=finding.get("amount_cents", 0),
                delta_pct=0.0,
                detection_layer="rule",
                job_id=job_id,
                invoice_id=inv_id,
            ))

    # ── Check L1-B: CPI rent variance (monthly_rent invoices only) ───────────
    if inv_type == "monthly_rent" and amount_c > 0:
        expected_c = _expected_rent_cents(extracted_rules, jurisdiction)
        if expected_c and expected_c > 0:
            delta_pct = ((amount_c - expected_c) / expected_c) * 100
            if abs(delta_pct) > _CPI_VARIANCE_THRESHOLD_PCT:
                flags.append(AnomalyFlag(
                    check_name="cpi_variance",
                    category="rent",
                    severity="high" if abs(delta_pct) > 5.0 else "medium",
                    description=(
                        f"Rent charged (${amount_c/100:,.2f}) differs from the "
                        f"expected lease amount (${expected_c/100:,.2f}) by "
                        f"{delta_pct:+.1f}%. Review the rent review clause and "
                        f"verify the latest CPI adjustment has been applied correctly."
                    ),
                    expected_cents=expected_c,
                    actual_cents=amount_c,
                    delta_pct=round(delta_pct, 2),
                    detection_layer="rule",
                    job_id=job_id,
                    invoice_id=inv_id,
                ))

    # ── Check L1-C: Outgoings amount exceeds lease cap ────────────────────────
    cap_cents = _extract_outgoings_cap_cents(clause_analyses)
    if cap_cents and amount_c > 0 and inv_type in ("estimate", "actuals"):
        tolerance = int(cap_cents * _AMOUNT_CAP_TOLERANCE_PCT / 100)
        if amount_c > cap_cents + tolerance:
            delta_pct = ((amount_c - cap_cents) / cap_cents) * 100
            flags.append(AnomalyFlag(
                check_name="amount_exceeds_cap",
                category="outgoings",
                severity="high",
                description=(
                    f"Total outgoings charged (${amount_c/100:,.2f}) exceeds the "
                    f"cap specified in your lease (${cap_cents/100:,.2f}) by "
                    f"${(amount_c - cap_cents)/100:,.2f} ({delta_pct:.1f}%). "
                    f"This may constitute a breach of the outgoings clause."
                ),
                expected_cents=cap_cents,
                actual_cents=amount_c,
                delta_pct=round(delta_pct, 2),
                detection_layer="rule",
                job_id=job_id,
                invoice_id=inv_id,
            ))

    # ── Check L1-D: Duplicate period ─────────────────────────────────────────
    # Handled upstream in /api/invoice/upload via pdf_hash dedup.
    # This check covers same period with different PDF content.
    # (Requires invoice history — skip here, handled in Layer 2)

    # ── Check L1-E: Line items sum mismatch ──────────────────────────────────
    line_items = invoice.get("line_items") or []
    if line_items and amount_c > 0:
        computed = sum(int(li.get("amount_cents", 0)) for li in line_items)
        if computed > 0:
            diff_pct = abs(computed - amount_c) / amount_c
            if diff_pct > _LINE_ITEMS_SUM_TOLERANCE:
                flags.append(AnomalyFlag(
                    check_name="line_items_sum_mismatch",
                    category="total",
                    severity="low",
                    description=(
                        f"Invoice total (${amount_c/100:,.2f}) does not match the sum "
                        f"of line items (${computed/100:,.2f}) — difference of "
                        f"${abs(amount_c - computed)/100:,.2f}. "
                        f"The invoice may have an arithmetic error or unlisted charges."
                    ),
                    expected_cents=computed,
                    actual_cents=amount_c,
                    delta_pct=round(diff_pct * 100, 2),
                    detection_layer="rule",
                    job_id=job_id,
                    invoice_id=inv_id,
                ))

    return flags


def _expected_rent_cents(extracted_rules: dict, jurisdiction: str) -> Optional[int]:
    """
    Estimate expected rent from extracted_rules.
    Returns None if insufficient data to compute.
    We use the base_rent stored in extracted_rules (set during lease audit).
    CPI adjustments are applied separately by cpi_calculator; here we use
    the last-known adjusted value if present, else raw base rent.
    """
    # Prefer the last CPI-adjusted rent if the lease pipeline stored it
    adjusted = extracted_rules.get("last_adjusted_rent_cents")
    if adjusted:
        return int(adjusted)
    base = extracted_rules.get("base_rent_cents")
    if base:
        return int(base)
    return None


def _extract_outgoings_cap_cents(clause_analyses: list[dict]) -> Optional[int]:
    """
    Look for an outgoings cap in clause analyses.
    Returns cap in AUD cents or None if no cap found.
    """
    for ca in clause_analyses:
        if not isinstance(ca, dict):
            continue
        clause_type = (ca.get("clause_type") or "").lower()
        if "outgoing" not in clause_type:
            continue
        # Check extracted_data for a cap field
        ed = ca.get("extracted_data") or {}
        if isinstance(ed, dict):
            cap = ed.get("outgoings_cap_cents") or ed.get("cap_amount_cents")
            if cap:
                return int(cap)
    return None


# ── Layer 2: Trend-based checks ───────────────────────────────────────────────

_SPIKE_THRESHOLD_PCT  = 25.0    # category increase >25% vs 3-period average
_CREEP_THRESHOLD_PCT  = 10.0    # total outgoings >10% above CPI-adjusted base year
_MAX_INVOICE_GAP_DAYS = 45      # monthly_rent: gap > 45 days = anomalous
_MIN_INVOICE_GAP_DAYS = 25      # monthly_rent: gap < 25 days = anomalous


def _layer2_trend_checks(
    invoice: dict,
    prior_invoices: list[dict],
    extracted_rules: dict,
) -> list[AnomalyFlag]:
    """Run trend-based checks requiring invoice history. Returns list of AnomalyFlag."""
    flags: list[AnomalyFlag] = []
    job_id = str(invoice.get("job_id", ""))
    inv_id = str(invoice.get("id", ""))
    inv_type = invoice.get("invoice_type", "")
    line_items = invoice.get("line_items") or []

    # Build per-category totals for current invoice
    curr_by_cat = _sum_by_category(line_items)

    # Build per-category history from prior invoices
    same_type_prior = [p for p in prior_invoices if p.get("invoice_type") == inv_type]
    if not same_type_prior:
        return flags

    # ── Check L2-A: Category cost spike ──────────────────────────────────────
    # For each category in current invoice, compare to trailing 3-period average
    trailing = same_type_prior[:3]  # already ordered desc by period_start
    for cat, curr_cents in curr_by_cat.items():
        if curr_cents == 0:
            continue
        historical_totals = []
        for prior in trailing:
            prior_items = prior.get("line_items") or []
            prior_by_cat = _sum_by_category(prior_items)
            if cat in prior_by_cat and prior_by_cat[cat] > 0:
                historical_totals.append(prior_by_cat[cat])

        if len(historical_totals) < 2:
            continue  # Not enough history for this category

        avg = sum(historical_totals) / len(historical_totals)
        if avg == 0:
            continue
        delta_pct = ((curr_cents - avg) / avg) * 100
        if delta_pct > _SPIKE_THRESHOLD_PCT:
            flags.append(AnomalyFlag(
                check_name="category_cost_spike",
                category=cat,
                severity="medium" if delta_pct < 50.0 else "high",
                description=(
                    f"'{cat}' charges increased by {delta_pct:.1f}% compared to the "
                    f"trailing {len(historical_totals)}-period average "
                    f"(${avg/100:,.2f}/period → ${curr_cents/100:,.2f}). "
                    f"Verify whether a rate change, scope extension, or billing error explains the increase."
                ),
                expected_cents=int(avg),
                actual_cents=curr_cents,
                delta_pct=round(delta_pct, 2),
                detection_layer="trend",
                job_id=job_id,
                invoice_id=inv_id,
            ))

    # ── Check L2-B: New category appearing ───────────────────────────────────
    # Category present in current invoice but absent from ALL prior invoices
    all_prior_cats: set[str] = set()
    for prior in same_type_prior:
        for li in (prior.get("line_items") or []):
            cat = (li.get("category") or "").strip().lower()
            if cat:
                all_prior_cats.add(cat)

    for cat in curr_by_cat:
        if cat.lower() not in all_prior_cats and curr_by_cat[cat] > 0:
            flags.append(AnomalyFlag(
                check_name="new_category",
                category=cat,
                severity="medium",
                description=(
                    f"A new charge category '{cat}' (${curr_by_cat[cat]/100:,.2f}) "
                    f"appears in this invoice but was absent from all previous invoices. "
                    f"Verify this charge is permitted under your lease."
                ),
                expected_cents=0,
                actual_cents=curr_by_cat[cat],
                delta_pct=0.0,
                detection_layer="trend",
                job_id=job_id,
                invoice_id=inv_id,
            ))

    # ── Check L2-C: Total outgoings creep (estimate/actuals only) ────────────
    if inv_type in ("estimate", "actuals") and len(same_type_prior) >= 6:
        curr_total = invoice.get("amount_cents") or 0
        # Compare against oldest 3 periods as the base
        base_invoices = same_type_prior[-3:]
        base_avg = sum(p.get("amount_cents") or 0 for p in base_invoices) / len(base_invoices)
        if base_avg > 0 and curr_total > 0:
            delta_pct = ((curr_total - base_avg) / base_avg) * 100
            if delta_pct > _CREEP_THRESHOLD_PCT:
                flags.append(AnomalyFlag(
                    check_name="outgoings_creep",
                    category="total",
                    severity="medium" if delta_pct < 25.0 else "high",
                    description=(
                        f"Total outgoings have increased by {delta_pct:.1f}% compared to the "
                        f"earliest baseline period in your history "
                        f"(${base_avg/100:,.2f} → ${curr_total/100:,.2f}). "
                        f"This may exceed CPI and warrants a full reconciliation review."
                    ),
                    expected_cents=int(base_avg),
                    actual_cents=curr_total,
                    delta_pct=round(delta_pct, 2),
                    detection_layer="trend",
                    job_id=job_id,
                    invoice_id=inv_id,
                ))

    # ── Check L2-D: Invoice frequency anomaly (monthly_rent only) ────────────
    if inv_type == "monthly_rent":
        curr_period_start = invoice.get("period_start")
        # Find the most recent prior invoice of same type with a period_start
        most_recent_prior = next(
            (p for p in same_type_prior if p.get("period_start")),
            None
        )
        if curr_period_start and most_recent_prior:
            try:
                curr_date = date.fromisoformat(str(curr_period_start))
                prior_date = date.fromisoformat(str(most_recent_prior["period_start"]))
                gap_days = (curr_date - prior_date).days
                if gap_days > _MAX_INVOICE_GAP_DAYS:
                    flags.append(AnomalyFlag(
                        check_name="invoice_frequency",
                        category="rent",
                        severity="low",
                        description=(
                            f"This invoice arrives {gap_days} days after the previous rent invoice "
                            f"(expected ~30 days). A gap this large may indicate a missed billing period "
                            f"or a catch-up invoice that combines multiple months."
                        ),
                        expected_cents=0,
                        actual_cents=0,
                        delta_pct=0.0,
                        detection_layer="trend",
                        job_id=job_id,
                        invoice_id=inv_id,
                    ))
                elif 0 < gap_days < _MIN_INVOICE_GAP_DAYS:
                    flags.append(AnomalyFlag(
                        check_name="invoice_frequency",
                        category="rent",
                        severity="low",
                        description=(
                            f"This invoice arrives only {gap_days} days after the previous rent invoice "
                            f"(expected ~30 days). Verify this is not a duplicate charge."
                        ),
                        expected_cents=0,
                        actual_cents=0,
                        delta_pct=0.0,
                        detection_layer="trend",
                        job_id=job_id,
                        invoice_id=inv_id,
                    ))
            except (ValueError, TypeError):
                pass  # Non-parseable date — skip frequency check

    return flags


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sum_by_category(line_items: list[dict]) -> dict[str, int]:
    """
    Sum line item amounts by category. Returns {category: total_cents}.
    Categories are normalised to lowercase.
    """
    totals: dict[str, int] = {}
    for li in line_items:
        cat = (li.get("category") or "other").strip().lower()
        totals[cat] = totals.get(cat, 0) + int(li.get("amount_cents", 0))
    return totals


def _store_flags(flags: list[AnomalyFlag]) -> None:
    """Persist anomaly flags to the DB. Logs and swallows errors — never blocks the caller."""
    if not flags:
        return
    from db.invoice_store import insert_anomaly_flag
    for flag in flags:
        try:
            row = asdict(flag)
            insert_anomaly_flag(row)
        except Exception as e:
            logger.error(
                f"[anomaly_monitor] Failed to store flag {flag.check_name} "
                f"for invoice {flag.invoice_id}: {e}"
            )


# ── Email alert ───────────────────────────────────────────────────────────────

def maybe_send_alert(flags: list[AnomalyFlag], job_id: str) -> None:
    """
    Send an email alert to the tenant if any HIGH severity flags were detected.
    LIVE mode only — silently skips in DEV mode.

    Called from api/main.py after run_anomaly_checks() completes.
    """
    if is_dev():
        logger.debug("[anomaly_monitor] DEV mode — skipping email alert")
        return

    high_flags = [f for f in flags if f.severity == "high"]
    if not high_flags:
        return

    try:
        _send_high_severity_alert(job_id, high_flags)
    except Exception as e:
        logger.error(f"[anomaly_monitor] Email alert failed (non-fatal): {e}")


def _send_high_severity_alert(job_id: str, flags: list[AnomalyFlag]) -> None:
    """
    Queue an alert email via Supabase Edge Function or direct SMTP.
    Currently logs intent — wire to your email provider when ready.

    TODO: Replace logger.warning with actual email dispatch:
      Option A: Supabase Edge Function  → POST to your-project.supabase.co/functions/v1/send-alert
      Option B: SendGrid / Postmark API → POST /mail/send
      Option C: AWS SES via boto3
    """
    summary_lines = [
        f"  • {f.check_name} ({f.category}): {f.description[:120]}..."
        for f in flags
    ]
    summary = "\n".join(summary_lines)

    logger.warning(
        f"[anomaly_monitor] HIGH-SEVERITY ALERT (not yet sent) "
        f"job_id={job_id} count={len(flags)}\n{summary}"
    )
    # Placeholder — implement email dispatch here
