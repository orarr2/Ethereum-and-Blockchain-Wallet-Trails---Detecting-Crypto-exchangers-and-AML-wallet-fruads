"""
Investigator dashboard - SEPARATE Streamlit app.

This is a standalone app for the investigator layer. The base project's
`streamlit_app.py` is untouched and still runs on its own. Launch this one with:

    streamlit run investigator/app.py

It adds exactly the two UI elements the spec calls for:
  * a Verdict widget on the Candidates tab (feeds Component B's online LinUCB);
  * a Dossier tab that runs the investigator agent on demand and renders the
    cited markdown, with the full reasoning trace in an expander.

The Candidates order follows the adaptive reranker when it is active (flag on +
enough verdicts); otherwise it falls back to the static `actionability` order,
identical to the base dashboard.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# make the `investigator` package importable when run as `streamlit run app.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from investigator import config as C
from investigator import verdicts_io
from investigator.adaptive_triage import FeatureBuilder, LinUCB, Reranker
from investigator.agent import Investigator
from investigator.llm_client import LLMClient

st.set_page_config(page_title="AML Investigator", layout="wide")
st.title("AML Investigator - agent + adaptive triage")
st.caption("Research leads only. Not a finding of guilt. Separate layer on top of "
           "the base detection pipeline; the base dashboard is unaffected.")


@st.cache_data
def load_master() -> pd.DataFrame:
    if not C.MASTER_CSV.exists():
        return pd.DataFrame()
    return pd.read_csv(C.MASTER_CSV)


def _row_features(row: pd.Series) -> dict:
    return {k: (v if isinstance(v, (int, float, bool, str)) or v is None else str(v))
            for k, v in row.items()}


master = load_master()
if not len(master):
    st.error(f"No master table at {C.MASTER_CSV}. Run the base notebook first.")
    st.stop()

# --- shared model state (persists across reruns within a session) -----------
if "linucb" not in st.session_state:
    st.session_state.linucb = LinUCB.load()
if "fb" not in st.session_state:
    st.session_state.fb = FeatureBuilder(FeatureBuilder.cluster_vocab_from_master(master))

linucb: LinUCB = st.session_state.linucb
fb: FeatureBuilder = st.session_state.fb

# --- sidebar ----------------------------------------------------------------
with st.sidebar:
    st.header("Controls")
    chains = sorted(master["chain"].dropna().unique().tolist())
    chain_sel = st.multiselect("Chains", chains, default=chains)
    flag_on = st.toggle("Adaptive triage (LinUCB rerank)", value=C.ADAPTIVE_TRIAGE_ENABLED,
                        help="When off, the queue uses the static actionability order.")
    st.divider()
    st.metric("Verdicts collected", verdicts_io.count())
    st.metric("LinUCB updates", linucb.n_updates)
    reranker = Reranker(linucb=linucb, feature_builder=fb, flag_enabled=flag_on)
    active = reranker.active()
    st.write("Reranker:", ":green[ACTIVE]" if active else ":gray[static (cold-start / off)]")
    if flag_on and not active:
        st.caption(f"Needs >= {C.MIN_VERDICTS_TO_ACTIVATE} verdicts to activate "
                   f"({linucb.n_updates} so far).")
    st.divider()
    st.caption(f"LLM: {C.LLM_PROVIDER}/{C.LLM_MODEL}")
    st.caption(f"Budgets: {C.MAX_TOOL_CALLS_PER_DOSSIER} calls / "
               f"${C.BQ_USD_BUDGET_PER_DOSSIER} BQ / {C.LLM_TOKEN_BUDGET_PER_DOSSIER} tok")

view = master[master["chain"].isin(chain_sel)] if chain_sel else master

tab_cand, tab_dossier, tab_about = st.tabs(["Candidates", "Dossier", "About"])

# ============================================================================
# Candidates + verdict widget
# ============================================================================
with tab_cand:
    ranked = reranker.rerank(view)
    show_cols = [c for c in ["wallet", "chain", "risk_score", "actionability",
                             "bandit_score", "has_exchange_anchor", "is_bridge_wallet",
                             "campaign_terror_score", "hits_interesting_country"]
                 if c in ranked.columns]
    st.subheader(f"{len(ranked)} candidates "
                 f"({'adaptive order' if active else 'static actionability order'})")
    st.dataframe(ranked[show_cols].head(200), use_container_width=True, height=380)

    st.markdown("#### Record a verdict")
    st.caption("Your verdict is appended to verdicts.jsonl and folded into the online "
               "LinUCB immediately; the queue re-ranks on the next interaction.")
    c1, c2, c3 = st.columns([3, 2, 2])
    with c1:
        wallets = ranked["wallet"].astype(str).tolist()
        pick = st.selectbox("Wallet", wallets, index=0 if wallets else None, key="verdict_wallet")
    with c2:
        verdict = st.selectbox("Verdict", sorted(verdicts_io.VERDICT_VALUES), key="verdict_value")
    with c3:
        note = st.text_input("Note (optional)", key="verdict_note")

    if st.button("Submit verdict", type="primary", disabled=not wallets):
        row = ranked[ranked["wallet"].astype(str) == pick].iloc[0]
        v = verdicts_io.append_new(pick, row.get("chain", ""), verdict,
                                   features=_row_features(row), note=note)
        reward = v["reward"]
        if reward is not None:
            reranker.observe(row, reward, persist=True)   # updates + saves LinUCB
            st.session_state.linucb = reranker.linucb
            st.success(f"Recorded '{verdict}' (reward {reward:+.1f}). "
                       f"LinUCB now has {reranker.linucb.n_updates} updates.")
        else:
            st.info(f"Recorded '{verdict}' (not used for training).")
        st.rerun()

# ============================================================================
# Dossier tab
# ============================================================================
with tab_dossier:
    st.subheader("Investigate a wallet")
    st.caption("Runs the autonomous agent live (graph_expand -> enrich -> mixer -> "
               "bridge -> OFAC/NBCTF -> write). Needs a BigQuery project + an LLM key "
               "for a real run; set INVESTIGATOR_LLM_PROVIDER=mock for an offline demo.")
    colA, colB, colC = st.columns([3, 2, 2])
    with colA:
        addr = st.text_input("Address", value=(view["wallet"].iloc[0] if len(view) else ""))
    with colB:
        chain = st.selectbox("Chain", chains, key="dossier_chain")
    with colC:
        provider = st.selectbox("LLM provider", ["(config default)", "anthropic", "openai", "mock"])

    if st.button("Investigate", type="primary", disabled=not addr):
        prov = None if provider == "(config default)" else provider
        with st.spinner("Investigating - this can take a minute..."):
            inv = Investigator(llm=LLMClient(provider=prov), master=master)
            try:
                res = inv.investigate(addr.strip(), chain)
            except Exception as e:
                st.error(f"Investigation failed: {type(e).__name__}: {e}")
                res = None
        if res is not None:
            badge = "PARTIAL" if res.partial else "complete"
            st.write(f"**Status:** {badge} ({res.stop_reason}) | "
                     f"sections {res.n_sections}/8 | BQ ${res.bq_cost_usd:.4f} | "
                     f"tokens {res.token_cost['total']} | {res.duration_s:.1f}s")
            if res.validation_problems:
                st.warning("Validation: " + "; ".join(res.validation_problems))
            md = Path(res.dossier_md_path).read_text(encoding="utf-8")
            st.markdown(md)
            with st.expander("Raw reasoning trace (JSON)"):
                st.code(Path(res.trace_json_path).read_text(encoding="utf-8"), language="json")

    st.divider()
    st.markdown("#### Existing dossiers")
    if C.DOSSIER_INDEX_PATH.exists():
        idx = json.loads(C.DOSSIER_INDEX_PATH.read_text(encoding="utf-8"))
        st.caption(f"From last batch: {idx.get('generated','')} "
                   f"({idx.get('n_dossiers',0)} dossiers)")
        st.dataframe(pd.DataFrame(idx.get("entries", [])), use_container_width=True)
    else:
        st.caption("No batch index yet. Run `python -m investigator.run_nightly`.")

# ============================================================================
# About
# ============================================================================
with tab_about:
    st.markdown("""
This layer implements the design in
`docs/SPEC_investigator_and_adaptive_triage.md`:

- **Component A - Investigator agent.** A ReAct loop over seven tools that builds
  a cited markdown dossier under strict budgets. Every claim anchors to a
  `[TOOL:n]` observation in the reasoning trace.
- **Component B - Adaptive triage.** Analyst verdicts become rewards for an
  online LinUCB reranker, warm-started weekly by an XGBoost pairwise LTR pass.

The base detection pipeline is never modified. Everything here writes only under
`investigator/`. All outputs are **research leads, not findings of guilt**.
""")
    st.json(C.summary())
