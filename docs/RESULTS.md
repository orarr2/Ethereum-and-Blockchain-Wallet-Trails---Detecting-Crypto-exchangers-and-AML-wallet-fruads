# Reference run - findings

A reference execution on **2026-06-10** with `LOOKBACK_DAYS=90` (Ethereum) and
`TRON_LOOKBACK_DAYS=7` (Tron). Your numbers will differ because the windows are
rolling and the BigQuery datasets are continuously updated.

## Headline numbers

| Chain | Window | Candidates after dust floor | After contract filter | Total inflow USDT |
|---|---|---|---|---|
| Ethereum (ERC-20) | 90 days | 430 | **412 EOAs** | $104.6 M |
| Tron (TRC-20) | 7 days | 1,000 (capped) | (no contract filter applied) | $1,657.5 M |

## Suspicious sub-cohorts

| Cut | Count |
|---|---|
| All funnel candidates | 1,412 |
| Risk_v2 ≥ 80 | 78 |
| Risk_v2 ≥ 90 | 32 |
| **Single-recipient funnels** (extreme aggregator pattern) | **554** (496 Tron + 58 ETH) |
| Inflow ≥ $1 M | ≈ 190 |
| Within 3 hops of an OFAC-listed address | **0** |

## Counter-terror-financing diagnostic

| Chain | Window | OFAC seeds | Touching transfers | Active seed addresses |
|---|---|---|---|---|
| Ethereum (USDT) | 90 days | 182 | **1** | 1 |
| Tron (USDT) | 30 days | 48 | **1** of 71.7 M | 1 |

Confirms the project's working hypothesis: **Tether freezes OFAC-listed
addresses**, so direct on-chain proximity is a dead end. Value is in funnel
detection itself, not in OFAC graph distance.

## Top candidates

**Ethereum:** `0x748cd46c...086b23` - 18,802 distinct senders, only 25 outgoing
transactions in 90 days. $1.9 M in / $1.3 M out.

**Tron:** `0x08146af6...d75dd983` - 6,212 distinct senders sending **$123.5 M**
to **one** recipient in just 7 days. Median transfer $3,000. Most likely a
single major unlisted regional exchange.

## Enrichment results

| Metric | Value |
|---|---|
| ETH candidates matching the ~30 K Etherscan label set | **15 / 412** |
| Top 100 ETH candidates with a primary ENS name set | **0 / 100** |
| Behavioural-signature groups with ≥ 3 members ("probable same operator") | **109** |
| Largest signature group | 88 Tron wallets with identical fingerprint |

## Dust-floor sensitivity

| Floor (USD) | ETH kept | Tron kept | ETH inflow kept |
|---|---|---|---|
| 50  | 100.0 % | 100.0 % | 100.0 % |
| 100 | 67.0 % | 81.0 % | 88.3 % |
| 200 | 33.3 % | 44.8 % | 68.9 % |
| 500 | 11.9 % | 25.0 % | 38.5 % |
| 1,000 | 4.6 % | 15.2 % | 26.0 % |

A $200 floor drops two-thirds of candidates but keeps ~69% of inflow dollars -
useful "sweet spot" for high-precision triage.

## Cost (reference run)

| Query | Bytes scanned | Cost |
|---|---|---|
| ETH funnel detection (90 d) | 46.23 GB | $0.28 |
| ETH contract filter | 4.11 GB | $0.03 |
| Tron funnel detection (7 d) | 3.23 GB | $0.02 |
| ETH edge fetch (graph) | 46.23 GB | $0.28 |
| OFAC ETH activity check | 42.39 GB | $0.26 |
| OFAC Tron activity check | 13.22 GB | $0.08 |
| **Total notebook** | **~155 GB** | **~$0.95** |
