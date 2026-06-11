# Crypto-AML-Analysis

USDT informal-exchanger ("funnel-account") detection and counter-terror-financing
(CTF) proximity testing on **Ethereum (ERC-20)** and **Tron (TRC-20)**, using
Google BigQuery public datasets.

> ⚖️ **Research leads, not findings of guilt.** High fan-in / low fan-out also
> fits payment processors, OTC desks, and exchange deposit wallets. Always
> corroborate independently. Public on-chain data only. Use authoritative OFAC
> seeds only.

---

## What it does

| Layer | Content |
|---|---|
| **Data** | BigQuery public datasets (`crypto_ethereum`, `goog_blockchain_tron_mainnet_us`, `crypto_bitcoin`) + OFAC sanctioned address list + community Etherscan labels |
| **Detection** | High fan-in / low fan-out "funnel" candidates with a dust floor (median per-transfer ≥ $50) and smart-contract filtering (EOAs only) |
| **Graph + ML** | NetworkX graph (PageRank / degree / clustering), mixer & funnel heuristics, multi-hop OFAC proximity, Isolation-Forest anomaly scoring |
| **CTF** | Diagnostic that confirms OFAC-listed addresses are frozen / dormant in USDT on both chains |
| **Identity deepening** | Bitcoin co-spend clustering, nearest-exchange anchor, behavioural fingerprint, ENS resolution, attributability score |
| **Dashboard** | Streamlit triage UI with filters, drill-down, and per-wallet block-explorer links |

---

## Quickstart

### 1. Install
```bash
git clone https://github.com/orarr2/Ethereum-and-Blockchain-Wallet-Trails---Detecting-Crypto-exchangers-and-AML-wallet-fruads.git
cd Ethereum-and-Blockchain-Wallet-Trails---Detecting-Crypto-exchangers-and-AML-wallet-fruads
pip install -r requirements.txt
```

### 2. Configure credentials

Copy the example and fill in your own values:
```bash
cp .env.example .env
```

Edit `.env`:
```
BQ_PROJECT=your-gcp-project-id            # required
BQ_ACCESS_TOKEN=ya29...                   # optional (1 h token from gcloud)
ETHERSCAN_API_KEY=your-key                # optional, for name-tag enrichment
```

You can get the token with:
```bash
gcloud auth login                         # one time
gcloud auth print-access-token            # repeat each session (~1 h validity)
```

Or, for persistent ADC auth (no token needed), run once:
```bash
gcloud auth application-default login
```
and leave `BQ_ACCESS_TOKEN` blank.

> 🔒 `.env` is in `.gitignore` — never commit credentials.

### 3. Run the core notebook
```bash
jupyter lab Crypto-AML-Analysis.ipynb
```
Run every cell top to bottom. Each query is dry-run cost-estimated and capped
with `maximum_bytes_billed`. A full run typically scans ~150 GB → **≈ $1**.

Output: `usdt_funnel_candidates.csv` (all candidates across both chains).

### 4. Run the extended pipeline (P0-P3)
```bash
python pipeline_extended.py     # BigQuery + local analysis (~$3-5 more)
python ens_lookup.py            # ENS resolution via public RPC (no token)
python pipeline_local.py        # alternative: local-only quick variant
```
All artifacts land under `outputs/`.

### 5. Launch the triage dashboard
```bash
streamlit run streamlit_app.py
```

---

## File layout

```
├── Crypto-AML-Analysis.ipynb     # Main notebook (data → ML → CTF → viz)
├── config.py                     # Central credential & dataset config
├── pipeline_extended.py          # BigQuery-driven P0-P3 pipeline
├── pipeline_local.py             # Local-only quick analyses
├── ens_lookup.py                 # ENS reverse-resolve via public RPC
├── streamlit_app.py              # Triage dashboard
├── extract_outputs.py            # Utility: dump executed notebook outputs
├── requirements.txt
├── .env.example                  # Credential template
├── .gitignore
├── docs/
│   ├── SETUP.md                  # Detailed auth + GCP setup
│   └── RESULTS.md                # Findings from the executed run
└── outputs/                      # All generated CSVs (git-ignored)
```

---

## Cost discipline

Every BigQuery query is:
1. **Dry-run estimated** — prints scan size and approx cost before running.
2. **Capped** with `maximum_bytes_billed` (default 150 GB per query).
3. **Partition-pruned** — `block_timestamp >= since` on every transfer query.

A typical end-to-end run (notebook + extended pipeline):

| Component | Bytes scanned | Cost @ $6.25/TB |
|---|---|---|
| Notebook | ~155 GB | ~$0.95 |
| Extended pipeline | ~250 GB | ~$1.50 |
| **Total** | **~400 GB** | **~$2.50** |

Tune `BQ_MAX_GB`, `LOOKBACK_DAYS`, and `TRON_LOOKBACK_DAYS` in `.env` to scale.

---

## Responsible use

- A pattern is not a crime. Match candidates with independent evidence before any
  action.
- This produces **entities and anchors**, never a real-world name. Attribution
  to a person is for authorised enforcement (subpoena to the exchange holding the KYC).
- Use authoritative OFAC designations for the sanctioned-address seeds.
- Do not deanonymise users. Operate within a lawful AML programme.

See [docs/RESULTS.md](docs/RESULTS.md) for findings from the reference run and
[docs/SETUP.md](docs/SETUP.md) for the detailed Google Cloud setup.
