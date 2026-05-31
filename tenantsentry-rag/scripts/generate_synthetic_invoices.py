"""
generate_synthetic_invoices.py
------------------------------
Generates realistic monthly invoices and EOFY outgoings statements for FY26 
(covering July 2025 to June 2026) using ReportLab.

These invoices correspond to the synthetic leases (NSW, VIC, QLD) and contain 
intentional billing overcharges/discrepancies designed to be caught by 
TenantSentry's audit engine.

Run with:
    python scripts/generate_synthetic_invoices.py
"""

import os
import sys
from pathlib import Path

# Setup paths
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
    from reportlab.lib import colors
except ImportError:
    print("ReportLab is not installed. Please install it with 'pip install reportlab'")
    sys.exit(1)


def draw_invoice(filename: Path, title: str, meta_data: list[list[str]], line_items: list[list[str]], total_text: str, notes: str = None):
    """Draw a highly professional, realistic B2B invoice PDF using ReportLab tables."""
    doc = SimpleDocTemplate(
        str(filename),
        pagesize=letter,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40
    )
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'InvoiceTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor='#1A365D',
        spaceAfter=15
    )
    
    sub_title_style = ParagraphStyle(
        'InvoiceSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=12,
        textColor='#4A5568'
    )
    
    text_style = ParagraphStyle(
        'InvoiceText',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor='#2D3748'
    )
    
    right_text_style = ParagraphStyle(
        'InvoiceRightText',
        parent=text_style,
        alignment=TA_RIGHT
    )
    
    bold_text_style = ParagraphStyle(
        'InvoiceBoldText',
        parent=text_style,
        fontName='Helvetica-Bold'
    )
    
    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor='#FFFFFF'
    )
    
    story = []
    
    # Title & Header
    story.append(Paragraph(title, title_style))
    story.append(Spacer(1, 10))
    
    # Metadata Block (Landlord & Tenant Info, Date, Invoice No)
    meta_table_data = []
    for row in meta_data:
        meta_table_data.append([
            Paragraph(row[0], bold_text_style),
            Paragraph(row[1], text_style),
            Paragraph(row[2], bold_text_style),
            Paragraph(row[3], text_style)
        ])
    
    meta_table = Table(meta_table_data, colWidths=[100, 160, 100, 160])
    meta_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 20))
    
    # Line Items Table
    headers = ["Description", "Unit Cost", "Qty", "Total (Ex GST)", "GST", "Total (Inc GST)"]
    formatted_items = [[Paragraph(h, table_header_style) for h in headers]]
    
    for item in line_items:
        formatted_items.append([
            Paragraph(item[0], text_style),
            Paragraph(item[1], right_text_style),
            Paragraph(item[2], right_text_style),
            Paragraph(item[3], right_text_style),
            Paragraph(item[4], right_text_style),
            Paragraph(item[5], right_text_style),
        ])
    
    # Total row
    formatted_items.append([
        Paragraph("<b>TOTAL PAYABLE:</b>", text_style),
        Paragraph("", text_style),
        Paragraph("", text_style),
        Paragraph("", text_style),
        Paragraph("", text_style),
        Paragraph(f"<b>{total_text}</b>", right_text_style),
    ])
    
    item_table = Table(formatted_items, colWidths=[200, 60, 40, 75, 55, 90])
    item_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1A365D')),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,0), 6),
        ('TOPPADDING', (0,0), (-1,0), 6),
        ('BOTTOMPADDING', (0,1), (-1,-1), 6),
        ('TOPPADDING', (0,1), (-1,-1), 6),
        ('LINEBELOW', (0,0), (-1,-2), 0.5, colors.HexColor('#E2E8F0')),
        ('LINEBELOW', (0,-1), (-1,-1), 1.5, colors.HexColor('#1A365D')),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#F7FAFC')),
    ]))
    story.append(item_table)
    story.append(Spacer(1, 15))
    
    if notes:
        story.append(Paragraph("<b>Important Notice / Billing Notes:</b>", sub_title_style))
        story.append(Spacer(1, 5))
        story.append(Paragraph(notes, text_style))
        
    doc.build(story)
    print(f"Created: {filename.name}")


def main():
    test_invoices_dir = ROOT / "test_invoices"
    nsw_dir = test_invoices_dir / "nsw"
    vic_dir = test_invoices_dir / "vic"
    qld_dir = test_invoices_dir / "qld"
    
    for d in [nsw_dir, vic_dir, qld_dir]:
        d.mkdir(parents=True, exist_ok=True)
        
    print("=== Generating Synthetic FY26 Invoices & EOFY Reconciliation Statements ===")
    
    # ─── 1. NSW INVOICES ──────────────────────────────────────────────────────────
    # Monthly invoice for October 2025
    nsw_meta_oct = [
        ["Landlord:", "Acme Commercial Holdings Pty Ltd", "Invoice Date:", "1 Oct 2025"],
        ["Tenant:", "Peta's Premium Bakery Pty Ltd", "Invoice Number:", "INV-2025-NSW-104"],
        ["Premises:", "Shop 4, 100 George St, Sydney NSW", "Due Date:", "14 Oct 2025"]
    ]
    nsw_items_oct = [
        ["Monthly Base Rent - October 2025", "$12,500.00", "1", "$12,500.00", "$1,250.00", "$13,750.00"],
        ["Proportional Outgoings Estimate (Monthly)", "$850.00", "1", "$850.00", "$85.00", "$935.00"]
    ]
    draw_invoice(
        nsw_dir / "nsw_monthly_rent_invoice_oct2025.pdf",
        "TAX INVOICE - MONTHLY RENT",
        nsw_meta_oct,
        nsw_items_oct,
        "$14,685.00",
        "Base rent billed in accordance with Clause 2.1 of your Lease Agreement."
    )
    
    # EOFY 2026 Outgoings Reconciliation Statement (CONTAINS RED FLAGS!)
    # Red Flags: 
    # - Charged Land Tax on a multi-holding portfolio basis (Violates Single-holding covenant)
    # - Charged for roof repairs / capital works (Structural - Prohibited under NSW Retail Leases Act s.12)
    nsw_meta_eofy = [
        ["Landlord:", "Acme Commercial Holdings Pty Ltd", "Statement Date:", "15 July 2026"],
        ["Tenant:", "Peta's Premium Bakery Pty Ltd", "Statement ID:", "STMT-2026-NSW-EOFY"],
        ["Premises:", "Shop 4, 100 George St, Sydney NSW", "Audit Period:", "FY26 (1 Jul 2025 - 30 Jun 2026)"]
    ]
    nsw_items_eofy = [
        ["Landlord Land Tax Recovery (NSW Multi-holding Portfolio rate)", "$15,000.00", "1", "$15,000.00", "$0.00", "$15,000.00"],
        ["Building Outgoings Share (Cleaning, Council Rates, Water)", "$10,200.00", "1", "$10,200.00", "$1,020.00", "$11,220.00"],
        ["Roof Refurbishment & Structural Tile Works Contribution", "$8,500.00", "1", "$8,500.00", "$850.00", "$9,350.00"],
        ["Less: Monthly Outgoings Contributions Paid during FY26", "-$850.00", "12", "-$10,200.00", "-$1,020.00", "-$11,220.00"]
    ]
    draw_invoice(
        nsw_dir / "nsw_eofy_outgoings_reconciliation_2026.pdf",
        "EOFY OUTGOINGS RECONCILIATION STATEMENT",
        nsw_meta_eofy,
        nsw_items_eofy,
        "$24,350.00",
        "Note: In accordance with Clause 3.1 & 3.2, land tax is assessed on the Landlord's portfolio valuation. Roof works represent the necessary upkeep of the building exterior structural elements."
    )
    
    # ─── 2. VIC INVOICES ──────────────────────────────────────────────────────────
    # Monthly invoice for October 2025
    vic_meta_oct = [
        ["Landlord:", "Melbourne Retail Syndicate", "Invoice Date:", "1 Oct 2025"],
        ["Tenant:", "Bounce Fitness Studio Pty Ltd", "Invoice Number:", "INV-2025-VIC-098"],
        ["Premises:", "Level 1, 350 Collins St, Melbourne VIC", "Due Date:", "14 Oct 2025"]
    ]
    vic_items_oct = [
        ["Monthly Base Rent - October 2025", "$15,000.00", "1", "$15,000.00", "$1,500.00", "$16,500.00"],
        ["Proportional Outgoings Estimate (Monthly)", "$1,100.00", "1", "$1,100.00", "$110.00", "$1,210.00"]
    ]
    draw_invoice(
        vic_dir / "vic_monthly_rent_invoice_oct2025.pdf",
        "TAX INVOICE - MONTHLY RENT",
        vic_meta_oct,
        vic_items_oct,
        "$17,710.00"
    )
    
    # EOFY 2026 Outgoings Reconciliation Statement (CONTAINS RED FLAGS!)
    # Red Flags:
    # - Recovering Land Tax in Victoria (VOID UNDER RETAIL LEASES ACT 2003 s.23!)
    # - Air Conditioning plant/elevator upgrade contribution (Capital replacement - Prohibited!)
    vic_meta_eofy = [
        ["Landlord:", "Melbourne Retail Syndicate", "Statement Date:", "10 July 2026"],
        ["Tenant:", "Bounce Fitness Studio Pty Ltd", "Statement ID:", "STMT-2026-VIC-EOFY"],
        ["Premises:", "Level 1, 350 Collins St, Melbourne VIC", "Audit Period:", "FY26 (1 Jul 2025 - 30 Jun 2026)"]
    ]
    vic_items_eofy = [
        ["Land Tax Recovery (Landlord Assessment)", "$12,000.00", "1", "$12,000.00", "$0.00", "$12,000.00"],
        ["Operational Outgoings Share (Council Rates, Insurance, Cleaning)", "$13,200.00", "1", "$13,200.00", "$1,320.00", "$14,520.00"],
        ["HVAC Cooling Tower / Aircon Plant Upgrade Contribution", "$7,500.00", "1", "$7,500.00", "$750.00", "$8,250.00"],
        ["Less: Monthly Outgoings Contributions Paid during FY26", "-$1,100.00", "12", "-$13,200.00", "-$1,320.00", "-$14,520.00"]
    ]
    draw_invoice(
        vic_dir / "vic_eofy_outgoings_reconciliation_2026.pdf",
        "EOFY OUTGOINGS RECONCILIATION STATEMENT",
        vic_meta_eofy,
        vic_items_eofy,
        "$20,250.00",
        "Note: Land tax is recovered in accordance with Clause 3.2. Air conditioning tower replacement has been apportioned to the tenant's share under Clause 3.3 for capital works."
    )
    
    # ─── 3. QLD INVOICES ──────────────────────────────────────────────────────────
    # Monthly invoice for October 2025 (CONTAINS RED FLAGS!)
    # Red Flag: Turnover Rent calculation from base zero threshold (Clause 3)
    qld_meta_oct = [
        ["Landlord:", "Brisbane Riverfront Properties Ltd", "Invoice Date:", "1 Oct 2025"],
        ["Tenant:", "Coastal Juice Bar Pty Ltd", "Invoice Number:", "INV-2025-QLD-201"],
        ["Premises:", "Shop 2, 50 Eagle St, Brisbane QLD", "Due Date:", "14 Oct 2025"]
    ]
    qld_items_oct = [
        ["Monthly Base Rent - October 2025", "$9,166.67", "1", "$9,166.67", "$916.67", "$10,083.34"],
        ["Turnover Rent (12% of Gross Sales $37,500.00 from $0.00 threshold)", "$4,500.00", "1", "$4,500.00", "$450.00", "$4,950.00"],
        ["Proportional Outgoings Estimate (Monthly)", "$750.00", "1", "$750.00", "$75.00", "$825.00"]
    ]
    draw_invoice(
        qld_dir / "qld_monthly_rent_invoice_oct2025.pdf",
        "TAX INVOICE - MONTHLY RENT & TURNOVER",
        qld_meta_oct,
        qld_items_oct,
        "$15,858.34",
        "Turnover rent is calculated at 12% of Gross sales in accordance with Clause 3 of your commercial shop lease."
    )
    
    # EOFY 2026 Outgoings Reconciliation Statement (CONTAINS RED FLAGS!)
    # Red Flag: Outgoings statement without auditor's report / waiver statement
    qld_meta_eofy = [
        ["Landlord:", "Brisbane Riverfront Properties Ltd", "Statement Date:", "25 July 2026"],
        ["Tenant:", "Coastal Juice Bar Pty Ltd", "Statement ID:", "STMT-2026-QLD-EOFY"],
        ["Premises:", "Shop 2, 50 Eagle St, Brisbane QLD", "Audit Period:", "FY26 (1 Jul 2025 - 30 Jun 2026)"]
    ]
    qld_items_eofy = [
        ["Actual Outgoings Reconciliation Balance (Council Rates, Water, Management)", "$9,800.00", "1", "$9,800.00", "$980.00", "$10,780.00"],
        ["Land Tax Recovery Contribution (FY26 Single-holding estimate)", "$3,400.00", "1", "$3,400.00", "$0.00", "$3,400.00"],
        ["Less: Monthly Outgoings Contributions Paid during FY26", "-$750.00", "12", "-$9,000.00", "-$900.00", "-$9,900.00"]
    ]
    draw_invoice(
        qld_dir / "qld_eofy_outgoings_reconciliation_2026.pdf",
        "EOFY OUTGOINGS RECONCILIATION STATEMENT",
        qld_meta_eofy,
        qld_items_eofy,
        "$4,280.00",
        "In accordance with Clause 4.2 of the lease agreement, the tenant has waived their right to receive a registered auditor's report for this outgoings reconciliation. This statement serves as the final reconciliation invoice."
    )
    
    print(f"\nSuccess! 6 synthetic invoices/statements generated in: {test_invoices_dir.relative_to(ROOT.parent)}")
    print("These contain specific overcharges matching the lease clauses to test the audit engine.")


if __name__ == "__main__":
    main()
