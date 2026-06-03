"""
evidence_pack.py
----------------
G6: Evidence Pack Generator

For each HIGH-severity risk flag in a completed audit, generates a ZIP bundle
containing everything a tenant needs to put a dispute letter in front of a
landlord (or their solicitor) that is legally hard to dismiss:

  evidence_pack_{job_id}_{flag_id}.zip
  ├── 01_clause_excerpt.pdf        — verbatim lease clause text + flag annotation
  ├── 02_legislative_basis.pdf     — exact Act section text(s) from bundled data
  ├── 03_cpi_verification.pdf      — ABS CPI data + deterministic math (CPI flags only)
  ├── 04_dispute_letter.pdf        — Claude-generated formal letter (draft for solicitor)
  └── README.txt                   — instructions for the tenant

A combined ZIP of all HIGH flags is also produced:
  evidence_pack_{job_id}_ALL_HIGH.zip

Public API:
    generate_evidence_packs(result: dict, job_id: str) -> str
        Returns path to the combined ZIP of all HIGH-flag bundles.

    generate_single_evidence_pack(result: dict, job_id: str, flag_id: str) -> str
        Returns path to the ZIP for one specific flag.
"""

import io
import os
import re
import json
import zipfile
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether,
)

# ── Brand colours (match report_generator) ────────────────────────────────────
NAVY     = colors.HexColor("#0f172a")
TEAL     = colors.HexColor("#0d9488")
RED      = colors.HexColor("#dc2626")
RED_LIGHT= colors.HexColor("#fee2e2")
AMBER    = colors.HexColor("#d97706")
GREEN    = colors.HexColor("#16a34a")
SLATE    = colors.HexColor("#64748b")
LIGHT_GREY = colors.HexColor("#f8fafc")
MID_GREY   = colors.HexColor("#e2e8f0")
TEXT       = colors.HexColor("#1e293b")
WHITE      = colors.white

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm

# ── Output directory ──────────────────────────────────────────────────────────
PACKS_DIR = Path(tempfile.gettempdir()) / "tenantsentry_evidence_packs"
PACKS_DIR.mkdir(exist_ok=True)

# ── ABS CPI API ───────────────────────────────────────────────────────────────
ABS_API_BASE = "https://api.data.abs.gov.au"

# CPI series keys: jurisdiction → (region_code, city_name)
# Used in ABS SDMX API: Measure.Region.Index.Type.Frequency
_JUR_CPI_REGION = {
    "NSW": ("1",     "Sydney"),
    "VIC": ("2",     "Melbourne"),
    "QLD": ("3",     "Brisbane"),
    "SA":  ("4",     "Adelaide"),
    "WA":  ("5",     "Perth"),
    "TAS": ("6",     "Hobart"),
    "NT":  ("7",     "Darwin"),
    "ACT": ("8",     "Canberra"),
}
_ALL_CITIES_REGION = ("10001", "Weighted Average of Eight Capital Cities")

# CPI flag trigger keywords — determines whether CPI verification section is generated
_CPI_KEYWORDS = {
    "cpi", "consumer price index", "rent review", "fixed increase",
    "ratchet", "market review", "annual increase", "escalation",
}


# ══════════════════════════════════════════════════════════════════════════════
# Public entry points
# ══════════════════════════════════════════════════════════════════════════════

def generate_evidence_packs(result: dict, job_id: str) -> str:
    """
    Generate evidence pack ZIPs for all HIGH-severity flags.

    Returns the path to a combined ZIP containing one sub-ZIP per HIGH flag.
    """
    high_flags = _collect_high_flags(result)
    if not high_flags:
        logger.info(f"[{job_id}] No HIGH flags — evidence pack skipped")
        return ""

    logger.info(f"[{job_id}] Generating evidence packs for {len(high_flags)} HIGH flags")

    combined_zip_path = str(PACKS_DIR / f"evidence_pack_{job_id}_ALL_HIGH.zip")
    with zipfile.ZipFile(combined_zip_path, "w", zipfile.ZIP_DEFLATED) as combined:
        for flag_id, flag, clause in high_flags:
            try:
                single_zip_bytes = _build_single_pack(
                    result=result,
                    job_id=job_id,
                    flag_id=flag_id,
                    flag=flag,
                    clause=clause,
                )
                safe_flag_id = re.sub(r"[^\w\-]", "_", flag_id)
                combined.writestr(
                    f"flag_{safe_flag_id}/evidence_pack_{safe_flag_id}.zip",
                    single_zip_bytes,
                )
                logger.info(f"[{job_id}] Evidence pack built for flag {flag_id}")
            except Exception as e:
                logger.error(f"[{job_id}] Failed to build pack for flag {flag_id}: {e}")

    logger.info(f"[{job_id}] Combined evidence pack: {combined_zip_path}")
    return combined_zip_path


def generate_single_evidence_pack(result: dict, job_id: str, flag_id: str) -> str:
    """
    Generate an evidence pack ZIP for one specific flag.

    Returns path to the ZIP file.
    """
    high_flags = _collect_high_flags(result)
    match = next(((fid, f, c) for fid, f, c in high_flags if fid == flag_id), None)
    if not match:
        # Fall back: find the flag anywhere in the result
        for ca in result.get("clause_analyses", []):
            for f in (ca.get("risk_flags") or []):
                if f.get("flag_id") == flag_id:
                    match = (flag_id, f, ca)
                    break
            if match:
                break

    if not match:
        raise ValueError(f"Flag '{flag_id}' not found in audit result")

    flag_id, flag, clause = match
    zip_bytes = _build_single_pack(result, job_id, flag_id, flag, clause)

    safe_id = re.sub(r"[^\w\-]", "_", flag_id)
    out_path = str(PACKS_DIR / f"evidence_pack_{job_id}_{safe_id}.zip")
    with open(out_path, "wb") as f:
        f.write(zip_bytes)
    return out_path


# ══════════════════════════════════════════════════════════════════════════════
# Internal builders
# ══════════════════════════════════════════════════════════════════════════════

def _collect_high_flags(result: dict) -> list[tuple[str, dict, dict]]:
    """Return [(flag_id, flag_dict, clause_dict)] for every HIGH flag."""
    items = []
    seen_ids: set[str] = set()
    for ca in result.get("clause_analyses", []):
        for flag in (ca.get("risk_flags") or []):
            if flag.get("severity") == "high":
                fid = flag.get("flag_id") or f"FLAG_{len(items)+1}"
                # Deduplicate by flag_id
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    items.append((fid, flag, ca))
    return items


def _build_single_pack(
    result: dict,
    job_id: str,
    flag_id: str,
    flag: dict,
    clause: dict,
) -> bytes:
    """Build and return the bytes of a single-flag evidence pack ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Clause excerpt PDF
        clause_pdf = _build_clause_excerpt_pdf(flag, clause, result)
        zf.writestr("01_clause_excerpt.pdf", clause_pdf)

        # 2. Legislative basis PDF
        leg_pdf = _build_legislation_pdf(flag, result)
        zf.writestr("02_legislative_basis.pdf", leg_pdf)

        # 3. CPI verification PDF (only for relevant flags)
        if _is_cpi_flag(flag, clause):
            cpi_pdf = _build_cpi_verification_pdf(flag, clause, result)
            zf.writestr("03_cpi_verification.pdf", cpi_pdf)

        # 4. Dispute letter PDF
        letter_pdf = _build_dispute_letter_pdf(flag, clause, result)
        zf.writestr("04_dispute_letter.pdf", letter_pdf)

        # 5. README
        readme = _build_readme(flag, result)
        zf.writestr("README.txt", readme)

    return buf.getvalue()


# ── Component 1: Clause Excerpt ───────────────────────────────────────────────

def _build_clause_excerpt_pdf(flag: dict, clause: dict, result: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )
    styles = getSampleStyleSheet()
    story = []

    _add_header(story, "EVIDENCE — LEASE CLAUSE EXCERPT", result)

    h2 = ParagraphStyle("H2", fontSize=13, textColor=NAVY, fontName="Helvetica-Bold", spaceAfter=4)
    body = ParagraphStyle("Body", fontSize=9, textColor=TEXT, leading=14, spaceAfter=6)
    label = ParagraphStyle("Label", fontSize=8, textColor=SLATE, fontName="Helvetica-Bold")
    flag_style = ParagraphStyle("Flag", fontSize=9, textColor=RED, leading=12,
                                 fontName="Helvetica-Bold")
    leg_style = ParagraphStyle("Leg", fontSize=8, textColor=TEAL, fontName="Helvetica-Oblique")

    story.append(Paragraph(
        f"Clause: <b>{clause.get('clause_heading', 'Unknown')}</b>", h2))
    story.append(HRFlowable(width="100%", thickness=2, color=TEAL))
    story.append(Spacer(1, 4*mm))

    # Purpose statement
    story.append(Paragraph(
        "This excerpt is the verbatim lease clause text that triggered the risk flag below. "
        "It is reproduced here as evidence of the contractual term under dispute.",
        ParagraphStyle("Intro", fontSize=8, textColor=SLATE, fontName="Helvetica-Oblique",
                       leading=12)
    ))
    story.append(Spacer(1, 4*mm))

    # Clause type + key terms
    clause_type = clause.get("clause_type", "")
    key_terms = clause.get("key_terms", [])
    if clause_type or key_terms:
        meta_rows = []
        if clause_type:
            meta_rows.append(["Clause type:", clause_type])
        if key_terms:
            meta_rows.append(["Key terms:", " · ".join(key_terms[:8])])
        meta_tbl = Table(meta_rows, colWidths=[35*mm, 135*mm])
        meta_tbl.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("TEXTCOLOR", (0, 0), (-1, -1), TEXT),
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GREY),
            ("TOPPADDING", (0, 0), (-1, -1), 2*mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2*mm),
            ("LEFTPADDING", (0, 0), (-1, -1), 3*mm),
            ("BOX", (0, 0), (-1, -1), 0.5, MID_GREY),
        ]))
        story.append(meta_tbl)
        story.append(Spacer(1, 4*mm))

    # Verbatim clause text in a "document" styled box
    story.append(Paragraph("VERBATIM LEASE CLAUSE TEXT", label))
    story.append(Spacer(1, 2*mm))

    clause_text = clause.get("clause_text", "(clause text not available)")
    clause_text_escaped = clause_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    clause_para = Paragraph(
        clause_text_escaped,
        ParagraphStyle("ClauseBody", fontSize=9, textColor=TEXT, leading=14,
                       fontName="Courier", backColor=colors.HexColor("#f8f4ee"))
    )
    clause_box = Table([[clause_para]], colWidths=[170*mm])
    clause_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8f4ee")),
        ("BOX", (0, 0), (-1, -1), 1, MID_GREY),
        ("TOPPADDING", (0, 0), (-1, -1), 4*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4*mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 4*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4*mm),
    ]))
    story.append(clause_box)
    story.append(Spacer(1, 6*mm))

    # Risk flag callout box
    story.append(Paragraph("IDENTIFIED RISK FLAG", label))
    story.append(Spacer(1, 2*mm))

    flag_desc = flag.get("description", "")
    flag_leg  = flag.get("legislation_ref", "")
    flag_text = flag_desc.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    flag_content = [
        Paragraph(f"● HIGH SEVERITY: {flag.get('flag_id', '')}", flag_style),
        Spacer(1, 2*mm),
        Paragraph(flag_text, body),
    ]
    if flag_leg:
        flag_content.append(Spacer(1, 2*mm))
        flag_content.append(Paragraph(
            f"Legislative basis: {flag_leg}", leg_style))

    flag_box = Table([
        [flag_content]
    ], colWidths=[170*mm])
    flag_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), RED_LIGHT),
        ("BOX", (0, 0), (-1, -1), 1.5, RED),
        ("TOPPADDING", (0, 0), (-1, -1), 4*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4*mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 4*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4*mm),
    ]))
    story.append(flag_box)

    _add_footer(story, result)
    doc.build(story)
    return buf.getvalue()


# ── Component 2: Legislative Basis ────────────────────────────────────────────

def _build_legislation_pdf(flag: dict, result: dict) -> bytes:
    from data.legislation_text import lookup_sections

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )
    styles = getSampleStyleSheet()
    story = []

    _add_header(story, "EVIDENCE — LEGISLATIVE BASIS", result)

    h2 = ParagraphStyle("H2", fontSize=13, textColor=NAVY, fontName="Helvetica-Bold", spaceAfter=4)
    h3 = ParagraphStyle("H3", fontSize=10, textColor=NAVY, fontName="Helvetica-Bold", spaceAfter=2)
    body = ParagraphStyle("Body", fontSize=9, textColor=TEXT, leading=14, spaceAfter=6)
    mono = ParagraphStyle("Mono", fontSize=8.5, textColor=TEXT, leading=13,
                           fontName="Courier", spaceAfter=4)
    plain_style = ParagraphStyle("Plain", fontSize=9, textColor=SLATE, leading=13,
                                  fontName="Helvetica-Oblique")
    ref_style = ParagraphStyle("Ref", fontSize=9, textColor=TEAL,
                                fontName="Helvetica-Bold", spaceAfter=2)

    legislation_ref = flag.get("legislation_ref", "")
    jurisdiction = result.get("jurisdiction", "")

    story.append(Paragraph("Statutory Basis for This Dispute", h2))
    story.append(HRFlowable(width="100%", thickness=2, color=TEAL))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph(
        "The legislation reproduced below establishes the legal basis for disputing the "
        "clause identified in this evidence pack. The relevant sections are cited verbatim "
        "from the applicable Australian state Retail Leases Act.",
        ParagraphStyle("Intro", fontSize=8, textColor=SLATE, fontName="Helvetica-Oblique", leading=12)
    ))
    story.append(Spacer(1, 5*mm))

    # Look up bundled section texts
    sections = lookup_sections(legislation_ref, jurisdiction)

    if sections:
        for sec in sections:
            story.append(Paragraph(sec["full_ref"], ref_style))
            story.append(Paragraph(
                f"<b>{sec['title']}</b>", h3))
            story.append(Spacer(1, 2*mm))

            # Statutory text box
            stat_text = sec["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            stat_para = Paragraph(stat_text, mono)
            stat_box = Table([[stat_para]], colWidths=[170*mm])
            stat_box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f9ff")),
                ("BOX", (0, 0), (-1, -1), 1, TEAL),
                ("TOPPADDING", (0, 0), (-1, -1), 4*mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4*mm),
                ("LEFTPADDING", (0, 0), (-1, -1), 4*mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4*mm),
            ]))
            story.append(stat_box)
            story.append(Spacer(1, 3*mm))

            # Plain English interpretation
            story.append(Paragraph("Plain-English Meaning:", ParagraphStyle(
                "PlainLabel", fontSize=8, fontName="Helvetica-Bold", textColor=SLATE)))
            story.append(Spacer(1, 1*mm))
            plain_box = Table([[Paragraph(sec["plain"], plain_style)]], colWidths=[170*mm])
            plain_box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GREY),
                ("BOX", (0, 0), (-1, -1), 0.5, MID_GREY),
                ("TOPPADDING", (0, 0), (-1, -1), 3*mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3*mm),
                ("LEFTPADDING", (0, 0), (-1, -1), 3*mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3*mm),
            ]))
            story.append(plain_box)
            story.append(Spacer(1, 6*mm))
    else:
        # No bundled text — show the ref string and a note
        if legislation_ref:
            story.append(Paragraph(f"Cited legislation: <b>{legislation_ref}</b>", body))
            story.append(Spacer(1, 3*mm))
        story.append(Paragraph(
            "The full text of this legislation is available at legislation.nsw.gov.au, "
            "legislation.vic.gov.au, or the relevant state parliamentary website. "
            "Your solicitor can obtain the exact section text for inclusion in formal correspondence.",
            plain_style
        ))

    # Always include official source links
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GREY))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph("Official Sources", ParagraphStyle(
        "SrcLabel", fontSize=9, fontName="Helvetica-Bold", textColor=NAVY)))
    story.append(Spacer(1, 2*mm))
    sources = [
        "NSW:  https://legislation.nsw.gov.au/view/html/inforce/current/act-1994-046",
        "VIC:  https://www.legislation.vic.gov.au/in-force/acts/retail-leases-act-2003",
        "QLD:  https://www.legislation.qld.gov.au/view/html/inforce/current/act-1994-122",
        "SA:   https://www.legislation.sa.gov.au/LZ/C/A/RETAIL%20AND%20COMMERCIAL%20LEASES%20ACT%201995",
        "WA:   https://www.legislation.wa.gov.au/legislation/statutes.nsf/main_mrtitle_659_homepage.html",
    ]
    for src in sources:
        story.append(Paragraph(src, ParagraphStyle(
            "Src", fontSize=7.5, textColor=SLATE, fontName="Courier", leading=11)))

    _add_footer(story, result)
    doc.build(story)
    return buf.getvalue()


# ── Component 3: CPI Verification ─────────────────────────────────────────────

def _is_cpi_flag(flag: dict, clause: dict) -> bool:
    """Return True if this flag relates to CPI or rent review."""
    combined = " ".join([
        flag.get("description", ""),
        flag.get("flag_id", ""),
        clause.get("clause_type", ""),
        clause.get("clause_heading", ""),
        " ".join(clause.get("key_terms", [])),
    ]).lower()
    return any(kw in combined for kw in _CPI_KEYWORDS)


def _fetch_abs_cpi(jurisdiction: str) -> dict:
    """
    Fetch the last 8 quarters of CPI data from the ABS SDMX API.
    Returns a dict with:
        city, region_code, periods, values, latest_quarter, base_quarter,
        latest_value, base_value, change_pct, source_url
    Returns an error dict if the fetch fails.
    """
    region_code, city = _JUR_CPI_REGION.get(jurisdiction.upper(), _ALL_CITIES_REGION)

    # All Groups CPI: Measure=1, Index=10, Type=50 (index numbers), Freq=Q
    data_key = f"1.{region_code}.10.50.Q"
    url = f"{ABS_API_BASE}/data/CPI/{data_key}"
    params = {"format": "jsondata"}

    try:
        resp = httpx.get(url, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()

        # Navigate SDMX-JSON structure
        obs = data["data"]["dataSets"][0]["series"]["0:0:0:0:0"]["observations"]
        time_periods = data["data"]["structure"]["dimensions"]["observation"][0]["values"]

        # Build sorted (period, value) list — last 8 quarters
        pairs = []
        for idx_str, vals in obs.items():
            idx = int(idx_str)
            period = time_periods[idx]["id"]   # e.g. "2024-Q2"
            value  = vals[0]
            pairs.append((period, float(value)))

        pairs.sort(key=lambda x: x[0])
        pairs = pairs[-8:]   # last 8 quarters

        if len(pairs) < 2:
            raise ValueError("Insufficient CPI data points")

        base_period, base_value = pairs[0]
        latest_period, latest_value = pairs[-1]
        change_pct = round(((latest_value - base_value) / base_value) * 100, 2)

        return {
            "ok": True,
            "city": city,
            "region_code": region_code,
            "periods": [p for p, _ in pairs],
            "values":  [v for _, v in pairs],
            "base_quarter": base_period,
            "latest_quarter": latest_period,
            "base_value": base_value,
            "latest_value": latest_value,
            "change_pct": change_pct,
            "source_url": url,
        }

    except Exception as e:
        logger.warning(f"ABS CPI fetch failed ({url}): {e}")
        return {
            "ok": False,
            "error": str(e),
            "city": city,
            "source_url": url,
        }


def _build_cpi_verification_pdf(flag: dict, clause: dict, result: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )
    story = []

    _add_header(story, "EVIDENCE — CPI VERIFICATION (ABS DATA)", result)

    h2 = ParagraphStyle("H2", fontSize=13, textColor=NAVY, fontName="Helvetica-Bold", spaceAfter=4)
    h3 = ParagraphStyle("H3", fontSize=10, textColor=NAVY, fontName="Helvetica-Bold", spaceAfter=2)
    body = ParagraphStyle("Body", fontSize=9, textColor=TEXT, leading=14, spaceAfter=6)
    mono = ParagraphStyle("Mono", fontSize=8.5, textColor=TEXT, leading=13, fontName="Courier")
    small = ParagraphStyle("Small", fontSize=7.5, textColor=SLATE, leading=11)

    jurisdiction = result.get("jurisdiction", "")
    cpi = _fetch_abs_cpi(jurisdiction)

    story.append(Paragraph("CPI Calculation Verification", h2))
    story.append(HRFlowable(width="100%", thickness=2, color=TEAL))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph(
        "This sheet provides independently verifiable CPI data from the Australian Bureau of "
        "Statistics (ABS) to check whether the rent review in this lease is consistent with "
        "the actual consumer price index movement.",
        ParagraphStyle("Intro", fontSize=8, textColor=SLATE, fontName="Helvetica-Oblique", leading=12)
    ))
    story.append(Spacer(1, 5*mm))

    if not cpi.get("ok"):
        # Fetch failed — provide manual verification instructions
        story.append(Paragraph("ABS Data Unavailable — Manual Verification Required", h3))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph(
            f"Automatic data retrieval failed: {cpi.get('error', 'network error')}. "
            "Please verify CPI figures manually using the ABS source below.",
            body
        ))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph("Manual verification steps:", h3))
        steps = [
            "1. Visit abs.gov.au → Statistics → Economy → Consumer Price Index",
            "2. Download 'Consumer Price Index, Australia' — Table 1 (All Groups CPI)",
            f"3. Locate the series for {cpi.get('city', 'your jurisdiction')}",
            "4. Find the index value at the base date specified in your lease",
            "5. Find the index value at the review date",
            "6. Calculate: (Review value ÷ Base value − 1) × 100 = CPI change %",
            "7. Compare to the rent increase your landlord has applied",
        ]
        for step in steps:
            story.append(Paragraph(step, body))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(
            f"ABS source URL: {cpi.get('source_url', 'https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia')}",
            mono
        ))
    else:
        # Live data available
        story.append(Paragraph(
            f"Series: All Groups CPI — {cpi['city']} (region code {cpi['region_code']})",
            ParagraphStyle("SeriesLabel", fontSize=9, fontName="Helvetica-Bold", textColor=TEAL)
        ))
        story.append(Spacer(1, 1*mm))
        story.append(Paragraph(
            f"Data retrieved: {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')} from ABS SDMX API",
            small
        ))
        story.append(Spacer(1, 4*mm))

        # CPI data table
        story.append(Paragraph("Quarterly CPI Index Numbers (last 8 quarters)", h3))
        story.append(Spacer(1, 2*mm))

        tbl_data = [["Quarter", "CPI Index Value", "Change from Prior Quarter"]]
        prev = None
        for period, value in zip(cpi["periods"], cpi["values"]):
            if prev is not None:
                change = f"{((value - prev) / prev * 100):+.2f}%"
            else:
                change = "—"
            tbl_data.append([period, f"{value:.1f}", change])
            prev = value

        cpi_tbl = Table(tbl_data, colWidths=[50*mm, 60*mm, 60*mm])
        cpi_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [LIGHT_GREY, WHITE]),
            ("TOPPADDING", (0, 0), (-1, -1), 2*mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2*mm),
            ("LEFTPADDING", (0, 0), (-1, -1), 3*mm),
            ("GRID", (0, 0), (-1, -1), 0.5, MID_GREY),
            # Highlight latest row
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e0f2fe")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ]))
        story.append(cpi_tbl)
        story.append(Spacer(1, 5*mm))

        # Math summary box
        story.append(Paragraph("Cumulative Change Calculation", h3))
        story.append(Spacer(1, 2*mm))

        math_lines = [
            f"Base period:          {cpi['base_quarter']}",
            f"Base CPI value:       {cpi['base_value']:.1f}",
            f"",
            f"Latest period:        {cpi['latest_quarter']}",
            f"Latest CPI value:     {cpi['latest_value']:.1f}",
            f"",
            f"CPI change formula:   (Latest ÷ Base − 1) × 100",
            f"                    = ({cpi['latest_value']:.1f} ÷ {cpi['base_value']:.1f} − 1) × 100",
            f"                    = {cpi['change_pct']:+.2f}%",
            f"",
            f"This is the MAXIMUM CPI-justified rent increase over this period.",
            f"Any landlord-applied increase exceeding {cpi['change_pct']:+.2f}% (above base)",
            f"is not supported by the CPI data.",
        ]
        math_text = "\n".join(math_lines)
        math_para = Paragraph(
            math_text.replace("\n", "<br/>"),
            ParagraphStyle("Math", fontSize=9, textColor=TEXT, fontName="Courier",
                           leading=14, backColor=colors.HexColor("#f0fdf4"))
        )
        math_box = Table([[math_para]], colWidths=[170*mm])
        math_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0fdf4")),
            ("BOX", (0, 0), (-1, -1), 1.5, GREEN),
            ("TOPPADDING", (0, 0), (-1, -1), 4*mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4*mm),
            ("LEFTPADDING", (0, 0), (-1, -1), 4*mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4*mm),
        ]))
        story.append(math_box)
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(
            f"Source: Australian Bureau of Statistics SDMX API. "
            f"URL: {cpi['source_url']}",
            small
        ))

    _add_footer(story, result)
    doc.build(story)
    return buf.getvalue()


# ── Component 4: Dispute Letter ───────────────────────────────────────────────

def _generate_letter_text(flag: dict, clause: dict, result: dict) -> str:
    """
    Call Claude (Sonnet) to generate a formal dispute letter draft.
    Falls back to a structured template if the API call fails.
    """
    try:
        import anthropic
        import os
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key or api_key.startswith("sk-ant-your"):
            raise ValueError("ANTHROPIC_API_KEY not set — using template")

        model = os.environ.get("SONNET_MODEL", "claude-sonnet-4-6")
        client = anthropic.Anthropic(api_key=api_key)

        flag_desc    = flag.get("description", "")
        flag_leg     = flag.get("legislation_ref", "N/A")
        clause_text  = clause.get("clause_text", "")[:800]  # cap for prompt length
        clause_head  = clause.get("clause_heading", "Unknown")
        recommended  = clause.get("recommended_action", "")
        tenant       = result.get("tenant_name", "The Tenant")
        jurisdiction = result.get("jurisdiction", "")

        prompt = (
            f"You are drafting a formal commercial lease dispute letter for an Australian tenant.\n\n"
            f"Tenant: {tenant}\n"
            f"Jurisdiction: {jurisdiction}\n"
            f"Clause in dispute: {clause_head}\n"
            f"Clause text (excerpt): {clause_text}\n"
            f"Issue identified: {flag_desc}\n"
            f"Legislative basis: {flag_leg}\n"
            f"Recommended resolution: {recommended}\n\n"
            f"Write a formal, professional dispute letter from the tenant to the landlord/property manager. "
            f"The letter should:\n"
            f"1. Be addressed '[Date]\\n\\nThe Landlord/Property Manager\\n[Property Address]'\n"
            f"2. Have subject line: 'Re: Lease Clause Dispute — {clause_head}'\n"
            f"3. Cite the specific clause number and the legislation section\n"
            f"4. State clearly what the tenant requires (the cure or amendment)\n"
            f"5. Set a 14-day deadline for written response\n"
            f"6. End with: 'This letter is a commercial risk analysis draft. "
            f"Before sending, please have it reviewed by a qualified solicitor.'\n"
            f"7. Be signed: 'Yours faithfully,\\n\\n{tenant}'\n\n"
            f"Use formal Australian legal letter style. No bullet points. "
            f"Plain paragraphs only. 3-4 paragraphs maximum."
        )

        msg = client.messages.create(
            model=model,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()

    except Exception as e:
        logger.warning(f"Dispute letter Claude generation failed: {e} — using template")
        return _letter_template(flag, clause, result)


def _letter_template(flag: dict, clause: dict, result: dict) -> str:
    """Structured fallback dispute letter template."""
    tenant       = result.get("tenant_name", "[Tenant Name]")
    jurisdiction = result.get("jurisdiction", "")
    clause_head  = clause.get("clause_heading", "[Clause Name]")
    flag_desc    = flag.get("description", "[Issue description]")
    flag_leg     = flag.get("legislation_ref") or "the applicable Retail Leases Act"
    recommended  = clause.get("recommended_action", "[Required amendment]")
    today        = datetime.now().strftime("%-d %B %Y")

    return textwrap.dedent(f"""\
        {today}

        The Landlord/Property Manager
        [Property Address]

        Dear Sir/Madam,

        Re: Lease Clause Dispute — {clause_head}

        We write on behalf of {tenant} in connection with the above-captioned retail shop lease.
        We have identified a clause that we believe is inconsistent with your obligations under
        {flag_leg}.

        Specifically, the clause titled "{clause_head}" contains the following issue: {flag_desc}
        This is inconsistent with {flag_leg}, which provides that any such obligation is void and
        unenforceable to the extent it conflicts with the tenant's statutory rights.

        We require that you: {recommended} We request your written response within 14 days of the
        date of this letter confirming your agreement to the required amendment. If we do not receive
        a satisfactory response within that time, we will seek advice from the relevant state retail
        tenancy authority and consider further steps.

        This letter is a commercial risk analysis draft prepared by TenantSentry.ai.
        Before sending, please have it reviewed by a qualified commercial lease solicitor.

        Yours faithfully,

        {tenant}
    """)


def _build_dispute_letter_pdf(flag: dict, clause: dict, result: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=25*mm, rightMargin=25*mm,
        topMargin=25*mm, bottomMargin=25*mm,
    )
    story = []

    # Watermark-style header for draft
    draft_style = ParagraphStyle(
        "Draft", fontSize=11, textColor=AMBER,
        fontName="Helvetica-Bold", alignment=TA_CENTER, spaceAfter=2*mm
    )
    story.append(Paragraph(
        "⚠  DRAFT — FOR REVIEW BY YOUR SOLICITOR BEFORE SENDING  ⚠", draft_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=AMBER))
    story.append(Spacer(1, 8*mm))

    letter_text = _generate_letter_text(flag, clause, result)

    letter_style = ParagraphStyle(
        "Letter", fontSize=10, textColor=TEXT,
        leading=16, fontName="Times-Roman", spaceAfter=4*mm,
        alignment=TA_LEFT
    )

    # Render letter line by line — blank lines become spacers
    for line in letter_text.split("\n"):
        if line.strip():
            escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(escaped, letter_style))
        else:
            story.append(Spacer(1, 3*mm))

    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GREY))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "This dispute letter draft was generated by TenantSentry.ai AI analysis. "
        "It does not constitute legal advice. Always consult a qualified commercial lease solicitor "
        "before sending any formal correspondence to your landlord.",
        ParagraphStyle("Disc", fontSize=7.5, textColor=SLATE, leading=11,
                       fontName="Helvetica-Oblique")
    ))

    doc.build(story)
    return buf.getvalue()


# ── Component 5: README ───────────────────────────────────────────────────────

def _build_readme(flag: dict, result: dict) -> str:
    tenant       = result.get("tenant_name", "Tenant")
    flag_id      = flag.get("flag_id", "")
    flag_desc    = flag.get("description", "")
    today        = datetime.now().strftime("%d %B %Y")

    return textwrap.dedent(f"""\
        TenantSentry.ai — Evidence Pack
        ================================
        Generated: {today}
        Tenant:    {tenant}
        Jurisdiction: {result.get("jurisdiction", "")}
        Flag ID:   {flag_id}
        Issue:     {flag_desc[:120]}

        CONTENTS
        --------
        01_clause_excerpt.pdf
            The verbatim lease clause that triggered the risk flag.
            Use this to identify the exact contractual provision under dispute.

        02_legislative_basis.pdf
            The exact text of the applicable state Retail Leases Act section(s).
            This is the statutory authority that makes the clause void or unenforceable.

        03_cpi_verification.pdf  (present for rent review / CPI flags only)
            Australian Bureau of Statistics CPI index data with deterministic
            math showing the maximum CPI-justified rent movement.
            Use this to verify whether the landlord's applied increase is supported
            by actual inflation data.

        04_dispute_letter.pdf
            A draft formal dispute letter generated by TenantSentry.ai.
            THIS IS A DRAFT ONLY. Have it reviewed by a solicitor before sending.

        HOW TO USE THIS PACK
        --------------------
        1. Review all documents carefully.
        2. Send 04_dispute_letter.pdf to a commercial lease solicitor for review.
        3. Once approved, send the letter (with this evidence pack attached)
           to your landlord or their property manager.
        4. Keep a copy of everything — date-stamp your sent letter.
        5. If no satisfactory response within 14 days, contact the relevant
           state Retail Tenancy Authority:
             NSW: NSW Fair Trading — 13 32 20
             VIC: Consumer Affairs Victoria — 1300 558 181
             QLD: Office of Fair Trading — 13 74 68
             SA:  CBS South Australia — 131 882
             WA:  Commerce WA — 1300 304 054

        DISCLAIMER
        ----------
        This evidence pack was generated by TenantSentry.ai AI analysis.
        It is for informational purposes only and does not constitute legal advice.
        Always consult a qualified Australian commercial lease solicitor
        before taking any formal action.

        tenantsentry.ai
    """)


# ── Shared PDF helpers ────────────────────────────────────────────────────────

def _add_header(story: list, title: str, result: dict) -> None:
    """Add branded header to every PDF in the pack."""
    header_data = [[
        [
            Paragraph(
                '<font color="#0d9488"><b>TenantSentry</b>.ai</font>',
                ParagraphStyle("Logo", fontSize=14, fontName="Helvetica-Bold")
            ),
            Paragraph(
                "Evidence Pack — Commercial Lease Audit",
                ParagraphStyle("Sub", fontSize=8, textColor=SLATE)
            ),
        ],
        Paragraph(
            f"<b>{result.get('tenant_name','')}</b><br/>"
            f"{result.get('jurisdiction','')} · "
            f"{result.get('filename','')[:40]}",
            ParagraphStyle("Meta", fontSize=8, textColor=SLATE, alignment=TA_RIGHT)
        ),
    ]]
    header_tbl = Table(header_data, colWidths=[100*mm, 70*mm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_tbl)
    story.append(HRFlowable(width="100%", thickness=2, color=TEAL))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        title,
        ParagraphStyle("Title", fontSize=11, textColor=NAVY,
                       fontName="Helvetica-Bold", spaceAfter=2*mm)
    ))
    story.append(Spacer(1, 2*mm))


def _add_footer(story: list, result: dict) -> None:
    """Add standard footer."""
    today = datetime.now().strftime("%d %b %Y")
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GREY))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"TenantSentry.ai Evidence Pack · {today} · Not legal advice · tenantsentry.ai",
        ParagraphStyle("Footer", fontSize=7, textColor=SLATE, alignment=TA_CENTER)
    ))
