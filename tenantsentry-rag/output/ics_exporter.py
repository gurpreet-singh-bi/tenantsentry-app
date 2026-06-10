"""
ics_exporter.py
---------------
AQ-NEW-9: Critical Dates Calendar Output (RFC 5545 .ics)

Converts extracted lease dates (from date_extractor.py) into a downloadable
iCalendar (.ics) file that the tenant can import into any calendar application
(Outlook, Google Calendar, Apple Calendar, etc.).

Each lease date becomes a VEVENT with:
  - SUMMARY: plain-English date description
  - DTSTART: the specific date (or omitted for relative-only dates)
  - DESCRIPTION: full context including clause reference and notes
  - ALARM: advance warning triggered alert_days_before the date
  - CATEGORIES: lease-type classification for calendar filtering

Dates with no date_value (relative-only, like dispute windows) are included
as a special "Watch For" all-day event anchored to TODAY, noting that the
clock starts when the tenant receives the landlord's notice.

DEV/LIVE: Pure data transformation — no LLM calls, no external dependencies.
Works transparently in both modes (uses whatever dates were extracted or mocked).

Public API
----------
    generate_ics(lease_dates, tenant_name, jurisdiction, job_id) -> bytes
    generate_ics_from_result(result, job_id) -> bytes
"""

import re
import uuid
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from loguru import logger


# ── ICS escaping (RFC 5545 §3.3.11) ──────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape a string for use in an iCalendar text field."""
    if not s:
        return ""
    s = s.replace("\\", "\\\\")
    s = s.replace(";",  "\\;")
    s = s.replace(",",  "\\,")
    s = s.replace("\n", "\\n")
    s = s.replace("\r", "")
    return s


def _fold(line: str) -> str:
    """
    RFC 5545 line folding: lines longer than 75 octets must be folded with
    CRLF + SPACE. We fold at 72 chars (leaving room for safety).
    """
    if len(line) <= 75:
        return line + "\r\n"
    result = []
    while len(line) > 72:
        result.append(line[:72])
        line = " " + line[72:]
    result.append(line)
    return "\r\n".join(result) + "\r\n"


def _ics_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to YYYYMMDD (ICS all-day DATE format)."""
    return iso_date.replace("-", "")


def _ics_datetime(dt: datetime) -> str:
    """Convert datetime to YYYYMMDDTHHMMSSZ (ICS UTC datetime format)."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ")


def _now_ics() -> str:
    return _ics_datetime(datetime.now(timezone.utc))


# ── Date type → calendar category mapping ─────────────────────────────────────

_DATE_TYPE_CATEGORY: dict[str, str] = {
    "lease_commencement":                  "Lease Milestones",
    "lease_expiry":                        "Lease Milestones",
    "option_exercise_deadline":            "Critical Deadlines",
    "rent_review_cpi":                     "Rent Reviews",
    "rent_review_market":                  "Rent Reviews",
    "rent_review_fixed":                   "Rent Reviews",
    "outgoings_reconciliation":            "Outgoings",
    "rent_free_end":                       "Lease Milestones",
    "fitout_completion_deadline":          "Critical Deadlines",
    "demolition_notice_window":            "Critical Deadlines",
    "bank_guarantee_expiry":               "Critical Deadlines",
    "make_good_deadline":                  "Exit & Make-Good",
    "rent_review_market_dispute_deadline": "Critical Deadlines",
    "other":                               "Lease Dates",
}

# Emoji prefix per category for calendar display
_CATEGORY_EMOJI: dict[str, str] = {
    "Critical Deadlines": "⚠️",
    "Rent Reviews":       "💰",
    "Outgoings":          "📊",
    "Exit & Make-Good":   "🔑",
    "Lease Milestones":   "📅",
    "Lease Dates":        "📋",
}


# ── VEVENT builder ────────────────────────────────────────────────────────────

def _build_vevent(
    date_dict: dict,
    tenant_name: str,
    jurisdiction: str,
    today: date,
) -> str:
    """
    Build a single VEVENT string for one lease date.

    Args:
        date_dict:    Dict with keys: date_type, date_description, date_value,
                      clause_reference, recurrence, alert_days_before, notes
        tenant_name:  For UID and DESCRIPTION
        jurisdiction: State code
        today:        Reference date for relative events

    Returns:
        RFC 5545 VEVENT string (CRLF line endings, folded)
    """
    dtype        = date_dict.get("date_type", "other")
    description  = date_dict.get("date_description", "Unknown date")
    date_value   = date_dict.get("date_value")   # ISO YYYY-MM-DD or None
    clause_ref   = date_dict.get("clause_reference") or ""
    recurrence   = date_dict.get("recurrence")   # "annual" | "monthly" | None
    alert_days   = int(date_dict.get("alert_days_before", 90))
    notes        = date_dict.get("notes") or ""

    category   = _DATE_TYPE_CATEGORY.get(dtype, "Lease Dates")
    emoji      = _CATEGORY_EMOJI.get(category, "📋")
    summary    = f"{emoji} {description} — {tenant_name} ({jurisdiction})"

    # Build long description
    desc_parts = [
        f"Lease: {tenant_name} ({jurisdiction})",
        f"Date type: {dtype.replace('_', ' ').title()}",
        f"Category: {category}",
    ]
    if clause_ref:
        desc_parts.append(f"Clause reference: {clause_ref}")
    if notes:
        desc_parts.append(f"Notes: {notes}")
    if alert_days > 0:
        desc_parts.append(f"Alert: {alert_days} days advance warning set")
    if dtype == "rent_review_market_dispute_deadline":
        desc_parts.append(
            "IMPORTANT: This is a RELATIVE deadline — it starts when you RECEIVE "
            "the landlord's market rent notice. The clock starts on receipt, not this event. "
            "Missing this window forfeits your right to dispute the rent."
        )

    full_description = "\\n".join(_esc(p) for p in desc_parts)
    uid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"tenantsentry.{tenant_name}.{dtype}.{date_value or 'relative'}"))
    dtstamp = _now_ics()

    lines = []
    lines.append("BEGIN:VEVENT")
    lines.append(f"UID:{uid}")
    lines.append(f"DTSTAMP:{dtstamp}")
    lines.append(f"SUMMARY:{_esc(summary)}")
    lines.append(f"DESCRIPTION:{full_description}")
    lines.append(f"CATEGORIES:{category}")

    if date_value:
        # Concrete date — standard all-day event
        dtstart = _ics_date(date_value)
        lines.append(f"DTSTART;VALUE=DATE:{dtstart}")
        lines.append(f"DTEND;VALUE=DATE:{dtstart}")   # all-day = same start/end in ICS

        # Recurrence rule
        if recurrence == "annual":
            lines.append("RRULE:FREQ=YEARLY")
        elif recurrence == "monthly":
            lines.append("RRULE:FREQ=MONTHLY")

        # VALARM: advance warning
        if alert_days > 0:
            alarm_trigger = f"-P{alert_days}D"  # ISO 8601 duration
            lines += [
                "BEGIN:VALARM",
                "TRIGGER:" + alarm_trigger,
                "ACTION:DISPLAY",
                f"DESCRIPTION:⚠️ {alert_days}-day reminder: {_esc(description)}",
                "END:VALARM",
            ]
        # Second alarm on the day itself for HIGH-priority deadlines
        if alert_days >= 90 or dtype in ("option_exercise_deadline", "rent_review_market_dispute_deadline"):
            lines += [
                "BEGIN:VALARM",
                "TRIGGER:PT9H",   # 9am on the day
                "ACTION:DISPLAY",
                f"DESCRIPTION:🚨 TODAY: {_esc(description)} — action required!",
                "END:VALARM",
            ]

    else:
        # Relative-only date — anchor to today as a "Watch For" event
        dtstart = today.strftime("%Y%m%d")
        lines.append(f"DTSTART;VALUE=DATE:{dtstart}")
        lines.append(f"DTEND;VALUE=DATE:{dtstart}")
        # Mark as a watch-for reminder, not a fixed deadline
        lines.append("TRANSP:TRANSPARENT")   # Does not block time in busy view
        # One immediate alarm to ensure tenant is aware
        lines += [
            "BEGIN:VALARM",
            "TRIGGER:PT0S",  # fires immediately when calendar is opened
            "ACTION:DISPLAY",
            f"DESCRIPTION:📌 RELATIVE DEADLINE: {_esc(description)} — check notes.",
            "END:VALARM",
        ]

    lines.append("END:VEVENT")

    return "".join(_fold(line) for line in lines)


# ── Main generator ────────────────────────────────────────────────────────────

def generate_ics(
    lease_dates: list[dict],
    tenant_name: str,
    jurisdiction: str,
    job_id: Optional[str] = None,
) -> bytes:
    """
    AQ-NEW-9: Generate an RFC 5545 iCalendar file from a list of lease dates.

    Args:
        lease_dates:  List of date dicts from date_extractor.extract_dates()
        tenant_name:  Tenant name for event summaries and calendar name
        jurisdiction: State code
        job_id:       For log correlation

    Returns:
        .ics file content as UTF-8 bytes, ready for download.
    """
    if not lease_dates:
        logger.warning(f"[{job_id}] ics_exporter: no dates provided — generating empty calendar")

    today = date.today()
    cal_name = f"TenantSentry — {tenant_name} ({jurisdiction}) Lease Dates"

    # Calendar header
    header_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TenantSentry AI//Lease Date Monitor//EN",
        f"X-WR-CALNAME:{_esc(cal_name)}",
        "X-WR-TIMEZONE:Australia/Sydney",
        "X-WR-CALDESC:Critical lease dates and deadlines extracted by TenantSentry AI audit. "
        "Import into Outlook\\, Google Calendar\\, or Apple Calendar.",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    # Sort dates: concrete dates first (by date_value), then relative
    def _sort_key(d: dict) -> tuple:
        dv = d.get("date_value") or ""
        # alert_days_before descending within each group — most critical first
        alert = d.get("alert_days_before", 0)
        return (0 if dv else 1, dv, -alert)

    sorted_dates = sorted(lease_dates, key=_sort_key)

    # Build VEVENT blocks
    events = []
    concrete_count = 0
    relative_count = 0
    for d in sorted_dates:
        try:
            vevent = _build_vevent(d, tenant_name, jurisdiction, today)
            events.append(vevent)
            if d.get("date_value"):
                concrete_count += 1
            else:
                relative_count += 1
        except Exception as e:
            logger.warning(f"[{job_id}] ics_exporter: skipped date {d.get('date_type')!r}: {e}")

    # Footer
    footer_lines = ["END:VCALENDAR"]

    # Assemble
    all_lines = (
        "".join(_fold(line) for line in header_lines)
        + "".join(events)
        + "".join(_fold(line) for line in footer_lines)
    )

    logger.info(
        f"[{job_id}] ics_exporter: generated {len(events)} events "
        f"({concrete_count} concrete, {relative_count} relative)"
    )
    return all_lines.encode("utf-8")


def generate_ics_from_result(result: dict, job_id: Optional[str] = None) -> bytes:
    """
    Convenience wrapper that extracts lease_dates from a serialised AuditResult dict.

    Args:
        result:  Serialised AuditResult (model_dump() or equivalent)
        job_id:  For log correlation

    Returns:
        .ics bytes
    """
    lease_dates  = result.get("lease_dates", [])
    tenant_name  = result.get("tenant_name", "Unknown Tenant")
    jurisdiction = result.get("jurisdiction", "")

    # lease_dates may be LeaseDate model instances or plain dicts — normalise
    normalised = []
    for d in lease_dates:
        if isinstance(d, dict):
            normalised.append(d)
        else:
            # Pydantic model or similar — convert to dict
            try:
                normalised.append(d.model_dump())
            except AttributeError:
                try:
                    normalised.append(vars(d))
                except Exception:
                    pass

    return generate_ics(normalised, tenant_name, jurisdiction, job_id)


# ── Mock helper ───────────────────────────────────────────────────────────────

def mock_ics_bytes() -> bytes:
    """DEV mode: generate a realistic .ics from mock lease dates."""
    from services.date_extractor import _mock_dates  # type: ignore[attr-defined]
    mock_dates = _mock_dates()
    return generate_ics(
        lease_dates=mock_dates,
        tenant_name="[MOCK] Acme Pty Ltd",
        jurisdiction="WA",
        job_id="mock",
    )
