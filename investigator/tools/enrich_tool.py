"""
enrich - identity enrichment via the base project's pluggable Enricher.

Wraps `enrichment.Enricher` (public Etherscan labels, Chainalysis sanction
oracle, ENS reverse resolution). All free sources; no paid API is touched.
Enricher is built once and cached on the context.
"""
from __future__ import annotations

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
        "labels": {"type": "array"},
        "label_name": {"type": "string"},
        "ens_name": {"type": "string"},
        "chainalysis_sanctioned": {"type": "boolean"},
        "sources": {"type": "array"},
    },
    "required": ["chainalysis_sanctioned"],
}


class EnrichTool(Tool):
    name = "enrich"
    description = ("Look up public labels, ENS reverse name, and the Chainalysis "
                   "on-chain sanction flag for one address. Free sources only.")
    input_schema = _IN_SCHEMA
    output_schema = _OUT_SCHEMA

    def _enricher(self, ctx: Context):
        if ctx.enricher is None:
            from enrichment import Enricher
            ctx.enricher = Enricher()
        return ctx.enricher

    def run(self, args: dict, ctx: Context) -> dict:
        validate(args, self.input_schema, "enrich.input")
        addr = args["address"]
        try:
            recs = self._enricher(ctx).enrich([addr])
        except Exception as e:
            # enrichment is best-effort; a network failure is not fatal
            return {"labels": [], "label_name": "", "ens_name": "",
                    "chainalysis_sanctioned": False, "sources": [],
                    "note": f"enrichment failed: {type(e).__name__}"}
        rec = recs.get(addr, {}) or {}
        result = {
            "labels": list(rec.get("label_tags", [])),
            "label_name": rec.get("label_name", "") or "",
            "ens_name": rec.get("ens_name", "") or "",
            "chainalysis_sanctioned": bool(rec.get("chainalysis_sanctioned", False)),
            "sources": list(rec.get("_sources", [])),
        }
        validate(result, self.output_schema, "enrich.output")
        return result
