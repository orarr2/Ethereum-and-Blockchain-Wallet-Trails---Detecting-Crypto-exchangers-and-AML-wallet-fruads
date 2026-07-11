"""
Verdict persistence - append-only JSONL of analyst decisions.

One JSON object per line (spec section 3.2). This is the reward signal that
feeds Component B. Writes are append-only so no verdict is ever lost when the
feature flag is toggled. `reward` is derived from the verdict via the tunable
schedule in config.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config as C

VERDICT_VALUES = set(C.REWARD_SCHEDULE.keys())

_SCHEMA_KEYS = ("ts", "wallet", "chain", "verdict", "reward", "features",
                "note", "analyst_id")


def reward_for(verdict: str):
    """Map a verdict string to its reward. Returns None for 'unclear' (skip)."""
    return C.REWARD_SCHEDULE.get(verdict, None)


def make_verdict(wallet: str, chain: str, verdict: str, features: dict | None = None,
                 note: str = "", analyst_id: str | None = None,
                 ts: str | None = None) -> dict:
    if verdict not in VERDICT_VALUES:
        raise ValueError(f"unknown verdict {verdict!r}; allowed: {sorted(VERDICT_VALUES)}")
    return {
        "ts": ts or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wallet": str(wallet),
        "chain": str(chain),
        "verdict": verdict,
        "reward": reward_for(verdict),
        "features": features or {},
        "note": note or "",
        "analyst_id": analyst_id or C.ANALYST_ID,
    }


def append(verdict: dict, path: Path | None = None) -> None:
    path = Path(path) if path else C.VERDICTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    missing = [k for k in _SCHEMA_KEYS if k not in verdict]
    if missing:
        raise ValueError(f"verdict missing keys: {missing}")
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(verdict, default=str) + "\n")


def append_new(wallet: str, chain: str, verdict: str, features: dict | None = None,
               note: str = "", analyst_id: str | None = None,
               path: Path | None = None) -> dict:
    v = make_verdict(wallet, chain, verdict, features, note, analyst_id)
    append(v, path)
    return v


def read_all(path: Path | None = None) -> list:
    path = Path(path) if path else C.VERDICTS_PATH
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def to_dataframe(path: Path | None = None) -> pd.DataFrame:
    rows = read_all(path)
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=list(_SCHEMA_KEYS))


def count(path: Path | None = None) -> int:
    return len(read_all(path))


def trainable(path: Path | None = None) -> list:
    """Verdicts with a non-None reward (drops 'unclear')."""
    return [v for v in read_all(path) if v.get("reward") is not None]
