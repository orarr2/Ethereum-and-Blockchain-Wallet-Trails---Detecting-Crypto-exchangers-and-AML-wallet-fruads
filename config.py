"""
Central configuration for the Crypto-AML pipeline.

All credentials are read from environment variables (or a local .env file).
NEVER hardcode tokens, project IDs, or API keys here. NEVER commit .env to git.

Quick setup
-----------
    1. cp .env.example .env
    2. Edit .env with your own values
    3. Run any pipeline script

Required environment variables
------------------------------
    BQ_PROJECT       Your billable GCP project ID (e.g. my-project-123456)

Optional environment variables
------------------------------
    BQ_ACCESS_TOKEN  Short-lived OAuth2 token from `gcloud auth print-access-token`.
                     If empty, Application Default Credentials (ADC) are used instead;
                     run `gcloud auth application-default login` first.
    BQ_MAX_GB        Per-query byte ceiling (default: 150).
    LOOKBACK_DAYS    Ethereum scan window (default: 90).
    TRON_LOOKBACK_DAYS  Tron scan window (default: 7).
    ETHERSCAN_API_KEY   Optional. Enables block-explorer name-tag enrichment.
    ETH_RPC_URLS     Comma-separated public Ethereum RPC fallbacks for ENS lookup.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Optional


def _load_dotenv() -> None:
    """Minimal .env loader (so we don't pull in python-dotenv as a dependency)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


# ---------------------------------------------------------------------------
# Required / required-ish
# ---------------------------------------------------------------------------
BQ_PROJECT: str = os.environ.get("BQ_PROJECT", "").strip()
BQ_ACCESS_TOKEN: str = os.environ.get("BQ_ACCESS_TOKEN", "").strip()

# ---------------------------------------------------------------------------
# Optional with sensible defaults
# ---------------------------------------------------------------------------
BQ_MAX_GB: float = float(os.environ.get("BQ_MAX_GB", "150"))
LOOKBACK_DAYS: int = int(os.environ.get("LOOKBACK_DAYS", "90"))
TRON_LOOKBACK_DAYS: int = int(os.environ.get("TRON_LOOKBACK_DAYS", "7"))
ETHERSCAN_API_KEY: str = os.environ.get("ETHERSCAN_API_KEY", "").strip()
ETH_RPC_URLS = [u.strip() for u in os.environ.get(
    "ETH_RPC_URLS",
    "https://ethereum-rpc.publicnode.com,https://cloudflare-eth.com,"
    "https://rpc.ankr.com/eth,https://eth.merkle.io,https://1rpc.io/eth"
).split(",") if u.strip()]

# ---------------------------------------------------------------------------
# Token contracts (constants, safe to share)
# ---------------------------------------------------------------------------
USDT_ERC20: str = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USDC_ERC20: str = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT_TRC20_HEX: str = "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"
USDT_DECIMALS: int = 6

# BigQuery dataset paths
ETH_DATASET: str = "bigquery-public-data.crypto_ethereum"
ETH_TOKEN_TRANSFERS: str = f"{ETH_DATASET}.token_transfers"
ETH_CONTRACTS: str = f"{ETH_DATASET}.contracts"
BTC_TRANSACTIONS: str = "bigquery-public-data.crypto_bitcoin.transactions"
TRON_DATASET: str = "bigquery-public-data.goog_blockchain_tron_mainnet_us"
TRON_EVENTS: str = f"{TRON_DATASET}.decoded_events"


def require_bq_project() -> str:
    """Fail fast with a clear message if BQ_PROJECT is missing."""
    if not BQ_PROJECT:
        print(
            "ERROR: BQ_PROJECT is not set.\n"
            "       Copy .env.example to .env and set BQ_PROJECT to your GCP project ID,\n"
            "       or export BQ_PROJECT=your-project-id before running.",
            file=sys.stderr,
        )
        sys.exit(1)
    return BQ_PROJECT


def make_bq_client():
    """Build a BigQuery client using BQ_ACCESS_TOKEN if set, else ADC."""
    require_bq_project()
    from google.cloud import bigquery
    if BQ_ACCESS_TOKEN:
        from google.oauth2.credentials import Credentials
        return bigquery.Client(project=BQ_PROJECT, credentials=Credentials(token=BQ_ACCESS_TOKEN))
    return bigquery.Client(project=BQ_PROJECT)


# Per-Python-session cumulative BigQuery spend, in USD. When
# INVESTIGATOR_BQ_KILL_USD_DAILY is set to > 0, `run_query` refuses to submit a
# query if the projected cumulative spend would cross the cap. Backwards
# compatible: unset (default) -> no cap enforcement, same behavior as before.
_BQ_SESSION_SPENT_USD = {"total": 0.0}
BQ_USD_PER_TB: float = float(os.environ.get("BQ_USD_PER_TB", "6.25"))


def _kill_switch_cap_usd() -> float:
    try:
        return float(os.environ.get("INVESTIGATOR_BQ_KILL_USD_DAILY", "0"))
    except ValueError:
        return 0.0


def _cost_usd_of_gb(gb: float) -> float:
    return float(gb) / 1024.0 * BQ_USD_PER_TB


def _check_kill_switch(projected_usd: float) -> None:
    cap = _kill_switch_cap_usd()
    if cap <= 0:
        return
    if _BQ_SESSION_SPENT_USD["total"] + projected_usd > cap:
        raise RuntimeError(
            "[BQ kill-switch] projected cumulative spend "
            f"${_BQ_SESSION_SPENT_USD['total']:.4f} + ${projected_usd:.4f} "
            f"would exceed INVESTIGATOR_BQ_KILL_USD_DAILY=${cap}. Aborting."
        )


def bq_session_spent_usd() -> float:
    """How much BigQuery has cost THIS Python session so far."""
    return _BQ_SESSION_SPENT_USD["total"]


def estimate_cost(client, sql: str, max_gb: Optional[float] = None) -> float:
    """Dry-run a query and return its scan size in GB. Refuse if > max_gb or if
    the projected cumulative session spend would exceed the kill-switch cap."""
    from google.cloud import bigquery
    cap = BQ_MAX_GB if max_gb is None else max_gb
    job = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
    gb = job.total_bytes_processed / 1024 ** 3
    usd = _cost_usd_of_gb(gb)
    print(f"  dry-run: {gb:,.2f} GB (~${usd:,.4f})")
    if gb > cap:
        raise RuntimeError(f"Would scan {gb:.1f} GB > {cap} GB cap")
    _check_kill_switch(usd)
    return gb


def run_query(client, sql: str, max_gb: Optional[float] = None):
    """Cost-estimate then execute a query, returning a pandas DataFrame."""
    from google.cloud import bigquery
    cap = BQ_MAX_GB if max_gb is None else max_gb
    gb = estimate_cost(client, sql, cap)   # already runs kill-switch pre-check
    usd = _cost_usd_of_gb(gb)
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(cap * 1024 ** 3))
    df = client.query(sql, job_config=cfg).to_dataframe(create_bqstorage_client=False)
    _BQ_SESSION_SPENT_USD["total"] += usd
    ks_cap = _kill_switch_cap_usd()
    if ks_cap > 0:
        print(f"  [BQ session spent: ${_BQ_SESSION_SPENT_USD['total']:.4f} "
              f"/ cap ${ks_cap}]")
    return df
