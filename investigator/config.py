"""
Investigator-layer configuration.

All knobs are environment-driven (read from the same .env the base project
uses, via the base `config._load_dotenv`). Nothing here is required to import;
missing values fall back to safe defaults so the package always imports.

Cost-relevant knobs (budgets, model, kill-switch) live here so an operator can
tune them without touching code.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import PACKAGE_ROOT, PROJECT_ROOT

# Reuse the base project's .env loader so INVESTIGATOR_* and ANTHROPIC_API_KEY
# are picked up from the same .env as BQ_PROJECT etc. Import is best-effort.
try:  # pragma: no cover - trivial
    import config as _base_config  # noqa: F401  (side effect: loads .env)
except Exception:  # base config not importable -> read os.environ directly
    _base_config = None


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


# ---------------------------------------------------------------------------
# Paths (everything the layer writes stays under investigator/)
# ---------------------------------------------------------------------------
DATA_DIR = PACKAGE_ROOT / "data"
OUTPUTS_DIR = PACKAGE_ROOT / "outputs"
DOSSIERS_DIR = OUTPUTS_DIR / "dossiers"
VERDICTS_PATH = DATA_DIR / "verdicts.jsonl"
LINUCB_STATE_PATH = DATA_DIR / "linucb_state.pkl"
LTR_MODEL_PATH = OUTPUTS_DIR / "ltr_model.json"
DOSSIER_INDEX_PATH = DOSSIERS_DIR / "index.json"

# The base project's master table is the single source of truth for triage.
MASTER_CSV = PROJECT_ROOT / "suspicious_wallets_master.csv"
NBCTF_JSON = PROJECT_ROOT / "data" / "nbctf_addresses.json"

for _d in (DATA_DIR, OUTPUTS_DIR, DOSSIERS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
LLM_PROVIDER = _get("INVESTIGATOR_LLM_PROVIDER", "anthropic")   # anthropic|openai|mock
LLM_MODEL = _get("INVESTIGATOR_LLM_MODEL", "claude-haiku-4-5")
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = _get("OPENAI_API_KEY", "")

# ---------------------------------------------------------------------------
# Per-dossier budgets (spec section 3.1)
# ---------------------------------------------------------------------------
HOPS_BUDGET = int(_get("INVESTIGATOR_HOPS", "2"))
BQ_USD_BUDGET_PER_DOSSIER = float(_get("INVESTIGATOR_BQ_USD", "0.10"))
LLM_TOKEN_BUDGET_PER_DOSSIER = int(_get("INVESTIGATOR_TOKENS", "30000"))
MAX_TOOL_CALLS_PER_DOSSIER = int(_get("INVESTIGATOR_MAX_CALLS", "25"))

# Daily kill-switch across a whole batch (spec section 12, risk 4).
BQ_KILL_USD_DAILY = float(_get("INVESTIGATOR_BQ_KILL_USD_DAILY", "50"))

# BigQuery on-demand price per TB scanned (USD). Used to convert dry-run bytes
# into a dollar figure for Budget.charge_bq. Matches base config's estimate.
BQ_USD_PER_TB = float(_get("BQ_USD_PER_TB", "6.25"))

# ---------------------------------------------------------------------------
# Component B - adaptive triage
# ---------------------------------------------------------------------------
ADAPTIVE_TRIAGE_ENABLED = _get("ADAPTIVE_TRIAGE_ENABLED", "false").lower() in ("1", "true", "yes", "on")
MIN_VERDICTS_TO_ACTIVATE = int(_get("INVESTIGATOR_MIN_VERDICTS", "30"))
LINUCB_ALPHA = float(_get("INVESTIGATOR_LINUCB_ALPHA", "1.0"))
ANALYST_ID = _get("INVESTIGATOR_ANALYST_ID", "local")

# Reward schedule (spec section 3.2). Tunable.
REWARD_SCHEDULE = {
    "real_informal_exchanger": 1.0,
    "OTC": 0.3,
    "CEX_deposit": 0.0,
    "legitimate_service": -0.2,
    "unclear": None,   # None => skip, do not train on it
}

# Batch mode
TOP_K_PER_CHAIN = int(_get("INVESTIGATOR_TOP_K", "25"))
CHAINS = [c.strip() for c in _get("INVESTIGATOR_CHAINS", "ethereum,tron,bitcoin").split(",") if c.strip()]

RESEARCH_LEAD_NOTICE = "Research lead - not a finding of guilt."


def summary() -> dict:
    """Human-readable snapshot of the active configuration (no secrets)."""
    return {
        "llm_provider": LLM_PROVIDER,
        "llm_model": LLM_MODEL,
        "anthropic_key_set": bool(ANTHROPIC_API_KEY),
        "hops_budget": HOPS_BUDGET,
        "bq_usd_per_dossier": BQ_USD_BUDGET_PER_DOSSIER,
        "token_budget": LLM_TOKEN_BUDGET_PER_DOSSIER,
        "max_tool_calls": MAX_TOOL_CALLS_PER_DOSSIER,
        "adaptive_triage_enabled": ADAPTIVE_TRIAGE_ENABLED,
        "min_verdicts_to_activate": MIN_VERDICTS_TO_ACTIVATE,
        "chains": CHAINS,
        "top_k_per_chain": TOP_K_PER_CHAIN,
    }
