"""
Dossier assembler + validators.

Turns the accumulated sections and tool-call trace into a single markdown file
plus a companion `.trace.json`. Enforces the citation discipline: every
informative section must anchor to a `[TOOL:n]` in the trace, and every
section header carries the mandatory "research lead" notice.

`[TOOL:n]` numbering: tool calls in the trace are numbered 1..N in call order;
a section's stored citation ids are rendered as those anchors so a reader can
map any claim back to the exact observation in Appendix A.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import config as C

_NOTICE = "> " + C.RESEARCH_LEAD_NOTICE

_EXPLORER = {
    "ethereum": "https://etherscan.io/address/{}",
    "tron": "https://tronscan.org/#/address/{}",
    "bitcoin": "https://mempool.space/address/{}",
    "zcash": "https://zcashblockexplorer.com/address/{}",
}


class DossierValidationError(Exception):
    pass


def _anchor_map(trace: list) -> dict:
    """call_id -> '[TOOL:n]' in call order."""
    return {c.get("call_id"): f"[TOOL:{i+1}]" for i, c in enumerate(trace) if c.get("call_id")}


def _render_citations(cids: list, anchors: dict) -> str:
    if not cids:
        return ""
    return " ".join(anchors.get(c, "[TOOL:?]") for c in cids)


def validate_sections(sections: list, optional_titles: set) -> list:
    """Return a list of validation problems (empty == valid)."""
    problems = []
    for s in sections:
        title = s.get("section", "")
        if title in optional_titles:
            continue
        if not s.get("citations"):
            problems.append(f"section '{title}' has no citation")
    return problems


def _evidence_rows(trace: list, chain: str) -> list:
    rows = []
    for c in trace:
        if c.get("error"):
            continue
        tool = c.get("tool", "")
        args = c.get("args", {}) or {}
        res = c.get("result", {}) or {}
        addr = args.get("address", "")
        value = _short_result(tool, res)
        src = ""
        if tool == "search_ofac":
            src = res.get("list_source", "")
        elif tool == "search_nbctf" and res.get("url"):
            src = res.get("url", "")
        elif addr and chain in _EXPLORER:
            src = _EXPLORER[chain].format(addr)
        rows.append({"address": addr, "tool": tool, "value": value, "source": src})
    return rows


def _short_result(tool: str, res: dict) -> str:
    if tool == "graph_expand":
        return f"{res.get('n_edges', 0)} edges / {res.get('n_nodes', 0)} nodes" + \
               (" (truncated)" if res.get("truncated") else "")
    if tool == "enrich":
        bits = []
        if res.get("label_name"):
            bits.append(f"label={res['label_name']}")
        if res.get("ens_name"):
            bits.append(f"ens={res['ens_name']}")
        if res.get("chainalysis_sanctioned"):
            bits.append("chainalysis_sanctioned=True")
        return ", ".join(bits) or "no labels"
    if tool == "detect_mixer":
        return f"mixer_like={res.get('is_mixer_like')} ({len(res.get('signals', []))} signals)"
    if tool == "detect_bridge":
        return f"bridge={res.get('is_bridge')} comps={res.get('components_bridged', 0)}"
    if tool == "search_ofac":
        return f"hit={res.get('hit')} hops={res.get('hops_to_hit')}"
    if tool == "search_nbctf":
        return f"hit={res.get('hit')} {res.get('affiliation', '')}".strip()
    if tool == "write_case_note":
        return f"section '{res.get('section', '')}'"
    return json.dumps(res)[:80]


def assemble(address: str, chain: str, sections: list, trace: list,
             meta: dict, optional_titles: set) -> str:
    anchors = _anchor_map(trace)
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    partial = meta.get("partial", False)
    status = ("PARTIAL - budget exhausted (%s)" % meta.get("stop_reason", "")) if partial else "complete"

    lines = []
    lines.append(f"# Investigator dossier - `{address}` ({chain})")
    lines.append("")
    lines.append(_NOTICE)
    lines.append("")
    lines.append(f"- Generated: {ts}")
    lines.append(f"- Model: {meta.get('provider', '')}/{meta.get('model', '')}")
    lines.append(f"- Status: **{status}**")
    lines.append(f"- Stop reason: {meta.get('stop_reason', '')}")
    b = meta.get("budget", {})
    if b:
        lines.append(f"- Budget: {b.get('calls', {}).get('used', 0)}/{b.get('calls', {}).get('cap', 0)} calls, "
                     f"${b.get('bq_usd', {}).get('used', 0):.4f}/{b.get('bq_usd', {}).get('cap', 0)} BQ, "
                     f"{b.get('tokens', {}).get('total', 0)}/{b.get('tokens', {}).get('cap', 0)} tokens")
    lines.append("")

    for i, s in enumerate(sections, 1):
        cite = _render_citations(s.get("citations", []), anchors)
        lines.append(f"## {i}. {s.get('section', '')}")
        lines.append(_NOTICE)
        lines.append("")
        lines.append(s.get("content", "").strip())
        if cite:
            lines.append("")
            lines.append(f"_Citations: {cite}_")
        lines.append("")

    # evidence table
    rows = _evidence_rows(trace, chain)
    lines.append("## Evidence table")
    lines.append("")
    lines.append("| # | address | tool | value | source |")
    lines.append("|---|---|---|---|---|")
    for n, r in enumerate(rows, 1):
        src = f"[link]({r['source']})" if r["source"] else ""
        lines.append(f"| {n} | `{r['address']}` | {r['tool']} | {r['value']} | {src} |")
    lines.append("")

    # appendix
    lines.append("## Appendix A - Full reasoning trace")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(trace, indent=2, default=str))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def write(address: str, chain: str, sections: list, trace: list, meta: dict,
          optional_titles: set, dossiers_dir: Path | None = None) -> dict:
    """Assemble, validate, and write the dossier. Returns paths + validation."""
    dossiers_dir = Path(dossiers_dir) if dossiers_dir else C.DOSSIERS_DIR
    out_dir = dossiers_dir / chain
    out_dir.mkdir(parents=True, exist_ok=True)

    problems = validate_sections(sections, optional_titles)
    md = assemble(address, chain, sections, trace, meta, optional_titles)

    safe = address.replace("/", "_")
    md_path = out_dir / f"{safe}.md"
    trace_path = out_dir / f"{safe}.trace.json"
    md_path.write_text(md, encoding="utf-8")
    trace_path.write_text(json.dumps({"address": address, "chain": chain,
                                      "meta": meta, "trace": trace}, indent=2, default=str),
                          encoding="utf-8")
    return {"md_path": md_path, "trace_path": trace_path,
            "validation_problems": problems, "valid": not problems}
