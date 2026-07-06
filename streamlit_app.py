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
master     = load_csv(ROOT / "suspicious_wallets_master.csv")   # AUDIT: primary source now
risk_v2    = load_csv(OUT / "risk_score_v2.csv")
anchors    = load_csv(OUT / "exchange_anchors.csv")
fp_peak    = load_csv(OUT / "behavioral_peak_per_wallet.csv")
ens        = load_csv(OUT / "ens_names.csv")
sens       = load_csv(OUT / "dust_sensitivity.csv")
attr       = load_csv(OUT / "attributability.csv")
usdc       = load_csv(OUT / "usdc_candidates.csv")
bursty     = load_csv(OUT / "bursty_wallets.csv")
cross      = load_csv(OUT / "cross_chain_matches.csv")

# Prefer the notebook-produced master (has actionability, anchors, bridges, geo).
# Fall back to risk_v2 then plain candidates.
df = master if len(master) else (risk_v2 if len(risk_v2) else candidates)
if not len(df):
    st.error("No data found - run the notebook and pipeline_extended.py first.")
    st.stop()

# Column-availability flags used by the new tabs
HAS_ACTIONABILITY = "actionability" in df.columns
HAS_ANCHOR   = "has_exchange_anchor" in df.columns
HAS_BRIDGE   = "is_bridge_wallet" in df.columns
HAS_GEO      = "country_codes" in df.columns
HAS_CAMPAIGN = "campaign_terror_score" in df.columns

# ----------------------------------------------------------------------------
# Sidebar - filters
# ----------------------------------------------------------------------------
all_chains = sorted(df["chain"].dropna().unique().tolist()) if "chain" in df.columns else ["ethereum","tron"]
with st.sidebar:
    st.header("Filters")
    chains = st.multiselect("Chain", all_chains, default=all_chains)
    min_senders = st.number_input("Min distinct senders", value=50, step=10)
    inflow_col = "in_usdt" if "in_usdt" in df.columns else ("raw_value_in" if "raw_value_in" in df.columns else None)
    min_inflow = st.number_input("Min inflow (USDT / native)", value=10_000, step=1_000)
    single_only = st.checkbox("Single-recipient only (extreme funnel)", value=False)
    if HAS_ACTIONABILITY:
        min_action = st.slider("Min actionability", 0, 130, 0)
    elif "risk_score" in df.columns:
        min_action = st.slider("Min risk_score", 0, 100, 0)
    elif "risk_v2" in df.columns:
        min_action = st.slider("Min risk_v2", 0, 100, 0)
    else:
        min_action = 0
    anchor_only = st.checkbox("Only exchange-anchored (KYC targets)", value=False) if HAS_ANCHOR else False
    bridge_only = st.checkbox("Only bridge wallets (articulation points)", value=False) if HAS_BRIDGE else False
    geo_only = st.checkbox("Only interesting-country hits", value=False) if HAS_GEO else False
    st.markdown("---")
    parts = [f"{ch}: {(df.chain==ch).sum()}" for ch in all_chains]
    st.caption(f"Total: **{len(df):,}** candidates ({' · '.join(parts)})")

mask = df["chain"].isin(chains) & (df["distinct_senders"] >= min_senders)
if inflow_col: mask &= (df[inflow_col].fillna(0) >= min_inflow)
if single_only:
    mask &= (df["distinct_recipients"] <= 1)
if HAS_ACTIONABILITY:
    mask &= (df["actionability"].fillna(0) >= min_action)
elif "risk_score" in df.columns:
    mask &= (df["risk_score"].fillna(0) >= min_action)
elif "risk_v2" in df.columns:
    mask &= (df["risk_v2"].fillna(0) >= min_action)
if anchor_only: mask &= df["has_exchange_anchor"].fillna(False).astype(bool)
if bridge_only: mask &= df["is_bridge_wallet"].fillna(False).astype(bool)
if geo_only:    mask &= df["hits_interesting_country"].fillna(False).astype(bool)
filt = df[mask].copy()

# ----------------------------------------------------------------------------
# Top metrics
# ----------------------------------------------------------------------------
cols = st.columns(6)
cols[0].metric("Filtered candidates", f"{len(filt):,}")
inflow_val = filt[inflow_col].sum()/1e6 if inflow_col and len(filt) else 0
cols[1].metric("Total inflow (M)", f"${inflow_val:,.1f}M")
cols[2].metric("Median senders/cand",
               f"{filt['distinct_senders'].median():.0f}" if len(filt) else "-")
cols[3].metric("Exchange-anchored",
               f"{int(filt['has_exchange_anchor'].fillna(False).sum()):,}" if HAS_ANCHOR else "n/a")
cols[4].metric("Bridge wallets",
               f"{int(filt['is_bridge_wallet'].fillna(False).sum()):,}" if HAS_BRIDGE else "n/a")
cols[5].metric("Interesting-country hits",
               f"{int(filt['hits_interesting_country'].fillna(False).sum()):,}" if HAS_GEO else "n/a")

# ----------------------------------------------------------------------------
# Tabs
# ----------------------------------------------------------------------------
tab_names = ["📋 Candidates", "📈 Plots", "🔎 Drilldown"]
if HAS_BRIDGE:   tab_names.append("🔗 Bridges")
if HAS_GEO:      tab_names.append("🌍 Geo")
tab_names.append("📄 Report")
tab_names.append("📂 Artifacts")
tabs = st.tabs(tab_names)
tab_table   = tabs[0]
tab_plots   = tabs[1]
tab_drilldown = tabs[2]
i = 3
tab_bridge = tabs[i] if HAS_BRIDGE else None
if HAS_BRIDGE: i += 1
tab_geo    = tabs[i] if HAS_GEO else None
if HAS_GEO: i += 1
tab_report = tabs[i]; i += 1
tab_artifacts = tabs[i]

with tab_table:
    if HAS_ACTIONABILITY: sort_col = "actionability"
    elif "risk_score" in filt.columns: sort_col = "risk_score"
    elif "risk_v2" in filt.columns: sort_col = "risk_v2"
    else: sort_col = "distinct_senders"
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
        infl = row.get("in_usdt", row.get("raw_value_in", 0))
        c2.metric("Inflow", f"${float(infl):,.0f}" if infl else "-")
        c3.metric("Distinct recipients", int(row.get("distinct_recipients", 0) or 0))
        for k in ("actionability", "risk_score", "risk_v2", "campaign_terror_score"):
            if k in row and pd.notna(row[k]):
                st.metric(k, f"{float(row[k]):.1f}")
                break
        if HAS_ANCHOR and row.get("has_exchange_anchor"):
            st.success(f"Exchange anchor: **{row.get('anchor_exchange','')}** "
                       f"(${float(row.get('anchor_usdt',0)):,.0f} across "
                       f"{int(row.get('anchor_links',0) or 0)} links)")
        if HAS_BRIDGE and row.get("is_bridge_wallet"):
            st.warning(f"Bridge wallet - bridges {int(row.get('components_bridged',0) or 0)} components")
        if HAS_GEO and row.get("hits_interesting_country"):
            st.info(f"Interesting-country hit: {row.get('country_codes','')}")
        chain_val = str(row.get("chain", ""))
        if chain_val == "ethereum": scan = f"https://etherscan.io/address/{sel}"
        elif chain_val == "tron":   scan = f"https://tronscan.org/#/address/{sel}"
        elif chain_val == "bitcoin":scan = f"https://mempool.space/address/{sel}"
        else: scan = ""
        if scan: st.markdown(f"[Open in block explorer]({scan})")

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

if tab_bridge is not None:
    with tab_bridge:
        st.subheader("Bridge wallets - articulation points across the funnel graph")
        st.caption("A single wallet whose removal disconnects two or more sub-networks - "
                   "candidate common operator or nested exchanger.")
        br = filt[filt["is_bridge_wallet"].fillna(False)].copy()
        if len(br):
            cc = ["wallet","chain","risk_score","actionability","components_bridged",
                  "total_bridged_size","has_exchange_anchor","anchor_exchange"]
            cc = [c for c in cc if c in br.columns]
            st.dataframe(br.sort_values(
                "components_bridged" if "components_bridged" in br.columns else "actionability",
                ascending=False)[cc].head(200), use_container_width=True, height=500)
        else:
            st.info("No bridge wallets in the current filter.")

if tab_geo is not None:
    with tab_geo:
        st.subheader("Country breakdown")
        st.caption("Countries touched via exchange labels + NBCTF affiliation. On-chain data "
                   "has no country/IP field; this is a coarse routing overlay, not physical location.")
        if "country_codes" in filt.columns:
            rows = []
            for _, r in filt.iterrows():
                codes = r["country_codes"]
                if isinstance(codes, str):
                    codes = [c.strip().strip("'\"") for c in codes.strip("[]").split(",") if c.strip()]
                for c in (codes or []):
                    rows.append({"country": c, "chain": r.get("chain","")})
            if rows:
                summ = pd.DataFrame(rows).groupby("country").size().rename("n").reset_index() \
                        .sort_values("n", ascending=False)
                interesting = {"IL","IR","LB","SY","AE","RU","KP","YE"}
                summ["is_interesting"] = summ["country"].isin(interesting)
                st.dataframe(summ, use_container_width=True)
                st.bar_chart(summ.set_index("country")["n"])
            else:
                st.info("No country tags on the filtered rows.")
        st.subheader("Interesting-country hits (details)")
        if HAS_GEO:
            hits = filt[filt["hits_interesting_country"].fillna(False)]
            cc = ["wallet","chain","risk_score","actionability","country_codes","country_source",
                  "anchor_exchange"]
            cc = [c for c in cc if c in hits.columns]
            if len(hits):
                st.dataframe(hits.sort_values(
                    "actionability" if "actionability" in hits.columns else "risk_score",
                    ascending=False)[cc].head(200), use_container_width=True, height=400)
            else:
                st.info("No wallets hit an interesting country in the current filter.")

with tab_report:
    st.subheader("Standalone HTML triage report")
    st.caption("Generated by report_html.build_report at the end of notebook section 13.6.")
    report_path = ROOT / "report.html"
    if report_path.exists():
        st.write(f"`{report_path.name}` - {report_path.stat().st_size:,} bytes")
        st.download_button("Download report.html", report_path.read_bytes(),
                           file_name="report.html", mime="text/html")
        with st.expander("Preview inline"):
            st.components.v1.html(report_path.read_text(encoding="utf-8"), height=800, scrolling=True)
    else:
        st.info("`report.html` not found yet. Run notebook section 13.6 to generate it.")

with tab_artifacts:
    st.subheader("Files in project root / outputs/")
    for p in sorted(ROOT.glob("suspicious_*.csv")) + sorted(OUT.glob("*.csv")):
        st.write(f"• `{p.name}`  -  {p.stat().st_size:,} bytes")
    st.subheader("Cross-chain matches")
    if len(cross): st.dataframe(cross.head(30))
    else: st.caption("No identical wallet hex on both chains.")
    st.subheader("Bursty wallets (>50% of inflow on one day)")
    if len(bursty): st.dataframe(bursty.head(30))
    st.subheader("USDC funnel candidates (ETH, 30d)")
    if len(usdc): st.dataframe(usdc.head(30))
