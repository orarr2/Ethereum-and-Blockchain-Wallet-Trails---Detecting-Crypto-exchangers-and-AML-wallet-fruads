"""
Deterministic mock plan for the offline LLM provider.

Given the running conversation, decide the next action so the ReAct loop walks
the full mandatory investigation once and then stops. This lets the entire
layer be tested end-to-end with `INVESTIGATOR_LLM_PROVIDER=mock` - no API key,
no vendor SDK, no reasoning, just a fixed auditable path over the real tools.

The plan:
  1) call each data tool once: graph_expand, enrich, detect_mixer,
     detect_bridge, search_ofac, search_nbctf
  2) write_case_note for sections 1..8, each citing a prior tool call
  3) stop with reason "sufficient_evidence"
"""
from __future__ import annotations

import re

from .llm_client import ChatResult

# order matters: this is the data-gathering phase
_DATA_TOOLS = ["graph_expand", "enrich", "detect_mixer", "detect_bridge",
               "search_ofac", "search_nbctf"]

# (section title, tool name whose call_id we cite)
_SECTION_PLAN = [
    ("Summary", "graph_expand"),
    ("Graph trace", "graph_expand"),
    ("Enrichment findings", "enrich"),
    ("Mixer signals", "detect_mixer"),
    ("Bridge signals", "detect_bridge"),
    ("Cross-chain hops", "graph_expand"),
    ("OFAC / NBCTF proximity", "search_ofac"),
    ("Risk conclusion", "search_nbctf"),
]

_SECTION_TEXT = {
    "Summary": ("Automated offline dossier for {addr} on {chain}. Fan-in / "
                "counterparty structure summarised from the graph_expand "
                "observation. Treat as a starting point for manual review."),
    "Graph trace": ("Neighbour set retrieved via graph_expand (see cited call). "
                    "Directions and per-edge values are in the evidence table."),
    "Enrichment findings": ("Labels / ENS / sanction-oracle status as returned by "
                            "the enrich tool for this address."),
    "Mixer signals": ("Mixer-shape heuristics evaluated over the retrieved "
                      "sub-graph (uniform outputs / peel chain / in==out)."),
    "Bridge signals": ("Articulation-point test over the retrieved sub-graph "
                       "(block-cut tree). See cited detect_bridge call."),
    "Cross-chain hops": ("No cross-chain correlation was performed beyond the "
                         "same-hex check; none asserted here."),
    "OFAC / NBCTF proximity": ("Direct and N-hop proximity to OFAC / NBCTF seed "
                               "sets, per the cited search calls."),
    "Risk conclusion": ("Evidence-only summary. This is a research lead, not a "
                        "determination. All signals above are investigative, "
                        "never proof."),
}


def _seed_from_messages(messages: list) -> tuple[str, str]:
    """Recover (address, chain) from the first user message text."""
    for m in messages:
        content = m.get("content")
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        addr = re.search(r"address=(\S+)", text)
        chain = re.search(r"chain=(\w+)", text)
        if addr and chain:
            return addr.group(1), chain.group(1)
    return "unknown", "ethereum"


def _scan_calls(messages: list) -> tuple[dict, list]:
    """Return (name->first_call_id from assistant turns, list of written section
    titles). Section titles are read back from the tool_use inputs of prior
    write_case_note calls."""
    name_to_id: dict = {}
    written: list = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, cid = block.get("name"), block.get("id")
            name_to_id.setdefault(name, cid)
            if name == "write_case_note":
                sec = (block.get("input") or {}).get("section")
                if sec:
                    written.append(sec)
    return name_to_id, written


def next_mock_action(system: str, messages: list, tools: list) -> ChatResult:
    addr, chain = _seed_from_messages(messages)
    name_to_id, written = _scan_calls(messages)
    usage = {"in": 400, "out": 120}   # nominal, so the token budget still ticks

    # phase 1: gather data
    for tool in _DATA_TOOLS:
        if tool not in name_to_id:
            args = {"address": addr}
            if tool == "graph_expand":
                args = {"address": addr, "chain": chain, "hops": 1,
                        "direction": "both", "max_edges": 200}
            elif tool in ("detect_mixer", "detect_bridge"):
                args = {"address": addr, "chain": chain}
            return ChatResult(
                text=f"(mock) gathering: {tool}",
                tool_calls=[{"id": f"mock_{tool}", "name": tool, "args": args}],
                stop_reason="tool_use", usage=usage)

    # phase 2: write sections 1..8
    for title, cite_tool in _SECTION_PLAN:
        if title not in written:
            cid = name_to_id.get(cite_tool) or next(iter(name_to_id.values()), "mock_graph_expand")
            content = _SECTION_TEXT[title].format(addr=addr, chain=chain)
            return ChatResult(
                text=f"(mock) writing section: {title}",
                tool_calls=[{"id": f"mock_note_{len(written)}", "name": "write_case_note",
                             "args": {"section": title, "content": content, "citations": [cid]}}],
                stop_reason="tool_use", usage=usage)

    # phase 3: done
    return ChatResult(text="(mock) sufficient_evidence", tool_calls=[],
                      stop_reason="end_turn", usage=usage)
