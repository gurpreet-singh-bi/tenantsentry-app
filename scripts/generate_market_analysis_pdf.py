import os
from fpdf import FPDF
from fpdf.enums import XPos, YPos

class CustomPDF(FPDF):
    def header(self):
        if self.page_no() > 1:
            self.set_font("helvetica", "I", 8)
            self.set_text_color(113, 128, 150) # Slate grey
            self.cell(0, 10, "PropTech Market Gap Analysis: AI-Powered Commercial Lease Auditing", 
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="R")
            self.ln(2)
            # Thin divider line
            self.set_draw_color(226, 232, 240)
            self.set_line_width(0.5)
            self.line(20, 22, 190, 22)
            self.ln(4)

    def footer(self):
        # Position at 1.5 cm from bottom
        self.set_y(-15)
        self.set_font("helvetica", "I", 8)
        self.set_text_color(113, 128, 150)
        # Page number
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

def create_report():
    pdf = CustomPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(left=20, top=20, right=20)
    pdf.alias_nb_pages()
    pdf.add_page()
    
    # ------------------ COVER PAGE / HEADER ------------------
    # Accent color top block
    pdf.set_fill_color(26, 54, 93) # Deep Indigo
    pdf.rect(0, 0, 210, 45, "F")
    
    # Title text over the colored block
    pdf.set_xy(20, 12)
    pdf.set_font("helvetica", "B", 18)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 10, "PROPTECH MARKET GAP ANALYSIS", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_xy(20, 22)
    pdf.set_font("helvetica", "B", 13)
    pdf.set_text_color(49, 151, 149) # Teal accent
    pdf.cell(0, 10, "AI-Powered Commercial Lease Auditing for Occupiers (Australia)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Reset cursor position below the banner
    pdf.set_xy(20, 52)
    
    # Meta information
    pdf.set_font("helvetica", "B", 9)
    pdf.set_text_color(45, 55, 72) # Charcoal
    pdf.write(6, "Date: ")
    pdf.set_font("helvetica", "", 9)
    pdf.write(6, "May 2026\n")
    
    pdf.set_font("helvetica", "B", 9)
    pdf.write(6, "Focus: ")
    pdf.set_font("helvetica", "", 9)
    pdf.write(6, "Australian Commercial Property Market (NSW, VIC, QLD)\n")
    
    pdf.set_font("helvetica", "B", 9)
    pdf.write(6, "Niche: ")
    pdf.set_font("helvetica", "", 9)
    pdf.write(6, "Tenant-Side Automated Billing Guard & Outgoings Auditor\n")
    
    pdf.ln(8)
    
    # ------------------ SECTION 1: EXECUTIVE SUMMARY ------------------
    pdf.set_font("helvetica", "B", 13)
    pdf.set_text_color(26, 54, 93)
    pdf.cell(0, 10, "1. Executive Summary", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    # Divider line
    pdf.set_draw_color(49, 151, 149)
    pdf.set_line_width(1)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(4)
    
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(45, 55, 72)
    
    summary_text = (
        "While the Australian PropTech landscape is heavily saturated with software serving landlords, "
        "agencies, and property managers, there is an underserved, high-value gap on the tenant (occupier) side. "
        "Commercial leases are highly complex, non-standardized legal agreements, resulting in significant "
        "billing discrepancies. It is estimated that commercial tenants lose between 3% and 7% of their annual "
        "occupancy costs due to landlord billing errors, incorrect outgoings reconciliation, and indexation errors.\n\n"
        "This report outlines a proposal for LeaseVerify AI, an AI-native auditing and billing guard "
        "specifically designed for multi-site commercial tenants (such as retail chains, franchise networks, "
        "medical clinics) and their representatives. By automating the parsing of leases, cross-referencing live "
        "market data, and analyzing landlord invoices, the platform enables occupiers to recover overcharged funds "
        "and prevent financial leakage."
    )
    pdf.multi_cell(0, 6, summary_text)
    pdf.ln(6)
    
    # ------------------ SECTION 2: THE PAIN POINT ------------------
    pdf.set_font("helvetica", "B", 13)
    pdf.set_text_color(26, 54, 93)
    pdf.cell(0, 10, "2. The Pain Point & Market Gap", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(4)
    
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(45, 55, 72)
    
    pain_point_text = (
        "Commercial property managers use platforms like Rex Cirrus8 or MRI to generate invoices. "
        "However, these systems require manual inputs from junior property managers. Because every lease is custom-drafted "
        "by different lawyers, errors are extremely common. The main categories of financial leakage include:\n"
    )
    pdf.multi_cell(0, 6, pain_point_text)
    pdf.ln(2)
    
    bullets = [
        ("Land Tax Double-Dipping: ", "Landlords overcharging tenants land tax based on a multiple-holdings basis (which reflects the landlord's entire property portfolio and falls into a higher tax bracket) rather than a single-holding basis as required by standard leases."),
        ("Disguised Capital Works: ", "Landlords billing tenants for capital improvements (e.g. building structure, roof replacements, new air conditioning units) under the guise of general maintenance, which is illegal under retail leasing acts."),
        ("Apportionment Errors: ", "Charging the tenant an incorrect percentage of the building's Net Lettable Area (NLA) or failing to adjust the share when parts of the property become vacant."),
        ("Incorrect Indexation: ", "Applying CPI increases using the wrong quarter's index numbers or matching the wrong capital city (e.g., using Sydney CPI for a Melbourne property).")
    ]
    
    for title, desc in bullets:
        pdf.set_font("helvetica", "B", 10)
        pdf.set_text_color(49, 151, 149) # Teal title
        pdf.write(6, "  * " + title)
        pdf.set_font("helvetica", "", 10)
        pdf.set_text_color(45, 55, 72)
        pdf.write(6, desc + "\n\n")
    
    pdf.ln(2)
    pdf.set_x(20)
    
    # Callout box for market sizes
    current_y = pdf.get_y()
    pdf.set_fill_color(247, 250, 252) # Light blue/grey
    pdf.set_draw_color(226, 232, 240)
    pdf.set_line_width(0.5)
    pdf.rect(20, current_y, 170, 24, "DF")
    
    pdf.set_xy(25, current_y + 3)
    pdf.set_font("helvetica", "B", 10)
    pdf.set_text_color(26, 54, 93)
    pdf.cell(0, 5, "The Financial Opportunity:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", "I", 9.5)
    pdf.set_text_color(45, 55, 72)
    pdf.set_x(25)
    pdf.multi_cell(160, 5, "For a mid-sized Australian retail chain with 30 stores paying $150k average rent, a 3% to 7% billing error rate translates to $135,000 to $315,000 in annual leakage. This creates a direct, measurable ROI for using an auditing platform.")
    
    pdf.set_xy(20, current_y + 30)
    
    # ------------------ SECTION 3: COMPETITIVE LANDSCAPE ------------------
    pdf.add_page()
    pdf.set_font("helvetica", "B", 13)
    pdf.set_text_color(26, 54, 93)
    pdf.cell(0, 10, "3. Competitive Landscape (Global vs. Local)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(4)
    
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(45, 55, 72)
    
    comp_intro = (
        "While several major players operate in PropTech, the specific combination of automated forensic auditing "
        "and Australian legislative compliance is a wide-open gap:\n"
    )
    pdf.multi_cell(0, 6, comp_intro)
    pdf.ln(2)
    
    comp_bullets = [
        ("CAMAudit.io (United States): ", "This is a dedicated, bootstrapped tool in the US that automates CAM (Common Area Maintenance) auditing. It is highly successful but operates strictly under US terminology, tax codes, and leasing conventions. They have no localization or compliance modules for the Australian market."),
        ("Accurait.ai (Australia): ", "An Australian AI-powered lease abstraction tool. However, its primary focus is on lease database creation and balance-sheet compliance (AASB 16 / IFRS 16) for large corporate entities. It does not perform transactional auditing on monthly invoices, nor does it check outgoings allocations dynamically or generate dispute notifications."),
        ("Landlord Software (Rex Cirrus8, Re-Leased, MRI): ", "These are property management systems built for landlords. They are designed to automate billing, not to check if the landlord is overcharging. Landlords have no incentive to introduce tenant-side audit checks into these programs.")
    ]
    
    for title, desc in comp_bullets:
        pdf.set_font("helvetica", "B", 10)
        pdf.set_text_color(26, 54, 93)
        pdf.write(6, "  - " + title)
        pdf.set_font("helvetica", "", 10)
        pdf.set_text_color(45, 55, 72)
        pdf.write(6, desc + "\n\n")
        
    pdf.ln(2)
    pdf.set_x(20)

    # ------------------ SECTION 4: LEGISLATIVE CATALYST ------------------
    pdf.set_font("helvetica", "B", 13)
    pdf.set_text_color(26, 54, 93)
    pdf.cell(0, 10, "4. The Legislative Catalyst", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(4)
    
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(45, 55, 72)
    
    leg_text = (
        "Unlike the US, where lease auditing is purely contract-driven, Australia's commercial lease auditing is heavily "
        "intersected by state legislation (specifically Retail Leases Acts). These laws contain mandatory rules that "
        "automatically override any conflicting clauses in the signed lease contract. This state-by-state divergence "
        "creates a high barrier to entry for global companies, but a major advantage for a localized product:\n"
    )
    pdf.multi_cell(0, 6, leg_text)
    pdf.ln(4)
    
    # Table headers
    pdf.set_fill_color(26, 54, 93)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(30, 8, "State", border=1, align="C", fill=True)
    pdf.cell(50, 8, "Land Tax Recovery", border=1, align="C", fill=True)
    pdf.cell(50, 8, "Capital Works Recovery", border=1, align="C", fill=True)
    pdf.cell(40, 8, "Outgoings Audit Rules", border=1, align="C", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Table Rows
    pdf.set_text_color(45, 55, 72)
    pdf.set_font("helvetica", "", 8.5)
    
    # Row 1 (VIC)
    pdf.set_x(20)
    pdf.cell(30, 12, "Victoria (VIC)", border=1, align="C")
    pdf.set_font("helvetica", "B", 8.5)
    pdf.set_text_color(229, 62, 62) # Red alert for VIC land tax
    pdf.cell(50, 12, "STRICTLY PROHIBITED (Sec 23)", border=1, align="C")
    pdf.set_font("helvetica", "", 8.5)
    pdf.set_text_color(45, 55, 72)
    pdf.cell(50, 12, "Prohibited (Landlord pays CapEx)", border=1, align="C")
    pdf.cell(40, 12, "Audited EOFY within 3 months", border=1, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Row 2 (NSW)
    pdf.set_x(20)
    pdf.cell(30, 12, "New South Wales (NSW)", border=1, align="C")
    pdf.cell(50, 12, "Single-Holding Basis Only", border=1, align="C")
    pdf.cell(50, 12, "Prohibited (Landlord pays structural)", border=1, align="C")
    pdf.cell(40, 12, "Audited statement required", border=1, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Row 3 (QLD)
    pdf.set_x(20)
    pdf.cell(30, 12, "Queensland (QLD)", border=1, align="C")
    pdf.cell(50, 12, "Allowed (Must disclose in advance)", border=1, align="C")
    pdf.cell(50, 12, "Prohibited (Landlord pays core CapEx)", border=1, align="C")
    pdf.cell(40, 12, "Annual registered audit required", border=1, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.ln(6)
    
    # ------------------ SECTION 5: AI & DATA SOLUTION ------------------
    pdf.add_page()
    pdf.set_font("helvetica", "B", 13)
    pdf.set_text_color(26, 54, 93)
    pdf.cell(0, 10, "5. The AI & Data Solution (LeaseVerify AI)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(4)
    
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(45, 55, 72)
    
    sol_intro = "The platform achieves automated auditing through a multi-stage software pipeline:\n"
    pdf.multi_cell(0, 6, sol_intro)
    pdf.ln(2)
    
    sol_steps = [
        ("Multimodal AI PDF Extraction: ", "The tenant uploads their lease PDF. A fine-tuned LLM parses the document layout, identifying key clauses (rent review dates, CPI city indicators, land tax clauses, NLA area share, and explicit outgoings exclusions). It outputs a structured JSON rule file."),
        ("External Data Integrations (APIs): ", "The engine pulls active market data to verify landlord math: (1) ABS API to fetch capital-city specific CPI increases; (2) State Revenue Office databases to verify land tax rates; (3) Local council rate cards to check municipal valuations."),
        ("Reconciliation Auditing: ", "When the tenant uploads their monthly rental invoices or annual EOFY outgoings statements, the audit engine runs automated calculations, comparing actual charges against the JSON rule file and external data. Any overcharges are flagged with precise dollar figures and linked back to the lease clause."),
        ("Legislative Dispute Generator: ", "The AI automatically drafts a formal dispute letter for the landlord or property manager. The letter is pre-populated with exact calculations, citations of the lease clauses, and citations of the governing State Retail Leases Act (e.g., citing Section 23 in Victoria).")
    ]
    
    for title, desc in sol_steps:
        pdf.set_font("helvetica", "B", 10)
        pdf.set_text_color(49, 151, 149)
        pdf.write(6, "  * " + title)
        pdf.set_font("helvetica", "", 10)
        pdf.set_text_color(45, 55, 72)
        pdf.write(6, desc + "\n\n")
        
    pdf.ln(2)
    pdf.set_x(20)
    
    # ------------------ SECTION 6: GTM & MONETIZATION ------------------
    pdf.set_font("helvetica", "B", 13)
    pdf.set_text_color(26, 54, 93)
    pdf.cell(0, 10, "6. Go-To-Market & Monetization", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(4)
    
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(45, 55, 72)
    
    gtm_text = (
        "The primary go-to-market strategy involves partnering with Tenant Representatives and Commercial Buyer's Agents. "
        "These advisors represent occupiers and can white-label the software to offer 'automated lease health audits' "
        "as a high-margin value-add. Additionally, the software can target mid-market retail franchises directly.\n\n"
        "Monetization consists of three tiers:\n"
    )
    pdf.multi_cell(0, 6, gtm_text)
    
    pdf.set_font("helvetica", "B", 10)
    pdf.set_text_color(26, 54, 93)
    pdf.write(6, "  - Subscription SaaS: ")
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(45, 55, 72)
    pdf.write(6, "$15 - $30 per lease per month for continuous portfolio auditing and critical date tracking.\n")
    
    pdf.set_font("helvetica", "B", 10)
    pdf.set_text_color(26, 54, 93)
    pdf.write(6, "  - Transactional Auditing: ")
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(45, 55, 72)
    pdf.write(6, "One-off historical audits (e.g. 3-year audit) priced at $299 per lease.\n")
    
    pdf.set_font("helvetica", "B", 10)
    pdf.set_text_color(26, 54, 93)
    pdf.write(6, "  - White-Label Partner Pricing: ")
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(45, 55, 72)
    pdf.write(6, "Volume-based API access for Tenant Rep and accounting agencies.\n")
    
    pdf.ln(6)
    pdf.set_x(20)

    # ------------------ SECTION 7: VALIDATION ------------------
    pdf.set_font("helvetica", "B", 13)
    pdf.set_text_color(26, 54, 93)
    pdf.cell(0, 10, "7. Validation Roadmap", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(4)
    
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(45, 55, 72)
    
    validation_steps = [
        "1. Interview 3-5 Tenant Reps or CFOs of retail chains to verify how they audit EOFY outgoings today.",
        "2. Secure 3-5 real leases and outgoings statements from businesses to find overcharges manually.",
        "3. Build a simple Python prototype to parse a lease PDF, extract parameters, and compare a sample invoice."
    ]
    for step in validation_steps:
        pdf.set_x(20)
        pdf.multi_cell(0, 6, step)
        
    pdf.ln(10)
    
    # Write the output PDF file
    output_filename = "C:/Users/gofor/.gemini/antigravity/scratch/PropTech_Market_Gap_Analysis_Australia.pdf"
    pdf.output(output_filename)
    print(f"Successfully generated PDF: {output_filename}")

if __name__ == "__main__":
    create_report()
