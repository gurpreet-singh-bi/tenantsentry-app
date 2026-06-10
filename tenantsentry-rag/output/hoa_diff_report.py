"""
hoa_diff_report.py
------------------
Area 3: Lease Creep Report PDF generator.

Generates a professional A4 PDF from a DiffResult showing:
  - Cover page: HoA vs Lease summary, finding counts
  - High severity findings (full detail)
  - Medium/Low findings (condensed)
  - Negotiation playbook: ready-to-copy email paragraphs per finding
"""

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

# ── Brand colours (match report_generator) ────────────────────────────────────
NAVY      = colors.HexColor("#0f172a")
TEAL      = colors.HexColor("#0d9488")
TEAL_LIGHT= colors.HexColor("#ccfbf1")
RED       = colors.HexColor("#dc2626")
RED_LIGHT = colors.HexColor("#fee2e2")
AMBER     = colors.HexColor("#d97706")
AMBER_LIGHT=colors.HexColor("#fef3c7")
GREEN     = colors.HexColor("#16a34a")
GREEN_LIGHT=colors.HexColor("#dcfce7")
SLATE     = colors.HexColor("#64748b")
LIGHT_GREY= colors.HexColor("#f8fafc")
MID_GREY  = colors.HexColor("#e2e8f0")
TEXT      = colors.HexColor("#1e293b")

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm

REPORTS_DIR = Path(tempfile.gettempdir()) / "tenantsentry_reports"
REPORTS_DIR.mkdir(exist_ok=True)

_CATEGORY_LABELS = {
    "rent":            "Rent / Rent Review",
    "make_good":       "Make Good",
    "relocation":      "Relocation",
    "demolition":      "Demolition / Redevelopment",
    "assignment":      "Assignment",
    "outgoings":       "Outgoings",
    "fitout":          "Fitout / Works",
    "bank_guarantee":  "Bank Guarantee / Security",
    "options":         "Options",
    "access":          "Access / Hours",
    "liability":       "Liability / Indemnity",
    "new_restriction": "New Restriction",
    "other":           "Other",
}


def _sev_color(s: str) -> colors.Color:
    return RED if s == "high" else (AMBER if s == "medium" else GREEN)


def _sev_bg(s: str) -> colors.Color:
    return RED_LIGHT if s == "high" else (AMBER_LIGHT if s == "medium" else GREEN_LIGHT)


def generate_hoa_diff_report(diff_result: dict, job_id: str) -> str:
    """
    Generate a Lease Creep Report PDF.

    Args:
        diff_result: DiffResult.to_dict()
        job_id:      Used to name the output file

    Returns:
        Absolute path to the generated PDF.
    """
    output_path = str(REPORTS_DIR / f"hoa_diff_{job_id}.pdf")

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )
    styles = getSampleStyleSheet()
    story  = []

    h1 = ParagraphStyle("H1", fontSize=22, textColor=NAVY, fontName="Helvetica-Bold", spaceAfter=4)
    h2 = ParagraphStyle("H2", fontSize=14, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=10, spaceAfter=4)
    h3 = ParagraphStyle("H3", fontSize=11, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=6, spaceAfter=2)
    body = ParagraphStyle("Body", fontSize=9, textColor=TEXT, leading=14)
    small= ParagraphStyle("Small", fontSize=8, textColor=SLATE, leading=11)
    mono = ParagraphStyle("Mono", fontSize=8, textColor=TEXT, fontName="Courier", leading=12)
    sec_label = ParagraphStyle("SecLabel", fontSize=7, textColor=SLATE, fontName="Helvetica-Bold",
                               spaceBefore=4, spaceAfter=1)
    caption = ParagraphStyle("Caption", fontSize=8, textColor=SLATE, fontName="Helvetica-Oblique")

    hoa_filename   = diff_result.get("hoa_filename", "HoA")
    lease_filename = diff_result.get("lease_filename", "Lease")
    jurisdiction   = diff_result.get("jurisdiction", "")
    total          = diff_result.get("total_findings", 0)
    high_count     = diff_result.get("high_count", 0)
    medium_count   = diff_result.get("medium_count", 0)
    low_count      = diff_result.get("low_count", 0)
    findings       = diff_result.get("findings", [])
    warnings       = diff_result.get("pipeline_warnings", [])
    today          = datetime.now().strftime("%d %B %Y")

    # ══════════════════════════════════════════════════════
    # COVER
    # ══════════════════════════════════════════════════════

    # Header bar
    hdr = Table([[
        Paragraph("TenantSentry.ai", ParagraphStyle("Brand", fontSize=20, textColor=colors.white, fontName="Helvetica-Bold")),
        Paragraph("LEASE CREEP REPORT", ParagraphStyle("Sub", fontSize=10, textColor=TEAL, fontName="Helvetica-Bold", alignment=TA_RIGHT)),
    ]], colWidths=[120*mm, 50*mm])
    hdr.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,-1), NAVY),
        ("VALIGN", (0,0),(-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0),(0,0), 8*mm),
        ("RIGHTPADDING", (-1,0),(-1,0), 8*mm),
        ("TOPPADDING", (0,0),(-1,-1), 6*mm),
        ("BOTTOMPADDING", (0,0),(-1,-1), 6*mm),
    ]))
    story.append(hdr)
    story.append(Spacer(1, 10*mm))

    story.append(Paragraph("HoA vs Final Lease Comparison", h1))
    story.append(Paragraph(
        "This report identifies every term that was altered, added, or removed between the "
        "Heads of Agreement and the final draft lease — what we call <b>lease creep</b>.",
        ParagraphStyle("Intro", fontSize=9, textColor=SLATE, fontName="Helvetica-Oblique", leading=13)
    ))
    story.append(Spacer(1, 6*mm))

    meta = Table([
        ["HoA document:",   hoa_filename],
        ["Final lease:",    lease_filename],
        ["Jurisdiction:",   jurisdiction],
        ["Comparison date:", today],
    ], colWidths=[42*mm, 128*mm])
    meta.setStyle(TableStyle([
        ("FONTNAME", (0,0),(0,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0),(-1,-1), 9),
        ("TEXTCOLOR", (0,0),(0,-1), SLATE),
        ("TEXTCOLOR", (1,0),(1,-1), TEXT),
        ("TOPPADDING", (0,0),(-1,-1), 2),
        ("BOTTOMPADDING", (0,0),(-1,-1), 2),
    ]))
    story.append(meta)
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=MID_GREY))
    story.append(Spacer(1, 6*mm))

    # Summary scorecard
    score_data = [[
        [
            Paragraph(f'<font size="36" color="{RED.hexval()}"><b>{total}</b></font><br/>'
                      f'<font size="8" color="#64748b">TOTAL FINDINGS</font>', ParagraphStyle("N", alignment=TA_CENTER)),
        ],
        Table([[
            Paragraph(f"<b>{high_count}</b>", ParagraphStyle("N", fontSize=22, alignment=TA_CENTER, textColor=RED)),
            Paragraph(f"<b>{medium_count}</b>", ParagraphStyle("N", fontSize=22, alignment=TA_CENTER, textColor=AMBER)),
            Paragraph(f"<b>{low_count}</b>", ParagraphStyle("N", fontSize=22, alignment=TA_CENTER, textColor=GREEN)),
        ],[
            Paragraph("High", ParagraphStyle("L", fontSize=7, alignment=TA_CENTER, textColor=SLATE)),
            Paragraph("Medium", ParagraphStyle("L", fontSize=7, alignment=TA_CENTER, textColor=SLATE)),
            Paragraph("Low", ParagraphStyle("L", fontSize=7, alignment=TA_CENTER, textColor=SLATE)),
        ]], colWidths=[40*mm]*3),
    ]]
    score_tbl = Table(score_data, colWidths=[50*mm, 120*mm])
    score_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,-1), LIGHT_GREY),
        ("BOX", (0,0),(-1,-1), 1, MID_GREY),
        ("VALIGN", (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0),(-1,-1), 6*mm),
        ("BOTTOMPADDING", (0,0),(-1,-1), 6*mm),
        ("LINEAFTER", (0,0),(0,-1), 1, MID_GREY),
    ]))
    story.append(score_tbl)
    story.append(Spacer(1, 4*mm))

    if high_count:
        story.append(Paragraph(
            f"<b>{high_count} HIGH severity findings</b> require immediate negotiation before the lease is executed. "
            f"These represent material departures from the agreed HoA terms.",
            ParagraphStyle("Alert", fontSize=8, textColor=RED, fontName="Helvetica-Bold", leading=12)
        ))
        story.append(Spacer(1, 2*mm))

    story.append(Paragraph(
        "This report was generated by TenantSentry.ai. It is for informational purposes only "
        "and does not constitute legal advice. Verify all findings against the original documents "
        "before sending correspondence to the landlord.",
        caption
    ))

    # ══════════════════════════════════════════════════════
    # FINDINGS — High priority first, then medium, then low
    # ══════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("Lease Creep Findings", h2))
    story.append(HRFlowable(width="100%", thickness=2, color=TEAL))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        "Each finding below identifies a term that was altered or added between the "
        "Heads of Agreement and the final draft lease. Findings are ordered by severity.",
        ParagraphStyle("Sub", fontSize=8, textColor=SLATE, fontName="Helvetica-Oblique", leading=12)
    ))
    story.append(Spacer(1, 4*mm))

    sorted_findings = (
        [f for f in findings if f.get("severity") == "high"] +
        [f for f in findings if f.get("severity") == "medium"] +
        [f for f in findings if f.get("severity") == "low"]
    )

    for i, finding in enumerate(sorted_findings, 1):
        sev       = finding.get("severity", "medium")
        category  = finding.get("category", "other")
        cat_label = _CATEGORY_LABELS.get(category, category.replace("_", " ").title())
        hoa_term  = finding.get("hoa_term", "")
        lease_term= finding.get("lease_term", "")
        clause_ref= finding.get("clause_reference", "")
        page_ref  = finding.get("page_reference")
        description = finding.get("description", "")
        action    = finding.get("recommended_action", "")
        email     = finding.get("negotiation_email", "")

        sev_c  = _sev_color(sev)
        sev_bg = _sev_bg(sev)

        loc_parts = []
        if clause_ref:
            loc_parts.append(clause_ref)
        if page_ref:
            loc_parts.append(f"Page {page_ref}")
        loc_str = "  ·  ".join(loc_parts) if loc_parts else "—"

        # Finding header
        fhdr = Table([[
            [
                Paragraph(f"<b>{i}. {cat_label}</b>",
                          ParagraphStyle("FH", fontSize=10, textColor=NAVY, fontName="Helvetica-Bold")),
                Paragraph(f"Location: {loc_str}",
                          ParagraphStyle("FLoc", fontSize=7, textColor=SLATE)),
            ],
            Paragraph(f"<b>{sev.upper()}</b>",
                      ParagraphStyle("FSev", fontSize=9, textColor=sev_c, fontName="Helvetica-Bold", alignment=TA_RIGHT)),
        ]], colWidths=[145*mm, 25*mm])
        fhdr.setStyle(TableStyle([
            ("BACKGROUND", (0,0),(-1,-1), sev_bg),
            ("TOPPADDING", (0,0),(-1,-1), 3*mm),
            ("BOTTOMPADDING", (0,0),(-1,-1), 3*mm),
            ("LEFTPADDING", (0,0),(0,0), 4*mm),
            ("RIGHTPADDING", (-1,0),(-1,0), 4*mm),
            ("VALIGN", (0,0),(-1,-1), "MIDDLE"),
        ]))
        story.append(KeepTogether([fhdr]))

        # HoA vs Lease comparison table
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("① HoA vs Final Lease", sec_label))
        diff_tbl = Table([
            [
                Paragraph("<b>Heads of Agreement</b>",
                          ParagraphStyle("DH", fontSize=8, textColor=NAVY, fontName="Helvetica-Bold", alignment=TA_CENTER)),
                Paragraph("<b>Final Draft Lease</b>",
                          ParagraphStyle("DH", fontSize=8, textColor=RED, fontName="Helvetica-Bold", alignment=TA_CENTER)),
            ],[
                Paragraph(hoa_term or "—", ParagraphStyle("DT", fontSize=8, textColor=TEXT, leading=12, fontName="Courier")),
                Paragraph(lease_term or "—", ParagraphStyle("DT", fontSize=8, textColor=TEXT, leading=12, fontName="Courier")),
            ],
        ], colWidths=[85*mm, 85*mm])
        diff_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0),(0,0), TEAL_LIGHT),
            ("BACKGROUND", (1,0),(1,0), RED_LIGHT),
            ("BACKGROUND", (0,1),(0,1), LIGHT_GREY),
            ("BACKGROUND", (1,1),(1,1), colors.HexColor("#fff7f7")),
            ("BOX", (0,0),(-1,-1), 0.5, MID_GREY),
            ("LINEAFTER", (0,0),(0,-1), 0.5, MID_GREY),
            ("TOPPADDING", (0,0),(-1,-1), 2*mm),
            ("BOTTOMPADDING", (0,0),(-1,-1), 2*mm),
            ("LEFTPADDING", (0,0),(-1,-1), 3*mm),
            ("RIGHTPADDING", (0,0),(-1,-1), 3*mm),
            ("VALIGN", (0,0),(-1,-1), "TOP"),
        ]))
        story.append(diff_tbl)

        # Description
        if description:
            story.append(Spacer(1, 2*mm))
            story.append(Paragraph("② Why this matters", sec_label))
            story.append(Paragraph(description, body))

        # Recommended action
        if action:
            story.append(Spacer(1, 2*mm))
            story.append(Paragraph("③ Required change", sec_label))
            act_row = Table([[
                Paragraph("→", ParagraphStyle("Icon", fontSize=9, textColor=TEAL, fontName="Helvetica-Bold")),
                Paragraph(action, ParagraphStyle("Act", fontSize=8, textColor=TEXT, leading=11)),
            ]], colWidths=[6*mm, 164*mm])
            act_row.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1)]))
            story.append(act_row)

        story.append(Spacer(1, 4*mm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GREY))
        story.append(Spacer(1, 3*mm))

    # ══════════════════════════════════════════════════════
    # NEGOTIATION PLAYBOOK — email templates
    # ══════════════════════════════════════════════════════
    email_findings = [f for f in sorted_findings if f.get("negotiation_email")]
    if email_findings:
        story.append(PageBreak())
        story.append(Paragraph("Negotiation Playbook", h2))
        story.append(HRFlowable(width="100%", thickness=2, color=TEAL))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(
            "Ready-to-copy email paragraphs for each finding. Address to the landlord's "
            "agent or solicitor. Review with your solicitor before sending.",
            ParagraphStyle("PI", fontSize=8, textColor=SLATE, fontName="Helvetica-Oblique", leading=12)
        ))
        story.append(Spacer(1, 4*mm))

        for i, finding in enumerate(email_findings, 1):
            cat_label = _CATEGORY_LABELS.get(finding.get("category","other"), "Other")
            sev       = finding.get("severity", "medium")
            email     = finding.get("negotiation_email", "")
            sev_c     = _sev_color(sev)

            story.append(Paragraph(
                f'<font color="{sev_c.hexval()}">●</font>  <b>{i}. {cat_label}</b>  '
                f'<font color="{SLATE.hexval()}" size="7">({sev.upper()})</font>',
                ParagraphStyle("EH", fontSize=9, textColor=NAVY, fontName="Helvetica-Bold", spaceAfter=2)
            ))

            email_box = Table([[
                Paragraph(email, ParagraphStyle("Email", fontSize=8.5, textColor=TEXT, leading=13, fontName="Times-Roman")),
            ]], colWidths=[170*mm])
            email_box.setStyle(TableStyle([
                ("BACKGROUND", (0,0),(-1,-1), LIGHT_GREY),
                ("BOX", (0,0),(-1,-1), 1, TEAL),
                ("TOPPADDING", (0,0),(-1,-1), 3*mm),
                ("BOTTOMPADDING", (0,0),(-1,-1), 3*mm),
                ("LEFTPADDING", (0,0),(-1,-1), 4*mm),
                ("RIGHTPADDING", (0,0),(-1,-1), 4*mm),
            ]))
            story.append(email_box)
            story.append(Spacer(1, 3*mm))

    # ── Disclaimer ─────────────────────────────────────────
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=MID_GREY))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "<b>Disclaimer:</b> This Lease Creep Report was generated by AI analysis of two uploaded documents. "
        "It is provided for informational purposes only and does not constitute legal advice. "
        "Always verify findings against the original documents. Consult a qualified commercial "
        "lease solicitor before sending any correspondence to the landlord or their solicitors. "
        "TenantSentry.ai accepts no liability for decisions made based on this report.",
        ParagraphStyle("Disc", fontSize=7.5, textColor=SLATE, leading=11, fontName="Helvetica-Oblique")
    ))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"Generated by TenantSentry.ai · {today} · tenantsentry.ai",
        ParagraphStyle("Footer", fontSize=7, textColor=SLATE, alignment=TA_CENTER)
    ))

    doc.build(story)
    logger.info(f"[HoA-diff] PDF report generated: {output_path}")
    return output_path
