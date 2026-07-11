"""
detect_mixer - mixer-shape heuristics over the accumulated sub-graph.

No new BigQuery scan: this reads the edges graph_expand already fetched onto the
context. Three cheap, explainable heuristics, each producing a named signal with
a 0..1 score and its evidence:

  uniform_outputs - outgoing values cluster around a few fixed denominations
                    (the Tornado-Cash signature: 0.1 / 1 / 10 / 100).
  peel_chain      - repeated "one large change output + one small spend" shape.
  balanced_io     - fan-in count ~ fan-out count with high throughput (a pass-
                    through service, not an accumulator).

None of these prove mixing; they are lead signals only.
"""
from __future__ import annotations

from collections import Counter

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
        "is_mixer_like": {"type": "boolean"},
        "signals": {"type": "array"},
        "note": {"type": "string"},
    },
    "required": ["is_mixer_like", "signals"],
}

# common fixed mixer denominations (ETH-side; used as a shape prior only)
_DENOMS = [0.1, 1.0, 10.0, 100.0, 1000.0]


def _near_denom(v: float, tol: float = 0.02) -> bool:
    for d in _DENOMS:
        if d > 0 and abs(v - d) / d <= tol:
            return True
    return False


class DetectMixerTool(Tool):
    name = "detect_mixer"
    description = ("Score mixer-like shape (uniform denominations, peel chain, "
                   "balanced high-throughput in/out) from the already-fetched "
                   "sub-graph. Reads the context graph; issues no new query.")
    input_schema = _IN_SCHEMA
    output_schema = _OUT_SCHEMA

    def run(self, args: dict, ctx: Context) -> dict:
        validate(args, self.input_schema, "detect_mixer.input")
        addr = str(args["address"])
        edges = ctx.edges
        if edges is None or not len(edges):
            result = {"is_mixer_like": False, "signals": [],
                      "note": "no sub-graph in context; call graph_expand first."}
            validate(result, self.output_schema, "detect_mixer.output")
            return result

        out_edges = edges[edges["from_address"].astype(str) == addr]
        in_edges = edges[edges["to_address"].astype(str) == addr]
        signals = []

        # (1) uniform outputs
        out_vals = [float(v) for v in out_edges["value"].dropna().tolist() if v and v > 0]
        if out_vals:
            frac_denom = sum(_near_denom(v) for v in out_vals) / len(out_vals)
            if frac_denom >= 0.5:
                signals.append({"name": "uniform_outputs", "score": round(frac_denom, 3),
                                "evidence": f"{int(frac_denom*len(out_vals))}/{len(out_vals)} "
                                            "outgoing values near fixed denominations"})

        # (2) peel chain: many outs where the value distribution is bimodal
        #     (one big + one small), approximated by a high max/median ratio
        if len(out_vals) >= 4:
            sv = sorted(out_vals)
            med = sv[len(sv) // 2]
            ratio = (max(sv) / med) if med > 0 else 0
            if ratio >= 20:
                signals.append({"name": "peel_chain", "score": round(min(1.0, ratio / 100), 3),
                                "evidence": f"max/median outgoing value ratio ~{ratio:.0f}"})

        # (3) balanced high-throughput
        n_in, n_out = len(in_edges), len(out_edges)
        if n_in >= 5 and n_out >= 5:
            bal = 1.0 - abs(n_in - n_out) / (n_in + n_out)
            if bal >= 0.7:
                signals.append({"name": "balanced_io", "score": round(bal, 3),
                                "evidence": f"in={n_in} out={n_out}, balance {bal:.2f}"})

        is_mixer = len(signals) >= 2 or any(s["score"] >= 0.8 for s in signals)
        result = {"is_mixer_like": bool(is_mixer), "signals": signals,
                  "note": f"evaluated over {len(edges)} edges in context"}
        validate(result, self.output_schema, "detect_mixer.output")
        return result
