"""
One-off verdict backfill.

Lets an analyst seed `verdicts.jsonl` by labelling the top candidates per chain
from the reference master run, so Component B has a prior before live use.

Two modes:
  * interactive  - prompt for a verdict per wallet in the terminal.
  * from-file    - read a CSV/JSONL of {wallet, chain, verdict} and bulk-append.

Stored `features` for each verdict is the full master row (as a dict), so the
LTR trainer can rebuild the context vector without re-reading the master.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from . import config as C
from . import verdicts_io

_ALLOWED = sorted(verdicts_io.VERDICT_VALUES)


def _top_per_chain(master: pd.DataFrame, k: int) -> pd.DataFrame:
    if "actionability" in master.columns:
        m = master.sort_values("actionability", ascending=False)
    else:
        m = master
    return m.groupby("chain", group_keys=False).head(k)


def _row_features(row: pd.Series) -> dict:
    # keep it JSON-serialisable and modest in size
    out = {}
    for k, v in row.items():
        if isinstance(v, (int, float, bool, str)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out


def from_file(path: Path, master: pd.DataFrame | None = None) -> int:
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        labels = verdicts_io.read_all(path)
    else:
        labels = pd.read_csv(path).to_dict("records")
    m_idx = {}
    if master is not None:
        for _, r in master.iterrows():
            m_idx[(str(r.get("wallet")), str(r.get("chain")))] = r
    n = 0
    for lab in labels:
        wallet, chain, verdict = lab.get("wallet"), lab.get("chain"), lab.get("verdict")
        if verdict not in verdicts_io.VERDICT_VALUES:
            print(f"  skip {wallet}: bad verdict {verdict!r}")
            continue
        row = m_idx.get((str(wallet), str(chain)))
        feats = _row_features(row) if row is not None else {}
        verdicts_io.append_new(wallet, chain, verdict, features=feats,
                               note=lab.get("note", "backfill"))
        n += 1
    print(f"[backfill] appended {n} verdicts -> {C.VERDICTS_PATH}")
    return n


def interactive(master: pd.DataFrame, k: int = 50) -> int:
    top = _top_per_chain(master, k)
    print(f"Labelling {len(top)} candidates. Allowed: {_ALLOWED} (or 'skip'/'quit').")
    n = 0
    for _, row in top.iterrows():
        prompt = (f"\n{row.get('chain')}  {row.get('wallet')}  "
                  f"risk={row.get('risk_score')}  action={row.get('actionability')}\n"
                  f"verdict> ")
        try:
            ans = input(prompt).strip()
        except EOFError:
            break
        if ans == "quit":
            break
        if ans == "skip" or not ans:
            continue
        if ans not in verdicts_io.VERDICT_VALUES:
            print(f"  '{ans}' not allowed; skipping.")
            continue
        verdicts_io.append_new(row.get("wallet"), row.get("chain"), ans,
                               features=_row_features(row), note="backfill-interactive")
        n += 1
    print(f"[backfill] appended {n} verdicts -> {C.VERDICTS_PATH}")
    return n


def main(argv=None):  # pragma: no cover
    ap = argparse.ArgumentParser(description="Seed verdicts.jsonl for adaptive triage.")
    ap.add_argument("--from-file", type=str, help="CSV/JSONL of {wallet,chain,verdict}")
    ap.add_argument("--k", type=int, default=50, help="top-K per chain (interactive)")
    args = ap.parse_args(argv)

    master = pd.read_csv(C.MASTER_CSV) if C.MASTER_CSV.exists() else pd.DataFrame()
    if args.from_file:
        from_file(Path(args.from_file), master if len(master) else None)
    else:
        if not len(master):
            print("No master CSV found; cannot run interactive backfill.", file=sys.stderr)
            sys.exit(1)
        interactive(master, k=args.k)


if __name__ == "__main__":  # pragma: no cover
    main()
