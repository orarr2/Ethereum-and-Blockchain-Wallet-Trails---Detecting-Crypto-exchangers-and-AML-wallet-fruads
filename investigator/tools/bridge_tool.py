"""
detect_bridge - articulation-point test for one address over the sub-graph.

Wraps `bridge_wallets.find_bridge_wallets` (block-cut-tree implementation) on
the sub-graph accumulated by graph_expand, then reads off whether THIS address
is an articulation point and, if so, how many components it holds together.
No new BigQuery scan.
"""
from __future__ import annotations

from .base import Context, Tool, validate

_IN_SCHEMA = {
    "type": "object",
    "properties": {"address": {"type": "string"}, "chain": {"type": "string"}},
    "required": ["address"],
    "additionalProperties": False,
}
_OUT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_bridge": {"type": "boolean"},
        "components_bridged": {"type": "integer"},
        "component_sizes": {"type": "array"},
        "note": {"type": "string"},
    },
    "required": ["is_bridge", "components_bridged"],
}


class DetectBridgeTool(Tool):
    name = "detect_bridge"
    description = ("Test whether the address is an articulation point (its "
                   "removal disconnects the sub-graph) and report how many "
                   "components it bridges. Reads the context graph; no new query.")
    input_schema = _IN_SCHEMA
    output_schema = _OUT_SCHEMA

    def run(self, args: dict, ctx: Context) -> dict:
        validate(args, self.input_schema, "detect_bridge.input")
        addr = str(args["address"])
        if ctx.edges is None or not len(ctx.edges):
            result = {"is_bridge": False, "components_bridged": 0, "component_sizes": [],
                      "note": "no sub-graph in context; call graph_expand first."}
            validate(result, self.output_schema, "detect_bridge.output")
            return result

        from bridge_wallets import find_bridge_wallets
        g = ctx.subgraph()
        bridges = find_bridge_wallets(g, min_component_size=2)

        row = bridges[bridges["wallet"].astype(str) == addr] if len(bridges) else bridges
        if len(row):
            r = row.iloc[0]
            result = {"is_bridge": True,
                      "components_bridged": int(r["components_bridged"]),
                      "component_sizes": list(r["component_sizes"]),
                      "note": f"articulation point in a {g.number_of_nodes()}-node sub-graph"}
        else:
            result = {"is_bridge": False, "components_bridged": 0, "component_sizes": [],
                      "note": f"not an articulation point in a {g.number_of_nodes()}-node sub-graph"}
        validate(result, self.output_schema, "detect_bridge.output")
        return result
