"""
report_generator.py
-------------------
Generates a branded PDF audit report from a completed AuditResult dict.

Output: professional A4 report with:
  - Cover page (tenant, jurisdiction, risk score, date)
  - Executive summary (risk score gauge, flag counts)
  - Clause-by-clause findings with risk flags
  - Recommended actions table
  - Legal disclaimer

Uses reportlab (already in requirements).
"""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether,
)
from loguru import logger

# ── Brand colours ─────────────────────────────────────────────────────────────
NAVY = colors.HexColor("#0f172a")
TEAL = colors.HexColor("#0d9488")
TEAL_LIGHT = colors.HexColor("#ccfbf1")
RED = colors.HexColor("#dc2626")
RED_LIGHT = colors.HexColor("#fee2e2")
AMBER = colors.HexColor("#d97706")
AMBER_LIGHT = colors.HexColor("#fef3c7")
GREEN = colors.HexColor("#16a34a")
GREEN_LIGHT = colors.HexColor("#dcfce7")
SLATE = colors.HexColor("#64748b")
LIGHT_GREY = colors.HexColor("#f8fafc")
MID_GREY = colors.HexColor("#e2e8f0")
TEXT = colors.HexColor("#1e293b")

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm

# ── Output directory ──────────────────────────────────────────────────────────
REPORTS_DIR = Path(tempfile.gettempdir()) / "tenantsentry_reports"
REPORTS_DIR.mkdir(exist_ok=True)


def _severity_color(severity: str) -> colors.Color:
    s = (severity or "").lower()
    if s == "high":
        return RED
    if s == "medium":
        return AMBER
    return GREEN


def _severity_bg(severity: str) -> colors.Color:
    s = (severity or "").lower()
    if s == "high":
        return RED_LIGHT
    if s == "medium":
        return AMBER_LIGHT
    return GREEN_LIGHT


def _risk_level_color(score: int) -> colors.Color:
    if score >= 60:
        return RED
    if score >= 30:
        return AMBER
    return GREEN


def _risk_level_label(score: int) -> str:
    if score >= 60:
        return "HIGH RISK"
    if score >= 30:
        return "MEDIUM RISK"
    return "LOW RISK"


def generate_pdf_report(result: dict, job_id: str) -> str:
    """
    Generate a PDF audit report from an AuditResult dict.

    Args:
        result: AuditResult.model_dump(mode='json')
        job_id: Used to name the output file

    Returns:
        Absolute path to the generated PDF file
    """
    output_path = str(REPORTS_DIR / f"audit_{job_id}.pdf")

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
    )

    styles = getSampleStyleSheet()
    story = []

    # ── Styles ─────────────────────────────────────────────────────────────
    h1 = ParagraphStyle("H1", parent=styles["Normal"],
                         fontSize=22, textColor=NAVY, fontName="Helvetica-Bold",
                         spaceAfter=4)
    h2 = ParagraphStyle("H2", parent=styles["Normal"],
                         fontSize=14, textColor=NAVY, fontName="Helvetica-Bold",
                         spaceBefore=12, spaceAfter=4)
    h3 = ParagraphStyle("H3", parent=styles["Normal"],
                         fontSize=11, textColor=NAVY, fontName="Helvetica-Bold",
                         spaceBefore=8, spaceAfter=2)
    body = ParagraphStyle("Body", parent=styles["Normal"],
                           fontSize=9, textColor=TEXT, leading=14,
                           fontName="Helvetica")
    small = ParagraphStyle("Small", parent=styles["Normal"],
                            fontSize=8, textColor=SLATE, fontName="Helvetica")
    caption = ParagraphStyle("Caption", parent=styles["Normal"],
                              fontSize=8, textColor=SLATE, fontName="Helvetica-Oblique")

    # ══════════════════════════════════════════════════════════════════════
    # COVER PAGE
    # ══════════════════════════════════════════════════════════════════════

    # Brand header bar
    cover_header = Table(
        [[ Paragraph("TenantSentry.ai", ParagraphStyle("Brand", fontSize=20,
                     textColor=colors.white, fontName="Helvetica-Bold")),
           Paragraph("LEASE AUDIT REPORT", ParagraphStyle("Sub", fontSize=10,
                     textColor=TEAL, fontName="Helvetica-Bold", alignment=TA_RIGHT)) ]],
        colWidths=[120*mm, 50*mm],
    )
    cover_header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 8*mm),
        ("RIGHTPADDING", (-1, 0), (-1, 0), 8*mm),
        ("TOPPADDING", (0, 0), (-1, -1), 6*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6*mm),
    ]))
    story.append(cover_header)
    story.append(Spacer(1, 12*mm))

    # Tenant info
    tenant = result.get("tenant_name", "Unknown")
    jurisdiction = result.get("jurisdiction", "")
    filename = result.get("filename", "")
    audit_date = result.get("audit_date", datetime.utcnow().isoformat())[:10]
    risk_score = result.get("risk_score", 0)
    total_clauses = result.get("total_clauses", 0)
    all_flags = result.get("all_risk_flags", [])
    high_count = sum(1 for f in all_flags if f.get("severity") == "high")
    medium_count = sum(1 for f in all_flags if f.get("severity") == "medium")
    low_count = sum(1 for f in all_flags if f.get("severity") == "low")

    story.append(Paragraph(f"Prepared for: {tenant}", h1))
    story.append(Spacer(1, 2*mm))

    meta_data = [
        ["Jurisdiction:", jurisdiction],
        ["Lease Document:", filename],
        ["Audit Date:", audit_date],
    ]
    meta_table = Table(meta_data, colWidths=[40*mm, 120*mm])
    meta_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), SLATE),
        ("TEXTCOLOR", (1, 0), (1, -1), TEXT),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=MID_GREY))
    story.append(Spacer(1, 8*mm))

    # Risk score summary box
    risk_color = _risk_level_color(risk_score)
    risk_label = _risk_level_label(risk_score)

    summary_data = [
        [
            Paragraph(f'<font color="{risk_color.hexval()}" size="36"><b>{risk_score}</b></font><br/>'
                      f'<font size="8" color="#64748b">RISK SCORE</font><br/>'
                      f'<font size="7" color="#94a3b8">(0 = safe · 100 = critical)</font>',
                      ParagraphStyle("Score", alignment=TA_CENTER)),
            Paragraph(f'<font color="{risk_color.hexval()}" size="14"><b>{risk_label}</b></font>',
                      ParagraphStyle("Level", alignment=TA_CENTER)),
            Table([
                [Paragraph(f"<b>{total_clauses}</b>", ParagraphStyle("Num", fontSize=18, alignment=TA_CENTER, textColor=TEXT)),
                 Paragraph(f"<b>{high_count}</b>", ParagraphStyle("Num", fontSize=18, alignment=TA_CENTER, textColor=RED)),
                 Paragraph(f"<b>{medium_count}</b>", ParagraphStyle("Num", fontSize=18, alignment=TA_CENTER, textColor=AMBER)),
                 Paragraph(f"<b>{low_count}</b>", ParagraphStyle("Num", fontSize=18, alignment=TA_CENTER, textColor=GREEN))],
                [Paragraph("Clauses", ParagraphStyle("Lbl", fontSize=7, alignment=TA_CENTER, textColor=SLATE)),
                 Paragraph("High Risk", ParagraphStyle("Lbl", fontSize=7, alignment=TA_CENTER, textColor=SLATE)),
                 Paragraph("Medium", ParagraphStyle("Lbl", fontSize=7, alignment=TA_CENTER, textColor=SLATE)),
                 Paragraph("Low", ParagraphStyle("Lbl", fontSize=7, alignment=TA_CENTER, textColor=SLATE))],
            ], colWidths=[25*mm]*4),
        ]
    ]

    summary_table = Table(summary_data, colWidths=[35*mm, 50*mm, 85*mm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GREY),
        ("BOX", (0, 0), (-1, -1), 1, MID_GREY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6*mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 4*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4*mm),
        ("LINEAFTER", (0, 0), (1, 0), 1, MID_GREY),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 6*mm))
    story.append(Paragraph(
        "This report was generated by TenantSentry.ai using AI-powered clause analysis and Australian "
        "commercial lease legislation. It is for informational purposes only and does not constitute "
        "legal advice. For dispute negotiations, consult a qualified commercial lease solicitor.",
        caption
    ))

    # ══════════════════════════════════════════════════════════════════════
    # EXECUTIVE SUMMARY — TOP PRIORITY ACTIONS
    # ══════════════════════════════════════════════════════════════════════
    # Collect clauses with at least one HIGH flag, ordered by number of HIGH flags desc
    high_clauses = []
    for ca in result.get("clause_analyses", []):
        flags = ca.get("risk_flags") or []
        n_high = sum(1 for f in flags if f.get("severity") == "high")
        if n_high > 0 and ca.get("recommended_action"):
            high_clauses.append((n_high, ca))
    high_clauses.sort(key=lambda x: x[0], reverse=True)
    top_clauses = [ca for _, ca in high_clauses[:3]]

    if top_clauses:
        story.append(Spacer(1, 8*mm))
        story.append(Paragraph("Priority Actions Before You Sign", h2))
        story.append(HRFlowable(width="100%", thickness=1, color=RED))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(
            "These are the highest-risk findings requiring immediate attention. "
            "Address these before executing the lease.",
            ParagraphStyle("ExecIntro", parent=styles["Normal"], fontSize=8,
                           textColor=SLATE, fontName="Helvetica-Oblique")
        ))
        story.append(Spacer(1, 3*mm))

        for rank, ca in enumerate(top_clauses, 1):
            heading = ca.get("clause_heading", "Unknown Clause")
            action = ca.get("recommended_action", "")
            flags = ca.get("risk_flags") or []
            n_high = sum(1 for f in flags if f.get("severity") == "high")

            exec_row = Table([[
                Paragraph(
                    f'<font color="{RED.hexval()}" size="14"><b>{rank}</b></font>',
                    ParagraphStyle("Rank", alignment=TA_CENTER)
                ),
                [
                    Paragraph(f"<b>{heading}</b> — {n_high} HIGH risk finding{'s' if n_high > 1 else ''}",
                              ParagraphStyle("ExecH", fontSize=9, textColor=NAVY,
                                             fontName="Helvetica-Bold", spaceAfter=2)),
                    Paragraph(action,
                              ParagraphStyle("ExecAct", fontSize=8, textColor=TEXT, leading=11)),
                ]
            ]], colWidths=[12*mm, 158*mm])
            exec_row.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), RED_LIGHT),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3*mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3*mm),
                ("LEFTPADDING", (0, 0), (0, 0), 3*mm),
                ("LEFTPADDING", (1, 0), (1, 0), 3*mm),
                ("RIGHTPADDING", (-1, 0), (-1, 0), 4*mm),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.white),
            ]))
            story.append(exec_row)
            story.append(Spacer(1, 2*mm))

    # ══════════════════════════════════════════════════════════════════════
    # CLAUSE-BY-CLAUSE ANALYSIS
    # ══════════════════════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("Clause-by-Clause Analysis", h2))
    story.append(HRFlowable(width="100%", thickness=2, color=TEAL))
    story.append(Spacer(1, 4*mm))

    clause_analyses = result.get("clause_analyses", [])

    for ca in clause_analyses:
        flags = ca.get("risk_flags") or []
        if not flags and not ca.get("plain_english_summary"):
            continue   # Skip boilerplate/empty clauses

        heading = ca.get("clause_heading", "Unknown Clause")
        clause_type = ca.get("clause_type", "")
        summary = ca.get("plain_english_summary", "")
        action = ca.get("recommended_action", "")
        key_terms = ca.get("key_terms", [])

        # Max severity for this clause
        severities = [f.get("severity", "low") for f in flags]
        max_sev = "high" if "high" in severities else ("medium" if "medium" in severities else "low")
        sev_color = _severity_color(max_sev)
        sev_bg = _severity_bg(max_sev)

        # ── Clause header (keep header + summary together to avoid orphan headings) ──
        anchor_elements = []

        header_data = [[
            Paragraph(f"<b>{heading}</b>", ParagraphStyle("ClauseH", fontSize=10, textColor=NAVY, fontName="Helvetica-Bold")),
            Paragraph(f"<b>{max_sev.upper()}</b>", ParagraphStyle("Sev", fontSize=8, textColor=sev_color, fontName="Helvetica-Bold", alignment=TA_RIGHT)),
        ]]
        header_table = Table(header_data, colWidths=[130*mm, 40*mm])
        header_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), sev_bg if flags else LIGHT_GREY),
            ("TOPPADDING", (0, 0), (-1, -1), 3*mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3*mm),
            ("LEFTPADDING", (0, 0), (0, 0), 4*mm),
            ("RIGHTPADDING", (-1, 0), (-1, 0), 4*mm),
        ]))
        anchor_elements.append(header_table)

        # Summary and key terms go with the header in KeepTogether
        if summary:
            anchor_elements.append(Spacer(1, 2*mm))
            anchor_elements.append(Paragraph(summary, body))

        if key_terms:
            terms_str = " · ".join(key_terms[:6])
            anchor_elements.append(Spacer(1, 1*mm))
            anchor_elements.append(Paragraph(f"<b>Key terms:</b> {terms_str}", small))

        # Wrap only the anchor block — header won't orphan, but flags can flow freely
        story.append(KeepTogether(anchor_elements))

        # ── Risk flags (flow freely — no KeepTogether to prevent dropped content) ──
        if flags:
            story.append(Spacer(1, 2*mm))
            for flag in flags:
                flag_sev = flag.get("severity", "low")
                flag_color = _severity_color(flag_sev)
                flag_desc = flag.get("description", "")
                flag_leg = flag.get("legislation_ref", "")
                flag_text = flag_desc
                if flag_leg:
                    flag_text += f" <i>({flag_leg})</i>"
                flag_row = Table([[
                    Paragraph(f"● {flag_sev.upper()}", ParagraphStyle("FlagSev", fontSize=7,
                               textColor=flag_color, fontName="Helvetica-Bold")),
                    Paragraph(flag_text, ParagraphStyle("FlagDesc", fontSize=8, textColor=TEXT, leading=11)),
                ]], colWidths=[18*mm, 152*mm])
                flag_row.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]))
                story.append(flag_row)

        # ── Recommended action ──
        if action:
            story.append(Spacer(1, 2*mm))
            action_table = Table([[
                Paragraph("→ ACTION", ParagraphStyle("ActLabel", fontSize=7, textColor=TEAL,
                           fontName="Helvetica-Bold")),
                Paragraph(action, ParagraphStyle("ActText", fontSize=8, textColor=TEXT, leading=11)),
            ]], colWidths=[18*mm, 152*mm])
            action_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]))
            story.append(action_table)

        story.append(Spacer(1, 3*mm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GREY))
        story.append(Spacer(1, 2*mm))

    # ══════════════════════════════════════════════════════════════════════
    # FOOTER DISCLAIMER
    # ══════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=MID_GREY))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "<b>Disclaimer:</b> This report is generated by AI analysis and is provided for informational "
        "purposes only. It does not constitute legal advice. The analysis is based on Australian commercial "
        "leasing legislation and known risk patterns. Individual lease circumstances may vary. Always consult "
        "a qualified commercial lease solicitor before taking action on any identified risk. "
        "TenantSentry.ai accepts no liability for decisions made based on this report.",
        caption
    ))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"Generated by TenantSentry.ai · {audit_date} · tenantsentry.ai",
        ParagraphStyle("Footer", fontSize=7, textColor=SLATE, alignment=TA_CENTER)
    ))

    # ── Build PDF ──────────────────────────────────────────────────────────
    doc.build(story)
    logger.info(f"PDF report generated: {output_path}")
    return output_path
