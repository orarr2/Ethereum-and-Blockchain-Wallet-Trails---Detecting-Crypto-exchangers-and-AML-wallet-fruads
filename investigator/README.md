# Investigator layer

An **optional, self-contained AI layer** on top of the base Crypto-AML pipeline.
Implements `docs/SPEC_investigator_and_adaptive_triage.md`:

- **Component A - Investigator agent.** A ReAct loop over 7 tools that builds a
  **cited** markdown dossier for one seed wallet, under strict budgets (hops,
  BigQuery dollars, LLM tokens, tool calls). Every claim anchors to a `[TOOL:n]`
  observation in the reasoning trace; a section without a citation is rejected.
- **Component B - Adaptive triage.** Analyst verdicts become rewards for an
  online **LinUCB** reranker, warm-started weekly by an offline **XGBoost**
  pairwise Learning-to-Rank pass.

> Everything here produces **research leads, not findings of guilt.**

## The base project is untouched

Nothing in the base project imports this package. This layer only *reads* base
modules (`config`, `enrichment`, `bridge_wallets`, `geo_tagging`,
`terror_signals`) and writes exclusively under `investigator/`. You can delete
this folder and the base pipeline still runs. The base dashboard
(`streamlit run streamlit_app.py`) and this one (`streamlit run
investigator/app.py`) are separate apps.

## Quick start

```bash
# 1. base deps first, then the layer's extras
pip install -r requirements.txt            # base project
pip install -r investigator/requirements.txt

# 2. offline demo - no API key, no BigQuery, no cost
export INVESTIGATOR_LLM_PROVIDER=mock       # PowerShell: $env:INVESTIGATOR_LLM_PROVIDER='mock'
jupyter notebook investigator/Investigator-Agent.ipynb

# 3. real run - needs BigQuery + an Anthropic key in .env
#    BQ_PROJECT=...   ANTHROPIC_API_KEY=...
export INVESTIGATOR_LLM_PROVIDER=anthropic
python -m investigator.run_nightly --top-k 5 --chains ethereum,tron,bitcoin
streamlit run investigator/app.py
```

## Layout

```
investigator/
  Investigator-Agent.ipynb   <- the separate notebook you run
  app.py                     <- separate Streamlit dashboard
  agent.py                   <- Component A: ReAct loop (Investigator)
  llm_client.py              <- provider-agnostic LLM (anthropic|openai|mock)
  budget.py                  <- per-dossier ceilings
  dossier_writer.py          <- markdown assembler + citation validation
  tools/                     <- graph_expand, enrich, detect_mixer, detect_bridge,
                                search_ofac, search_nbctf, write_case_note
  adaptive_triage.py         <- Component B: FeatureBuilder + LinUCB + Reranker
  verdicts_io.py             <- verdicts.jsonl I/O
  train_ltr.py               <- offline XGBoost LTR + LinUCB warm-start
  backfill_verdicts.py       <- seed verdicts from the reference run
  run_nightly.py             <- batch: rerank -> top-K -> dossiers -> index.html
  config.py                  <- all env-driven knobs
  outputs/dossiers/<chain>/  <- generated dossiers + trace.json + index.html
  data/                      <- verdicts.jsonl, linucb_state.pkl (created at runtime)
```

## Configuration (env vars, read from the same `.env` as the base project)

| Var | Default | Meaning |
|---|---|---|
| `INVESTIGATOR_LLM_PROVIDER` | `anthropic` | `anthropic` \| `openai` \| `mock` |
| `INVESTIGATOR_LLM_MODEL` | `claude-haiku-4-5` | model id |
| `ANTHROPIC_API_KEY` | - | required for a real run |
| `INVESTIGATOR_HOPS` | `2` | graph-traversal budget |
| `INVESTIGATOR_BQ_USD` | `0.10` | BigQuery budget per dossier |
| `INVESTIGATOR_TOKENS` | `30000` | LLM token budget per dossier |
| `INVESTIGATOR_MAX_CALLS` | `25` | tool-call backstop per dossier |
| `INVESTIGATOR_BQ_KILL_USD_DAILY` | `50` | batch-wide BigQuery kill-switch |
| `ADAPTIVE_TRIAGE_ENABLED` | `false` | on -> queue uses LinUCB order |
| `INVESTIGATOR_MIN_VERDICTS` | `30` | cold-start floor before rerank activates |
| `INVESTIGATOR_TOP_K` | `25` | dossiers per chain in batch mode |

## Cost

With `provider=mock`: **$0** (offline). Real runs are bounded per dossier by the
budgets above (~$0.10 BigQuery + a few cents of Haiku tokens) and per batch by
the daily kill-switch. Component B (LinUCB/XGBoost) is local compute only.

## Two-way Telegram bot

A second workflow (`.github/workflows/investigator-bot.yml`) polls Telegram
every 5 minutes and dispatches commands from the whitelisted chat. No webhook,
no server - runs on the same free tier as the scan. Anything from other chats
is silently ignored.

| Command | Effect |
|---|---|
| `/report` | full PDF report (from `report.html`) + master CSV as document attachments |
| `/scan` | current top-3-per-chain digest built from the master table |
| `/top [chain] [N]` | top-N leads for one chain (default: ethereum, 10; N up to 25) |
| `/wallet <address>` | details for one wallet from the master |
| `/stats` | row counts per chain + max actionability + signal counts |
| `/help`, `/start` | list of commands |

Every scheduled scan also attaches the PDF and the CSV to the digest, so the
phone receives a real professional deliverable and a ranked pool for follow-up.
The PDF is built by wrapping the notebook-produced `report.html` with
`weasyprint` - no content is re-authored, only the delivery format changes.

Latency: up to 5 min (cron interval; GitHub can add a few more minutes at peak).
Persistence: none - the bot uses Telegram's native offset acknowledgement, so
Telegram holds unprocessed messages for 24 h and re-delivers them next poll.

## Phone delivery (runs even when your PC is off)

The scan runs on **GitHub Actions** (free, always-on cron), publishes dossiers to
**GitHub Pages**, and pushes a digest to your phone via a **Telegram bot**. The
whole thing works with zero secrets (offline `mock` provider + dry-run notify);
add secrets to make it real.

```
GitHub Actions (cron 2x/day) -> run_nightly -> publish_site -> GitHub Pages
                                            \-> notify -------> Telegram -> phone
```

**One-time setup:**

1. **Telegram bot** - in Telegram, message `@BotFather` -> `/newbot` -> copy the
   **bot token**. Then message your new bot once, open
   `https://api.telegram.org/bot<TOKEN>/getUpdates`, and copy your **chat id**.
2. **GitHub repo -> Settings -> Secrets and variables -> Actions**:
   - Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
   - For real dossiers also add secret `ANTHROPIC_API_KEY`, secret `GCP_SA_KEY`
     (a BigQuery service-account JSON), and variable `BQ_PROJECT` +
     variable `INVESTIGATOR_LLM_PROVIDER=anthropic`.
3. **GitHub repo -> Settings -> Pages -> Source: GitHub Actions.**
4. Push, then **Actions -> Investigator scan -> Run workflow** to test now (or
   wait for the 06:00 / 18:00 UTC cron).

Change the cadence in `.github/workflows/investigator-scan.yml` (the two `cron`
lines). Other channels are built in: set repo variable `NOTIFY_CHANNEL=email`
(with `SMTP_*` / `NOTIFY_EMAIL_TO`) or `=ntfy` (with `NTFY_TOPIC`).

Local test of just the phone push (dry-run prints the message):

```bash
python -m investigator.notify           # no token -> prints what it would send
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python -m investigator.notify
```

## Ethics

Dossiers are investigative leads. Each section carries the "research lead - not a
finding of guilt" notice, each claim cites a tool observation, and the full
reasoning trace ships with every dossier. No real-world identity attribution -
that stays with authorised enforcement via a KYC subpoena to the exchange.
