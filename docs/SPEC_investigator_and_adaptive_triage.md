# Specification & Design: Investigator Agent + Adaptive Triage

**Target repository:** `Ethereum-and-Blockchain-Wallet-Trails---Detecting-Crypto-exchangers-and-AML-wallet-fruads`
**Target branch for implementation:** `main`
**Document type:** Specification & architecture proposal. No implementation. This document is the brief the implementer will work from.
**Version:** 1.0

---

## 1. Objective

Add two coordinated layers on top of the existing pipeline **without touching the detection stage** (`pipeline_extended.py`, the notebook, `bridge_wallets.py`, `enrichment.py`, `terror_signals.py`):

1. **Autonomous investigator agent.** Given a seed wallet from the ranked queue, the agent builds a structured markdown case dossier on its own: graph traversal over N hops, per-node enrichment, mixer/bridge/cross-chain detection, and a summary in which every claim cites the specific tool observation that supports it.
2. **Adaptive triage.** Analyst verdicts on the ranked queue become reward signals that re-rank future candidates. The static Isolation-Forest score gets a companion online learner that discovers which of its own features actually matter for the analyst's specific use case (CTF, sanctions, informal exchangers, general AML).

The original detection pipeline stays untouched. Everything new "sits on top."

---

## 2. Current state (baseline snapshot)

- **Source of truth for triage:** `suspicious_wallets_master.csv` (emitted by the notebook, section 13.6). Contains every column the new stack needs: `wallet`, `chain`, `distinct_senders`, `distinct_recipients`, `in_usdt`, `in_median_usdt`, `in_out_tx_ratio`, `risk_score`, `actionability`, `has_exchange_anchor`, `anchor_exchange`, `anchor_usdt`, `is_bridge_wallet`, `components_bridged`, `campaign_terror_score`, `hits_interesting_country`, `country_codes`, `country_source`, `informal_score`, `funnel_signal`, `cluster_id` (when written), `risk_categories`.
- **UI:** `streamlit_app.py` — tabs Candidates / Plots / Drilldown / Bridges / Geo / Report / Artifacts. The Candidates tab is sorted statically by `actionability`.
- **Static report:** `report_html.build_report()` emits a self-contained `report.html`.
- **BigQuery:** `config.run_query()` performs a dry-run + `maximum_bytes_billed` cap. Cost budget is already the norm.
- **Enrichment:** `Enricher` with pluggable `EnrichmentSource` — `PublicLabelsSource`, `ChainalysisOracleSource`, `ENSSource`, `ExchangeAnchorSource`.
- **Bridges:** `bridge_wallets.find_bridge_wallets(graph)` — block-cut-tree implementation, ready to be wrapped as a tool.
- **CTF:** `terror_signals.campaign_score(df)` + `top_leads()`.
- **Registry sources:** `data/nbctf_addresses.json` + OFAC list (via `0xB10C/ofac-sanctioned-digital-currency-addresses`).

**Existing gap:**

1. The queue ordering is static — nothing learns from analyst verdicts.
2. Drill-down on a candidate is manual — the analyst has to open the block explorer, cross-reference anchors/bridges/countries, and summarize.

---

## 3. Functional requirements

### 3.1 Component A — Investigator agent

**Input:** `(address, chain)` + budgets (hops, BigQuery USD, LLM tokens).
**Output:** markdown file `outputs/dossiers/<chain>/<address>.md` + `outputs/dossiers/<chain>/<address>.trace.json`.

**Functional requirements:**

1. The agent runs a ReAct-style loop with a chain-agnostic tool set. At each step: reasoning → tool selection → invocation → observation is folded back in.
2. The agent **must** cite every claim in the dossier to a specific tool call in the trace (anchor `[TOOL:<n>]`). A claim without an anchor is a defect and fails dossier validation.
3. The agent must respect budgets. Any exceedance triggers a clean stop plus a "Partial dossier — budget exhausted" section.
4. The output must contain the phrase "Research lead — not a finding of guilt" in the header of every section.
5. The full trace is appended to the dossier as Appendix A.
6. There is no "guilty / clean" gate — the agent only summarizes evidence. Any determination stays with the analyst.

**Mandatory dossier sections:**

```
1. Summary (2–4 lines)
2. Graph trace (N-hop walk, table: hop | counterparty | direction | value | notes)
3. Enrichment findings (labels, ENS, Chainalysis oracle)
4. Mixer signals (heuristics + detect_mixer output)
5. Bridge signals (articulation points)
6. Cross-chain hops (if any were detected)
7. OFAC / NBCTF proximity (0..N hops)
8. Risk conclusion (evidence-only, no verdict)
9. Evidence table (address | tool | value | source_uri)
Appendix A: Full reasoning trace (JSON block)
```

**Required tools:**

| Tool | Input schema | Output schema | Notes |
|---|---|---|---|
| `graph_expand` | `{address, chain, hops, direction: "in|out|both", max_edges}` | `{edges: [{from, to, value, tx_count, first_ts, last_ts}], truncated: bool}` | Wraps BigQuery, honors `maximum_bytes_billed` and the partition filter |
| `enrich` | `{address}` | `{labels, ens_name, chainalysis_sanctioned, sources: [...]}` | Wraps `enrichment.Enricher` |
| `detect_mixer` | `{address, chain}` | `{is_mixer_like: bool, signals: [{name, score, evidence}]}` | Heuristics: Tornado-like, peel chain, uniform outputs |
| `detect_bridge` | `{address, chain}` | `{is_bridge: bool, components_bridged: int, component_sizes: [int]}` | Wraps `bridge_wallets.find_bridge_wallets` on a sub-graph |
| `search_ofac` | `{address}` | `{hit: bool, hops_to_hit: int|null, list_source: str}` | Direct + N-hop search |
| `search_nbctf` | `{address}` | `{hit: bool, order?: str, affiliation?: str, url?: str}` | Reads `data/nbctf_addresses.json` |
| `write_case_note` | `{section, content, citations: [tool_call_id]}` | `{ok: bool}` | Appends to the dossier; requires at least one citation per informative section |

**Optional tools (budget permitting):**

| Tool | Input | Output | Notes |
|---|---|---|---|
| `cross_chain_hop` | `{address}` | `{same_hex_on: [chains]}` | Searches for the same hex in `suspicious_wallets_master` across other chains |
| `country_context` | `{address}` | `{country_codes, country_source}` | Reads from `geo_tagging` |

**Budgets (defaults):**

- `HOPS_BUDGET = 2`
- `BQ_USD_BUDGET_PER_DOSSIER = 0.10`
- `LLM_TOKEN_BUDGET_PER_DOSSIER = 30_000` (in + out)
- `MAX_TOOL_CALLS_PER_DOSSIER = 25`

Exceedance in any dimension triggers a clean shutdown and marks the dossier metadata with `partial=true`.

**Clean-stop conditions:**

- The address has been investigated to budget exhaustion, or
- The agent emits `stop_reason: "sufficient_evidence"` after having issued `write_case_note` for every one of sections 1–8, or
- Any budget cap is hit.

**Batch mode:**

A nightly cron job reads the top 25 per chain from `master` after re-ranking, runs the investigator on each, writes dossiers to `outputs/dossiers/<chain>/`, updates the index (`outputs/dossiers/index.json`), and refreshes `report.html` with a new "Dossier" column that links each row to its dossier.

**Interactive mode:**

A new "Dossier" tab in Streamlit — the analyst picks an address from the queue, clicks "Investigate", the dossier renders in place, and the reasoning trace is exposed in an expander. A `force=True` button re-investigates on demand.

### 3.2 Component B — Adaptive triage

**Input:** analyst verdicts + feature vectors for every candidate.
**Output:** live re-rank of the Streamlit queue + persisted model updates on disk.

**Verdict values:**

```
real_informal_exchanger   → full positive reward (+1.0)
OTC                       → partial positive reward (+0.3)
CEX_deposit               → zero (0.0)
legitimate_service        → small negative reward (-0.2)
unclear                   → zero, not used for training (skip)
```

Reward values are tunable and represent the default schedule.

**Persistence:**

`data/verdicts.jsonl` — one line per verdict:

```json
{
  "ts": "2026-07-09T12:34:56Z",
  "wallet": "0x...",
  "chain": "ethereum",
  "verdict": "real_informal_exchanger",
  "reward": 1.0,
  "features": {...},
  "note": "free text",
  "analyst_id": "anon-1",
  "arm_context_hash": "..."
}
```

**Feature vector (context):**

Numeric:

```
risk_score, actionability, informal_score, funnel_signal,
distinct_senders_log, distinct_recipients_log, in_usdt_log,
in_median_usdt_log, in_out_tx_ratio, campaign_terror_score,
components_bridged, hop_to_ofac (∞ = large constant),
night_share, round_amount_ratio
```

Binary (0/1):

```
has_exchange_anchor, is_bridge_wallet, hits_interesting_country,
chain_ethereum, chain_tron, chain_bitcoin, chain_zcash
```

Clusters:

```
cluster_id_onehot (top-20 clusters + "other")
```

**Rerankers:**

- **Online:** LinUCB (Li et al., 2010) — single "show_to_analyst" arm with a per-candidate context. `A` and `b` are updated after every verdict. Default `α = 1.0`.
- **Offline:** Pairwise LTR with XGBoost (`objective=rank:pairwise`) — trained on every pair of verdicts from the same analyst. Retrained weekly or on demand.
- **Bridge:** the LTR predictions warm-start LinUCB's `θ` on the overlapping feature dimensions.

**Feature flag:**

`ADAPTIVE_TRIAGE_ENABLED` (env var / config). When off, Streamlit sorts by `actionability` as it does today. When on, it sorts by `bandit_score = ucb_upper_bound(context)`. The switch must be flipped in a single toggle without losing any collected verdicts.

**Live update:**

Every analyst verdict in Streamlit → append to `verdicts.jsonl` → `LinUCB.update(context, reward)` → the table re-renders and the new ordering is visible immediately.

**Cold-start:**

Below a floor of `n_verdicts < 30`, LinUCB falls back to the static `actionability` ordering (the queue does not move). This avoids meaningless re-ranks under signal starvation.

**One-off backfill:**

A `backfill_verdicts.py` script that lets an analyst manually label the 50 top candidates per chain from the reference run as a prior. Produces a seed `verdicts.jsonl`.

---

## 4. Architecture proposals

Three alternatives are laid out. The choice is a cost / simplicity / future-extensibility trade-off.

### 4.1 Architecture 1 — "Batch-first" (recommended)

```
                   ┌─────────────────────────────────────────┐
                   │  Notebook + pipeline_extended  (unchanged)│
                   └───────────────┬─────────────────────────┘
                                   │ writes
                                   ▼
                   suspicious_wallets_master.csv (source of truth)
                                   │
                                   ├─────────────────────┐
                                   │                     │
                                   ▼                     ▼
                       ┌──────────────────┐   ┌───────────────────────┐
                       │  AdaptiveTriage  │   │   Investigator (batch) │
                       │   (module)       │   │   agent.py              │
                       │                  │   │                         │
                       │  LinUCB (online) │   │  ReAct loop             │
                       │  XGBoost (weekly)│   │  7+ tools               │
                       └────────┬─────────┘   └──────────┬──────────────┘
                                │                        │
                                │ rerank                 │ writes
                                ▼                        ▼
                       reranked queue           outputs/dossiers/*.md
                                │                        │
                                └───────────┬────────────┘
                                            ▼
                                   ┌──────────────────┐
                                   │  streamlit_app   │
                                   │  + report_html   │
                                   │                  │
                                   │  new UI:         │
                                   │  Verdict widget  │
                                   │  Dossier viewer  │
                                   └──────────────────┘
```

**New components:**

- `agent.py` — Investigator ReAct loop + tool registry (~350–500 LOC).
- `tools/` (new package):
  - `graph_tool.py`
  - `enrich_tool.py`
  - `mixer_tool.py`
  - `bridge_tool.py`
  - `ofac_tool.py`
  - `nbctf_tool.py`
  - `writer_tool.py`
- `adaptive_triage.py` — LinUCB + `FeatureBuilder` + `Reranker` (~200 LOC).
- `train_ltr.py` — offline XGBoost pairwise training.
- `dossier_writer.py` — markdown assembler + validators.
- `verdicts_io.py` — I/O for `verdicts.jsonl` + schema validation.
- `run_nightly.py` — cron entrypoint: rerank → top-25 per chain → investigate → refresh report.
- `streamlit_app.py` diffs — verdict widget + Dossier tab + feature-flag hook.
- `report_html.py` diffs — a "Dossier" column with relative links.

**LLM provider:** isolated in `llm_client.py` (provider-agnostic), chosen by env var. Batch mode uses strict JSON output for tool calls.

**Pros:**

- Changes are isolated. The existing pipeline is untouched.
- Streamlit does not depend on the LLM to function (feature flag).
- Easy to run retroactively on a backlog of candidates.
- Easy to add tools / sources later.

**Cons:**

- Two paths (batch + interactive) — needs a clear rule about which dossier is served when.

**Estimated cost per chain per night:**

- BigQuery: 25 × $0.10 = $2.50
- LLM: 25 × ~$0.03–0.08 = $0.75–2.00
- Total per chain per night: **~$3.25–4.50**
- Four chains: **~$13–18/day**. Must be added to the budget.

---

### 4.2 Architecture 2 — "Streamlit-native"

The investigator runs on demand from Streamlit only. No nightly batch.

**Delta vs. #1:**

- No `run_nightly.py`.
- No pre-built dossiers. Every one is generated live when the analyst clicks "Investigate".
- `report.html` does not link to dossiers (or only links to cached ones).

**Pros:**

- Minimal operational cost. Pay only for what the analyst asks for.
- No scheduler dependency.

**Cons:**

- Initial dossier load is a multi-minute wait (LLM roundtrip + BigQuery).
- No coverage while the analyst is offline.
- The "time-to-actionable-lead" metric is worse.

**When to pick:** if the operational budget target is ~$0/day and the workflow is strictly interactive.

---

### 4.3 Architecture 3 — "Micro-service"

The investigator lives as an HTTP service (FastAPI) that exposes `POST /investigate {address, chain}` → `{dossier_md, trace_json}`. Streamlit and cron call it.

**Pros:**

- Can be reached by multiple clients (CLI, Streamlit, external tooling).
- Can be exposed to other teams later.

**Cons:**

- Adds infrastructure: deployment, ports, auth, monitoring.
- Adds runtime dependencies (Streamlit no longer standalone).
- Over-engineering for a $1/run research pipeline.

**When to pick:** if the intent is to expose the capability to a team that does not run the notebook.

---

## 5. Recommendation

**Architecture 1 (Batch-first).** It matches the research nature of the project, preserves the shipped-static-report property, allows retroactive runs on the backlog, and is cleanly driven by cron. The interactive Streamlit piece stays thin.

---

## 6. Data flow

### 6.1 Verdict → LinUCB (online)

```
analyst clicks the widget in the Streamlit Candidates tab
        │
        ▼
POST-like handler in streamlit_app.py
        │
        ▼
verdicts_io.append({wallet, chain, verdict, reward, features_at_show_time, note, ts})
        │
        ▼
adaptive_triage.linucb.update(context=features, reward=reward)
        │
        ▼
persist model to data/linucb_state.pkl (A_matrix, b_vector, n_updates)
        │
        ▼
Streamlit re-runs → new ordering visible immediately
```

### 6.2 Nightly batch

```
cron @02:00 UTC
        │
        ▼
run_nightly.py
        │
        ├── load master.csv + linucb_state.pkl
        │
        ├── for chain in [ethereum, tron, bitcoin]:
        │       reranked = adaptive_triage.rerank(chain_df)
        │       top25    = reranked.head(25)
        │       for w in top25:
        │           investigator.investigate(w, chain)
        │           dossier_writer.write(...)
        │
        ├── dossiers_index.json ← manifest
        │
        └── report_html.build_report(master, dossiers_dir=...) → report.html
```

### 6.3 Investigator loop (per address)

```
seed = (address, chain)
budget = Budget(hops=2, bq_usd=0.10, tokens=30_000, max_calls=25)
trace  = []

while not budget.exhausted() and not stop_reason:
    reasoning_step = llm.next_action(state, trace)
    if reasoning_step.action == "stop":
        stop_reason = reasoning_step.reason
        break

    tool = registry[reasoning_step.tool]
    validate(reasoning_step.args, tool.input_schema)
    result = tool.run(reasoning_step.args, budget)
    validate(result, tool.output_schema)

    trace.append({call_id, tool, args, result, cost})
    budget.charge(result.cost)

    if reasoning_step.tool == "write_case_note":
        dossier.append_section(...)

finalize:
    dossier.render(trace)
    write to outputs/dossiers/<chain>/<address>.md
```

---

## 7. Proposed file layout

```
repo/
├── agent.py                          NEW  Investigator class + ReAct loop
├── adaptive_triage.py                NEW  LinUCB + rerank helpers
├── train_ltr.py                      NEW  Offline XGBoost pairwise trainer
├── verdicts_io.py                    NEW  verdicts.jsonl I/O + schema
├── dossier_writer.py                 NEW  Markdown assembler + validators
├── llm_client.py                     NEW  Provider-agnostic LLM interface
├── run_nightly.py                    NEW  Cron entrypoint
├── backfill_verdicts.py              NEW  Seed prior
├── tools/                            NEW  package
│   ├── __init__.py
│   ├── base.py                            Tool abc + Budget
│   ├── graph_tool.py
│   ├── enrich_tool.py
│   ├── mixer_tool.py
│   ├── bridge_tool.py
│   ├── ofac_tool.py
│   ├── nbctf_tool.py
│   └── writer_tool.py
├── schemas/                          NEW  jsonschema per tool
│   ├── graph_expand.in.json
│   ├── graph_expand.out.json
│   └── ...
├── streamlit_app.py                  MOD  verdict widget + Dossier tab + feature flag
├── report_html.py                    MOD  Dossier column
├── data/
│   ├── nbctf_addresses.json               unchanged
│   ├── verdicts.jsonl                NEW
│   └── linucb_state.pkl              NEW
├── outputs/
│   ├── dossiers/
│   │   ├── ethereum/*.md            NEW
│   │   ├── tron/*.md                NEW
│   │   ├── bitcoin/*.md             NEW
│   │   └── index.json               NEW
│   └── ltr_model.json               NEW
└── tests/                            NEW  package
    ├── test_tools_smoke.py
    ├── test_dossier_validation.py
    ├── test_linucb.py
    └── test_verdicts_io.py
```

**No existing file changes its public API. The edits to `streamlit_app.py` and `report_html.py` are additive.**

---

## 8. Interface contracts

### 8.1 `adaptive_triage.py`

```python
class LinUCB:
    def __init__(self, feature_dim: int, alpha: float = 1.0): ...
    def score(self, context: np.ndarray) -> float
    def update(self, context: np.ndarray, reward: float) -> None
    def save(self, path: Path) -> None
    @classmethod
    def load(cls, path: Path) -> "LinUCB"

class Reranker:
    def __init__(self, linucb: LinUCB, feature_builder: FeatureBuilder,
                 flag_enabled: bool, min_verdicts_to_activate: int = 30): ...
    def rerank(self, df: pd.DataFrame) -> pd.DataFrame:
        """Returns df with a new `bandit_score` column and reordered rows."""

class FeatureBuilder:
    def build(self, row: pd.Series) -> np.ndarray
    def build_frame(self, df: pd.DataFrame) -> np.ndarray
```

### 8.2 `agent.py`

```python
@dataclass
class Budget:
    hops: int
    bq_usd: float
    tokens: int
    max_calls: int
    def charge_bq(self, usd: float) -> None
    def charge_tokens(self, in_toks: int, out_toks: int) -> None
    def exhausted(self) -> tuple[bool, str]

class Investigator:
    def __init__(self, llm: LLMClient, tools: ToolRegistry,
                 dossier_writer: DossierWriter): ...
    def investigate(self, address: str, chain: str,
                    budget: Budget | None = None) -> DossierResult

@dataclass
class DossierResult:
    dossier_md_path: Path
    trace_json_path: Path
    partial: bool
    stop_reason: str
    bq_cost_usd: float
    token_cost: dict
    duration_s: float
```

### 8.3 `tools/base.py`

```python
class Tool:
    name: str
    input_schema: dict
    output_schema: dict
    def run(self, args: dict, budget: Budget) -> dict
    def estimated_cost(self, args: dict) -> float
```

---

## 9. Budgets and ceilings

- Each tool reports `estimated_cost(args)` **before** running. If the projected charge exceeds the remaining budget, the tool returns a `budget_exceeded` error and does not execute the call.
- BigQuery calls go through `config.run_query()`, which already dry-runs and enforces `maximum_bytes_billed`. Wiring: `Budget.charge_bq()` is billed by the dry-run's actual number.
- LLM: every call routes through `llm_client.chat()`, which records `usage`. Wiring: `Budget.charge_tokens()` is billed after every call.
- `max_calls` is the backstop against faulty logic. Exceedance = hard stop + `partial=true`.

---

## 10. Rollout plan

**Phase 0 — Preparation (pre-implementation):**
- Approve budget knobs in config.
- Select an LLM provider and API key.
- Decide on `analyst_id` shape (hash of user email? one-off placeholder?).

**Phase 1 — Verdict scaffold:**
- Verdict widget in Streamlit + `verdicts.jsonl` + `verdicts_io.py`.
- `backfill_verdicts.py` produces a small seed set.
- No learning yet — collection only.

**Phase 2 — Baseline LinUCB:**
- Ship `adaptive_triage.py`.
- Feature flag wired to Streamlit — when on, the ordering swaps.
- Metric: precision@25 vs. static ordering on a small hold-out set.

**Phase 3 — Tools:**
- Wrap `enrichment.py`, `bridge_wallets.py`, and the BigQuery `graph_expand` query as JSON-schema-validated tools. BigQuery-mocked tests.

**Phase 4 — Investigator loop:**
- `agent.py` + `dossier_writer.py`.
- 5 hand-audited dossiers end-to-end. Prompt tuning.

**Phase 5 — Batch + report integration:**
- `run_nightly.py` + Dossier column in `report.html`.

**Phase 6 — LTR:**
- Weekly `train_ltr.py` + warm-start into LinUCB.

**Phase 7 — Cron:**
- Pin schedule + monitoring.

Every phase is standalone and can be merged into `main` independently.

---

## 11. Success metrics

| Metric | Target | How measured |
|---|---|---|
| Precision@25 (per chain) | +20 percentage points over baseline | Hold-out set of 50 verdicts not used for training |
| Verdicts per hour | +30% | Timestamps in `verdicts.jsonl` |
| Time-to-lead (cached) | < 60s | Streamlit-side timer on the Dossier tab |
| Cost per dossier | ≤ $0.15 | Reported in `DossierResult` metadata |
| Dossier quality (manual) | Mean ≥ 4 / 5 | Sample of 20 dossiers, 4-dimension rubric |
| Exploration coverage | Monotonic growth | Fraction of candidates whose LinUCB confidence upper bound is under a threshold |

---

## 12. Risks and mitigations

1. **Verdict mixing across analysts.** Multiple analysts with different mandates dilute a single model. **Mitigation:** `analyst_id` on every verdict, optionally a per-analyst LinUCB. Deferred to a later phase.
2. **Dossiers may read as more authoritative than they are.** **Mitigation:** mandatory "research lead" preface in every section header, injected by the `write_case_note` tool, plus dossier validation that rejects any claim without a `[TOOL:n]` citation.
3. **LLM hallucinations.** **Mitigation:** strict JSON mode for every tool call, jsonschema validation, invalid output surfaces as a tool error the agent can retry from. No free-text in the dossier without a citation.
4. **Weekend BigQuery cost.** **Mitigation:** a kill-switch (`BQ_KILL_USD_DAILY`) — the Budget layer halts the entire cron.
5. **Data leakage to the LLM provider.** **Mitigation:** the prompt sends only addresses and computed metrics. The OFAC list is not sent in bulk. NBCTF prompts include only the `search_nbctf` result (hit / miss + affiliation), not the underlying JSON file.

---

## 13. Ethics stance (extension of the existing README)

Reproduced here without changes:

1. Dossiers must preserve "research lead — not a finding" wording in the header of every section and every conclusion line.
2. Tools must not attempt real-world identity attribution. Attribution to a person stays with authorized enforcement, via KYC subpoena.
3. The full reasoning trace ships alongside the dossier as an appendix. If an analyst cannot follow the trace end-to-end, the dossier does not leave the dashboard.

---

## 14. New dependencies

To be added to `requirements.txt`:

```
xgboost>=2.0.0            # pairwise LTR
jsonschema>=4.21.0        # tool I/O validation
```

**Not** added:
- No specific LLM SDK in `requirements.txt` — `llm_client.py` resolves by env var. Keeps operational cost separate from install cost.
- No vowpal_wabbit — LinUCB is implemented locally (~80 LOC).
- No LangGraph — a small ReAct loop lives in `agent.py` (~150 LOC).

---

## 15. Open questions (to decide before implementation)

1. **Analyst identifier.** Do we want to attribute verdicts (hash of email / env var) or start anonymous? **Suggested default:** `analyst_id="local"` until a reason to change appears.
2. **Cold-start floor.** Is 30 verdicts right, or should it be 50? **Suggested default:** 30. Raise it if precision@25 drops under baseline during warm-up.
3. **Reward values for "OTC" and "legitimate_service".** Are +0.3 / -0.2 correct? **Suggested default:** start with these, recalibrate after 100 verdicts.
4. **Zcash batch coverage.** Transparent is thin; if 0 candidates, should the pipeline skip? **Suggested default:** skip if empty, log info.
5. **Cron scheduler.** system cron / GitHub Actions / Airflow? **Suggested default:** shell script + system cron, minimal.

---

## 16. Expected output after implementation

- `suspicious_wallets_master.csv` is unchanged.
- `report.html` looks nearly identical, with one new column (Dossier).
- Streamlit has 2 new UI elements (or upgrades to existing tabs): a Verdict widget inside Candidates, a Dossier viewer.
- `outputs/dossiers/<chain>/<address>.md` — complete files with an evidence table plus a reasoning trace.
- `data/verdicts.jsonl` grows with usage.
- `data/linucb_state.pkl` is rewritten on every update.
- The pipeline can still be run in the old shape by setting `ADAPTIVE_TRIAGE_ENABLED=false`.

**Single success criterion:** an analyst reading a dossier can justify every claim in it by clicking the citation and seeing which tool returned what. No orphaned claims.

---

## 17. Non-goals (what this document does **not** ask for)

- Do not touch the notebook, the pipeline SQL, or `funnel_signal` / `informal_score`.
- Do not add new enrichment sources (Chainalysis premium, TRM, Arkham, etc.).
- Do not build a full case-management UI (Approve / Escalate / Assign). The atomic unit here is a single verdict, and that is enough.
- Do not attempt real-world identity attribution. The README already draws this line and this document keeps it.
- Do not open an external API / micro-service (unless Architecture 3 is explicitly selected).

---

# End of document
