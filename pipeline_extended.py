"""
Extended pipeline (P0-P3 in the project roadmap) - runs additional BigQuery
queries and produces enrichment artifacts on top of the core notebook output.

Reads credentials from environment variables (or a local .env file). See
config.py / .env.example for the supported variables.

Phases
------
P0  Finish the notebook pipeline
    .1  Tron contract filter
    .2  Bitcoin co-spend clustering (fixed schema)
    .3  Sections 13.2 (exchange anchors), 13.3 (behavioural fingerprint),
        13.4 (ENS), 13.5 (attributability)
    .4  Lift Tron LIMIT
P1  Improve the model
    .5  Replacement risk score (risk_v2)
    .6  Cross-chain matches
    .7  Behavioural-signature proxy groups
    .8  Time-series burst detection
P2  Productize
    .10 Daily snapshot
    .11 Block-explorer lookup template (+ optional Etherscan API enrichment)
P3  Research extension
    .12 Dust-floor sensitivity
    .13 USDC funnel candidates
    .14 Validation template
"""
from __future__ import annotations
import json
import traceback
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

import config

ROOT = Path(__file__).parent
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)

INPUT_CSV = ROOT / "usdt_funnel_candidates.csv"
if not INPUT_CSV.exists():
    raise SystemExit(
        f"Input file {INPUT_CSV.name} not found.\n"
        "Run the notebook Crypto-AML-Analysis.ipynb first; it produces this file."
    )

client = config.make_bq_client()
print(f"BQ client ready. Project={client.project}")

candidates = pd.read_csv(INPUT_CSV)
print(f"Loaded {len(candidates)} candidates "
      f"({(candidates.chain == 'ethereum').sum()} ETH, "
      f"{(candidates.chain == 'tron').sum()} Tron)")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def run(sql: str, max_gb: float | None = None):
    return config.run_query(client, sql, max_gb)


# =============================================================================
# P0.1  Tron contract filter
# =============================================================================
section("[P0.1] Tron contract filter")
tron = candidates[candidates.chain == "tron"].copy()
tron_addrs = ", ".join(repr(a) for a in tron["wallet"])
try:
    sql = f"""
    DECLARE since TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY);
    SELECT DISTINCT address AS contract_addr
    FROM `{config.TRON_EVENTS}`
    WHERE block_timestamp >= since AND address IN ({tron_addrs})
    """
    contracts_df = run(sql, max_gb=30)
    contracts = set(contracts_df["contract_addr"])
    tron_eoa = tron[~tron["wallet"].isin(contracts)].copy()
    tron_eoa.to_csv(OUT / "tron_eoa_filtered.csv", index=False)
    print(f"  Contracts identified: {len(contracts)} -> Tron EOAs: {len(tron_eoa)}")
except Exception as e:
    print(f"  [WARN] Tron contract filter failed: {e}")
    tron_eoa = tron.copy()

# =============================================================================
# P0.2  Bitcoin co-spend clustering (transactions table; partitioned by month)
# =============================================================================
section("[P0.2] Bitcoin co-spend clustering (7d, fixed schema)")
btc_sql = """
DECLARE since_mon DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY);
DECLARE since TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY);
WITH multi AS (
  SELECT t.`hash` AS transaction_hash, addr
  FROM `bigquery-public-data.crypto_bitcoin.transactions` t,
       UNNEST(t.inputs) inp,
       UNNEST(inp.addresses) addr
  WHERE t.block_timestamp_month >= since_mon
    AND t.block_timestamp >= since
    AND ARRAY_LENGTH(t.inputs) >= 2
),
tx_rep AS (
  SELECT transaction_hash, MIN(addr) AS entity_rep
  FROM multi GROUP BY transaction_hash HAVING COUNT(DISTINCT addr) >= 2
),
addr_entity AS (
  SELECT m.addr AS address, MIN(r.entity_rep) AS entity
  FROM multi m JOIN tx_rep r USING (transaction_hash)
  GROUP BY m.addr
)
SELECT entity, COUNT(*) AS n_addresses, ARRAY_AGG(address LIMIT 5) AS sample_addresses
FROM addr_entity
GROUP BY entity
ORDER BY n_addresses DESC
LIMIT 200
"""
try:
    btc = run(btc_sql, max_gb=80)
    btc["sample_addresses"] = btc["sample_addresses"].apply(lambda a: ",".join(a))
    btc.to_csv(OUT / "btc_cospend_entities.csv", index=False)
    print(f"  Found {len(btc)} BTC entities. Top entity merges "
          f"{int(btc['n_addresses'].iloc[0])} addresses.")
except Exception as e:
    print(f"  [WARN] BTC clustering failed: {e}")
    traceback.print_exc()

# =============================================================================
# P0.3 / 13.2  Nearest exchange anchor (ETH)
# =============================================================================
section("[P0.3] Section 13.2 - Nearest exchange anchor (ETH)")
LABELS_URL = ("https://raw.githubusercontent.com/brianleect/etherscan-labels/"
              "main/data/etherscan/combined/combinedAllLabels.json")
with urllib.request.urlopen(LABELS_URL, timeout=60) as r:
    labels = {k.lower(): v for k, v in json.load(r).items()}

EXCHANGE_TAGS = {"exchange", "binance", "coinbase", "kraken", "okx", "kucoin",
                 "bitfinex", "huobi", "bybit", "gate.io", "crypto-com",
                 "bitstamp", "gemini", "mexc", "bitget"}
exchanges = {a for a, v in labels.items() if set(v.get("labels", [])) & EXCHANGE_TAGS}
print(f"  Loaded {len(exchanges)} known exchange addresses")

eth_cands = candidates[candidates.chain == "ethereum"]["wallet"].tolist()
cand_s = ", ".join(repr(a) for a in eth_cands)
exch_s = ", ".join(repr(a) for a in exchanges)
anchors = pd.DataFrame()
if exchanges and eth_cands:
    anchor_sql = f"""
    DECLARE usdt STRING DEFAULT '{config.USDT_ERC20}';
    DECLARE since TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(),
                                                   INTERVAL {config.LOOKBACK_DAYS} DAY);
    SELECT
      IF(from_address IN ({cand_s}), from_address, to_address) AS candidate,
      IF(from_address IN ({exch_s}), 'received_from_exchange',
                                     'sent_to_exchange')        AS direction,
      IF(from_address IN ({exch_s}), from_address, to_address)  AS exchange_addr,
      COUNT(*)                                                  AS n_tx,
      SUM(SAFE_CAST(value AS BIGNUMERIC) / POW(10, {config.USDT_DECIMALS})) AS usdt
    FROM `{config.ETH_TOKEN_TRANSFERS}`
    WHERE token_address = usdt AND block_timestamp >= since
      AND ((from_address IN ({cand_s}) AND to_address IN ({exch_s}))
        OR (from_address IN ({exch_s}) AND to_address IN ({cand_s})))
    GROUP BY candidate, direction, exchange_addr
    ORDER BY usdt DESC
    """
    try:
        anchors = run(anchor_sql, max_gb=60)
        anchors["exchange_name"] = anchors["exchange_addr"].map(
            lambda a: (labels.get(a) or {}).get("name", ""))
        anchors.to_csv(OUT / "exchange_anchors.csv", index=False)
        print(f"  {len(anchors)} candidate-exchange links, "
              f"{anchors['candidate'].nunique()} unique candidates anchored")
        if len(anchors):
            print(anchors.head(10).to_string(index=False))
    except Exception as e:
        print(f"  [WARN] Anchor query failed: {e}")

# =============================================================================
# P0.3 / 13.3  Behavioural fingerprint (hour-of-day, round amounts)
# =============================================================================
section("[P0.3] Section 13.3 - Behavioural fingerprint (ETH)")
fp_sql = f"""
DECLARE usdt STRING DEFAULT '{config.USDT_ERC20}';
DECLARE since TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(),
                                               INTERVAL {config.LOOKBACK_DAYS} DAY);
SELECT to_address AS wallet,
       EXTRACT(HOUR FROM block_timestamp) AS hour_utc,
       COUNT(*) AS n_tx,
       COUNTIF(MOD(SAFE_CAST(value AS BIGNUMERIC),
                   CAST(100 * POW(10, {config.USDT_DECIMALS}) AS BIGNUMERIC)) = 0) AS round_100s
FROM `{config.ETH_TOKEN_TRANSFERS}`
WHERE token_address = usdt AND block_timestamp >= since
  AND to_address IN ({cand_s})
GROUP BY wallet, hour_utc
"""
fp = pd.DataFrame()
try:
    fp = run(fp_sql, max_gb=60)
    fp.to_csv(OUT / "behavioral_fingerprint.csv", index=False)
    peak = fp.loc[fp.groupby("wallet")["n_tx"].idxmax()].copy()
    peak["round_pct"] = (peak["round_100s"] / peak["n_tx"] * 100).round(1)
    peak["tz_guess_offset"] = ((12 - peak["hour_utc"]) % 24).astype(int)
    peak.to_csv(OUT / "behavioral_peak_per_wallet.csv", index=False)
    print(f"  Fingerprinted {fp['wallet'].nunique()} wallets")
    print(f"  Peak UTC-hour distribution:")
    print(peak["hour_utc"].value_counts().sort_index().to_string())
except Exception as e:
    print(f"  [WARN] Fingerprint failed: {e}")

# =============================================================================
# P0.3 / 13.4  ENS resolution - handled by ens_lookup.py (no BQ needed)
# =============================================================================
section("[P0.3] Section 13.4 - ENS resolution")
print("  Run `python ens_lookup.py` separately - uses a public Ethereum RPC, no BQ token.")
try:
    ens_results = {}
    ens_csv = OUT / "ens_names.csv"
    if ens_csv.exists():
        ens_results = dict(zip(*pd.read_csv(ens_csv)[["wallet", "ens_name"]].T.values))
    print(f"  ENS names already resolved: {len(ens_results)}")
except Exception:
    ens_results = {}

# =============================================================================
# P0.3 / 13.5  Attributability score
# =============================================================================
section("[P0.3] Section 13.5 - Attributability score")
eth_df = candidates[candidates.chain == "ethereum"].copy()
score = pd.Series(0.0, index=eth_df.index)
w_lower = eth_df["wallet"].str.lower()

anchor_wallets = set(anchors["candidate"].str.lower()) if len(anchors) else set()
score += w_lower.isin(anchor_wallets).astype(float) * 40
label_match = w_lower.map(lambda x: bool((labels.get(x) or {}).get("name")))
score += label_match.astype(float) * 30
ens_lower = {k.lower() for k in ens_results}
score += w_lower.isin(ens_lower).astype(float) * 20

eth_df["has_exchange_anchor"] = w_lower.isin(anchor_wallets)
eth_df["has_label"] = label_match
eth_df["has_ens"] = w_lower.isin(ens_lower)
eth_df["attributability"] = score.clip(0, 100)
eth_df.sort_values("attributability", ascending=False).to_csv(OUT / "attributability.csv", index=False)
print(f"  median={score.median():.0f}  max={score.max():.0f}")
print(f"  exchange-anchor: {int(eth_df['has_exchange_anchor'].sum())} | "
      f"label: {int(eth_df['has_label'].sum())} | "
      f"ENS: {int(eth_df['has_ens'].sum())}")

# =============================================================================
# P0.4  Lift Tron LIMIT
# =============================================================================
section("[P0.4] Tron LIMIT 5000")
tron_sql = f"""
DECLARE since TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(),
                                               INTERVAL {config.TRON_LOOKBACK_DAYS} DAY);
WITH transfers AS (
  SELECT JSON_VALUE(args,'$[0]') from_address, JSON_VALUE(args,'$[1]') to_address,
         SAFE_CAST(JSON_VALUE(args,'$[2]') AS BIGNUMERIC) / POW(10, {config.USDT_DECIMALS}) amt,
         block_timestamp
  FROM `{config.TRON_EVENTS}`
  WHERE address = '{config.USDT_TRC20_HEX}'
    AND event_signature = 'Transfer(address,address,uint256)'
    AND block_timestamp >= since
),
incoming AS (
  SELECT to_address wallet, COUNT(*) in_cnt,
         COUNT(DISTINCT from_address) distinct_senders,
         SUM(amt) in_usdt, APPROX_QUANTILES(amt, 2)[OFFSET(1)] in_median_usdt,
         MIN(block_timestamp) first_in, MAX(block_timestamp) last_in
  FROM transfers GROUP BY wallet
),
outgoing AS (
  SELECT from_address wallet, COUNT(*) out_cnt,
         COUNT(DISTINCT to_address) distinct_recipients, SUM(amt) out_usdt
  FROM transfers GROUP BY wallet
)
SELECT i.wallet, i.in_cnt, i.distinct_senders, i.in_usdt, i.in_median_usdt,
       COALESCE(o.out_cnt, 0) out_cnt,
       COALESCE(o.distinct_recipients, 0) distinct_recipients,
       COALESCE(o.out_usdt, 0) out_usdt,
       SAFE_DIVIDE(i.in_cnt, COALESCE(o.out_cnt, 0)) in_out_tx_ratio,
       TIMESTAMP_DIFF(i.last_in, i.first_in, HOUR) active_hours
FROM incoming i LEFT JOIN outgoing o USING (wallet)
WHERE i.distinct_senders >= 50 AND COALESCE(o.out_cnt, 0) <= 25
  AND i.in_usdt >= 10000 AND i.in_median_usdt >= 50
ORDER BY i.distinct_senders DESC, i.in_usdt DESC
LIMIT 5000
"""
try:
    tron_ext = run(tron_sql, max_gb=10)
    tron_ext["chain"] = "tron"
    tron_ext.to_csv(OUT / "tron_extended.csv", index=False)
    print(f"  Extended Tron pool: {len(tron_ext)} (was capped at 1000)")
except Exception as e:
    print(f"  [WARN] Tron extension failed: {e}")
    tron_ext = pd.DataFrame()

# =============================================================================
# P1.5  Improved risk score (risk_v2) - local
# =============================================================================
section("[P1.5] risk_v2")
all_c = candidates.copy()
all_c["fanin_pct"] = all_c.groupby("chain")["distinct_senders"].rank(pct=True)
all_c["inflow_pct"] = all_c.groupby("chain")["in_usdt"].rank(pct=True)
all_c["ratio_pct"] = all_c.groupby("chain")["in_out_tx_ratio"].rank(pct=True)
all_c["single_recipient"] = (all_c["distinct_recipients"] <= 1).astype(int)
all_c["risk_v2"] = (30 * all_c["fanin_pct"] + 25 * all_c["inflow_pct"]
                    + 25 * all_c["ratio_pct"] + 20 * all_c["single_recipient"]).round(1)
all_c.sort_values("risk_v2", ascending=False).to_csv(OUT / "risk_score_v2.csv", index=False)
print(f"  range {all_c['risk_v2'].min():.1f}–{all_c['risk_v2'].max():.1f} | "
      f">=80: {int((all_c['risk_v2'] >= 80).sum())} | "
      f">=90: {int((all_c['risk_v2'] >= 90).sum())}")

# =============================================================================
# P1.6  Cross-chain matches - local
# =============================================================================
section("[P1.6] Cross-chain matches")
eth_set = set(candidates[candidates.chain == "ethereum"]["wallet"])
tron_set = set(candidates[candidates.chain == "tron"]["wallet"])
overlap = eth_set & tron_set
print(f"  Same hex on both chains: {len(overlap)}")
cc_rows = []
for w in overlap:
    e = candidates[(candidates.chain == "ethereum") & (candidates.wallet == w)].iloc[0]
    t = candidates[(candidates.chain == "tron") & (candidates.wallet == w)].iloc[0]
    cc_rows.append({
        "wallet": w,
        "eth_in_usdt": float(e["in_usdt"]), "eth_senders": int(e["distinct_senders"]),
        "tron_in_usdt": float(t["in_usdt"]), "tron_senders": int(t["distinct_senders"]),
    })
pd.DataFrame(cc_rows).to_csv(OUT / "cross_chain_matches.csv", index=False)

# =============================================================================
# P1.7  Behavioural-signature proxy clusters - local
# =============================================================================
section("[P1.7] Behavioural-signature proxy clusters")
all_c["in_usdt_bucket"] = (np.log10(all_c["in_usdt"].clip(lower=1)) * 4).round() / 4
all_c["median_bucket"] = (np.log10(all_c["in_median_usdt"].clip(lower=1)) * 4).round() / 4
sig = (all_c.groupby(["chain", "distinct_recipients", "in_usdt_bucket", "median_bucket"])
       .agg(group_size=("wallet", "count"),
            members=("wallet", lambda s: ", ".join(list(s)[:5])))
       .reset_index())
sig = sig[sig["group_size"] >= 3].sort_values("group_size", ascending=False)
sig.to_csv(OUT / "sender_overlap_proxy_groups.csv", index=False)
print(f"  Signature-groups with >=3 members: {len(sig)}")

# =============================================================================
# P1.8  Time-series burst detection - BigQuery
# =============================================================================
section("[P1.8] Time-series burst detection (top 50 ETH)")
top50 = candidates[candidates.chain == "ethereum"].nlargest(50, "in_usdt")["wallet"].tolist()
if top50:
    top50_s = ", ".join(repr(a) for a in top50)
    burst_sql = f"""
    DECLARE usdt STRING DEFAULT '{config.USDT_ERC20}';
    DECLARE since TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(),
                                                   INTERVAL {config.LOOKBACK_DAYS} DAY);
    SELECT to_address AS wallet, DATE(block_timestamp) AS d,
           COUNT(*) AS n_tx,
           SUM(SAFE_CAST(value AS BIGNUMERIC) / POW(10, {config.USDT_DECIMALS})) AS usdt
    FROM `{config.ETH_TOKEN_TRANSFERS}`
    WHERE token_address = usdt AND block_timestamp >= since
      AND to_address IN ({top50_s})
    GROUP BY wallet, d
    ORDER BY wallet, d
    """
    try:
        burst = run(burst_sql, max_gb=60)
        burst.to_csv(OUT / "time_series_top50_eth.csv", index=False)
        daily_max = burst.groupby("wallet")["usdt"].max().rename("max_day_usdt")
        daily_sum = burst.groupby("wallet")["usdt"].sum().rename("total_usdt")
        summary = pd.concat([daily_max, daily_sum], axis=1).reset_index()
        summary["max_day_share"] = summary["max_day_usdt"] / summary["total_usdt"]
        bursty = summary[summary["max_day_share"] >= 0.5].sort_values("max_day_share", ascending=False)
        bursty.to_csv(OUT / "bursty_wallets.csv", index=False)
        print(f"  Wallets with >50% of inflow on a single day: {len(bursty)}")
    except Exception as e:
        print(f"  [WARN] Burst query failed: {e}")

# =============================================================================
# P2.10  Daily snapshot - local
# =============================================================================
section("[P2.10] Daily snapshot")
from datetime import datetime
today = datetime.utcnow().strftime("%Y-%m-%d")
candidates.to_csv(OUT / f"snapshot_{today}.csv", index=False)
print(f"  outputs/snapshot_{today}.csv ({len(candidates)} rows)")

# =============================================================================
# P2.11  Block-explorer lookup template + optional Etherscan API
# =============================================================================
section("[P2.11] Block-explorer lookup template")
top_eth = candidates[candidates.chain == "ethereum"].nlargest(30, "in_usdt").copy()
top_tron = candidates[candidates.chain == "tron"].nlargest(30, "in_usdt").copy()
top_eth["scan_url"] = "https://etherscan.io/address/" + top_eth["wallet"]
top_tron["scan_url"] = "https://tronscan.org/#/address/" + top_tron["wallet"]
look = pd.concat([top_eth, top_tron], ignore_index=True)[
    ["chain", "wallet", "distinct_senders", "in_usdt", "distinct_recipients", "scan_url"]]
look["public_tag"] = ""
look["category"] = ""

# Optional: enrich with Etherscan API if a key is set
if config.ETHERSCAN_API_KEY:
    print("  Enriching ETH candidates via Etherscan API...")
    import time
    for i, row in look[look["chain"] == "ethereum"].iterrows():
        try:
            url = (f"https://api.etherscan.io/api?module=contract&action=getsourcecode"
                   f"&address={row['wallet']}&apikey={config.ETHERSCAN_API_KEY}")
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.load(r)
                if data.get("status") == "1" and data["result"]:
                    look.at[i, "public_tag"] = data["result"][0].get("ContractName", "")
            time.sleep(0.25)  # respect rate limit
        except Exception:
            pass
look.to_csv(OUT / "etherscan_lookup_template.csv", index=False)
print(f"  outputs/etherscan_lookup_template.csv ({len(look)} rows)")

# =============================================================================
# P3.12  Dust-floor sensitivity - local
# =============================================================================
section("[P3.12] Dust-floor sensitivity")
rows = []
for floor in [50, 100, 200, 500, 1000, 5000, 10000]:
    for chain in ["ethereum", "tron"]:
        base = candidates[candidates.chain == chain]
        if not len(base):
            continue
        sub = base[base.in_median_usdt >= floor]
        rows.append({"chain": chain, "dust_floor_usd": floor, "n_candidates": len(sub),
                     "pct_kept": round(100 * len(sub) / len(base), 1),
                     "total_inflow_usd": round(float(sub["in_usdt"].sum()))})
pd.DataFrame(rows).to_csv(OUT / "dust_sensitivity.csv", index=False)
print("  outputs/dust_sensitivity.csv saved")

# =============================================================================
# P3.13  USDC funnel candidates (Ethereum, 30 days)
# =============================================================================
section("[P3.13] USDC funnel candidates (ETH, 30d)")
usdc_sql = f"""
DECLARE usdc STRING DEFAULT '{config.USDC_ERC20}';
DECLARE since TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY);
WITH transfers AS (
  SELECT from_address, to_address,
         SAFE_CAST(value AS BIGNUMERIC) / POW(10, 6) amt, block_timestamp
  FROM `{config.ETH_TOKEN_TRANSFERS}`
  WHERE token_address = usdc AND block_timestamp >= since
),
incoming AS (
  SELECT to_address wallet, COUNT(*) in_cnt,
         COUNT(DISTINCT from_address) distinct_senders,
         SUM(amt) in_usdc, APPROX_QUANTILES(amt, 2)[OFFSET(1)] in_median_usdc
  FROM transfers GROUP BY wallet
),
outgoing AS (
  SELECT from_address wallet, COUNT(*) out_cnt FROM transfers GROUP BY wallet
)
SELECT i.wallet, i.in_cnt, i.distinct_senders, i.in_usdc, i.in_median_usdc,
       COALESCE(o.out_cnt, 0) out_cnt
FROM incoming i LEFT JOIN outgoing o USING (wallet)
WHERE i.distinct_senders >= 50 AND COALESCE(o.out_cnt, 0) <= 25
  AND i.in_usdc >= 10000 AND i.in_median_usdc >= 50
ORDER BY i.distinct_senders DESC, i.in_usdc DESC
LIMIT 200
"""
try:
    usdc = run(usdc_sql, max_gb=60)
    usdc["chain"] = "ethereum_usdc"
    usdc.to_csv(OUT / "usdc_candidates.csv", index=False)
    print(f"  USDC candidates: {len(usdc)}")
except Exception as e:
    print(f"  [WARN] USDC query failed: {e}")

# =============================================================================
# P3.14  Validation template
# =============================================================================
section("[P3.14] Validation template")
top50 = all_c.nlargest(50, "risk_v2")[
    ["wallet", "chain", "distinct_senders", "in_usdt",
     "distinct_recipients", "in_out_tx_ratio", "risk_v2"]].copy()
top50["scan_url"] = top50.apply(
    lambda r: f"https://etherscan.io/address/{r['wallet']}" if r["chain"] == "ethereum"
              else f"https://tronscan.org/#/address/{r['wallet']}", axis=1)
top50["manual_label"] = ""
top50["confidence"] = ""
top50["notes"] = ""
top50.to_csv(OUT / "validation_template.csv", index=False)
print(f"  outputs/validation_template.csv ({len(top50)} rows)")

print(f"\n{'=' * 70}\nPIPELINE COMPLETE\n{'=' * 70}")
for p in sorted(OUT.glob("*.csv")):
    print(f"  {p.name}  ({p.stat().st_size:,} bytes)")
