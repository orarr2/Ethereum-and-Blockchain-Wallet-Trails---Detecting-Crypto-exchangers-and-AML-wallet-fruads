"""
Local-only portions of the extended pipeline - no BigQuery required.
Runs against the existing usdt_funnel_candidates.csv.

Useful as a quick smoke-test before running the heavier pipeline_extended.py.

Covers:
    P1.5  risk_v2
    P1.6  cross-chain matches
    P1.7  behavioural-signature proxy groups
    P2.10 daily snapshot
    P2.11 lookup template (no external API)
    P3.12 dust-floor sensitivity
    P3.14 validation template
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)

INPUT_CSV = ROOT / "usdt_funnel_candidates.csv"
if not INPUT_CSV.exists():
    raise SystemExit(
        f"Input file {INPUT_CSV.name} not found.\n"
        "Run Crypto-AML-Analysis.ipynb first - it produces this file."
    )

candidates = pd.read_csv(INPUT_CSV)
print(f"Loaded {len(candidates)} candidates "
      f"({(candidates.chain == 'ethereum').sum()} ETH, "
      f"{(candidates.chain == 'tron').sum()} Tron)")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ---- P1.5 risk_v2 ----------------------------------------------------------
section("[P1.5] Improved risk score (risk_v2)")
all_c = candidates.copy()
all_c["fanin_pct"] = all_c.groupby("chain")["distinct_senders"].rank(pct=True)
all_c["inflow_pct"] = all_c.groupby("chain")["in_usdt"].rank(pct=True)
all_c["ratio_pct"] = all_c.groupby("chain")["in_out_tx_ratio"].rank(pct=True)
all_c["single_recipient"] = (all_c["distinct_recipients"] <= 1).astype(int)
all_c["risk_v2"] = (30 * all_c["fanin_pct"] + 25 * all_c["inflow_pct"]
                    + 25 * all_c["ratio_pct"] + 20 * all_c["single_recipient"]).round(1)
all_c.sort_values("risk_v2", ascending=False).to_csv(OUT / "risk_score_v2.csv", index=False)
print(f"  range  : {all_c['risk_v2'].min():.1f} – {all_c['risk_v2'].max():.1f}")
print(f"  >= 80  : {int((all_c['risk_v2'] >= 80).sum())}")
print(f"  >= 90  : {int((all_c['risk_v2'] >= 90).sum())}")

# ---- P1.6 cross-chain ------------------------------------------------------
section("[P1.6] Cross-chain matches")
eth_set = set(candidates[candidates.chain == "ethereum"]["wallet"])
tron_set = set(candidates[candidates.chain == "tron"]["wallet"])
overlap = eth_set & tron_set
print(f"  Same wallet hex on both chains: {len(overlap)}")
cc_rows = []
for w in overlap:
    e = candidates[(candidates.chain == "ethereum") & (candidates.wallet == w)].iloc[0]
    t = candidates[(candidates.chain == "tron") & (candidates.wallet == w)].iloc[0]
    cc_rows.append({"wallet": w,
                    "eth_in_usdt": float(e["in_usdt"]),
                    "eth_senders": int(e["distinct_senders"]),
                    "tron_in_usdt": float(t["in_usdt"]),
                    "tron_senders": int(t["distinct_senders"])})
pd.DataFrame(cc_rows).to_csv(OUT / "cross_chain_matches.csv", index=False)

# ---- P1.7 proxy clusters ---------------------------------------------------
section("[P1.7] Behavioural-signature proxy groups")
all_c["in_usdt_bucket"] = (np.log10(all_c["in_usdt"].clip(lower=1)) * 4).round() / 4
all_c["median_bucket"] = (np.log10(all_c["in_median_usdt"].clip(lower=1)) * 4).round() / 4
sig = (all_c.groupby(["chain", "distinct_recipients", "in_usdt_bucket", "median_bucket"])
       .agg(group_size=("wallet", "count"),
            members=("wallet", lambda s: ", ".join(list(s)[:5])))
       .reset_index())
sig = sig[sig["group_size"] >= 3].sort_values("group_size", ascending=False)
sig.to_csv(OUT / "sender_overlap_proxy_groups.csv", index=False)
print(f"  Signature-groups with >=3 members: {len(sig)}")

# ---- P2.10 snapshot --------------------------------------------------------
section("[P2.10] Daily snapshot")
today = datetime.utcnow().strftime("%Y-%m-%d")
candidates.to_csv(OUT / f"snapshot_{today}.csv", index=False)
print(f"  outputs/snapshot_{today}.csv ({len(candidates)} rows)")

# ---- P2.11 lookup template -------------------------------------------------
section("[P2.11] Block-explorer lookup template")
top_eth = candidates[candidates.chain == "ethereum"].nlargest(30, "in_usdt").copy()
top_tron = candidates[candidates.chain == "tron"].nlargest(30, "in_usdt").copy()
top_eth["scan_url"] = "https://etherscan.io/address/" + top_eth["wallet"]
top_tron["scan_url"] = "https://tronscan.org/#/address/" + top_tron["wallet"]
look = pd.concat([top_eth, top_tron], ignore_index=True)[
    ["chain", "wallet", "distinct_senders", "in_usdt", "distinct_recipients", "scan_url"]]
look["public_tag"] = ""
look["category"] = ""
look.to_csv(OUT / "etherscan_lookup_template.csv", index=False)
print(f"  outputs/etherscan_lookup_template.csv ({len(look)} rows)")

# ---- P3.12 dust sensitivity ------------------------------------------------
section("[P3.12] Dust-floor sensitivity")
rows = []
for floor in [50, 100, 200, 500, 1000, 5000, 10000]:
    for chain in ["ethereum", "tron"]:
        base = candidates[candidates.chain == chain]
        if not len(base):
            continue
        sub = base[base.in_median_usdt >= floor]
        rows.append({"chain": chain, "dust_floor_usd": floor,
                     "n_candidates": len(sub),
                     "pct_kept": round(100 * len(sub) / len(base), 1),
                     "total_inflow_usd": round(float(sub["in_usdt"].sum()))})
pd.DataFrame(rows).to_csv(OUT / "dust_sensitivity.csv", index=False)
print("  outputs/dust_sensitivity.csv saved")

# ---- P3.14 validation template ---------------------------------------------
section("[P3.14] Validation template")
top50 = (all_c.nlargest(50, "risk_v2")
         [["wallet", "chain", "distinct_senders", "in_usdt",
           "distinct_recipients", "in_out_tx_ratio", "risk_v2"]].copy())
top50["scan_url"] = top50.apply(
    lambda r: f"https://etherscan.io/address/{r['wallet']}" if r["chain"] == "ethereum"
              else f"https://tronscan.org/#/address/{r['wallet']}", axis=1)
top50["manual_label"] = ""
top50["confidence"] = ""
top50["notes"] = ""
top50.to_csv(OUT / "validation_template.csv", index=False)
print(f"  outputs/validation_template.csv ({len(top50)} rows)")

print(f"\n{'=' * 70}\nLOCAL PIPELINE COMPLETE\n{'=' * 70}")
for p in sorted(OUT.glob("*.csv")):
    print(f"  {p.name}  ({p.stat().st_size:,} bytes)")
