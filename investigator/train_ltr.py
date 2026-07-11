"""
Offline pairwise Learning-to-Rank + LinUCB warm-start.

Weekly (or on-demand) pass over the accumulated verdicts:
  1. Build a context vector per verdict (from the stored show-time features, or
     rebuilt from the master row).
  2. Train an XGBoost pairwise ranker (objective=rank:pairwise) and save it to
     outputs/ltr_model.json - the richer offline scorer.
  3. Derive a LINEAR warm-start vector via ridge regression of reward on the
     features. With A near identity this becomes LinUCB's theta prior, so the
     online bandit starts from the offline signal instead of cold. (XGBoost is
     nonlinear and cannot seed a linear bandit directly; the ridge projection is
     the documented bridge.)

Everything degrades gracefully: if xgboost is not installed the ranker step is
skipped and only the ridge warm-start is produced.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from . import verdicts_io
from .adaptive_triage import FEATURE_DIM, FEATURE_NAMES, FeatureBuilder, LinUCB


def _build_matrix(verdicts: list, master: pd.DataFrame | None,
                  fb: FeatureBuilder) -> tuple:
    master_idx = {}
    if master is not None and len(master):
        for _, r in master.iterrows():
            master_idx[(str(r.get("wallet")), str(r.get("chain")))] = r

    X, y, keys = [], [], []
    for v in verdicts:
        feats = v.get("features") or {}
        if feats:
            vec = fb.build(feats)
        else:
            row = master_idx.get((str(v.get("wallet")), str(v.get("chain"))))
            if row is None:
                continue
            vec = fb.build(row)
        X.append(vec)
        y.append(float(v.get("reward", 0.0)))
        keys.append((v.get("wallet"), v.get("chain")))
    if not X:
        return np.zeros((0, FEATURE_DIM)), np.zeros(0), []
    return np.vstack(X), np.asarray(y), keys


def _ridge_weights(X: np.ndarray, y: np.ndarray, lam: float = 1.0) -> np.ndarray:
    """Closed-form ridge weights: (X'X + lam I)^-1 X'y. Centres y so the prior
    reflects relative preference, not the mean reward."""
    if not len(X):
        return np.zeros(FEATURE_DIM)
    yc = y - y.mean()
    d = X.shape[1]
    return np.linalg.solve(X.T @ X + lam * np.identity(d), X.T @ yc)


def train(master: pd.DataFrame | None = None, warm_start_strength: float = 2.0,
          apply_warm_start: bool = True, verbose: bool = True) -> dict:
    verdicts = verdicts_io.trainable()
    fb = FeatureBuilder(FeatureBuilder.cluster_vocab_from_master(master))
    X, y, keys = _build_matrix(verdicts, master, fb)

    summary = {"n_verdicts": len(verdicts), "n_usable": int(len(X)),
               "xgboost_trained": False, "ltr_model_path": None,
               "warm_start_applied": False, "linucb_state_path": None}

    if len(X) < 2:
        if verbose:
            print(f"[train_ltr] only {len(X)} usable verdicts; need >=2. Skipping.")
        return summary

    # (2) XGBoost pairwise ranker (optional dependency)
    try:
        import xgboost as xgb
        ranker = xgb.XGBRanker(objective="rank:pairwise", n_estimators=100,
                               max_depth=4, learning_rate=0.1)
        # single group = all verdicts from the pool (per-analyst grouping can be
        # added later; see spec risk 1)
        ranker.fit(X, _rank_labels(y), group=[len(X)])
        C.LTR_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        ranker.save_model(str(C.LTR_MODEL_PATH))
        summary["xgboost_trained"] = True
        summary["ltr_model_path"] = str(C.LTR_MODEL_PATH)
        if verbose:
            print(f"[train_ltr] XGBRanker trained on {len(X)} rows -> {C.LTR_MODEL_PATH.name}")
    except ImportError:
        if verbose:
            print("[train_ltr] xgboost not installed; skipping ranker, using ridge warm-start only.")
    except Exception as e:
        if verbose:
            print(f"[train_ltr] XGBoost step failed ({type(e).__name__}: {e}); continuing with ridge.")

    # (3) ridge warm-start vector
    w = _ridge_weights(X, y)
    top = sorted(zip(FEATURE_NAMES, w), key=lambda t: -abs(t[1]))[:8]
    summary["top_features"] = [{"feature": n, "weight": round(float(val), 4)} for n, val in top]

    if apply_warm_start:
        linucb = LinUCB.load()
        linucb.warm_start(w, strength=warm_start_strength)
        path = linucb.save()
        summary["warm_start_applied"] = True
        summary["linucb_state_path"] = str(path)
        if verbose:
            print(f"[train_ltr] warm-started LinUCB (strength={warm_start_strength}) -> {path.name}")
            print("[train_ltr] top prior features:",
                  ", ".join(f"{n}={val:+.3f}" for n, val in top))

    return summary


def _rank_labels(y: np.ndarray) -> np.ndarray:
    """Map continuous rewards to non-negative integer relevance grades for the
    pairwise ranker (higher reward -> higher grade)."""
    order = {v: i for i, v in enumerate(sorted(set(y.tolist())))}
    return np.array([order[v] for v in y.tolist()], dtype=int)


if __name__ == "__main__":  # pragma: no cover
    m = None
    if C.MASTER_CSV.exists():
        m = pd.read_csv(C.MASTER_CSV)
    out = train(master=m)
    print(json.dumps(out, indent=2))
