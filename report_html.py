"""
Self-contained HTML triage report.

Generates one offline `report.html` file with:
  - KPI cards
  - Top actionable leads
  - Bridge wallets (articulation points across the graph)
  - Country breakdown (interesting countries only)
  - Co-funding clusters
  - Exchange anchors table
  - NBCTF hits with provenance (order + affiliation)

No external assets. No JS build. Sends everything inline so the file can be
mailed, saved to disk, or opened by any browser without a server.
"""
from __future__ import annotations
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Dict
import pandas as pd


def _fmt_int(x):
    try: return f"{int(x):,}"
    except Exception: return str(x)


def _fmt_money(x):
    try: return f"${float(x):,.0f}"
    except Exception: return str(x)


def _fmt_pct(x, total):
    try: return f"{100*float(x)/max(1,float(total)):.1f}%"
    except Exception: return "-"


def _scan_url(row):
    w = str(row.get("wallet", ""))
    chain = str(row.get("chain", ""))
    if chain == "ethereum": return f"https://etherscan.io/address/{w}"
    if chain == "tron":     return f"https://tronscan.org/#/address/{w}"
    if chain == "bitcoin":  return f"https://mempool.space/address/{w}"
    if chain == "zcash":    return f"https://zcashblockexplorer.com/address/{w}"
    return ""


def _table(df: pd.DataFrame, cols: list, formatters: dict | None = None,
           row_class_fn=None, add_scan: bool = True) -> str:
    formatters = formatters or {}
    if df is None or not len(df):
        return '<p class="muted">No rows.</p>'
    cols_present = [c for c in cols if c in df.columns]
    header_cells = "".join(f"<th>{escape(c)}</th>" for c in cols_present)
    if add_scan:
        header_cells += "<th>explorer</th>"
    body_rows = []
    for _, r in df.iterrows():
        cls = row_class_fn(r) if row_class_fn else ""
        cells = []
        for c in cols_present:
            v = r[c]
            if c in formatters:
                v = formatters[c](v)
            elif isinstance(v, (list, tuple, set)):
                v = ", ".join(str(x) for x in v)
            elif isinstance(v, bool):
                v = "yes" if v else ""
            elif pd.isna(v):
                v = ""
            cells.append(f"<td>{escape(str(v))}</td>")
        if add_scan:
            url = _scan_url(r)
            link = f'<a href="{escape(url)}" target="_blank">open</a>' if url else ""
            cells.append(f'<td>{link}</td>')
        body_rows.append(f'<tr class="{cls}">{"".join(cells)}</tr>')
    return f'<table><thead><tr>{header_cells}</tr></thead><tbody>{"".join(body_rows)}</tbody></table>'


CSS = """
:root { --bg:#0e1116; --panel:#161b22; --border:#2d333b; --text:#e6edf3; --muted:#8b949e;
        --accent:#58a6ff; --good:#3fb950; --bad:#f85149; --warn:#d29922; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
header { padding:24px 32px; border-bottom:1px solid var(--border); background:var(--panel); }
h1 { margin:0 0 4px 0; font-size:22px; }
h2 { font-size:16px; margin:32px 0 12px 0; padding-bottom:6px; border-bottom:1px solid var(--border); color:var(--accent); }
.muted { color:var(--muted); font-size:12px; }
main { padding:24px 32px 48px; max-width:1400px; margin:0 auto; }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:8px; }
.kpi { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px 16px; }
.kpi .n { font-size:22px; font-weight:600; }
.kpi .l { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.4px; }
.wrap { overflow-x:auto; background:var(--panel); border:1px solid var(--border); border-radius:8px; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th, td { padding:8px 10px; text-align:left; border-bottom:1px solid var(--border); white-space:nowrap; }
th { background:#1c222b; font-weight:600; color:var(--muted); text-transform:uppercase; font-size:10.5px; letter-spacing:.3px; position:sticky; top:0; }
tr:hover td { background:#1a2029; }
tr.high td { background:rgba(63,185,80,0.06); }
tr.interesting td { background:rgba(210,153,34,0.10); }
tr.terror td { background:rgba(248,81,73,0.10); }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.footer { color:var(--muted); font-size:11px; padding-top:32px; }
.chips { display:flex; flex-wrap:wrap; gap:6px; }
.chip { padding:2px 8px; background:#22272e; border:1px solid var(--border); border-radius:12px; font-size:11px; }
"""


def build_report(master: pd.DataFrame,
                 bridges: pd.DataFrame | None = None,
                 country_summary: pd.DataFrame | None = None,
                 anchors: pd.DataFrame | None = None,
                 clusters: pd.DataFrame | None = None,
                 nbctf_meta: dict | None = None,
                 title: str = "Crypto-AML triage report",
                 out_path: str = "report.html") -> str:
    """Render an offline HTML triage report and return the written file path."""
    m = master.copy() if master is not None else pd.DataFrame()
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    n_total = len(m)
    n_chains = m["chain"].value_counts().to_dict() if "chain" in m else {}
    n_anchor = int(m.get("has_exchange_anchor", pd.Series([False]*n_total)).sum()) if n_total else 0
    n_bridge = int(m.get("is_bridge_wallet", pd.Series([False]*n_total)).sum()) if n_total else 0
    n_interesting = int(m.get("hits_interesting_country", pd.Series([False]*n_total)).sum()) if n_total else 0
    n_terror = int(m.get("risk_categories", pd.Series([[]]*n_total)).apply(
        lambda cs: "terror" in (cs or [])).sum()) if n_total else 0

    kpi_html = "".join(f'<div class="kpi"><div class="n">{_fmt_int(v)}</div><div class="l">{escape(k)}</div></div>'
                        for k, v in [
                            ("total candidates", n_total),
                            ("ethereum", n_chains.get("ethereum", 0)),
                            ("tron", n_chains.get("tron", 0)),
                            ("bitcoin", n_chains.get("bitcoin", 0)),
                            ("exchange-anchored", n_anchor),
                            ("bridge wallets", n_bridge),
                            ("interesting-country hits", n_interesting),
                            ("NBCTF/terror hits", n_terror),
                        ])

    def _row_cls(r):
        if "terror" in (r.get("risk_categories") or []): return "terror"
        if r.get("hits_interesting_country"): return "interesting"
        if r.get("has_exchange_anchor") and r.get("risk_score", 0) >= 60: return "high"
        return ""

    top_actionable = (m.sort_values(["actionability", "risk_score"], ascending=False).head(50)
                      if "actionability" in m.columns else m.head(50))
    actionable_html = _table(
        top_actionable,
        cols=["wallet", "chain", "risk_score", "actionability", "has_exchange_anchor",
              "anchor_exchange", "anchor_usdt", "country_codes",
              "is_bridge_wallet", "campaign_terror_score"],
        formatters={"anchor_usdt": _fmt_money},
        row_class_fn=_row_cls)

    bridges_html = _table(
        bridges.head(50) if bridges is not None else None,
        cols=["wallet", "chain", "components_bridged", "component_sizes",
              "total_bridged_size", "degree"],
        add_scan=True)

    country_html = _table(
        country_summary if country_summary is not None else None,
        cols=["country", "n_candidates", "is_interesting"],
        add_scan=False)

    clusters_html = _table(
        clusters.head(50) if clusters is not None else None,
        cols=["cluster_id", "cluster_size", "wallet"],
        add_scan=True)

    anchor_leads = None
    if "has_exchange_anchor" in m.columns:
        anchor_leads = (m[m["has_exchange_anchor"] == True]
                        .sort_values("actionability", ascending=False).head(50))
    anchors_html = _table(
        anchor_leads,
        cols=["wallet", "chain", "risk_score", "actionability",
              "anchor_exchange", "anchor_usdt", "anchor_links", "country_codes"],
        formatters={"anchor_usdt": _fmt_money},
        row_class_fn=_row_cls)

    terror_html = ""
    if nbctf_meta:
        rows = [{"address": k, "order": v.get("order", ""),
                 "affiliation": v.get("affiliation", ""),
                 "url": v.get("url", "")} for k, v in nbctf_meta.items()]
        terror_df = pd.DataFrame(rows)
        terror_html = _table(terror_df, cols=["address", "order", "affiliation", "url"], add_scan=False)

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{escape(title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style></head><body>
<header>
  <h1>{escape(title)}</h1>
  <div class="muted">Generated {escape(ts)} - research leads only, not findings of guilt.</div>
</header>
<main>
  <div class="kpis">{kpi_html}</div>

  <h2>Top-50 actionable leads (risk_score + exchange anchor)</h2>
  <div class="wrap">{actionable_html}</div>

  <h2>Bridge wallets - articulation points across the graph</h2>
  <p class="muted">Wallets whose removal disconnects two or more sub-networks - candidate common operators or nested exchangers.</p>
  <div class="wrap">{bridges_html}</div>

  <h2>Country breakdown (via exchange labels + NBCTF affiliation)</h2>
  <div class="wrap">{country_html}</div>

  <h2>Exchange anchors - direct KYC/subpoena targets</h2>
  <div class="wrap">{anchors_html}</div>

  <h2>Co-funding clusters</h2>
  <p class="muted">Candidates that share depositors - probable same-operator groups.</p>
  <div class="wrap">{clusters_html}</div>

  {"<h2>NBCTF terror-financing tagged addresses</h2><div class='wrap'>" + terror_html + "</div>" if terror_html else ""}

  <p class="footer">Public on-chain data only. High fan-in / low fan-out patterns and OFAC / NBCTF proximity are
  investigative signals, never proof. Identity attribution to a person is for authorised enforcement
  (subpoena to the exchange that holds the KYC).</p>
</main></body></html>"""

    Path(out_path).write_text(html, encoding="utf-8")
    return out_path
