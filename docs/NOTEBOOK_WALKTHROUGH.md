# Crypto-AML-Analysis - notebook walkthrough (cell by cell)

This document walks every one of the 56 cells in `Crypto-AML-Analysis.ipynb` and
answers, for each:

- **What it does** - the operational purpose in one paragraph.
- **APIs / SQL touched** - which external service or BigQuery public dataset is
  hit, and the representative query where applicable.
- **Produces** - the DataFrame / graph / dict that downstream cells consume.

Public BigQuery datasets used:

| Chain     | Dataset                                                          |
|-----------|------------------------------------------------------------------|
| Ethereum  | `bigquery-public-data.crypto_ethereum` (`token_transfers`, `contracts`) |
| Tron      | `bigquery-public-data.goog_blockchain_tron_mainnet_us` (`decoded_events`) |
| Bitcoin   | `bigquery-public-data.crypto_bitcoin` (`transactions`)           |
| Zcash     | `bigquery-public-data.crypto_zcash` (`transactions`)             |

Public HTTP sources used: OFAC sanctions repo (0xB10C mirror), CryptoScamDB,
NBCTF mirror (kept in this repo under `data/nbctf_addresses.json`),
Etherscan-labels community JSON (brianleect mirror), Chainalysis on-chain
`isSanctioned` oracle contract via public Ethereum RPCs, ENS reverse
resolution via public Ethereum RPCs.

Nothing here is paid or licensed - the pipeline is designed so any analyst
with a GCP billing project can reproduce it end to end.

---

## Section 1 - Setup and authentication

### Cell 0 - markdown, title and abstract
Header markdown: the notebook detects USDT informal-exchanger candidates plus
CTF proximity leads across Ethereum, Tron, Bitcoin and Zcash. States the two
refinements (dust floor, TRC-20 path) and the intentional scope
(`$10K-$2M` in-band, `<= 5,000` distinct senders).

### Cell 1 - markdown, "Setup and authentication"
Section header.

### Cell 2 - code, BigQuery authentication
Prompts the user for `BQ_PROJECT` and `BQ_ACCESS_TOKEN` via `getpass.getpass`
(masked input). Builds a `google.oauth2.credentials.Credentials` object with a
`refresh_handler` and a naive-UTC expiry, then creates `bigquery.Client`. Falls
back to gcloud Application Default Credentials if the token is blank.

- APIs: `google.cloud.bigquery`, `google.oauth2.credentials`.
- Produces: the module-global `client` used by every subsequent SQL cell.

Excerpt:
```python
BQ_PROJECT      = getpass.getpass("1) GCP project ID or number: ").strip()
BQ_ACCESS_TOKEN = getpass.getpass("2) BigQuery access token (blank = use gcloud ADC): ").strip()
...
client = bigquery.Client(project=BQ_PROJECT, credentials=_make_token_credentials(BQ_ACCESS_TOKEN))
```

### Cell 3 - markdown, "Config - datasets, contracts, thresholds"

### Cell 4 - code, configuration constants
All dataset paths, USDT contract addresses (ERC-20 hex and TRC-20 hex/base58),
per-chain lookback windows, per-chain result limits, informal-exchanger
detection thresholds (senders band, USDT band, dust floor, max out-tx), the
`MAX_GB_PER_QUERY` cost ceiling (150 GB, hard cap on `maximum_bytes_billed`),
and the known-exchange exclusion set.

- Produces: names read by every downstream cell.

Excerpt:
```python
MIN_DISTINCT_SENDERS = 50
MAX_DISTINCT_SENDERS = 5_000
MIN_IN_USDT          = 10_000
MAX_IN_USDT          = 2_000_000
MIN_MEDIAN_USDT      = 50
MAX_OUT_TX           = 25
MAX_GB_PER_QUERY     = float(os.environ.get("BQ_MAX_GB", 150))
```

## Section 2 - Query helpers

### Cell 5 - markdown, "Query helpers (live)"

### Cell 6 - code, `run_query` / `run_query_chunked` / `cached`
Wraps `client.query` in a `QueryJobConfig(maximum_bytes_billed=...)` so every
SQL statement is bounded server-side. `run_query_chunked` splits an address
array bigger than `ADDR_CHUNK_SIZE` (5,000 by default) into several
`IN UNNEST(@addrs)` queries and concatenates the results. `cached(name, fn,
key)` pickles the return of `fn()` to `_cache/name_<sha1>.pkl` so re-runs skip
BigQuery when the lookback / limit parameters have not changed.

- APIs: BigQuery `QueryJobConfig`, `ArrayQueryParameter`.
- Produces: the wrappers used by every candidate-detection cell below.

Excerpt:
```python
def run_query(sql: str, params=None, max_gb: float | None = None) -> pd.DataFrame:
    cap_gb = MAX_GB_PER_QUERY if max_gb is None else max_gb
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(cap_gb * 1024**3))
    if params: cfg.query_parameters = params
    job = client.query(sql, job_config=cfg)
    df  = job.to_dataframe(create_bqstorage_client=False)
    billed_gb = (job.total_bytes_billed or 0) / 1024**3
    print(f"Billed: {billed_gb:,.3f} GB (~${billed_gb/1024*PRICE_PER_TB_USD:,.4f}) | rows={len(df):,}")
    return df
```

## Section 3 - OFAC seeds and risk attribution

### Cell 7 - markdown, "OFAC sanctioned seeds + Tron base58->hex"

### Cell 8 - code, load OFAC seeds
Fetches the three OFAC digital-currency lists (`ETH`, `USDT`, `XBT` for
Bitcoin, `ZEC`) from the `0xB10C/ofac-sanctioned-digital-currency-addresses`
public mirror. Also converts `TRX` base58 addresses to their hex form so they
can be matched against `crypto_ethereum`-style hex fields.

- API: `https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/lists/sanctioned_addresses_<sym>.json`
- Produces: `SANCTIONED_ETH`, `SANCTIONED_BTC`, `SANCTIONED_ZEC`, `SANCTIONED_TRON` sets.

### Cell 9 - markdown, "Risk Attribution - multi-source wallet flagging"

### Cell 10 - code, risk-attribution registry
Builds `RISK_TAGS = {addr_lower: {"source:category", ...}}` combining:

1. OFAC (from cell 8).
3. CryptoScamDB (public HTTP API `https://api.cryptoscamdb.org/v1/addresses`).
5. Etherscan Phish/Hack tags (populated lazily after the labels JSON loads in
   section 9).
8. NBCTF (Israel National Bureau for Counter-Terror Financing) addresses,
   loaded from `data/nbctf_addresses.json` in this repo, or from the remote
   mirror as fallback. Preserves NBCTF metadata (`order`, `affiliation`, `url`)
   in `NBCTF_META` so downstream cells can display provenance.
2. Chainalysis on-chain oracle (`0x40C57923...`) - deferred to the enrichment
   cell so we only pay RPC for the actual candidate set.

Defines `annotate_risk(df, wallet_col)` which appends `risk_tags`,
`risk_categories`, `risk_source_count`, `risk_attribution_score` (0-100),
`is_known_risk`, `confirmed` (>=2 independent sources or sanctions/terror).

- APIs: HTTP (OFAC, CryptoScamDB, NBCTF), on-chain view call
  (`isSanctioned(address)`).
- Produces: `RISK_TAGS`, `NBCTF_META`, `annotate_risk`,
  `query_chainalysis_oracle`.

## Section 4 - Ethereum ERC-20 candidate detection

### Cell 11 - markdown, "Candidate detection - Ethereum USDT (ERC-20)"

### Cell 12 - code, Ethereum candidate SQL
The heart of the ETH detector. Groups every USDT-ERC20 transfer of the last
`LOOKBACK_DAYS` (90 by default) by receiver, computes per-wallet
`in_cnt`, `distinct_senders`, `in_usdt`, `in_median_usdt`, `active_days`,
`hours_of_day_active`, `night_cnt`, `round100_cnt`, then joins it with per-
wallet outgoing stats (`out_cnt`, `distinct_recipients`, `out_usdt`). Filters
on:

- `distinct_senders BETWEEN 50 AND 5_000`
- `out_cnt <= 25`
- `in_usdt BETWEEN 10_000 AND 2_000_000`
- `in_median_usdt >= 50` (the dust floor)
- wallet NOT IN the known-exchange exclusion set

- SQL target: `bigquery-public-data.crypto_ethereum.token_transfers`.
- Produces: the SQL string `eth_sql` (executed by the next cell).

Excerpt (the WHERE clause is the "funnel-shape" gate):
```sql
WHERE i.distinct_senders BETWEEN 50 AND 5000
  AND COALESCE(o.out_cnt,0) <= 25
  AND i.in_usdt BETWEEN 10000 AND 2000000
  AND i.in_median_usdt >= 50
  AND i.wallet NOT IN (<known-exchange addresses>)
ORDER BY i.distinct_senders DESC, i.in_usdt DESC
LIMIT 10000
```

### Cell 13 - code, execute ETH SQL
`eth = run_query(eth_sql)`. Simple execution + preview.

### Cell 14 - markdown, "Drop smart contracts -> keep EOA only"

### Cell 15 - code, drop contract addresses
Joins the candidates against `crypto_ethereum.contracts`, keeps only
externally-owned accounts (EOA). This is what turns "any address" into
"human-operated wallet".

- SQL: `SELECT address FROM crypto_ethereum.contracts WHERE address IN UNNEST(@addrs)`.
- Produces: `eth_eoa` (EOA-only frame).

## Section 5 - Tron TRC-20 candidate detection

### Cell 16 - markdown, "Candidate detection - Tron USDT (TRC-20)"

### Cell 17 - code, Tron candidate SQL
Same shape as the ETH detector but reads TRC-20 Transfer events out of the
Google Cloud public Tron dataset. The event's `args` array contains
`[from, to, value]` as JSON strings; the SQL extracts each with `JSON_VALUE`
and divides `value` by `10^USDT_DECIMALS` (6).

- SQL target: `bigquery-public-data.goog_blockchain_tron_mainnet_us.decoded_events`
  filtered by `address = <USDT_TRC20_HEX>` and
  `event_signature = 'Transfer(address,address,uint256)'`.
- Produces: `tron_sql`.

### Cell 18 - code, run Tron SQL + behavioural feature engineering
Runs `tron_sql`, drops Tron contract addresses (any address that ALSO appears
as a `decoded_events.address` emitter), then defines and applies:

- `add_behavioural_features(df)` -> `night_share`, `round100_share`,
  `pass_through` (=`out_usdt/in_usdt`), `is_accumulator`,
  `human_hours`, `human_schedule`.
- `informal_exchanger_score(df)` -> composite rank in [0,1] over
  `distinct_senders`, `round100_share`, balance of `pass_through`,
  human schedule, non-accumulator, and a small bonus for
  `distinct_recipients in [2, 6]`.

Applies both to `eth_eoa` and `tron_eoa`, producing the ranked frames
`eth_ranked` and `tron_ranked`.

## Section 5b - Bitcoin candidate detection

### Cell 19 - markdown, "Bitcoin (BTC) funnel-account detection"

### Cell 20 - code, Bitcoin candidate SQL and execution
Reads `crypto_bitcoin.transactions` (partitioned by `block_timestamp_month`
for cheap partition pruning). Unnests `outputs.addresses` to build a
`(tx_hash, block_timestamp, out_value_btc, recipient)` stream, joins on
`tx_hash` with the `inputs.addresses` stream to attribute a sender per output,
then groups by wallet with the same fan-in / fan-out / night / active-days
features as the ETH detector. Computes a `funnel_signal` inside SQL so the
per-chain `LIMIT` chops the tail rather than the head:

```sql
LN(1.0 + i.distinct_senders)                    -- fan-in
+ 2.0 * (1.0 / (1.0 + COALESCE(o.out_cnt, 0)))  -- concentration bonus
+ 0.5 * LN(1.0 + i.night_cnt)                   -- off-hours
+ 0.3 * LN(1.0 + i.active_days)                 -- long-lived
```

- SQL target: `bigquery-public-data.crypto_bitcoin.transactions`.
- Produces: `btc` DataFrame.

## Section 5c - Zcash transparent candidate detection

### Cell 21 - markdown, "Zcash (ZEC) transparent funnel-account detection"

### Cell 22 - code, Zcash candidate SQL and execution
Same partitioned-transactions pattern as BTC. Additionally computes
`deshield_in_share` per receiver: the fraction of the receiver's incoming
transactions whose input side had `addresses IS NULL` or an empty array,
which is the Zcash on-chain footprint of a shielded-pool -> t-addr boundary
(unshielding). Only transparent (`t-addr`) traffic is on-chain visible;
shielded z-addrs are explicitly out of scope.

- SQL target: `bigquery-public-data.crypto_zcash.transactions`.
- Produces: `zec` DataFrame (empty is expected on quiet windows).

## Section 5d - Unify the pool

### Cell 23 - markdown, "Unify the candidate pool across all 4 chains"

### Cell 24 - code, normalise + concatenate
Casts any BigQuery `Decimal` cells to `float`, then rewrites the per-chain
frames into a common schema (`chain`, `asset`, `raw_value_in`,
`raw_value_out`, `raw_value_median`, `value_in_usd_est`, `value_unit`) using
`BTC_PRICE_USD` and `ZEC_PRICE_USD` for cross-chain USD ranking.

- Produces: `all_candidates` (one row per candidate wallet, one column set).

## Section 6 - Transaction graph

### Cell 25 - markdown, "Build the transaction graph (Layer 2)"

### Cell 26 - code, per-chain edge queries + `nx.DiGraph`
For each chain, defines a `fetch_edges_<chain>(wallets)` function that pulls
every transfer where either endpoint is one of our candidates
(`from_address IN UNNEST(@addrs) OR to_address IN UNNEST(@addrs)`). BTC and
ZEC use the partitioned-transactions pattern to stay cheap. `build_graph`
aggregates edges by `(from, to)` and inserts them into a `networkx.DiGraph`
weighted by `total_usdt` and `tx_count`. Results are pickled through
`cached()` so re-runs skip BQ.

- SQL targets: `crypto_ethereum.token_transfers`, `goog_blockchain_tron_mainnet_us.decoded_events`,
  `crypto_bitcoin.transactions`, `crypto_zcash.transactions`.
- Produces: `edges_eth`, `edges_tron`, `edges_btc`, `edges_zec`, `G`, `G_tron`,
  `G_btc`, `G_zec`.

## Section 7 - Graph features

### Cell 27 - markdown, "Features (Layer 3) - graph + mixer/funnel + sanctioned proximity"

### Cell 28 - code, `graph_features`
For each graph, computes weighted PageRank, in/out degree, clustering
coefficient, in/out USDT volume, in/out tx count. Also derives:

- `mixer_score = degree_balance * value_balance * multi_hop_indicator`
- `funnel_score = LN(1 + in_degree) * (1 / (1 + out_degree))`
- `hops_to_sanctioned` via a bidirectional BFS (`hops_to_sanctioned` walks
  both forward and reverse edges, taking the min; this was a bug fix from an
  earlier version that only walked outgoing edges).
- `is_sanctioned` (candidate is itself in the OFAC list).

- APIs: `networkx.pagerank`, `networkx.clustering`.
- Produces: `feats_eth`, `feats_tron`, `feats_btc`, `feats_zec`.

## Section 8 - Anomaly scoring

### Cell 29 - markdown, "Unsupervised anomaly scoring (Layer 3) -> risk score"

### Cell 30 - code, `score` -> Isolation-Forest + blended `risk_score`
Standard-scales the ten graph/behavioural features per chain, fits an
`IsolationForest(n_estimators=300, contamination=0.05)`, then blends:

```
risk_score = 60 * anomaly_scaled + 25 * informal_scaled + 15 * proximity_scaled
```

Fits only on the detected candidate set (not the full graph population), so
the model discriminates funnels among themselves rather than against normal
depositors. Overwrites `risk_score = 100` for any `is_sanctioned=True`.

- APIs: `sklearn.ensemble.IsolationForest`,
  `sklearn.preprocessing.StandardScaler`, `MinMaxScaler`.
- Produces: `scored_eth`, `scored_tron`, `scored_btc`, `scored_zec`, then a
  unified `scored` frame.

## Section 9 - Enrichment (labels)

### Cell 31 - markdown, "Enrichment (Layer 1+) - human-readable labels"

### Cell 32 - code, load Etherscan labels + Chainalysis oracle
Downloads the community Etherscan-labels JSON
(`brianleect/etherscan-labels/main/data/etherscan/combined/combinedAllLabels.json`)
and merges `label_name` / `label_tags` onto `scored`. Pushes any
`phish-hack` / `fake_phishing` tags into `RISK_TAGS` so the attribution layer
picks them up. Then calls `query_chainalysis_oracle` on the ETH candidate
subset - up to 2,000 sequential `isSanctioned(address)` view calls against
a public Ethereum RPC (`ethereum-rpc.publicnode.com` first, then
`cloudflare-eth.com` / `rpc.ankr.com` / `eth.merkle.io` as fallbacks).

- APIs: HTTP (Etherscan-labels JSON), on-chain view call
  (`Chainalysis SanctionsList.isSanctioned`).
- Produces: enriched `scored` frame with label columns and any Chainalysis
  hits merged into `RISK_TAGS`.

## Section 10 - CTF diagnostic

### Cell 33 - markdown, "CTF diagnostic - are OFAC-listed addresses even active?"

### Cell 34 - code, OFAC-touching activity queries (ETH + Tron)
Counts USDT transfers where a sanctioned address is either sender or receiver
in the last 90 days (ETH) / 30 days (Tron). Confirms the well-known result
that Tether freezes sanctioned USDT addresses, so OFAC proximity is close to
useless as a live signal - the notebook shifts CTF signal to
behavioural/campaign shape (section 13.6).

- SQL targets: `crypto_ethereum.token_transfers`, `decoded_events`.
- Produces: `ofac_eth`, `ofac_tron` diagnostic frames.

## Section 11 - Master frame and cross-chain visualisation

### Cell 35 - markdown, "Visualise + export"

### Cell 36 - code, master merge + plots + first CSV write
- `merge_master(scored, all_candidates)` left-joins the scored graph features
  onto the unified candidate frame, materialising the `MASTER_COLS` schema.
- Sorts by a `priority = 0.7 * risk_score + 0.3 * attributability`.
- Writes `suspicious_wallets_master.csv` and per-chain slices
  (`suspicious_usdt_eth.csv`, `suspicious_usdt_trc20.csv`,
  `suspicious_btc.csv`, `suspicious_zec.csv`) to the repo root, as an early
  checkpoint before the more expensive downstream cells.
- Renders four visualisations:
  1. Cross-chain fan-in vs fan-out scatter (`plot_cross_chain_scatter`) -
     `distinct_senders` on log-x vs `out_cnt` on y, faceted by chain,
     coloured by `risk_score`, red rings on `is_known_risk`.
  2. Per-chain hub-and-spoke network (`plot_funnel_network`) - top-40 in-
     degree hubs plus a bounded neighbourhood, rendered with a spring layout.
  3. Risk-tag provenance bar (`plot_risk_provenance`).
  4. Top-50 attributable-lead table.

- Produces: `master` DataFrame; four PNG-quality inline plots; the master CSV.

## Section 13 - Identity deepening

### Cell 37 - markdown, "Identity deepening / attribution"
### Cell 38 - markdown, "13.1 Co-funding clustering - group funnels run by the same operator"

### Cell 39 - code, `cofunding_clusters`
For each depositor that funded more than one candidate wallet, records the
pair; groups candidates by shared-depositor edges; drops "CEX hot wallet"
depositors that fund more than `depositor_out_cap=100` candidates
(otherwise every candidate ends up in one giant Binance cluster). Reports
connected components of size >= 2 as candidate operator groups.

- APIs: pure pandas / networkx.
- Produces: `clusters` DataFrame (`cluster_id`, `cluster_size`, `wallet`),
  `cofund_g` (networkx.Graph of candidate-candidate co-funding).

### Cell 40 - markdown, "13.1b Bitcoin co-spend clustering"

### Cell 41 - code, Bitcoin multi-input co-spend clustering SQL
Applies the standard Bitcoin ownership heuristic: addresses that ever
co-appear as inputs to the same transaction likely share ownership. Excludes
transactions that look like CoinJoin (>=10 inputs and >=90% distinct
addresses). Maps every clustered BTC address to a canonical `entity`
(the sorted-min address in its component), then flags any BTC candidate that
sits in the same entity as a sanctioned BTC address as
`btc_cospend:sanctioned_cluster` in `RISK_TAGS`.

- SQL target: `crypto_bitcoin.transactions`.
- Produces: `btc_entities` DataFrame (`entity`, `n_addresses`,
  `sample_addresses`); new risk tags merged into `scored` via
  `annotate_risk(scored)`.

### Cell 42 - markdown, "13.2 Nearest-exchange anchor (ETH)"

### Cell 43 - code, exchange-anchor query
`exchange_addresses()` reads the loaded Etherscan-labels dict and filters
addresses whose `labels` intersect a curated set of exchange tags
(`binance`, `coinbase`, `kraken`, `okx`, ...). `nearest_exchange` then runs
one BigQuery pass over `crypto_ethereum.token_transfers` that keeps only
direct transfers where one endpoint is a candidate and the other is a
labelled exchange. This is the KYC/subpoena anchor - the highest-value
downstream lead.

- SQL target: `crypto_ethereum.token_transfers`.
- Produces: `anchors` DataFrame (`candidate`, `direction`, `exchange_addr`,
  `n_tx`, `usdt`) plus the module-global `ex` set of exchange addresses.

### Cell 44 - markdown, "13.3 Behavioural fingerprint - active hours + round amounts"

### Cell 45 - code, activity-by-UTC-hour fingerprint
One SQL that groups each candidate's incoming transfers by
`EXTRACT(HOUR FROM block_timestamp)` and counts round-100 USDT amounts. The
Python side picks the busiest hour per wallet, guesses a UTC offset as
`(12 - peak_hour_utc) % 24` (crude midday anchor), and computes
`round_pct = round_100s / n_tx * 100`.

- SQL target: `crypto_ethereum.token_transfers`.
- Produces: `fp` (raw hourly counts), `peak` (per-wallet fingerprint).

### Cell 46 - markdown, "13.4 ENS resolution"

### Cell 47 - code, ENS reverse resolution
Tries a handful of public Ethereum RPCs, uses `ens.ENS.from_web3` to reverse-
resolve each of the top-100 ETH candidates to a primary `.eth` name. Silent
skip when `web3` is not installed or no RPC is reachable.

- APIs: `web3`, `ens`, public Ethereum RPC.
- Produces: `ens` dict (`{address: ens_name}`).

### Cell 48 - markdown, "13.5 Attributability score"

### Cell 49 - code, `attributability` score
Blends four attribution signals to a 0-100 `attributability` score:
`+40` if the candidate has a direct exchange anchor, `+30` for an Etherscan
label, `+20` for an ENS name, `+10` if within 2 hops of a sanctioned address,
plus half of `risk_attribution_score`. Sorted result becomes the "most
attributable leads" preview.

- Produces: `scored` frame gets an `attributability` column and is re-sorted.

### Cell 50 - markdown, "13.6 Behavioural CTF signal + modular enrichment (free)"

### Cell 51 - code, campaign score + anchors + bridges + geo + enrichment + report + CSVs
This is the assembly cell for the final master frame and outputs. In order:

1. `terror_signals.campaign_score(all_candidates)` computes
   `campaign_terror_score in [0, 100]` per candidate as a weighted blend of
   fan-in percentile, smallness of the median transfer, single-recipient
   concentration, and (if available) a temporal burst share. Then
   `select_leads(..., per_chain_k=25)` flags the top-25 per chain as
   `is_top_campaign_lead`.
2. Materialises `anchor_exchange`, `anchor_exchange_addr`, `anchor_usdt`,
   `anchor_links`, `has_exchange_anchor`. Defines
   `actionability = risk_score + 25 * has_exchange_anchor`.
3. `bridge_wallets.bridges_across_graphs({chain: G})` runs articulation-point
   detection on each per-chain graph and stacks the results; annotates each
   master row with `is_bridge_wallet` and `components_bridged`.
4. `geo_tagging.annotate_geo(...)` maps exchange names to country lists and
   NBCTF `affiliation` free-text to country codes; adds `country_codes`,
   `country_source`, `hits_interesting_country`.
5. `enrichment.Enricher(sources=[PublicLabelsSource(labels=labels_dict)])`
   labels the top-100 by `actionability`. NB: `ChainalysisOracleSource` and
   `ENSSource` are intentionally omitted here because they already ran in
   cells 32 and 47 over the same wallets - re-issuing ~100 sequential
   public-RPC calls per source would make the cell take minutes.
6. Computes `funnel_ratio_recipients = distinct_senders / max(1, distinct_recipients)`
   and `funnel_ratio_out_cnt = distinct_senders / max(1, out_cnt)`, and a
   boolean `is_single_recipient = distinct_recipients <= 1`. These make the
   "many senders -> one wallet -> few/one recipient" concentration explicit
   as first-class columns downstream analysts can sort or threshold on.
7. Writes CSVs to `data/`:
   - `data/suspicious_wallets.csv` - full ranked master
   - `data/ctf_leads.csv` - CTF cut (per-chain top-K OR any `terror` risk category)
   - `data/suspicious_usdt_eth.csv`, `..._usdt_trc20.csv`, `..._btc.csv`, `..._zec.csv`
   - `suspicious_wallets_master.csv` at repo root (legacy path, same content
     as `data/suspicious_wallets.csv`)
8. `report_html.build_report(...)` writes a self-contained `report.html` with
   KPI cards, top-50 actionable leads, bridge wallets, country breakdown,
   exchange anchors, co-funding clusters, and NBCTF hits with provenance.

- APIs: local modules (`terror_signals`, `bridge_wallets`, `geo_tagging`,
  `enrichment`, `report_html`).
- Produces: final `master` DataFrame, `report.html`, and all CSVs.

## Section 12 - Limitations

### Cell 52 - markdown, "Limitations and responsible use"
Notes: research leads only, not proof; XMR out of scope (no public ledger);
Zcash shielded pool out of scope (encrypted); OFAC USDT addresses dormant
because Tether freezes them.

## Section 14 - Final summary

### Cell 53 - markdown, "Final summary - suspicious wallets across 4 chains"

### Cell 54 - code, `print_summary`
Prints per-chain wallet counts, how many carry a public risk tag, how many
sit in a co-funding cluster, and the aggregate confirmed count (>=2
independent sources OR sanctions/terror). Prints the visibility boundaries as
a final reminder.

### Cell 55 - code, blank trailing cell
Empty by convention.

---

## Follow-up: how the "funnel" cross-section is enforced

The pattern the analyst cares about - **many unique senders -> one wallet ->
one (or few) unique recipients** - is implemented across three layers:

1. **SQL fan-in gate** (cell 12, cell 17, cell 20, cell 22): every candidate
   passes `distinct_senders BETWEEN 50 AND 5,000`.
2. **SQL fan-out gate** (same cells): `out_cnt <= 25`. This is transaction
   count, not distinct-recipient count - a wallet that sends 25 transfers to
   the same single address passes both filters, which is the intended
   informal-exchanger shape.
3. **Scoring bonuses**:
   - Bitcoin composite `funnel_signal` (cell 20) rewards low `out_cnt` with a
     `2.0 * (1 / (1 + out_cnt))` term.
   - `informal_exchanger_score` (cell 18) gives a bonus when
     `distinct_recipients` is in the "small handful" range of 2-6.
   - `campaign_terror_score` (`terror_signals.py`) has a
     `concentration = (distinct_recipients <= 1)` sub-signal weighted 20% of
     the total, so single-recipient funnels get the maximum concentration
     boost.

Cell 51 additionally materialises the explicit ratios
`funnel_ratio_recipients = distinct_senders / max(1, distinct_recipients)`
and `funnel_ratio_out_cnt = distinct_senders / max(1, out_cnt)` and the
boolean `is_single_recipient` on the master frame, so a downstream analyst
can sort by them directly.
