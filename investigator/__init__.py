"""
Investigator layer - autonomous AML investigator agent + adaptive triage.

This package "sits on top" of the existing detection pipeline. It is a
SEPARATE, OPTIONAL layer: the base project (notebook, pipeline_extended.py,
bridge_wallets.py, enrichment.py, terror_signals.py, streamlit_app.py) runs
completely standalone and is never imported *from* here in a way that changes
its behaviour. Nothing in the base project imports this package.

Two coordinated components (see docs/SPEC_investigator_and_adaptive_triage.md):
  A. Investigator agent   - a ReAct loop that builds a cited markdown dossier
                            for a single seed wallet under strict budgets.
  B. Adaptive triage      - LinUCB online reranker warm-started by a weekly
                            XGBoost pairwise LTR pass, fed by analyst verdicts.

Design rule: every claim in a dossier cites the tool observation that supports
it. A dossier is a *research lead*, never a finding of guilt.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The base project lives one directory up. Add it to sys.path so we can reuse
# its modules (config, enrichment, bridge_wallets, geo_tagging, terror_signals)
# without copying code. This is the ONLY coupling to the base project, and it
# is read-only: we import their functions, we never mutate their files.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

PROJECT_ROOT = _PROJECT_ROOT
PACKAGE_ROOT = Path(__file__).resolve().parent

__all__ = ["PROJECT_ROOT", "PACKAGE_ROOT"]
