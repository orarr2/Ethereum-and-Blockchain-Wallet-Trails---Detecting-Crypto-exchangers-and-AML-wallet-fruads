"""
Nightly batch entrypoint.

Pipeline: load master + LinUCB state -> rerank each chain -> take the top-K ->
investigate each into a dossier -> write an index + a standalone HTML index
that links every dossier. A daily BigQuery kill-switch halts the whole run if
cumulative BQ spend crosses `BQ_KILL_USD_DAILY`.

This is fully self-contained: it writes only under investigator/outputs and
never touches the base project's report.html. Run it from the project root:

    python -m investigator.run_nightly            # real run (needs BQ + LLM key)
    INVESTIGATOR_LLM_PROVIDER=mock python -m investigator.run_nightly   # offline
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from html import escape
from pathlib import Path

import pandas as pd

from . import config as C
from .adaptive_triage import FeatureBuilder, LinUCB, Reranker
from .agent import Investigator
from .llm_client import LLMClient


def _load_master() -> tuple:
    """Return ``(DataFrame, is_demo)``; falls back to the committed demo sample
    when the notebook-produced master is absent, so CI never crashes."""
    master, is_demo = C.load_master()
    if not len(master):
        raise FileNotFoundError(
            f"no master found: neither {C.MASTER_CSV} nor {C.SAMPLE_MASTER_CSV} "
            f"exists. Run the notebook to produce the master table.")
    return master, is_demo


def run(top_k: int | None = None, chains: list | None = None,
        provider: str | None = None, force: bool = False, verbose: bool = True) -> dict:
    master, is_demo = _load_master()
    if is_demo and verbose:
        print("[nightly] NOTE: real master not found - using committed DEMO sample "
              "(investigator/data/sample_master.csv). Output is labelled DEMO.")
    top_k = top_k or C.TOP_K_PER_CHAIN
    chains = chains or C.CHAINS

    linucb = LinUCB.load()
    fb = FeatureBuilder(FeatureBuilder.cluster_vocab_from_master(master))
    reranker = Reranker(linucb=linucb, feature_builder=fb)
    llm = LLMClient(provider=provider) if provider else LLMClient()
    investigator = Investigator(llm=llm, master=master, verbose=verbose)

    if verbose:
        print(f"[nightly] {datetime.utcnow():%Y-%m-%d %H:%M UTC} | provider={llm.provider} "
              f"| reranker_active={reranker.active()} | kill=${C.BQ_KILL_USD_DAILY}/day")

    entries = []
    bq_spent_total = 0.0
    halted = False

    for chain in chains:
        chain_df = master[master["chain"] == chain]
        if not len(chain_df):
            if verbose:
                print(f"[nightly] {chain}: 0 candidates, skipping.")
            continue
        ranked = reranker.rerank(chain_df).head(top_k)
        if verbose:
            print(f"[nightly] {chain}: investigating {len(ranked)} candidates")

        for _, row in ranked.iterrows():
            if bq_spent_total >= C.BQ_KILL_USD_DAILY:
                print(f"[nightly] KILL-SWITCH: BQ spend ${bq_spent_total:.2f} >= "
                      f"${C.BQ_KILL_USD_DAILY}; halting.")
                halted = True
                break
            wallet = str(row["wallet"])
            existing = C.DOSSIERS_DIR / chain / f"{wallet}.md"
            if existing.exists() and not force:
                entries.append({"address": wallet, "chain": chain,
                                "dossier": str(existing), "cached": True})
                continue
            res = investigator.investigate(wallet, chain)
            bq_spent_total += res.bq_cost_usd
            entry = res.to_index_entry()
            entry["cached"] = False
            entries.append(entry)
            if verbose:
                print(f"    {wallet[:16]}... sections={res.n_sections} "
                      f"partial={res.partial} bq=${res.bq_cost_usd:.4f}")
        if halted:
            break

    index = {
        "generated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": llm.provider, "model": llm.model,
        "reranker_active": reranker.active(),
        "top_k_per_chain": top_k, "chains": chains,
        "bq_spent_total_usd": round(bq_spent_total, 4),
        "halted_by_kill_switch": halted, "demo": is_demo,
        "n_dossiers": len(entries), "entries": entries,
    }
    C.DOSSIER_INDEX_PATH.write_text(json.dumps(index, indent=2, default=str), encoding="utf-8")
    html_path = _write_html_index(index)
    if verbose:
        print(f"[nightly] wrote {len(entries)} dossiers | index: {C.DOSSIER_INDEX_PATH.name} "
              f"| html: {html_path.name} | BQ ${bq_spent_total:.4f}")
    index["html_index"] = str(html_path)
    return index


def _write_html_index(index: dict) -> Path:
    rows = []
    for e in index["entries"]:
        dossier = e.get("dossier", "")
        rel = Path(dossier).relative_to(C.DOSSIERS_DIR) if dossier else ""
        link = f'<a href="{escape(str(rel))}">open</a>' if dossier else ""
        rows.append(
            f"<tr><td><code>{escape(str(e.get('address','')))}</code></td>"
            f"<td>{escape(str(e.get('chain','')))}</td>"
            f"<td>{'partial' if e.get('partial') else ('cached' if e.get('cached') else 'complete')}</td>"
            f"<td>{e.get('n_sections','')}</td>"
            f"<td>${e.get('bq_cost_usd',0)}</td>"
            f"<td>{link}</td></tr>")
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Investigator dossiers</title>
<style>
body{{font:14px/1.5 system-ui,sans-serif;margin:24px;background:#0e1116;color:#e6edf3;}}
h1{{font-size:20px;}} .muted{{color:#8b949e;font-size:12px;}}
table{{border-collapse:collapse;width:100%;margin-top:12px;font-size:13px;}}
th,td{{padding:6px 10px;border-bottom:1px solid #2d333b;text-align:left;}}
th{{color:#8b949e;text-transform:uppercase;font-size:10.5px;}}
a{{color:#58a6ff;text-decoration:none;}} code{{font-size:12px;}}
</style></head><body>
<h1>Investigator dossiers</h1>
{'<div style="background:#3d2b00;border:1px solid #d29922;color:#f0c674;padding:8px 12px;border-radius:6px;margin:8px 0;font-size:13px;">DEMO DATA - built from the committed sample table (no real BigQuery run). Add BQ + LLM keys for live leads.</div>' if index.get('demo') else ''}
<div class="muted">Generated {escape(index['generated'])} - provider {escape(index['provider'])}/{escape(index['model'])} -
reranker {'active' if index['reranker_active'] else 'static'} -
BQ ${index['bq_spent_total_usd']}{' - HALTED (kill-switch)' if index['halted_by_kill_switch'] else ''}.
Research leads only, not findings of guilt.</div>
<table><thead><tr><th>address</th><th>chain</th><th>status</th><th>sections</th><th>bq cost</th><th>dossier</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>"""
    path = C.DOSSIERS_DIR / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


def main(argv=None):  # pragma: no cover
    ap = argparse.ArgumentParser(description="Nightly investigator batch.")
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--chains", type=str, default=None, help="comma-separated")
    ap.add_argument("--provider", type=str, default=None, help="anthropic|openai|mock")
    ap.add_argument("--force", action="store_true", help="re-investigate cached dossiers")
    args = ap.parse_args(argv)
    chains = [c.strip() for c in args.chains.split(",")] if args.chains else None
    run(top_k=args.top_k, chains=chains, provider=args.provider, force=args.force)


if __name__ == "__main__":  # pragma: no cover
    main()
