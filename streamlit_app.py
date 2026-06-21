"""
Crypto-AML triage dashboard.

Loads the artifacts produced by the notebook + pipeline_extended.py and presents
a sortable / filterable view of funnel candidates with per-wallet drill-down.

Run with:
    streamlit run streamlit_app.py
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent
OUT  = ROOT / "outputs"

st.set_page_config(page_title="Crypto-AML Triage", layout="wide")
st.title("Crypto-AML - USDT funnel triage")
st.caption("Research leads only. High fan-in / low fan-out also fits processors, OTC desks, "
           "and exchange deposits. Not a finding of guilt.")

# ----------------------------------------------------------------------------
# Loaders (cached)
# ----------------------------------------------------------------------------
@st.cache_data
def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)

candidates = load_csv(ROOT / "usdt_funnel_candidates.csv")
risk_v2    = load_csv(OUT / "risk_score_v2.csv")
anchors    = load_csv(OUT / "exchange_anchors.csv")
fp_peak    = load_csv(OUT / "behavioral_peak_per_wallet.csv")
ens        = load_csv(OUT / "ens_names.csv")
sens       = load_csv(OUT / "dust_sensitivity.csv")
attr       = load_csv(OUT / "attributability.csv")
usdc       = load_csv(OUT / "usdc_candidates.csv")
bursty     = load_csv(OUT / "bursty_wallets.csv")
cross      = load_csv(OUT / "cross_chain_matches.csv")

# Prefer risk_v2 if available; else candidates
df = risk_v2 if len(risk_v2) else candidates
if not len(df):
    st.error("No data found - run the notebook and pipeline_extended.py first.")
    st.stop()

# ----------------------------------------------------------------------------
# Sidebar - filters
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("Filters")
    chains = st.multiselect("Chain", ["ethereum", "tron"], default=["ethereum", "tron"])
    min_senders = st.number_input("Min distinct senders", value=50, step=10)
    min_inflow = st.number_input("Min inflow (USDT)", value=10_000, step=1_000)
    single_only = st.checkbox("Single-recipient only (extreme funnel)", value=False)
    if "risk_v2" in df.columns:
        min_risk = st.slider("Min risk_v2", 0, 100, 0)
    else:
        min_risk = 0
    st.markdown("---")
    st.caption(f"Total dataset: **{len(df):,}** candidates "
               f"({(df.chain=='ethereum').sum()} ETH, {(df.chain=='tron').sum()} Tron)")

mask = df["chain"].isin(chains) & (df["distinct_senders"] >= min_senders) & (df["in_usdt"] >= min_inflow)
if single_only:
    mask &= (df["distinct_recipients"] <= 1)
if "risk_v2" in df.columns:
    mask &= (df["risk_v2"] >= min_risk)
filt = df[mask].copy()

# ----------------------------------------------------------------------------
# Top metrics
# ----------------------------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Filtered candidates", f"{len(filt):,}")
c2.metric("Total inflow", f"${filt['in_usdt'].sum()/1e6:,.1f}M")
c3.metric("Median senders/cand", f"{filt['distinct_senders'].median():.0f}")
c4.metric("Single-recipient share",
          f"{100*(filt['distinct_recipients']<=1).mean():.1f}%" if len(filt) else "-")

# ----------------------------------------------------------------------------
# Tabs
# ----------------------------------------------------------------------------
tab_table, tab_plots, tab_drilldown, tab_artifacts = st.tabs(
    ["📋 Candidates", "📈 Plots", "🔎 Drilldown", "📂 Artifacts"])

with tab_table:
    sort_col = "risk_v2" if "risk_v2" in filt.columns else "distinct_senders"
    show = filt.sort_values(sort_col, ascending=False).head(500)
    st.dataframe(show, use_container_width=True, height=600)
    st.download_button("Download filtered CSV", filt.to_csv(index=False).encode(),
                       file_name="filtered_candidates.csv")

with tab_plots:
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Fan-in vs fan-out (funnel = bottom-right)")
        fig, ax = plt.subplots(figsize=(6, 4))
        for ch, color in [("ethereum", "tab:blue"), ("tron", "tab:red")]:
            sub = filt[filt.chain == ch]
            ax.scatter(sub["distinct_senders"], sub["out_cnt"], s=8, alpha=0.5, label=ch, c=color)
        ax.set_xscale("log")
        ax.set_xlabel("distinct senders (log)")
        ax.set_ylabel("outgoing tx count")
        ax.legend()
        st.pyplot(fig)
    with col2:
        if "risk_v2" in filt.columns:
            st.subheader("risk_v2 distribution")
            fig, ax = plt.subplots(figsize=(6, 4))
            ax.hist(filt["risk_v2"], bins=30, color="tab:purple")
            ax.set_xlabel("risk_v2 (0-100)")
            st.pyplot(fig)
    if len(sens):
        st.subheader("Dust-floor sensitivity")
        pivoted = sens.pivot(index="dust_floor_usd", columns="chain", values="n_candidates")
        st.line_chart(pivoted)
    if len(fp_peak):
        st.subheader("Behavioral fingerprint - peak hour distribution (UTC)")
        hour_counts = fp_peak["hour_utc"].value_counts().sort_index()
        st.bar_chart(hour_counts)

with tab_drilldown:
    wallets = filt["wallet"].tolist()
    if wallets:
        sel = st.selectbox("Pick a wallet", wallets[:200])
        row = filt[filt.wallet == sel].iloc[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Distinct senders", int(row["distinct_senders"]))
        c2.metric("Inflow", f"${row['in_usdt']:,.0f}")
        c3.metric("Distinct recipients", int(row["distinct_recipients"]))
        if "risk_v2" in row:
            st.metric("risk_v2", f"{row['risk_v2']:.1f}")
        scan = (f"https://etherscan.io/address/{sel}" if row["chain"] == "ethereum"
                else f"https://tronscan.org/#/address/{sel}")
        st.markdown(f"[Open in block explorer]({scan})")

        # Enrichment cards
        if len(anchors):
            sa = anchors[anchors["candidate"].str.lower() == sel.lower()]
            if len(sa):
                st.subheader("Direct exchange anchors")
                st.dataframe(sa[["direction", "exchange_addr", "exchange_name", "n_tx", "usdt"]])
        if len(fp_peak):
            f_row = fp_peak[fp_peak["wallet"] == sel]
            if len(f_row):
                st.subheader("Behavioral fingerprint")
                st.write(f"Peak hour UTC: **{int(f_row['hour_utc'].iloc[0])}**  ·  "
                         f"Estimated UTC offset: **{int(f_row['tz_guess_offset'].iloc[0])}**  ·  "
                         f"Round-$100 share: **{f_row['round_pct'].iloc[0]:.1f}%**")
        if len(ens):
            e = ens[ens["wallet"] == sel]
            if len(e):
                st.success(f"ENS: **{e['ens_name'].iloc[0]}**")
    else:
        st.info("Filters returned no rows.")

with tab_artifacts:
    st.subheader("Files in outputs/")
    for p in sorted(OUT.glob("*.csv")):
        st.write(f"• `{p.name}`  -  {p.stat().st_size:,} bytes")
    st.subheader("Cross-chain matches")
    if len(cross): st.dataframe(cross.head(30))
    else: st.caption("No identical wallet hex on both chains.")
    st.subheader("Bursty wallets (>50% of inflow on one day)")
    if len(bursty): st.dataframe(bursty.head(30))
    st.subheader("USDC funnel candidates (ETH, 30d)")
    if len(usdc): st.dataframe(usdc.head(30))
