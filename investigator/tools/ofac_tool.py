"""
search_ofac - direct and N-hop proximity to OFAC-sanctioned addresses.

Seed sets come from the same community-maintained list the notebook uses
(`0xB10C/ofac-sanctioned-digital-currency-addresses`), fetched once and cached
on the context. Direct membership plus a bounded breadth-first search over the
already-fetched sub-graph gives `hops_to_hit`.

Note (documented, not a bug): OFAC USDT addresses are frozen by Tether and thus
mostly dormant on-chain, so a graph-proximity miss is the common and expected
result. The behavioural CTF signal (terror_signals) is the intended companion.
"""
from __future__ import annotations

import json
import urllib.request

from .base import Context, Tool, validate

_IN_SCHEMA = {
    "type": "object",
    "properties": {"address": {"type": "string"}},
    "required": ["address"],
    "additionalProperties": False,
}
_OUT_SCHEMA = {
    "type": "object",
    "properties": {
        "hit": {"type": "boolean"},
        "hops_to_hit": {"type": ["integer", "null"]},
        "list_source": {"type": "string"},
    },
    "required": ["hit", "hops_to_hit", "list_source"],
}

_OFAC_URL = ("https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/"
             "lists/sanctioned_addresses_{}.json")
_LIST_SOURCE = "0xB10C/ofac-sanctioned-digital-currency-addresses"


def _fetch(sym: str) -> list:
    try:
        with urllib.request.urlopen(_OFAC_URL.format(sym), timeout=30) as r:
            return json.load(r)
    except Exception:
        return []


def _load_ofac_sets(ctx: Context) -> dict:
    if ctx.ofac_sets is not None:
        return ctx.ofac_sets
    eth = {a.lower() for a in _fetch("ETH")} | {a.lower() for a in _fetch("USDT")}
    btc = set(_fetch("XBT"))                      # case-sensitive
    # tron base58 -> hex, reuse the notebook's decoder shape
    tron = set()
    _b58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    for a in _fetch("TRX"):
        try:
            n = 0
            for ch in a:
                n = n * 58 + _b58.index(ch)
            tron.add("0x" + n.to_bytes(25, "big")[1:21].hex())
        except Exception:
            continue
    ctx.ofac_sets = {"ethereum": eth, "tron": tron, "bitcoin": btc, "_all": eth | tron | btc}
    return ctx.ofac_sets


class SearchOFACTool(Tool):
    name = "search_ofac"
    description = ("Check an address for direct OFAC listing and for N-hop "
                   "proximity to any OFAC address within the fetched sub-graph. "
                   "Returns hops_to_hit (null if no hit within budget).")
    input_schema = _IN_SCHEMA
    output_schema = _OUT_SCHEMA

    def run(self, args: dict, ctx: Context) -> dict:
        validate(args, self.input_schema, "search_ofac.input")
        raw = str(args["address"])
        sets = _load_ofac_sets(ctx)
        sanctioned_all = sets["_all"]

        def _match(a: str) -> bool:
            return a in sanctioned_all or a.lower() in sanctioned_all

        if _match(raw):
            result = {"hit": True, "hops_to_hit": 0, "list_source": _LIST_SOURCE}
            validate(result, self.output_schema, "search_ofac.output")
            return result

        # bounded BFS over the sub-graph
        hops_to_hit = None
        if ctx.edges is not None and len(ctx.edges):
            import networkx as nx
            g = ctx.subgraph()
            if raw in g:
                max_hops = max(1, ctx.budget.hops)
                lengths = nx.single_source_shortest_path_length(g, raw, cutoff=max_hops)
                for node, dist in lengths.items():
                    if dist > 0 and _match(str(node)):
                        hops_to_hit = dist if hops_to_hit is None else min(hops_to_hit, dist)

        result = {"hit": hops_to_hit is not None, "hops_to_hit": hops_to_hit,
                  "list_source": _LIST_SOURCE}
        validate(result, self.output_schema, "search_ofac.output")
        return result
