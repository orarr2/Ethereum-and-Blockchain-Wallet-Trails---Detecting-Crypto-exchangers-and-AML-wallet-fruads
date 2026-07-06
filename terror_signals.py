"""
Behavioural counter-terror-financing (CTF) signal - free, on-chain-only.

Rationale
---------
Direct graph proximity to OFAC-listed addresses is a dead end: Tether freezes
sanctioned USDT addresses, so they are dormant (see the notebook's OFAC
diagnostic - 1 touching transfer out of ~72M). Instead of chasing frozen
seeds, this module scores the *behavioural signature of a donation campaign*,
which is what terror-financing collection wallets actually look like on-chain:

  - MANY small senders  -> a crowd of donors, not a few large counterparties
  - SMALL median transfer -> retail-sized "donations", not settlement flows
  - CONCENTRATED outflow  -> funds funnelled onward to one/few controllers
  - TEMPORAL BURST (optional) -> a campaign has a start and an end, unlike a
    steady-state processor

None of these prove terror financing - they are *investigative lead* signals,
identical in spirit to the funnel detector but tuned for the campaign shape.
Everything here is computed from columns the notebook already produces, uses
no paid API, and is fully unit-testable offline.
"""
from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd

# Columns this module reads if present (all optional except distinct_senders).
_SENDERS = "distinct_senders"
# median transfer size may arrive under several names depending on the caller
# (per-chain notebook frames vs. the unified master); try them in order.
_MEDIAN_CANDIDATES = ("in_median_usdt", "raw_value_median", "in_median")
_RECIPIENTS = "distinct_recipients"
_BURST = "max_day_share"   # share of inflow arriving on the single busiest day


def _median_col(df: pd.DataFrame) -> Optional[str]:
    for c in _MEDIAN_CANDIDATES:
        if c in df.columns:
            return c
    return None


def _pct_rank(s: pd.Series) -> pd.Series:
    """Percentile rank in [0,1]; flat 0.0 if the column is constant/empty."""
    if s is None or not len(s):
        return pd.Series(dtype=float)
    r = s.rank(pct=True, method="average")
    return r.fillna(0.0)


def campaign_score(
    df: pd.DataFrame,
    small_donation_usd: float = 200.0,
    per_chain: bool = True,
    weights: Optional[dict] = None,
) -> pd.DataFrame:
    """Return `df` with a `campaign_terror_score` column in [0,100].

    The score is a weighted blend of four sub-signals, each mapped to [0,1]:
      fanin        - percentile of distinct_senders (many donors)
      small        - continuous smallness of the median transfer
      concentration- single-recipient outflow (funnelled onward)
      burst        - share of inflow on the busiest day (campaign timing)

    Weights default to fanin .35 / small .30 / concentration .20 / burst .15.
    If `max_day_share` is absent, the burst weight is redistributed pro-rata
    so the score still spans [0,100]. Ranking is done per chain when
    `per_chain=True` so ETH and Tron are compared within their own population.
    """
    if not len(df):
        out = df.copy()
        out["campaign_terror_score"] = pd.Series(dtype=float)
        return out

    w = {"fanin": 0.35, "small": 0.30, "concentration": 0.20, "burst": 0.15}
    if weights:
        w.update(weights)

    out = df.copy()
    median_col = _median_col(out)
    has_burst = _BURST in out.columns
    if not has_burst:
        # redistribute the burst weight across the remaining three signals
        spare = w.pop("burst")
        total = sum(w.values())
        for k in w:
            w[k] += spare * (w[k] / total)

    def _score_block(block: pd.DataFrame) -> pd.Series:
        senders = block[_SENDERS].astype(float) if _SENDERS in block else pd.Series(0.0, index=block.index)
        median = block[median_col].astype(float) if median_col else pd.Series(np.nan, index=block.index)
        recips = block[_RECIPIENTS].astype(float) if _RECIPIENTS in block else pd.Series(np.nan, index=block.index)

        fanin = _pct_rank(senders)
        # smallness: 1.0 at $0, 0.0 at >= 2*small_donation_usd, linear between
        cap = 2.0 * small_donation_usd
        small = (1.0 - (median.clip(lower=0, upper=cap) / cap)).fillna(0.0)
        concentration = (recips <= 1).astype(float).fillna(0.0)

        s = w["fanin"] * fanin + w["small"] * small + w["concentration"] * concentration
        if has_burst:
            burst = block[_BURST].astype(float).clip(0, 1).fillna(0.0)
            s = s + w["burst"] * burst
        return (100.0 * s).round(1)

    if per_chain and "chain" in out.columns:
        parts = [_score_block(block) for _, block in out.groupby("chain")]
        res = pd.concat(parts) if parts else pd.Series(dtype=float)
        out["campaign_terror_score"] = res.reindex(out.index)
    else:
        out["campaign_terror_score"] = _score_block(out)
    return out


def flag_campaigns(df: pd.DataFrame, threshold: float = 70.0, **kwargs) -> pd.DataFrame:
    """Convenience: add `campaign_terror_score` + boolean `is_campaign_lead`.

    NB: a fixed score threshold floods on chains whose funnels *all* have many
    small senders (Bitcoin), so treat `is_campaign_lead` as a coarse cut and
    prefer `top_leads()` / `select_leads()` for a triage-sized list.
    """
    scored = campaign_score(df, **kwargs)
    scored["is_campaign_lead"] = scored["campaign_terror_score"] >= threshold
    return scored


def top_leads(df: pd.DataFrame, per_chain_k: int = 25,
              score_col: str = "campaign_terror_score") -> pd.DataFrame:
    """Return the highest-scoring `per_chain_k` rows PER CHAIN.

    A fixed threshold produced ~1,965 "leads" (40% of the pool) because
    Bitcoin funnels inherently have thousands of small senders. Ranking within
    each chain and keeping the top K yields a short, comparable, triage-sized
    list instead of a flood. The returned frame is sorted by score descending.
    """
    if not len(df) or score_col not in df.columns:
        return df.head(0) if len(df) else df
    if "chain" in df.columns:
        picked = (df.sort_values(score_col, ascending=False)
                    .groupby("chain", group_keys=False).head(per_chain_k))
    else:
        picked = df.sort_values(score_col, ascending=False).head(per_chain_k)
    return picked.sort_values(score_col, ascending=False)


def select_leads(df: pd.DataFrame, per_chain_k: int = 25,
                 score_col: str = "campaign_terror_score") -> pd.DataFrame:
    """Add a boolean `is_top_campaign_lead` marking the per-chain top-K rows,
    without dropping any rows (keeps the full frame for export)."""
    out = df.copy()
    if not len(out) or score_col not in out.columns:
        out["is_top_campaign_lead"] = False
        return out
    keep = set(top_leads(out, per_chain_k=per_chain_k, score_col=score_col).index)
    out["is_top_campaign_lead"] = out.index.isin(keep)
    return out
