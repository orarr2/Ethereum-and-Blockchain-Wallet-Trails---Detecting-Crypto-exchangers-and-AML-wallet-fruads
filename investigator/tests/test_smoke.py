"""
Offline smoke tests for the investigator layer.

None of these touch the network or BigQuery: the agent test uses the `mock`
provider and stubs the network-facing tools, so the whole suite runs anywhere.

Run:  python -m pytest investigator/tests -q
  or: python investigator/tests/test_smoke.py   (falls back to a plain runner)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from investigator.budget import Budget
from investigator.adaptive_triage import (FEATURE_DIM, FeatureBuilder, LinUCB, Reranker)
from investigator import verdicts_io


def test_budget_exhaustion():
    b = Budget(hops=2, bq_usd=0.10, tokens=100, max_calls=3)
    assert b.exhausted() == (False, "")
    b.charge_tokens(60, 60)                 # 120 > 100
    done, why = b.exhausted()
    assert done and why == "tokens"


def test_budget_bq_preflight():
    b = Budget(bq_usd=0.10)
    b.charge_bq(0.08)
    assert b.would_exceed_bq(0.05) is True
    assert b.would_exceed_bq(0.01) is False


def test_feature_builder_fixed_dim():
    fb = FeatureBuilder()
    row = {"risk_score": 80, "actionability": 90, "chain": "ethereum",
           "distinct_senders": 2000, "has_exchange_anchor": True}
    v = fb.build(row)
    assert v.shape == (FEATURE_DIM,)
    # chain one-hot: ethereum slot set, others clear
    names = ["chain_ethereum", "chain_tron", "chain_bitcoin", "chain_zcash"]
    from investigator.adaptive_triage import FEATURE_NAMES
    idx = {n: FEATURE_NAMES.index(n) for n in names}
    assert v[idx["chain_ethereum"]] == 1.0
    assert v[idx["chain_tron"]] == 0.0


def test_linucb_learns_direction():
    d = FEATURE_DIM
    lin = LinUCB(feature_dim=d, alpha=0.0)   # alpha 0 -> pure exploitation for the test
    pos = np.zeros(d); pos[0] = 1.0
    neg = np.zeros(d); neg[1] = 1.0
    for _ in range(20):
        lin.update(pos, 1.0)
        lin.update(neg, -1.0)
    assert lin.score(pos) > lin.score(neg)
    assert lin.n_updates == 40


def test_linucb_save_load(tmp_path=None):
    import tempfile
    d = FEATURE_DIM
    lin = LinUCB(feature_dim=d)
    lin.update(np.ones(d), 1.0)
    p = Path(tempfile.mkdtemp()) / "state.pkl"
    lin.save(p)
    lin2 = LinUCB.load(p)
    assert lin2.n_updates == 1
    assert np.allclose(lin2.b, lin.b)


def test_reranker_cold_start_preserves_order():
    df = pd.DataFrame({"wallet": list("abcd"), "chain": ["ethereum"] * 4,
                       "actionability": [10, 40, 20, 30]})
    rk = Reranker(linucb=LinUCB(), feature_builder=FeatureBuilder(),
                  flag_enabled=True, min_verdicts_to_activate=30)
    assert rk.active() is False
    out = rk.rerank(df)
    # static actionability order, descending
    assert out["wallet"].tolist() == ["b", "d", "c", "a"]
    assert out["bandit_score"].isna().all()


def test_verdict_reward_schedule():
    assert verdicts_io.reward_for("real_informal_exchanger") == 1.0
    assert verdicts_io.reward_for("legitimate_service") == -0.2
    assert verdicts_io.reward_for("unclear") is None


def test_verdicts_roundtrip():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "verdicts.jsonl"
    verdicts_io.append_new("0xabc", "ethereum", "OTC", features={"risk_score": 50}, path=p)
    verdicts_io.append_new("0xdef", "tron", "unclear", path=p)
    rows = verdicts_io.read_all(p)
    assert len(rows) == 2
    assert len(verdicts_io.trainable(p)) == 1   # 'unclear' dropped


def test_agent_mock_end_to_end():
    """Full ReAct loop offline: 8 cited sections, valid dossier."""
    import tempfile
    import investigator.tools.enrich_tool as et
    import investigator.tools.ofac_tool as ot

    class _StubEnricher:
        def enrich(self, addrs):
            return {a: {} for a in addrs}

    et.EnrichTool._enricher = lambda self, ctx: _StubEnricher()
    ot._fetch = lambda sym: []

    from investigator.agent import Investigator
    from investigator.llm_client import LLMClient

    out_dir = Path(tempfile.mkdtemp())
    inv = Investigator(llm=LLMClient(provider="mock"), dossiers_dir=out_dir)
    res = inv.investigate("0xad285fdedfc0d5f944a33e478356524293c7ec68", "ethereum")
    assert res.n_sections == 8
    assert res.valid is True
    assert not res.partial
    assert Path(res.dossier_md_path).exists()
    text = Path(res.dossier_md_path).read_text(encoding="utf-8")
    assert "Research lead - not a finding of guilt." in text
    assert "[TOOL:1]" in text


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    return passed == len(fns)


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
