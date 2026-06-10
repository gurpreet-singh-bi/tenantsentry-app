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


def generate_pdf_report(
    result: dict,
    job_id: str,
    firm_name: Optional[str] = None,
    firm_tagline: Optional[str] = None,
) -> str:
    """
    Generate a PDF audit report from an AuditResult dict.

    Args:
        result:       AuditResult.model_dump(mode='json')
        job_id:       Used to name the output file
        firm_name:    White-label firm name (e.g. "Smith Advisory Group").
                      When set, replaces "TenantSentry.ai" in the header and footer.
        firm_tagline: One-line tagline shown under firm_name on the cover (optional).

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

    # ── White-label config ─────────────────────────────────────────────────
    # When firm_name is set, replace all "TenantSentry.ai" references in the report
    # so channel partners can deliver under their own branding.
    brand_name    = firm_name.strip() if firm_name and firm_name.strip() else "TenantSentry.ai"
    brand_url     = "" if firm_name else "tenantsentry.ai"
    brand_tagline = firm_tagline.strip() if firm_tagline and firm_tagline.strip() else None

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
        [[ Paragraph(brand_name, ParagraphStyle("Brand", fontSize=20,
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
    story.append(Spacer(1, 4*mm))

    # Risk score supporting text
    flagged_clause_count = sum(
        1 for ca in result.get("clause_analyses", [])
        if ca.get("risk_flags")
    )
    clean_clause_count = total_clauses - flagged_clause_count
    risk_text_parts = []
    if high_count > 0:
        risk_text_parts.append(
            f"<b>{high_count} HIGH risk {'flag' if high_count == 1 else 'flags'}</b> "
            f"represent serious legal or financial risks requiring immediate attention before signing."
        )
    if medium_count > 0:
        risk_text_parts.append(
            f"<b>{medium_count} MEDIUM risk {'flag' if medium_count == 1 else 'flags'}</b> "
            f"highlight unusual terms that warrant careful review and possible negotiation."
        )
    if low_count > 0:
        risk_text_parts.append(
            f"<b>{low_count} LOW risk {'flag' if low_count == 1 else 'flags'}</b> "
            f"are minor deviations from standard practice that are worth noting."
        )
    if clean_clause_count > 0:
        risk_text_parts.append(
            f"<b>{clean_clause_count} {'clause' if clean_clause_count == 1 else 'clauses'}</b> "
            f"{'appears' if clean_clause_count == 1 else 'appear'} compliant with no issues identified."
        )
    if risk_text_parts:
        story.append(Paragraph(
            "  ".join(risk_text_parts),
            ParagraphStyle("RiskSummaryText", parent=styles["Normal"],
                           fontSize=8, textColor=TEXT, leading=12, fontName="Helvetica")
        ))
        story.append(Spacer(1, 4*mm))

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

    # ── Section label style ──
    sec_label = ParagraphStyle("SecLabel", parent=styles["Normal"],
                               fontSize=7, textColor=SLATE, fontName="Helvetica-Bold",
                               textTransform="uppercase", spaceBefore=4, spaceAfter=1,
                               letterSpacing=0.5)

    for ca in clause_analyses:
        flags = ca.get("risk_flags") or []
        if not flags and not ca.get("plain_english_summary"):
            continue   # Skip boilerplate/empty clauses

        heading = ca.get("clause_heading", "Unknown Clause")
        page_number = ca.get("page_number")
        clause_type = ca.get("clause_type", "")
        clause_text = ca.get("clause_text", "")
        summary = ca.get("plain_english_summary", "")
        action = ca.get("recommended_action", "")
        key_terms = ca.get("key_terms", [])
        # Area 1: build citation label e.g. "Clause 14.2 · Page 45"
        citation_label = heading
        if page_number:
            citation_label = f"{heading}  ·  Page {page_number}"

        # Max severity for this clause
        severities = [f.get("severity", "low") for f in flags]
        max_sev = "high" if "high" in severities else ("medium" if "medium" in severities else "low")
        sev_color = _severity_color(max_sev)
        sev_bg = _severity_bg(max_sev)

        # ── Clause header ──
        anchor_elements = []

        # Build severity badges string for header
        flag_badge_text = ""
        if flags:
            counts = {"high": 0, "medium": 0, "low": 0}
            for f in flags:
                counts[f.get("severity", "low")] = counts.get(f.get("severity", "low"), 0) + 1
            parts = []
            if counts["high"]:
                parts.append(f'<font color="{RED.hexval()}">{counts["high"]} HIGH</font>')
            if counts["medium"]:
                parts.append(f'<font color="{AMBER.hexval()}">{counts["medium"]} MEDIUM</font>')
            if counts["low"]:
                parts.append(f'<font color="{GREEN.hexval()}">{counts["low"]} LOW</font>')
            flag_badge_text = "  ·  ".join(parts)
            total_flags = len(flags)
            flag_badge_text = f"<b>{total_flags} flag{'s' if total_flags != 1 else ''}:</b>  " + flag_badge_text

        header_data = [[
            [
                Paragraph(f"<b>{citation_label}</b>", ParagraphStyle("ClauseH", fontSize=10, textColor=NAVY, fontName="Helvetica-Bold")),
                Paragraph(flag_badge_text or "CLEAN", ParagraphStyle("ClauseBadges", fontSize=7, textColor=SLATE, fontName="Helvetica", leading=10)),
            ],
            Paragraph(f"<b>{max_sev.upper()}</b>", ParagraphStyle("Sev", fontSize=8, textColor=sev_color, fontName="Helvetica-Bold", alignment=TA_RIGHT)),
        ]]
        header_table = Table(header_data, colWidths=[140*mm, 30*mm])
        header_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), sev_bg if flags else LIGHT_GREY),
            ("TOPPADDING", (0, 0), (-1, -1), 3*mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3*mm),
            ("LEFTPADDING", (0, 0), (0, 0), 4*mm),
            ("RIGHTPADDING", (-1, 0), (-1, 0), 4*mm),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        anchor_elements.append(header_table)
        story.append(KeepTogether(anchor_elements))

        # ── ① ORIGINAL CLAUSE ──
        if clause_text:
            story.append(Spacer(1, 2*mm))
            story.append(Paragraph("① Original Clause", sec_label))
            orig_box = Table([[
                Paragraph(clause_text,
                          ParagraphStyle("OrigText", parent=styles["Normal"],
                                         fontSize=8, textColor=SLATE, leading=12,
                                         fontName="Helvetica-Oblique")),
            ]], colWidths=[170*mm])
            orig_box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GREY),
                ("BOX", (0, 0), (-1, -1), 0.5, MID_GREY),
                ("LEFTPADDING", (0, 0), (-1, -1), 4*mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4*mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2*mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2*mm),
            ]))
            story.append(orig_box)

        # ── ② MEANING ──
        if summary:
            story.append(Spacer(1, 2*mm))
            story.append(Paragraph("② Meaning", sec_label))
            story.append(Paragraph(summary, body))

        if key_terms:
            terms_str = " · ".join(key_terms[:6])
            story.append(Spacer(1, 1*mm))
            story.append(Paragraph(f"<b>Key terms:</b> {terms_str}", small))

        # ── ③ RISKS ──
        if flags:
            story.append(Spacer(1, 2*mm))
            story.append(Paragraph(f"③ Risks  ({len(flags)} flag{'s' if len(flags) != 1 else ''})", sec_label))
            for flag in flags:
                flag_sev = flag.get("severity", "low")
                flag_color = _severity_color(flag_sev)
                flag_desc = flag.get("description", "")
                flag_leg = flag.get("legislation_ref", "")
                flag_impact = flag.get("financial_impact_estimate", "")
                # Area 1: source citation — "Clause 14.2, Page 45"
                page_ref = f", Page {page_number}" if page_number else ""
                source_ref = f"{heading}{page_ref}"
                flag_text = flag_desc
                if flag_leg:
                    flag_text += f" <i>({flag_leg})</i>"
                if flag_impact:
                    flag_text += f"  <b>Est. financial exposure: {flag_impact}</b>"
                flag_text += f'  <font color="{SLATE.hexval()}" size="7">— {source_ref}</font>'
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

        # ── ④ RECOMMENDATIONS ──
        if action:
            story.append(Spacer(1, 2*mm))
            story.append(Paragraph("④ Recommendations", sec_label))
            action_table = Table([[
                Paragraph("→", ParagraphStyle("ActIcon", fontSize=9, textColor=TEAL,
                           fontName="Helvetica-Bold")),
                Paragraph(action, ParagraphStyle("ActText", fontSize=8, textColor=TEXT, leading=11)),
            ]], colWidths=[6*mm, 164*mm])
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
    # FLAG DEFINITIONS GLOSSARY
    # ══════════════════════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("Understanding Risk Flags", h2))
    story.append(HRFlowable(width="100%", thickness=1, color=TEAL))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "Each clause in this report may carry one or more risk flags. A single clause can trigger "
        "multiple flags if it raises several distinct concerns. Flags are classified by severity "
        "as follows:",
        ParagraphStyle("GlossIntro", parent=styles["Normal"], fontSize=8,
                       textColor=SLATE, fontName="Helvetica-Oblique", spaceAfter=4)
    ))
    story.append(Spacer(1, 2*mm))

    flag_defs = [
        (RED, RED_LIGHT, "HIGH RISK",
         "The clause presents a serious legal or financial risk to the tenant. "
         "High risk flags typically indicate terms that significantly disadvantage the tenant, "
         "may breach applicable state legislation (e.g. imposing prohibited costs, restricting "
         "statutory rights), or could result in substantial financial loss. "
         "<b>These clauses must be reviewed and, where possible, negotiated before signing.</b>"),
        (AMBER, AMBER_LIGHT, "MEDIUM RISK",
         "The clause contains terms that are unusual, one-sided, or depart from industry standard "
         "practice in a way that could disadvantage the tenant over the lease term. Medium risk "
         "flags do not necessarily indicate a legal breach, but the clause warrants careful review, "
         "clarification from the landlord, or negotiation before execution."),
        (GREEN, GREEN_LIGHT, "LOW RISK",
         "The clause is a minor deviation from standard practice or contains a term that may have "
         "limited practical impact. Low risk flags are noted for transparency. While they do not "
         "require urgent action, tenants should be aware of these terms and may choose to seek "
         "clarification if in doubt."),
    ]

    for flag_color, flag_bg, flag_label, flag_def in flag_defs:
        def_row = Table([[
            Paragraph(f"<b>{flag_label}</b>",
                      ParagraphStyle("DefLabel", fontSize=9, textColor=flag_color,
                                     fontName="Helvetica-Bold", alignment=TA_CENTER)),
            Paragraph(flag_def,
                      ParagraphStyle("DefBody", fontSize=8, textColor=TEXT, leading=12,
                                     fontName="Helvetica")),
        ]], colWidths=[28*mm, 142*mm])
        def_row.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), flag_bg),
            ("BACKGROUND", (1, 0), (1, 0), LIGHT_GREY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3*mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3*mm),
            ("LEFTPADDING", (0, 0), (0, 0), 3*mm),
            ("RIGHTPADDING", (-1, 0), (-1, 0), 3*mm),
            ("LEFTPADDING", (1, 0), (1, 0), 3*mm),
            ("BOX", (0, 0), (-1, -1), 0.5, MID_GREY),
            ("LINEAFTER", (0, 0), (0, 0), 0.5, MID_GREY),
        ]))
        story.append(def_row)
        story.append(Spacer(1, 2*mm))

    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "<b>Note on multiple flags per clause:</b> A single lease clause may trigger more than one "
        "flag if it contains several distinct risk elements. For example, a rent review clause might "
        "carry both a HIGH flag (for an annual-in-advance payment obligation) and a MEDIUM flag "
        "(for the landlord's right to unilaterally change the payment destination). Each flag is "
        "assessed independently and should be reviewed on its own merits.",
        ParagraphStyle("MultiNote", parent=styles["Normal"], fontSize=8,
                       textColor=TEXT, leading=12, fontName="Helvetica",
                       borderPad=4, borderColor=MID_GREY)
    ))

    # ═════════════

    # ══════════════════════════════════════════════════════════════════════
    # NEGOTIATION PLAYBOOK
    # ══════════════════════════════════════════════════════════════════════
    # Collect HIGH and MEDIUM flags that have a negotiation_email
    playbook_items = []
    for ca in result.get("clause_analyses", []):
        heading = ca.get("clause_heading", "Unknown Clause")
        page_num = ca.get("page_number")
        clause_ref = f"Clause: {heading}" + (f", Page {page_num}" if page_num else "")

        flags = ca.get("risk_flags") or []
        for flag in flags:
            sev = flag.get("severity", "").lower()
            if sev not in ("high", "medium"):
                continue
            email = flag.get("negotiation_email") or ca.get("negotiation_email")
            if not email:
                continue
            position = flag.get("negotiation_position") or ca.get("negotiation_position") or ""
            impact = flag.get("financial_impact_estimate") or ""
            playbook_items.append({
                "severity": sev,
                "clause_ref": clause_ref,
                "description": flag.get("description", ""),
                "position": position,
                "impact": impact,
                "email": email,
            })

    # Sort: HIGH first, then MEDIUM
    playbook_items.sort(key=lambda x: 0 if x["severity"] == "high" else 1)

    if playbook_items:
        story.append(PageBreak())
        story.append(Paragraph("Negotiation Playbook", h1))
        story.append(HRFlowable(width="100%", thickness=2, color=TEAL))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph(
            "Ready-to-use negotiation positions and email templates for each flagged clause. "
            "Copy, personalise with your name and lease reference, and send to the landlord's agent.",
            ParagraphStyle("PlaybookIntro", parent=styles["Normal"], fontSize=8,
                           textColor=SLATE, fontName="Helvetica-Oblique", spaceAfter=4)
        ))
        story.append(Spacer(1, 4*mm))

        for item in playbook_items:
            sev = item["severity"]
            if sev == "high":
                sev_color = RED
                sev_bg    = RED_LIGHT
                sev_label = "HIGH RISK"
            else:
                sev_color = AMBER
                sev_bg    = AMBER_LIGHT
                sev_label = "MEDIUM RISK"

            # Severity badge + clause ref header
            badge_row = Table([[
                Paragraph(
                    f'<font color="{colors.white.hexval()}"><b>{sev_label}</b></font>',
                    ParagraphStyle("Badge", fontSize=8, fontName="Helvetica-Bold",
                                   alignment=TA_CENTER, textColor=colors.white)
                ),
                Paragraph(
                    f"<b>{item['clause_ref']}</b>",
                    ParagraphStyle("ClauseRef", fontSize=9, fontName="Helvetica-Bold",
                                   textColor=NAVY)
                ),
            ]], colWidths=[22*mm, 148*mm])
            badge_row.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), sev_color),
                ("BACKGROUND", (1, 0), (1, 0), sev_bg),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3*mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3*mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2*mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2*mm),
            ]))
            story.append(badge_row)

            # Description + financial exposure
            story.append(Spacer(1, 1*mm))
            story.append(Paragraph(
                item["description"],
                ParagraphStyle("PlayDesc", parent=styles["Normal"], fontSize=8,
                               textColor=TEXT, leading=11, leftIndent=4*mm)
            ))
            if item["impact"]:
                story.append(Paragraph(
                    f'<font color="{RED.hexval()}"><b>Financial exposure: {item["impact"]}</b></font>',
                    ParagraphStyle("Impact", parent=styles["Normal"], fontSize=8,
                                   fontName="Helvetica-Bold", leftIndent=4*mm, spaceAfter=2)
                ))

            # What to demand
            if item["position"]:
                story.append(Paragraph(
                    f'<font color="{TEAL.hexval()}">&#9658;</font>  <b>What to demand:</b>  {item["position"]}',
                    ParagraphStyle("Demand", parent=styles["Normal"], fontSize=8,
                                   textColor=TEXT, leading=11, leftIndent=4*mm, spaceAfter=2)
                ))

            # Ready-to-copy email box
            email_box = Table([[
                Paragraph(
                    "<b>Ready-to-copy email paragraph:</b>",
                    ParagraphStyle("EmailHdr", fontSize=7, fontName="Helvetica-Bold",
                                   textColor=SLATE, spaceAfter=3)
                )
            ], [
                Paragraph(
                    item["email"].replace('\n', '<br/>'),
                    ParagraphStyle("EmailText", fontSize=8, fontName="Times-Roman",
                                   textColor=TEXT, leading=12)
                )
            ]], colWidths=[170*mm])
            email_box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GREY),
                ("LEFTPADDING", (0, 0), (-1, -1), 4*mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4*mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3*mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3*mm),
                ("LINEAFTER", (0, 0), (0, -1), 2, TEAL),
                ("BOX", (0, 0), (-1, -1), 0.5, MID_GREY),
            ]))
            story.append(email_box)
            story.append(Spacer(1, 5*mm))

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
        f"{brand_name} accepts no liability for decisions made based on this report.",
        caption
    ))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        "Data hosted in Australia. Your documents are not used to train any AI model.",
        ParagraphStyle("Privacy", fontSize=7, textColor=SLATE, alignment=TA_CENTER,
                       fontName="Helvetica-Oblique")
    ))
    story.append(Spacer(1, 1*mm))
    footer_brand = brand_name + (f" \xb7 {brand_url}" if brand_url else "")
    story.append(Paragraph(
        f"Generated by {footer_brand} \xb7 {audit_date}",
        ParagraphStyle("Footer", fontSize=7, textColor=SLATE, alignment=TA_CENTER)
    ))

    # ── Build PDF ──────────────────────────────────────────────────────────
    doc.build(story)
    logger.info(f"PDF report generated: {output_path}")
    return output_path
