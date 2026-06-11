# Detailed Setup

## Prerequisites
- Python 3.10+ recommended (3.9 works but emits deprecation warnings from the Google libraries)
- A Google Cloud account with a billable project
- BigQuery API enabled on that project
- The `gcloud` CLI installed (https://cloud.google.com/sdk/docs/install)

## Step 1 — Create / pick a GCP project

```bash
gcloud auth login
gcloud projects list
# pick one and set as default
gcloud config set project YOUR_PROJECT_ID
# enable BigQuery
gcloud services enable bigquery.googleapis.com
```

The `crypto_ethereum`, `goog_blockchain_tron_mainnet_us`, and `crypto_bitcoin`
datasets are public — you don't need to copy them, just pay for the bytes you scan.

## Step 2 — Pick an authentication path

### Option A: short-lived access token (simplest)
```bash
gcloud auth print-access-token
```
Paste the result into `.env` as `BQ_ACCESS_TOKEN`. **Token expires after ~1 hour.**
Re-run when it expires.

### Option B: Application Default Credentials (best for repeated runs)
```bash
gcloud auth application-default login
```
A browser opens. Sign in once. Credentials are stored locally and refreshed
automatically. Leave `BQ_ACCESS_TOKEN` blank in `.env`.

### Option C: service-account JSON (CI / production)
```bash
gcloud iam service-accounts create crypto-aml-runner
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:crypto-aml-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
gcloud iam service-accounts keys create key.json \
  --iam-account=crypto-aml-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com
export GOOGLE_APPLICATION_CREDENTIALS=$PWD/key.json
```
Leave `BQ_ACCESS_TOKEN` blank in `.env`. **Never commit `key.json`** — it is in
`.gitignore`.

## Step 3 — Optional: Etherscan API key

For block-explorer name-tag enrichment in `pipeline_extended.py`:

1. Get a free API key at https://etherscan.io/myapikey
2. Add to `.env`:  `ETHERSCAN_API_KEY=YourKeyHere`

Without a key the pipeline still emits a lookup URL template you can use manually.

## Step 4 — Smoke test

```bash
python -c "import config; print('Project:', config.require_bq_project())"
```
Should print your project ID. If it fails, check `.env`.

```bash
python -c "import config; c = config.make_bq_client(); print(list(c.list_datasets(max_results=1)))"
```
If this succeeds you can hit BigQuery — you're ready.

## Step 5 — First run

```bash
jupyter lab Crypto-AML-Analysis.ipynb
```
Run cells top to bottom. Expect ~3 minutes wall-clock and ~$1 in BigQuery cost
for the default 90-day Ethereum + 7-day Tron windows.

## Cost guardrails

| Knob | Default | Effect |
|---|---|---|
| `BQ_MAX_GB` | 150 | Per-query scan ceiling — aborts before billing |
| `LOOKBACK_DAYS` | 90 | Ethereum scan window — main cost lever |
| `TRON_LOOKBACK_DAYS` | 7 | Tron scan window — Tron USDT volume is huge |

To experiment cheaply, set `LOOKBACK_DAYS=14` and `TRON_LOOKBACK_DAYS=3`.

## Cleanup

When you're done for the day, revoke any tokens you've issued:
```bash
gcloud auth revoke
```
ADC creds persist until explicitly revoked with:
```bash
gcloud auth application-default revoke
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `RefreshError: credentials do not contain the necessary fields` | Access token expired — `gcloud auth print-access-token` again |
| `403: ... does not have ... bigquery.jobs.create` | Account missing BigQuery permissions — add `roles/bigquery.jobUser` |
| `404: Not found: Project ...` | `BQ_PROJECT` is wrong; use `gcloud projects list` to confirm |
| Notebook fails on `float \| None` syntax | Python 3.9 — upgrade to 3.10+ or run `pipeline_local.py` instead of the notebook |
| `Could not find a version that satisfies networkx>=3.3` | Python 3.9 — `pip install "networkx<3.3"` |
| `LlamaRPC` / public RPC connection refused | Try other endpoints in `ETH_RPC_URLS`; some are geo-restricted |
