"""
search_nbctf - lookup against the project's public NBCTF seizure-order address
set (`data/nbctf_addresses.json`).

Only the match result (hit / miss + affiliation + order + url) is returned to
the agent; the underlying JSON file is never sent to the LLM in bulk (spec
section 12, risk 5).
"""
from __future__ import annotations

import json

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
        "order": {"type": "string"},
        "affiliation": {"type": "string"},
        "url": {"type": "string"},
    },
    "required": ["hit"],
}


def _load_nbctf(ctx: Context) -> dict:
    if ctx.nbctf is not None:
        return ctx.nbctf
    from .. import config as C
    flat: dict = {}
    try:
        raw = json.loads(C.NBCTF_JSON.read_text(encoding="utf-8"))
    except Exception:
        ctx.nbctf = {}
        return ctx.nbctf
    for key, entries in raw.items():
        if key.startswith("_") or not isinstance(entries, list):
            continue
        for e in entries:
            addr = str(e.get("address", "")).strip()
            if addr:
                flat[addr.lower()] = {"order": e.get("order", ""),
                                      "affiliation": e.get("affiliation", ""),
                                      "url": e.get("url", "")}
    ctx.nbctf = flat
    return ctx.nbctf


class SearchNBCTFTool(Tool):
    name = "search_nbctf"
    description = ("Check an address against the public NBCTF administrative "
                   "seizure-order set. Returns affiliation/order/url on a hit. "
                   "Every match is an investigative lead, not proof of guilt.")
    input_schema = _IN_SCHEMA
    output_schema = _OUT_SCHEMA

    def run(self, args: dict, ctx: Context) -> dict:
        validate(args, self.input_schema, "search_nbctf.input")
        addr = str(args["address"]).strip().lower()
        meta = _load_nbctf(ctx).get(addr)
        if meta:
            result = {"hit": True, "order": meta.get("order", ""),
                      "affiliation": meta.get("affiliation", ""), "url": meta.get("url", "")}
        else:
            result = {"hit": False, "order": "", "affiliation": "", "url": ""}
        validate(result, self.output_schema, "search_nbctf.output")
        return result
