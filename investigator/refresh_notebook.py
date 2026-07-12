"""
refresh_notebook - execute Crypto-AML-Analysis.ipynb end-to-end from CI.

Runs the notebook via papermill; the notebook itself calls `config.run_query`
for every BigQuery hit, and `config.run_query` enforces the per-session
kill-switch (INVESTIGATOR_BQ_KILL_USD_DAILY). So no extra guarding logic
lives here - the daily USD ceiling is a policy on the base module.

The output artifacts (`suspicious_wallets_master.csv`, per-chain CSVs,
`report.html`, `data/*.csv`) are written by the notebook itself to the
repo root and to `data/`. The workflow then commits them back with `[skip
ci]` so the next scan cycle picks them up.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import PROJECT_ROOT


def main() -> int:
    try:
        import papermill as pm
    except ImportError:
        print("[refresh] papermill is not installed. "
              "Add it to CI: `pip install papermill`.", file=sys.stderr)
        return 2

    nb_in = PROJECT_ROOT / "Crypto-AML-Analysis.ipynb"
    if not nb_in.exists():
        print(f"[refresh] notebook not found: {nb_in}", file=sys.stderr)
        return 2

    out_dir = PROJECT_ROOT / "investigator" / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    nb_out = out_dir / "notebook_executed.ipynb"

    cap = os.environ.get("INVESTIGATOR_BQ_KILL_USD_DAILY", "(unset)")
    print(f"[refresh] executing {nb_in.name} - BQ kill-switch cap = ${cap}")
    try:
        pm.execute_notebook(
            str(nb_in), str(nb_out),
            kernel_name="python3", cwd=str(PROJECT_ROOT),
            progress_bar=False,
        )
    except Exception as e:
        print(f"[refresh] notebook execution failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        # papermill wraps the underlying error; if it's the kill-switch, exit
        # code 3 so the workflow surfaces the reason without a generic 1.
        if "kill-switch" in str(e).lower():
            return 3
        return 1

    print(f"[refresh] executed notebook -> {nb_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
