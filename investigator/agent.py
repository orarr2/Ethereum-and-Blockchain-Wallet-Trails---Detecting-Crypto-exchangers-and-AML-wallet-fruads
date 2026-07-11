"""
Investigator - the ReAct loop.

Given a seed (address, chain) and a Budget, the agent drives an LLM through a
tool-use loop: reason -> pick a tool -> observe -> fold back in. It gathers
graph / enrichment / mixer / bridge / OFAC / NBCTF evidence, then writes the
eight mandatory dossier sections (each cited) and stops. Any budget cap ends the
loop cleanly and produces a PARTIAL dossier.

The loop is provider-agnostic (see llm_client). With the `mock` provider it runs
fully offline on a fixed plan, so the whole layer is testable without a key.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import config as C
from . import dossier_writer
from .budget import Budget
from .llm_client import LLMClient
from .tools.base import Context, ToolError, build_registry

MANDATORY_SECTIONS = [
    "Summary", "Graph trace", "Enrichment findings", "Mixer signals",
    "Bridge signals", "Cross-chain hops", "OFAC / NBCTF proximity",
    "Risk conclusion",
]
OPTIONAL_CITATION_SECTIONS = {"Cross-chain hops"}

SYSTEM_PROMPT = f"""You are an autonomous crypto-AML investigator. You build a
structured, EVIDENCE-ONLY case dossier for a single seed wallet.

Hard rules:
- {C.RESEARCH_LEAD_NOTICE} Never state or imply guilt. You summarise signals; the
  analyst decides.
- Every informative dossier section MUST cite at least one prior tool call id.
  Call a data tool BEFORE writing the section that relies on it.
- Do real work with tools; do not invent observations. If a tool returns nothing,
  say so plainly.

Workflow:
1. Use `graph_expand` to pull the wallet's neighbourhood (this also populates the
   shared sub-graph the mixer/bridge/ofac tools analyse).
2. Use `enrich`, `detect_mixer`, `detect_bridge`, `search_ofac`, `search_nbctf`
   to gather signals.
3. Then write the dossier with `write_case_note`, one call per section, in this
   order: {", ".join(MANDATORY_SECTIONS)}.
   Provide `citations` = the tool call ids that support each section.
4. When all eight sections are written, STOP (end your turn without another tool
   call).

You operate under strict budgets (BigQuery dollars, tokens, hops, tool calls).
If you run low, prioritise writing the sections you can support, then stop."""


@dataclass
class DossierResult:
    address: str
    chain: str
    dossier_md_path: Path | None
    trace_json_path: Path | None
    partial: bool
    stop_reason: str
    bq_cost_usd: float
    token_cost: dict
    duration_s: float
    n_sections: int
    valid: bool
    validation_problems: list = field(default_factory=list)

    def to_index_entry(self) -> dict:
        return {
            "address": self.address, "chain": self.chain,
            "dossier": str(self.dossier_md_path) if self.dossier_md_path else None,
            "partial": self.partial, "stop_reason": self.stop_reason,
            "bq_cost_usd": round(self.bq_cost_usd, 6), "token_cost": self.token_cost,
            "duration_s": round(self.duration_s, 2), "n_sections": self.n_sections,
            "valid": self.valid,
        }


class Investigator:
    def __init__(self, llm: LLMClient | None = None, dossiers_dir: Path | None = None,
                 master: pd.DataFrame | None = None, verbose: bool = False):
        self.llm = llm or LLMClient()
        self.dossiers_dir = Path(dossiers_dir) if dossiers_dir else C.DOSSIERS_DIR
        self.master = master
        self.verbose = verbose

    def _log(self, *a):
        if self.verbose:
            print("[investigator]", *a)

    def investigate(self, address: str, chain: str,
                    budget: Budget | None = None) -> DossierResult:
        t0 = time.perf_counter()
        budget = budget or Budget.from_config()
        ctx = Context(address=address, chain=chain, budget=budget, master=self.master)
        registry = build_registry(ctx)
        tool_specs = [t.spec() for t in registry.values()]

        messages = [{
            "role": "user",
            "content": (f"Investigate this seed wallet. address={address} chain={chain}\n"
                        f"Build the dossier per your instructions, citing tool calls."),
        }]

        stop_reason = ""
        partial = False

        while True:
            done, reason = budget.exhausted()
            if done:
                stop_reason, partial = reason, True
                self._log("budget exhausted:", reason)
                break

            result = self.llm.chat(SYSTEM_PROMPT, messages, tool_specs)
            budget.charge_tokens(result.usage.get("in", 0), result.usage.get("out", 0))

            if not result.tool_calls:
                stop_reason = "sufficient_evidence" if _all_sections(ctx) else "model_stop"
                self._log("model stopped:", stop_reason)
                break

            # rebuild the assistant turn in Anthropic-native block form
            assistant_content = []
            if result.text:
                assistant_content.append({"type": "text", "text": result.text})
            for tc in result.tool_calls:
                assistant_content.append({"type": "tool_use", "id": tc["id"],
                                          "name": tc["name"], "input": tc["args"]})
            messages.append({"role": "assistant", "content": assistant_content})

            # execute each tool call, collect tool_result blocks
            tool_result_blocks = []
            for tc in result.tool_calls:
                budget.charge_call()
                name, args, call_id = tc["name"], tc.get("args", {}), tc["id"]
                tool = registry.get(name)
                rec = {"call_id": call_id, "tool": name, "args": args}
                if tool is None:
                    rec["error"] = f"unknown tool {name}"
                    ctx.trace.append(rec)
                    tool_result_blocks.append(_err_block(call_id, rec["error"]))
                    continue
                try:
                    out = tool.run(args, ctx)
                    rec["result"] = out
                    ctx.trace.append(rec)
                    tool_result_blocks.append(_ok_block(call_id, out))
                    self._log(f"{name} -> ok")
                except ToolError as e:
                    rec["error"] = e.message
                    ctx.trace.append(rec)
                    tool_result_blocks.append(_err_block(call_id, e.message))
                    self._log(f"{name} -> tool_error: {e.message}")
                except Exception as e:  # unexpected; keep the loop alive
                    msg = f"{type(e).__name__}: {e}"
                    rec["error"] = msg
                    ctx.trace.append(rec)
                    tool_result_blocks.append(_err_block(call_id, msg))
                    self._log(f"{name} -> exception: {msg}")

                # budget can be spent mid-batch (e.g. graph_expand BQ); stop early
                d2, r2 = budget.exhausted()
                if d2:
                    stop_reason, partial = r2, True
                    break

            messages.append({"role": "user", "content": tool_result_blocks})
            if partial:
                self._log("budget exhausted mid-turn:", stop_reason)
                break

        # finalize
        meta = {
            "provider": self.llm.provider, "model": self.llm.model,
            "partial": partial, "stop_reason": stop_reason or "complete",
            "budget": budget.snapshot(),
        }
        written = dossier_writer.write(address, chain, ctx.sections, ctx.trace, meta,
                                       OPTIONAL_CITATION_SECTIONS, self.dossiers_dir)
        return DossierResult(
            address=address, chain=chain,
            dossier_md_path=written["md_path"], trace_json_path=written["trace_path"],
            partial=partial, stop_reason=stop_reason or "complete",
            bq_cost_usd=budget.bq_spent,
            token_cost={"in": budget.tokens_in, "out": budget.tokens_out,
                        "total": budget.tokens_total},
            duration_s=time.perf_counter() - t0,
            n_sections=len(ctx.sections), valid=written["valid"],
            validation_problems=written["validation_problems"],
        )


def _all_sections(ctx: Context) -> bool:
    have = {s["section"] for s in ctx.sections}
    return all(sec in have for sec in MANDATORY_SECTIONS)


def _ok_block(call_id: str, out: dict) -> dict:
    import json
    return {"type": "tool_result", "tool_use_id": call_id,
            "content": [{"type": "text", "text": json.dumps(out, default=str)}]}


def _err_block(call_id: str, message: str) -> dict:
    return {"type": "tool_result", "tool_use_id": call_id, "is_error": True,
            "content": [{"type": "text", "text": f"ERROR: {message}"}]}
