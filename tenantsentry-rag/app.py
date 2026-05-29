"""
app.py — TenantSentry Local Prototype
-------------------------------------
Streamlit app for AI-powered commercial lease analysis.
Uses real PDF parsing + keyword-based mock analysis (no API keys needed).

Run with:
    python -m streamlit run app.py
"""

import streamlit as st
import tempfile, os, time, json
from pathlib import Path
from datetime import datetime

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="TenantSentry",
    page_icon="🛡️",
    layout="centered",
    initial_sidebar_state="collapsed",
)

import sys
sys.path.insert(0, str(Path(__file__).parent))

from ingestion.pdf_parser import parse_pdf
from ingestion.chunker import chunk_document
from mock.analyser import analyse_clause_mock, compute_risk_score

SIGNUPS_FILE = Path(__file__).parent / "signups.json"

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    [data-testid="collapsedControl"] { display: none; }
    #MainMenu, footer, header { visibility: hidden; }
    .stApp { background: #faf9f7; }
    html, body, [class*="css"] { font-family: 'Inter', system-ui, sans-serif; }
    .block-container { padding-top: 2rem !important; max-width: 820px; }

    /* Brand */
    .brand { text-align: center; margin-bottom: 1.8rem; position: relative; }
    .brand-logo { font-size: 1.9rem; font-weight: 800; color: #1c1917; letter-spacing: -0.5px; }
    .brand-logo span { color: #d97706; }
    .brand-sub { font-size: 0.85rem; color: #a8a29e; margin-top: 0.2rem; }
    .brand-signup-btn { position: absolute; top: 0; right: 0; }

    /* Upload card */
    .upload-area {
        background: #ffffff;
        border: 1.5px dashed #e7e5e4;
        border-radius: 16px;
        padding: 1.6rem 1.8rem 1.2rem;
        margin-bottom: 1.5rem;
    }

    /* Stat strip */
    .stat-strip {
        display: flex;
        gap: 2rem;
        margin: 0.8rem 0 1.6rem;
        padding: 0.9rem 1.2rem;
        background: white;
        border-radius: 12px;
        border: 1px solid #e7e5e4;
    }
    .stat-val { font-size: 1.4rem; font-weight: 800; color: #1c1917; line-height: 1; }
    .stat-lbl { font-size: 0.7rem; color: #a8a29e; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 3px; }

    /* Badge */
    .badge { display:inline-block; padding:3px 11px; border-radius:999px; font-size:0.76rem; font-weight:700; margin-left:8px; vertical-align:middle; }
    .badge-HIGH   { background:#fef2f2; color:#b91c1c; }
    .badge-MEDIUM { background:#fffbeb; color:#92400e; }
    .badge-LOW    { background:#f0fdf4; color:#166534; }

    /* Filter bar */
    .filter-bar {
        display: flex;
        gap: 0.4rem;
        flex-wrap: wrap;
        margin-bottom: 1rem;
        padding: 0.7rem 0.9rem;
        background: #fff;
        border-radius: 10px;
        border: 1px solid #e7e5e4;
        align-items: center;
    }
    .filter-label { font-size: 0.72rem; color: #a8a29e; text-transform: uppercase; letter-spacing: 0.5px; margin-right: 0.3rem; }

    /* Clause row */
    .clause-row {
        padding: 0.75rem 1rem;
        border-radius: 10px;
        border: 1px solid #e7e5e4;
        background: white;
        margin-bottom: 0.5rem;
        cursor: pointer;
        transition: border-color 0.15s;
    }
    .clause-row:hover { border-color: #d97706; }
    .clause-row-high   { border-left: 3px solid #ef4444; }
    .clause-row-medium { border-left: 3px solid #f59e0b; }
    .clause-row-low    { border-left: 3px solid #34d399; }
    .clause-row-none   { border-left: 3px solid #e7e5e4; }

    .clause-row-title { font-weight: 600; font-size: 0.88rem; color: #1c1917; }
    .clause-row-meta  { font-size: 0.76rem; color: #a8a29e; margin-top: 2px; }

    /* Pills */
    .pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:0.72rem; font-weight:600; margin-right:3px; }
    .pill-high   { background:#fef2f2; color:#b91c1c; }
    .pill-medium { background:#fffbeb; color:#92400e; }
    .pill-low    { background:#f0fdf4; color:#166534; }
    .pill-type   { background:#fef9f0; color:#92400e; border:1px solid #fde68a; }

    /* Summary / action boxes */
    .summary-box { background:#fffbeb; border-left:3px solid #fbbf24; border-radius:0 8px 8px 0; padding:0.7rem 1rem; font-size:0.86rem; color:#44403c; margin:0.5rem 0; line-height:1.55; }
    .action-box  { background:#f0fdf4; border-left:3px solid #4ade80; border-radius:0 8px 8px 0; padding:0.7rem 1rem; font-size:0.86rem; color:#14532d; margin:0.5rem 0; line-height:1.55; }

    /* Clause body */
    .clause-text { background:#faf9f7; border-radius:8px; padding:0.7rem 0.9rem; font-size:0.81rem; color:#57534e; max-height:150px; overflow-y:auto; line-height:1.6; margin:0.5rem 0; }
    .term-tag { display:inline-block; background:#f5f5f4; color:#57534e; border-radius:6px; padding:2px 7px; font-size:0.73rem; margin:2px; }
    .leg-ref  { font-size:0.74rem; color:#a8a29e; font-style:italic; }

    /* Flag rows */
    .flag-row { padding:0.7rem 0; border-bottom:1px solid #f5f5f4; }
    .flag-title { font-weight:600; color:#1c1917; font-size:0.86rem; }
    .flag-desc  { color:#57534e; font-size:0.81rem; margin-top:2px; }

    /* Empty state */
    .empty { text-align:center; color:#a8a29e; font-size:0.84rem; padding:2rem 0; }

    /* Footer */
    .foot { text-align:center; font-size:0.73rem; color:#d6d3d1; margin-top:3rem; padding-top:1rem; border-top:1px solid #f5f5f4; }

    /* Signup modal overrides */
    div[data-testid="stModal"] { border-radius: 16px !important; }

    label { font-size:0.81rem !important; color:#78716c !important; font-weight:600 !important; }
</style>
""", unsafe_allow_html=True)


# ── Signup helpers ────────────────────────────────────────────────────────────
def load_signups() -> list:
    if SIGNUPS_FILE.exists():
        with open(SIGNUPS_FILE) as f:
            return json.load(f)
    return []

def save_signup(name: str, email: str, role: str):
    signups = load_signups()
    signups.append({
        "name": name,
        "email": email,
        "role": role,
        "signed_up_at": datetime.utcnow().isoformat(),
    })
    with open(SIGNUPS_FILE, "w") as f:
        json.dump(signups, f, indent=2)

def email_exists(email: str) -> bool:
    return any(s["email"].lower() == email.lower() for s in load_signups())


# ── Signup dialog ─────────────────────────────────────────────────────────────
@st.dialog("Get early access 🛡️")
def signup_dialog():
    st.markdown("Join the waitlist and be first to know when TenantSentry launches.")
    st.markdown("")
    name  = st.text_input("Your name", placeholder="Garry")
    email = st.text_input("Email address", placeholder="you@example.com")
    role  = st.selectbox("I am a…", [
        "Commercial tenant",
        "Business owner",
        "Tenant's solicitor / advisor",
        "Property manager",
        "Other",
    ])
    st.markdown("")
    if st.button("Join waitlist →", type="primary", use_container_width=True):
        if not name.strip():
            st.error("Please enter your name.")
        elif "@" not in email or "." not in email:
            st.error("Please enter a valid email address.")
        elif email_exists(email):
            st.warning("You're already on the list — we'll be in touch soon! 🎉")
        else:
            save_signup(name.strip(), email.strip(), role)
            st.success(f"You're on the list, {name.split()[0]}! We'll reach out to {email} when we launch.")
            st.balloons()


# ── Analysis pipeline ─────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def run_analysis(file_bytes: bytes, filename: str, jurisdiction: str, tenant: str):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        parsed = parse_pdf(tmp_path)
        chunks = chunk_document(parsed.pages, {
            "jurisdiction": jurisdiction,
            "tenant_name": tenant or "Unknown",
            "filename": filename,
        })
        analyses = [
            analyse_clause_mock(
                clause_heading=c.metadata.get("clause_heading", ""),
                clause_text=c.content,
                jurisdiction=jurisdiction,
            )
            for c in chunks
        ]
        all_flags = [f for a in analyses for f in a.risk_flags]
        return {
            "analyses": analyses,
            "all_flags": all_flags,
            "score": compute_risk_score(all_flags),
            "is_scanned": parsed.is_scanned,
        }
    finally:
        os.unlink(tmp_path)


def max_sev(flags):
    for s in ("high", "medium", "low"):
        if any(f["severity"] == s for f in flags):
            return s
    return "none"


# ── Brand header ──────────────────────────────────────────────────────────────
col_brand, col_btn = st.columns([5, 1])
with col_brand:
    st.markdown("""
    <div class="brand" style="text-align:left; margin-bottom:0.5rem;">
        <div class="brand-logo">🛡️ Lease<span>Guard</span></div>
        <div class="brand-sub">AI-powered lease risk analysis for Australian commercial tenants</div>
    </div>
    """, unsafe_allow_html=True)
with col_btn:
    st.markdown("<div style='padding-top:0.6rem;'>", unsafe_allow_html=True)
    if st.button("Get access", type="secondary"):
        signup_dialog()
    st.markdown("</div>", unsafe_allow_html=True)

st.markdown("<hr style='border:none;border-top:1px solid #e7e5e4;margin:0.5rem 0 1.2rem;'>", unsafe_allow_html=True)


# ── Input area ────────────────────────────────────────────────────────────────
st.markdown('<div class="upload-area">', unsafe_allow_html=True)
col_j, col_t = st.columns(2)
with col_j:
    jurisdiction = st.selectbox("Jurisdiction", ["NSW","VIC","QLD","SA","WA","TAS","ACT","NT"])
with col_t:
    tenant_name = st.text_input("Tenant name", placeholder="e.g. Acme Pty Ltd")
uploaded_file = st.file_uploader("Upload lease PDF", type=["pdf"],
    help="Drop a commercial lease PDF — nothing leaves your machine")
st.markdown('</div>', unsafe_allow_html=True)


# ── Landing ───────────────────────────────────────────────────────────────────
if not uploaded_file:
    st.markdown("""
    <div class="empty">
        Upload a lease PDF above to get started.<br><br>
        Every clause will be extracted, labelled, and checked for risks —<br>
        with plain-English explanations your tenant can actually understand.
    </div>
    """, unsafe_allow_html=True)
    st.markdown('<div class="foot">TenantSentry Prototype v0.1 · Mock analysis · Not legal advice</div>', unsafe_allow_html=True)
    st.stop()


# ── Run analysis ──────────────────────────────────────────────────────────────
with st.spinner("Analysing your lease…"):
    result = run_analysis(
        file_bytes=uploaded_file.read(),
        filename=uploaded_file.name,
        jurisdiction=jurisdiction,
        tenant=tenant_name,
    )

if result["is_scanned"]:
    st.warning("This PDF looks scanned — text extraction may be partial. OCR support coming soon.")

analyses  = result["analyses"]
all_flags = result["all_flags"]
score     = result["score"]
high_flags   = [f for f in all_flags if f["severity"] == "high"]
medium_flags = [f for f in all_flags if f["severity"] == "medium"]
low_flags    = [f for f in all_flags if f["severity"] == "low"]
risk_level   = "HIGH" if score >= 60 else "MEDIUM" if score >= 30 else "LOW"
risk_emoji   = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟢"}[risk_level]


# ── Result header + stats ─────────────────────────────────────────────────────
st.markdown(f"""
<div style="margin:1rem 0 0.4rem;">
    <span style="font-size:1.05rem;font-weight:700;color:#1c1917;">{uploaded_file.name}</span>
    <span class="badge badge-{risk_level}">{risk_emoji} {risk_level} RISK</span>
    <div style="font-size:0.8rem;color:#a8a29e;margin-top:3px;">{jurisdiction} · {tenant_name or 'Tenant not specified'}</div>
</div>
""", unsafe_allow_html=True)

st.markdown(f"""
<div class="stat-strip">
    <div class="stat-item">
        <div class="stat-val" style="color:{'#b91c1c' if risk_level=='HIGH' else '#92400e' if risk_level=='MEDIUM' else '#166534'}">{score}</div>
        <div class="stat-lbl">Risk score</div>
    </div>
    <div class="stat-item"><div class="stat-val">{len(analyses)}</div><div class="stat-lbl">Clauses</div></div>
    <div class="stat-item"><div class="stat-val" style="color:#b91c1c">{len(high_flags)}</div><div class="stat-lbl">High risk</div></div>
    <div class="stat-item"><div class="stat-val" style="color:#92400e">{len(medium_flags)}</div><div class="stat-lbl">Medium</div></div>
    <div class="stat-item"><div class="stat-val" style="color:#166534">{len(low_flags)}</div><div class="stat-lbl">Low</div></div>
</div>
""", unsafe_allow_html=True)


# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_clauses, tab_flags, tab_overview = st.tabs([
    f"Clauses ({len(analyses)})",
    f"Risk Flags ({len(all_flags)})",
    "Overview",
])


# ── Tab: Clauses ──────────────────────────────────────────────────────────────
with tab_clauses:

    all_types = sorted(set(a.clause_type for a in analyses))

    # ── Filter bar ──
    fc1, fc2 = st.columns([3, 2])
    with fc1:
        risk_filter = st.radio(
            "Risk level",
            ["All", "🔴 High", "🟠 Medium", "🟢 Low", "✅ Clean"],
            horizontal=True,
            label_visibility="collapsed",
        )
    with fc2:
        type_filter = st.selectbox(
            "Clause type",
            ["All types"] + all_types,
            label_visibility="collapsed",
        )

    # Map radio choice → severity string
    sev_map = {
        "All": None,
        "🔴 High": "high",
        "🟠 Medium": "medium",
        "🟢 Low": "low",
        "✅ Clean": "none",
    }
    selected_sev = sev_map[risk_filter]

    # Apply filters
    filtered = analyses
    if selected_sev is not None:
        filtered = [a for a in filtered if max_sev(a.risk_flags) == selected_sev]
    if type_filter != "All types":
        filtered = [a for a in filtered if a.clause_type == type_filter]

    st.markdown(f"<div style='font-size:0.78rem;color:#a8a29e;margin-bottom:0.6rem;'>{len(filtered)} clause{'s' if len(filtered)!=1 else ''} shown</div>", unsafe_allow_html=True)

    if not filtered:
        st.markdown('<div class="empty">No clauses match the selected filters.</div>', unsafe_allow_html=True)
    else:
        for i, a in enumerate(filtered):
            sev = max_sev(a.risk_flags)
            icon = {"high": "⚠️", "medium": "🔸", "low": "💛", "none": "✅"}[sev]
            heading = a.clause_heading or f"Clause {i+1}"

            with st.expander(f"{icon}  {heading}  —  {a.clause_type}", expanded=(sev == "high")):

                # Type + key term tags
                pills = f'<span class="pill pill-type">{a.clause_type}</span>'
                if a.key_terms:
                    pills += "&nbsp; " + " ".join(f'<span class="term-tag">{t}</span>' for t in a.key_terms[:6])
                st.markdown(pills, unsafe_allow_html=True)

                # Raw clause text
                st.markdown(
                    f'<div class="clause-text">{a.clause_text[:1200]}{"…" if len(a.clause_text)>1200 else ""}</div>',
                    unsafe_allow_html=True,
                )

                # Plain-English summary + action
                st.markdown(f'<div class="summary-box">💬 {a.plain_english_summary}</div>', unsafe_allow_html=True)
                if a.recommended_action:
                    st.markdown(f'<div class="action-box">✅ {a.recommended_action}</div>', unsafe_allow_html=True)

                # Risk flags for this clause
                if a.risk_flags:
                    st.markdown("<div style='margin-top:0.4rem;'>", unsafe_allow_html=True)
                    for flag in a.risk_flags:
                        s = flag["severity"]
                        st.markdown(
                            f'<span class="pill pill-{s}">{s.upper()}</span> '
                            f'<strong>{flag["flag_id"]}</strong> — {flag["description"][:160]}',
                            unsafe_allow_html=True,
                        )
                        if flag.get("legislation_ref"):
                            st.markdown(f'<span class="leg-ref">📖 {flag["legislation_ref"]}</span>', unsafe_allow_html=True)
                        st.markdown("")
                    st.markdown("</div>", unsafe_allow_html=True)


# ── Tab: Risk Flags ───────────────────────────────────────────────────────────
with tab_flags:
    if not all_flags:
        st.success("No risk flags detected in this lease.")
    else:
        # Filter controls
        ff1, ff2 = st.columns([3, 2])
        with ff1:
            flag_sev_filter = st.radio(
                "Severity",
                ["All", "🔴 High", "🟠 Medium", "🟢 Low"],
                horizontal=True,
                label_visibility="collapsed",
            )
        flag_sev_map = {"All": None, "🔴 High": "high", "🟠 Medium": "medium", "🟢 Low": "low"}
        sel_flag_sev = flag_sev_map[flag_sev_filter]

        filtered_flags = all_flags if sel_flag_sev is None else [f for f in all_flags if f["severity"] == sel_flag_sev]
        st.markdown(f"<div style='font-size:0.78rem;color:#a8a29e;margin-bottom:0.6rem;'>{len(filtered_flags)} flag{'s' if len(filtered_flags)!=1 else ''} shown</div>", unsafe_allow_html=True)

        sev_order = {"high": 0, "medium": 1, "low": 2}
        sorted_flags = sorted(filtered_flags, key=lambda f: sev_order.get(f["severity"], 3))

        for flag in sorted_flags:
            s = flag["severity"]
            st.markdown(f"""
<div class="flag-row">
    <div><span class="pill pill-{s}">{s.upper()}</span> <span class="flag-title">{flag['flag_id']}</span></div>
    <div class="flag-desc">{flag['description']}</div>
    {'<div class="leg-ref" style="margin-top:4px;">📖 ' + flag['legislation_ref'] + '</div>' if flag.get('legislation_ref') else ''}
    {'<div style="margin-top:5px;font-size:0.79rem;color:#57534e;">💡 ' + flag.get("recommended_action","")[:220] + '</div>' if flag.get('recommended_action') else ''}
</div>
""", unsafe_allow_html=True)


# ── Tab: Overview ─────────────────────────────────────────────────────────────
with tab_overview:
    if risk_level == "HIGH":
        st.error(f"**{len(high_flags)} high-risk clause(s) detected.** We strongly recommend a commercial leasing solicitor review before signing.")
    elif risk_level == "MEDIUM":
        st.warning(f"**{len(medium_flags)} medium-risk clause(s) found.** Several clauses could be negotiated to better protect your interests.")
    else:
        st.success("This lease appears relatively low risk. Standard clauses detected with no major concerns.")

    st.markdown("#### Clause types found")
    type_counts = {}
    for a in analyses:
        type_counts[a.clause_type] = type_counts.get(a.clause_type, 0) + 1
    has_flag_types = {a.clause_type for a in analyses if a.risk_flags}
    for ctype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        icon = "⚠️" if ctype in has_flag_types else "✅"
        st.markdown(
            f"{icon} **{ctype}** &nbsp; <span style='color:#a8a29e;font-size:0.8rem;'>{count} clause{'s' if count>1 else ''}</span>",
            unsafe_allow_html=True,
        )


# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown('<div class="foot">TenantSentry Prototype v0.1 · Mock analysis · Not legal advice · Always consult a solicitor before signing</div>', unsafe_allow_html=True)
