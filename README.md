# Crypto-AML-Analysis

Data-science / ML research that surfaces, as **investigative leads**, independent/unregulated USDT
exchangers ("funnel accounts") and tests **counter-terror-financing (CTF)** proximity to OFAC-sanctioned
addresses - on **Ethereum (ERC-20)** and **Tron (TRC-20)**, via Google BigQuery public datasets.

Everything lives in **one self-contained notebook**: [`Crypto-AML-Analysis.ipynb`](Crypto-AML-Analysis.ipynb).

## What it does

| Layer | Content |
|---|---|
| **Data** | BigQuery public datasets (`crypto_ethereum`, `goog_blockchain_tron_mainnet_us`) + OFAC sanctioned list + community Etherscan labels |
| **Detection** | High fan-in / low fan-out "funnel" candidates within a **human-scale band** ($10K–$2M inflow, ≤5,000 distinct senders), a **dust floor** (median per-transfer ≥ $50), and smart-contract filtering (EOAs only) |
| **Behavioural ranking** | Candidates ranked by an `informal_score` - depositor count, round-amount ratio, pass-through balance, and a human activity schedule (`hours_of_day_active`, `night_share`). Net accumulators (`pass_through < 0.5`) are flagged and excluded |
| **Graph + ML** | NetworkX directed graph (PageRank / degree / clustering), mixer/funnel heuristics, multi-hop OFAC proximity, Isolation-Forest risk score - run on **both** chains |
| **CTF** | OFAC-listed addresses are frozen/dormant in USDT (down-weighted), so CTF signal comes from a **behavioural donation-campaign score** (`terror_signals.py`): many small donors -> concentrated outflow, with an optional temporal burst |
| **Identity** | Co-funding clustering (group funnels by shared depositors), **nearest-exchange anchor promoted to a first-class `actionability` signal** (a funnel with a direct link to a labelled exchange is a concrete KYC/subpoena target), behavioural fingerprint (timezone), ENS resolution, and a **modular free-source enrichment layer** (`enrichment.py`) |
| **Triage** | Leads are the **per-chain top-K** by score, not a fixed threshold (which floods on Bitcoin); the master export is sorted by `actionability` = `risk_score` + exchange-anchor bonus |
| **Visualise** | Fan-in/fan-out scatter coloured by `informal_score` + a node-link network plot of the top funnel hubs and their neighbours |

## Targeting

The detection is deliberately scoped to **human-operated, informal exchangers**. The `$10K–$2M` inflow band
and the `≤5,000` distinct-sender cap exclude institutional/CEX/OTC aggregators (wallets moving tens to
hundreds of millions), and candidates are ranked by behaviour rather than by raw size. Widen
`MAX_IN_USDT` / `MAX_DISTINCT_SENDERS` in the config cell to study the institutional tier instead.

## Quickstart

```bash
pip install -r requirements.txt
# then open the notebook
jupyter lab Crypto-AML-Analysis.ipynb
```

In the notebook's **Setup** cell, paste a BigQuery access token (`gcloud auth print-access-token`) into
`BQ_ACCESS_TOKEN`, or use `gcloud auth application-default login`, and set `BQ_PROJECT`. Run top to bottom.
Every query is **dry-run cost-estimated** and capped with `maximum_bytes_billed`; address-list queries use
parameterised `UNNEST(@addr)`, and results can be memoised to Parquet with `cached()` so re-runs cost $0.
Keep the `block_timestamp` partition filter to control cost.

## Responsible use

A high fan-in / low fan-out pattern, a mixer signature, a campaign-shaped inflow, or an N-hop link to a
sanctioned address are **research signals, not findings of guilt** - they also fit legitimate processors,
OTC desks and exchange deposit wallets. Identity work produces **entities and anchors**, never a real-world
name; attribution to a person is for authorised enforcement (subpoena to the exchange that holds the KYC).

**On IP / real-world attribution:** an IP address is *not* derivable from on-chain data - a ledger records
value flow, never the network origin of the broadcasting peer. Recovering it requires off-chain
infrastructure (listening nodes) or a licensed intelligence provider. The enrichment layer therefore ships
only **free, on-chain sources** by default; IP attribution is a documented, non-wired plug point
(`enrichment.IPAttributionSource`). Public on-chain data only; use authoritative OFAC designations for seeds.
