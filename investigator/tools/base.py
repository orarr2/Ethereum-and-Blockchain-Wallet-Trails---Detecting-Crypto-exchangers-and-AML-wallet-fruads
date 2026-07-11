"""
Tool base classes, shared investigation context, and the registry.

Every tool:
  - declares a JSON Schema for its input and output;
  - validates its input before running and its output after (a schema miss is
    surfaced to the agent as a retryable error, never a crash);
  - reads and writes the shared `Context`, which carries the budget, lazily
    built clients, the accumulated sub-graph, the OFAC/NBCTF seed sets, and the
    dossier-in-progress.

The accumulating sub-graph is the key design choice: `graph_expand` fetches
edges from BigQuery and stores them on the context; `detect_mixer`,
`detect_bridge`, and `search_ofac` then analyse that already-paid-for sub-graph
instead of each issuing their own BigQuery scan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from ..budget import Budget


class ToolError(Exception):
    """Raised on a validation miss or a recoverable tool failure. The agent
    catches it and feeds the message back to the model as a tool_result error
    the model can retry from."""

    def __init__(self, message: str, kind: str = "tool_error"):
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---------------------------------------------------------------------------
# Shared investigation context
# ---------------------------------------------------------------------------
@dataclass
class Context:
    address: str
    chain: str
    budget: Budget

    # lazily built shared clients / data
    bq_client: Any = None
    enricher: Any = None
    ofac_sets: Optional[dict] = None       # {chain: set(addr)}
    nbctf: Optional[dict] = None           # {addr_lower: {order, affiliation, url}}
    master: Optional[pd.DataFrame] = None  # suspicious_wallets_master.csv (for cross-chain)

    # accumulating evidence
    edges: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(
        columns=["from_address", "to_address", "value", "tx_count", "first_ts", "last_ts"]))
    seen_nodes: set = field(default_factory=set)

    # dossier-in-progress: list of {section, content, citations}
    sections: list = field(default_factory=list)
    # trace of tool calls (populated by the agent); tools read it for citation checks
    trace: list = field(default_factory=list)

    def add_edges(self, df: pd.DataFrame) -> None:
        if df is None or not len(df):
            return
        self.edges = pd.concat([self.edges, df], ignore_index=True)
        self.seen_nodes.update(df["from_address"].astype(str))
        self.seen_nodes.update(df["to_address"].astype(str))

    def subgraph(self):
        """Build an undirected networkx graph from the accumulated edges."""
        import networkx as nx
        g = nx.Graph()
        for r in self.edges.itertuples(index=False):
            g.add_edge(str(r.from_address), str(r.to_address))
        return g

    def known_call_ids(self) -> set:
        return {c.get("call_id") for c in self.trace if c.get("call_id")}


# ---------------------------------------------------------------------------
# Tool base
# ---------------------------------------------------------------------------
class Tool:
    name: str = "base"
    description: str = ""
    input_schema: dict = {"type": "object", "properties": {}, "additionalProperties": True}
    output_schema: dict = {"type": "object"}

    def estimated_cost_usd(self, args: dict, ctx: Context) -> float:
        """Projected BigQuery dollars for this call. Non-BQ tools return 0."""
        return 0.0

    def run(self, args: dict, ctx: Context) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- convenience for the agent to expose the tool to the model ---------
    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


def validate(instance: dict, schema: dict, what: str) -> None:
    """Validate `instance` against `schema`; raise ToolError on a miss.

    Uses jsonschema if installed; otherwise falls back to a minimal required-key
    check so the layer still runs without the optional dependency.
    """
    try:
        import jsonschema
    except ImportError:
        for key in schema.get("required", []):
            if key not in (instance or {}):
                raise ToolError(f"{what}: missing required field '{key}'", kind="schema_error")
        return
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as e:  # type: ignore[attr-defined]
        raise ToolError(f"{what}: {e.message}", kind="schema_error") from e


def build_registry(ctx: Context) -> dict:
    """Instantiate every tool and return {name: tool}. Import is local so a
    missing optional dep in one tool never blocks the others."""
    from .graph_tool import GraphExpandTool
    from .enrich_tool import EnrichTool
    from .mixer_tool import DetectMixerTool
    from .bridge_tool import DetectBridgeTool
    from .ofac_tool import SearchOFACTool
    from .nbctf_tool import SearchNBCTFTool
    from .writer_tool import WriteCaseNoteTool

    tools = [GraphExpandTool(), EnrichTool(), DetectMixerTool(), DetectBridgeTool(),
             SearchOFACTool(), SearchNBCTFTool(), WriteCaseNoteTool()]
    return {t.name: t for t in tools}
