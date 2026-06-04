"""
generate_synthetic_leases.py
-----------------------------
Generates realistic synthetic Australian retail/commercial lease agreement PDFs 
using ReportLab. These leases contain specific clauses designed to trigger 
TenantSentry's red flag rules (RF001–RF010) under NSW, VIC, and QLD jurisdictions.

Run with:
    python scripts/generate_synthetic_leases.py
"""

import os
import sys
from pathlib import Path

# Setup paths
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
except ImportError:
    print("ReportLab is not installed. Please install it with 'pip install reportlab'")
    sys.exit(1)


# Define Lease Contents containing specific red-flag vulnerabilities for testing
NSW_LEASE_TEXT = [
    ("RETAIL LEASE AGREEMENT (NEW SOUTH WALES)", "title"),
    ("THIS LEASE is made on 1 July 2025 between the Landlord (Acme Commercial Holdings Pty Ltd) and the Tenant (Peta's Premium Bakery Pty Ltd) for Shop 4, 100 George Street, Sydney NSW 2000.", "body"),
    
    ("1. TERM AND COMMENCEMENT", "heading"),
    ("The lease term is five (5) years, commencing on 1 July 2025 and expiring on 30 June 2030. There are no options to renew contained within this agreement.", "body"),
    
    ("2. RENT AND RENT REVIEW", "heading"),
    ("2.1 The base rent is $150,000 per annum, exclusive of GST, payable in equal monthly installments of $12,500 in advance.", "body"),
    ("2.2 Rent Review Mechanism: On each anniversary of the Commencement Date, the Rent shall be reviewed to the greater of CPI or a fixed increase of 6.5% per annum. For the avoidance of doubt, the rent payable for the year following a review shall in no circumstances be less than the rent payable in the year immediately prior to the review (Ratchet Clause).", "body"),
    
    ("3. OUTGOINGS AND TAXES", "heading"),
    ("3.1 The Tenant must pay 100% of all Outgoings associated with the building. Outgoings shall include, but are not limited to, general maintenance, structural repairs, capital improvements, roof replacement, elevator upgrades, and land tax.", "body"),
    ("3.2 Land Tax Recovery: The Tenant agrees to pay or reimburse the Landlord for Land Tax assessed against the property. Land tax shall be calculated on the Landlord's multi-holding portfolio basis rather than a single-holding basis, reflecting the Landlord's overall tax bracket in New South Wales.", "body"),
    
    ("4. USE OF PREMISES AND EXCLUSIVITY", "heading"),
    ("4.1 The Permitted Use of the premises is a retail bakery and coffee shop.", "body"),
    ("4.2 Exclusivity: The Landlord does not grant the Tenant any exclusivity of trade. The Landlord reserves the right to lease any other shops in the building or surrounding development to competing bakeries, cafes, or food retailers at its sole discretion.", "body"),
    
    ("5. ASSIGNMENT AND SUBLETTING", "heading"),
    ("5.1 The Tenant shall not assign, sublet, or part with possession of the premises without the prior written consent of the Landlord.", "body"),
    ("5.2 Landlord's Discretion: The Landlord may withhold consent to an assignment or sublease in its absolute discretion. The Landlord shall have up to 90 days to respond to any formal request for assignment, and may charge an administrative fee of $5,000 for reviewing the application.", "body"),
    
    ("6. MAKE GOOD OBLIGATIONS", "heading"),
    ("6.1 Upon termination or expiry of this Lease, the Tenant must, at its own expense, completely remove all fitout, signage, and equipment installed during the term.", "body"),
    ("6.2 The Tenant must reinstate the premises to its original bare-shell condition, returning the concrete slab floor and walls to a completely clean, unpainted state, regardless of any normal wear and tear.", "body"),
    
    ("7. DIRECTORS' PERSONAL GUARANTEES", "heading"),
    ("7.1 The Directors of the Tenant company (Peter Baker and Sarah Baker) must provide an absolute and unlimited personal guarantee, jointly and severally, to secure the performance of all Tenant covenants, rent payments, and indemnity obligations under this Lease.", "body"),
    ("7.2 The personal guarantee remains active for the full duration of the lease term, any holdover period, and any subsequent renewals, without any cap or limitation on liability.", "body"),
]

VIC_LEASE_TEXT = [
    ("RETAIL PREMISES LEASE (VICTORIA)", "title"),
    ("THIS LEASE is made on 1 July 2025 between the Landlord (Melbourne Retail Syndicate) and the Tenant (Bounce Fitness Studio Pty Ltd) for Level 1, 350 Collins Street, Melbourne VIC 3000.", "body"),
    
    ("1. TERM AND COMMENCEMENT", "heading"),
    ("The lease term is three (3) years, commencing on 1 July 2025 and expiring on 30 June 2028.", "body"),
    
    ("2. RENT AND RENT REVIEW", "heading"),
    ("2.1 The base rent is $180,000 per annum, exclusive of GST, payable in equal monthly installments of $15,000 in advance.", "body"),
    ("2.2 Rent Review: Rent shall be reviewed on each anniversary of the Commencement Date by a fixed increase of 5% per annum.", "body"),
    
    ("3. OUTGOINGS AND LAND TAX", "heading"),
    ("3.1 The Tenant must pay its proportional share (15%) of all operational outgoings of the building including security, cleaning, utilities, insurance, and municipal rates.", "body"),
    ("3.2 Land Tax Recovery: The Tenant agrees to pay the Landlord's Land Tax assessed against the land on which the building is erected, representing the Tenant's proportional share of the Landlord's single-holding land tax assessment.", "body"),
    ("3.3 Capital Works: The Tenant shall contribute to a capital replacement fund for the air-conditioning plant and building lifts, calculated on an annual basis and billed monthly as a recoverable outgoing.", "body"),
    
    ("4. ASSIGNMENT AND SUBLETTING", "heading"),
    ("4.1 The Tenant must not transfer, assign, or sublet the premises without the Landlord's prior written consent, which shall not be unreasonably withheld.", "body"),
    
    ("5. DEMOLITION AND REDEVELOPMENT", "heading"),
    ("5.1 In the event that the Landlord intends to demolish, renovate, or redevelop the building, the Landlord may terminate this lease by providing ninety (90) days written notice to the Tenant.", "body"),
    ("5.2 Upon expiry of the demolition notice, the Tenant must deliver vacant possession of the premises. The Tenant agrees that no compensation shall be payable by the Landlord for fitout write-offs, relocation expenses, or loss of business profits resulting from early termination under this clause.", "body"),
    
    ("6. OVERHOLDING / PENALTY RATE", "heading"),
    ("6.1 If the Tenant remains in possession of the premises after the expiration of the term with the Landlord's consent, the tenancy shall continue as a month-to-month holdover tenancy.", "body"),
    ("6.2 During any overholding period, the Tenant shall pay holdover rent calculated at a rate of 200% of the rent payable immediately prior to the lease expiration, billed daily in advance.", "body"),
]

QLD_LEASE_TEXT = [
    ("COMMERCIAL SHOP LEASE (QUEENSLAND)", "title"),
    ("THIS LEASE is made on 1 July 2025 between the Landlord (Brisbane Riverfront Properties Ltd) and the Tenant (Coastal Juice Bar Pty Ltd) for Shop 2, 50 Eagle Street, Brisbane QLD 4000.", "body"),
    
    ("1. TERM AND COMMENCEMENT", "heading"),
    ("The lease term is four (4) years, commencing on 1 July 2025 and expiring on 30 June 2029.", "body"),
    
    ("2. RENT AND RENT REVIEW", "heading"),
    ("2.1 The base rent is $110,000 per annum, exclusive of GST, payable in equal monthly installments of $9,166.67 in advance.", "body"),
    ("2.2 Rent Review: On each anniversary of the Commencement Date, the Rent shall be reviewed to the market rent as determined by the Landlord's appointed valuer.", "body"),
    
    ("3. TURNOVER RENT", "heading"),
    ("3.1 In addition to the base rent, the Tenant must pay Turnover Rent equal to 12% of the Tenant's gross monthly turnover.", "body"),
    ("3.2 The Turnover Rent is payable from dollar one, meaning the turnover threshold is set to zero ($0.00), and is billed monthly based on the Tenant's certified sales reports.", "body"),
    
    ("4. OUTGOINGS AND AUDITS", "heading"),
    ("4.1 The Tenant must pay 100% of all outgoings, including municipal rates, water, cleaning, management fees, and land tax.", "body"),
    ("4.2 EOFY Statement: The Landlord will provide the Tenant with an annual statement of actual outgoings. The Tenant agrees to waive any right to receive a registered auditor's report or certified statement of outgoings, and accepts the Landlord's internal accounting records as final and binding.", "body"),
    
    ("5. RELOCATION RIGHTS", "heading"),
    ("5.1 Relocation Clause: The Landlord reserves the right to relocate the Tenant to alternative premises within the commercial development at any time during the term.", "body"),
    ("5.2 Notice and Cost: The Landlord shall give the Tenant fourteen (14) days written notice of relocation. The Tenant shall bear all costs of dismantling its fitout, moving stock, and installing new fitout in the alternative premises. The alternative premises shall be accepted by the Tenant in its 'as-is' condition.", "body"),
]


def create_lease_pdf(filename: Path, data: list[tuple[str, str]]):
    """Generate a clean, structured PDF from the lease text data using ReportLab."""
    doc = SimpleDocTemplate(
        str(filename),
        pagesize=letter,
        rightMargin=54,
        leftMargin=54,
        topMargin=54,
        bottomMargin=54
    )
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'LeaseTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        alignment=TA_CENTER,
        textColor='#1A365D',
        spaceAfter=20
    )
    
    heading_style = ParagraphStyle(
        'LeaseHeading',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor='#2C5282',
        spaceBefore=15,
        spaceAfter=8,
        keepWithNext=True
    )
    
    body_style = ParagraphStyle(
        'LeaseBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        alignment=TA_JUSTIFY,
        spaceAfter=10
    )
    
    story = []
    
    for text, style_type in data:
        if style_type == 'title':
            story.append(Paragraph(text, title_style))
            story.append(Spacer(1, 10))
        elif style_type == 'heading':
            story.append(Paragraph(text, heading_style))
        else:
            story.append(Paragraph(text, body_style))
            
    doc.build(story)
    print(f"Created: {filename.name}")


def main():
    test_leases_dir = ROOT / "test_leases"
    test_leases_dir.mkdir(parents=True, exist_ok=True)
    
    print("=== Generating Synthetic Australian Leases ===")
    
    leases = [
        ("nsw_retail_lease_sample.pdf", NSW_LEASE_TEXT),
        ("vic_retail_lease_sample.pdf", VIC_LEASE_TEXT),
        ("qld_retail_lease_sample.pdf", QLD_LEASE_TEXT),
    ]
    
    for filename, text_data in leases:
        filepath = test_leases_dir / filename
        create_lease_pdf(filepath, text_data)
        
    print(f"\nSuccess! 3 sample leases generated in: {test_leases_dir.relative_to(ROOT.parent)}")
    print("You can now upload these files to test your lease parsing and AI audit logic.")


if __name__ == "__main__":
    main()
