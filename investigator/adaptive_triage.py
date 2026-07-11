"""
Component B - adaptive triage.

Three pieces (spec section 3.2 + 8.1):
  FeatureBuilder - turns a master-table row into a fixed-length numeric context
                   vector. Defensive about missing columns (the live master has
                   a slightly different column set than the spec's ideal list),
                   so it looks each feature up by a list of candidate names and
                   falls back to 0.
  LinUCB         - a one-arm contextual bandit ("show this candidate to the
                   analyst?"). Updated after every verdict; scores drive the
                   re-rank. Can be warm-started from an offline LTR pass.
  Reranker       - applies the feature flag + cold-start floor. When disabled or
                   under the floor, it preserves today's static `actionability`
                   ordering exactly, so flipping the flag never destabilises the
                   queue or loses verdicts.

Design note: the context vector has a FIXED dimension (42) regardless of which
columns are present, so a saved LinUCB state stays load-compatible across runs.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C

# ---------------------------------------------------------------------------
# Feature schema (fixed order + fixed dimension)
# ---------------------------------------------------------------------------
# each entry: (candidate column names, transform, scale)  -> scalar in ~[0,1]
_L1P = lambda s: np.log1p(np.clip(s, 0, None))

_NUMERIC = [
    (["risk_score"], "linear", 100.0),
    (["actionability"], "linear", 150.0),
    (["informal_score"], "linear", 1.0),
    (["funnel_signal", "funnel_ratio_recipients"], "linear", 1000.0),
    (["distinct_senders"], "log1p", 15.0),
    (["distinct_recipients"], "log1p", 10.0),
    (["in_usdt", "value_in_usd_est", "raw_value_in"], "log1p", 20.0),
    (["in_median_usdt", "raw_value_median"], "log1p", 15.0),
    (["in_out_tx_ratio", "funnel_ratio_out_cnt"], "linear", 1000.0),
    (["campaign_terror_score"], "linear", 100.0),
    (["components_bridged"], "log1p", 5.0),
    (["hop_to_ofac", "hops_to_sanctioned"], "hop", 1.0),
    (["night_share"], "linear", 1.0),
    (["round_amount_ratio", "round100_share"], "linear", 1.0),
]
_NUMERIC_NAMES = [
    "risk_score", "actionability", "informal_score", "funnel_signal",
    "distinct_senders_log", "distinct_recipients_log", "in_usdt_log",
    "in_median_usdt_log", "in_out_tx_ratio", "campaign_terror_score",
    "components_bridged_log", "hop_to_ofac_inv", "night_share", "round_amount_ratio",
]
_BINARY = [
    (["has_exchange_anchor"], None),
    (["is_bridge_wallet"], None),
    (["hits_interesting_country"], None),
    (["chain"], "ethereum"),
    (["chain"], "tron"),
    (["chain"], "bitcoin"),
    (["chain"], "zcash"),
]
_BINARY_NAMES = [
    "has_exchange_anchor", "is_bridge_wallet", "hits_interesting_country",
    "chain_ethereum", "chain_tron", "chain_bitcoin", "chain_zcash",
]
N_CLUSTER_SLOTS = 20   # + 1 "other" = 21 cluster dims
FEATURE_DIM = len(_NUMERIC) + len(_BINARY) + N_CLUSTER_SLOTS + 1   # 14 + 7 + 21 = 42

FEATURE_NAMES = (_NUMERIC_NAMES + _BINARY_NAMES +
                 [f"cluster_{i}" for i in range(N_CLUSTER_SLOTS)] + ["cluster_other"])


def _first_present(row, cols):
    for c in cols:
        if c in row and pd.notna(row[c]):
            return row[c]
    return None


def _to_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, bool):
            return 1.0 if x else 0.0
        return float(x)
    except (TypeError, ValueError):
        return default


class FeatureBuilder:
    """Row/frame -> fixed-length context vector (dimension = FEATURE_DIM)."""

    def __init__(self, cluster_vocab: list | None = None):
        # map cluster_id -> slot index (0..N_CLUSTER_SLOTS-1); overflow -> "other"
        self.cluster_vocab = list(cluster_vocab or [])[:N_CLUSTER_SLOTS]
        self._cluster_index = {c: i for i, c in enumerate(self.cluster_vocab)}

    # -- numeric ----------------------------------------------------------
    def _numeric(self, row) -> list:
        vals = []
        for cols, kind, scale in _NUMERIC:
            raw = _first_present(row, cols)
            v = _to_float(raw, default=0.0)
            if kind == "log1p":
                v = float(_L1P(v)) / scale
            elif kind == "hop":
                # direct hit (0) -> 1.0; far/∞/NaN -> ~0
                if raw is None or (isinstance(raw, float) and np.isnan(raw)):
                    v = 0.0
                else:
                    v = 1.0 / (1.0 + max(0.0, v))
            else:  # linear
                v = v / scale
            vals.append(float(np.clip(v, -5.0, 5.0)))
        return vals

    def _binary(self, row) -> list:
        vals = []
        for cols, want in _BINARY:
            raw = _first_present(row, cols)
            if want is None:
                vals.append(1.0 if _to_float(raw) >= 0.5 or raw is True else 0.0)
            else:
                vals.append(1.0 if str(raw).lower() == want else 0.0)
        return vals

    def _cluster(self, row) -> list:
        slots = [0.0] * (N_CLUSTER_SLOTS + 1)
        cid = _first_present(row, ["cluster_id"])
        if cid is None:
            slots[-1] = 1.0   # "other"
            return slots
        idx = self._cluster_index.get(cid)
        if idx is None:
            slots[-1] = 1.0
        else:
            slots[idx] = 1.0
        return slots

    def build(self, row) -> np.ndarray:
        if isinstance(row, dict):
            row = pd.Series(row)
        vec = self._numeric(row) + self._binary(row) + self._cluster(row)
        arr = np.asarray(vec, dtype=float)
        if arr.shape[0] != FEATURE_DIM:  # defensive; should never fire
            arr = np.resize(arr, FEATURE_DIM)
        return arr

    def build_frame(self, df: pd.DataFrame) -> np.ndarray:
        if df is None or not len(df):
            return np.zeros((0, FEATURE_DIM))
        return np.vstack([self.build(r) for _, r in df.iterrows()])

    @staticmethod
    def cluster_vocab_from_master(df: pd.DataFrame, top: int = N_CLUSTER_SLOTS) -> list:
        if df is None or "cluster_id" not in df.columns:
            return []
        vc = df["cluster_id"].dropna().value_counts()
        return list(vc.head(top).index)


# ---------------------------------------------------------------------------
# LinUCB (Li et al. 2010), single-arm contextual
# ---------------------------------------------------------------------------
class LinUCB:
    def __init__(self, feature_dim: int = FEATURE_DIM, alpha: float = None):
        self.d = int(feature_dim)
        self.alpha = float(alpha if alpha is not None else C.LINUCB_ALPHA)
        self.A = np.identity(self.d)
        self.b = np.zeros(self.d)
        self.n_updates = 0

    def _theta(self) -> np.ndarray:
        return np.linalg.solve(self.A, self.b)

    def score(self, context: np.ndarray) -> float:
        x = np.asarray(context, dtype=float).reshape(-1)
        A_inv_x = np.linalg.solve(self.A, x)
        mean = float(self._theta() @ x)
        ucb = self.alpha * float(np.sqrt(max(0.0, x @ A_inv_x)))
        return mean + ucb

    def score_frame(self, X: np.ndarray) -> np.ndarray:
        if X is None or not len(X):
            return np.zeros(0)
        return np.array([self.score(x) for x in X])

    def update(self, context: np.ndarray, reward: float) -> None:
        x = np.asarray(context, dtype=float).reshape(-1)
        self.A += np.outer(x, x)
        self.b += float(reward) * x
        self.n_updates += 1

    def warm_start(self, weights: np.ndarray, strength: float = 1.0) -> None:
        """Seed a linear prior (from the offline LTR pass) on the overlapping
        dimensions. With A near identity this makes theta ~= strength*weights."""
        w = np.asarray(weights, dtype=float).reshape(-1)
        if w.shape[0] != self.d:
            w = np.resize(w, self.d)
        self.b += strength * w

    def save(self, path: Path | None = None) -> Path:
        path = Path(path) if path else C.LINUCB_STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump({"d": self.d, "alpha": self.alpha, "A": self.A,
                         "b": self.b, "n_updates": self.n_updates}, f)
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> "LinUCB":
        path = Path(path) if path else C.LINUCB_STATE_PATH
        if not path.exists():
            return cls()
        with path.open("rb") as f:
            st = pickle.load(f)
        obj = cls(feature_dim=st["d"], alpha=st["alpha"])
        obj.A, obj.b, obj.n_updates = st["A"], st["b"], st["n_updates"]
        return obj


# ---------------------------------------------------------------------------
# Reranker - feature flag + cold-start floor
# ---------------------------------------------------------------------------
class Reranker:
    def __init__(self, linucb: LinUCB | None = None,
                 feature_builder: FeatureBuilder | None = None,
                 flag_enabled: bool | None = None,
                 min_verdicts_to_activate: int | None = None):
        self.linucb = linucb or LinUCB.load()
        self.feature_builder = feature_builder or FeatureBuilder()
        self.flag_enabled = (C.ADAPTIVE_TRIAGE_ENABLED if flag_enabled is None else flag_enabled)
        self.min_verdicts = (C.MIN_VERDICTS_TO_ACTIVATE if min_verdicts_to_activate is None
                             else min_verdicts_to_activate)

    def active(self) -> bool:
        return bool(self.flag_enabled) and self.linucb.n_updates >= self.min_verdicts

    def rerank(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return df with a `bandit_score` column, reordered. When inactive
        (flag off or under the cold-start floor) the static `actionability`
        order is preserved and bandit_score is NaN."""
        if df is None or not len(df):
            return df
        out = df.copy()
        if not self.active():
            out["bandit_score"] = np.nan
            if "actionability" in out.columns:
                out = out.sort_values("actionability", ascending=False)
            return out.reset_index(drop=True)
        X = self.feature_builder.build_frame(out)
        out["bandit_score"] = self.linucb.score_frame(X)
        return out.sort_values("bandit_score", ascending=False).reset_index(drop=True)

    def observe(self, row, reward: float, persist: bool = True) -> None:
        """Fold one verdict into the online model."""
        x = self.feature_builder.build(row)
        self.linucb.update(x, reward)
        if persist:
            self.linucb.save()
