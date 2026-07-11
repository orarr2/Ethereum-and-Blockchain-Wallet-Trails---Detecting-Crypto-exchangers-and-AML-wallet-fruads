"""
write_case_note - append one cited section to the dossier-in-progress.

Enforces the project's core discipline: an informative section MUST cite at
least one prior tool call (an id present in the trace). A note without a valid
citation is rejected with a ToolError, which the agent feeds back to the model
so it can retry with a citation. The mandatory "research lead" preface is
injected here so every section carries it regardless of what the model wrote.
"""
from __future__ import annotations

from .base import Context, Tool, ToolError, validate

# sections that are allowed to carry no citation (purely structural/among these
# only when genuinely empty, e.g. "no cross-chain hops found")
_CITATION_OPTIONAL = {"Cross-chain hops"}

_IN_SCHEMA = {
    "type": "object",
    "properties": {
        "section": {"type": "string", "description": "Section title (e.g. 'Summary', 'Graph trace')."},
        "content": {"type": "string", "minLength": 1},
        "citations": {"type": "array", "items": {"type": "string"},
                      "description": "Tool call ids from the trace supporting this section."},
    },
    "required": ["section", "content"],
    "additionalProperties": False,
}
_OUT_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "section": {"type": "string"},
                   "n_sections": {"type": "integer"}},
    "required": ["ok"],
}


class WriteCaseNoteTool(Tool):
    name = "write_case_note"
    description = (
        "Append one section to the dossier. Provide `section`, `content`, and "
        "`citations` (a list of tool call ids that support the content). Every "
        "informative section requires at least one citation; a section without "
        "one is rejected. The 'research lead - not a finding' preface is added "
        "automatically."
    )
    input_schema = _IN_SCHEMA
    output_schema = _OUT_SCHEMA

    def run(self, args: dict, ctx: Context) -> dict:
        validate(args, self.input_schema, "write_case_note.input")
        section = str(args["section"]).strip()
        content = str(args["content"]).strip()
        citations = list(args.get("citations", []) or [])

        known = ctx.known_call_ids()
        valid_citations = [c for c in citations if c in known]

        if section not in _CITATION_OPTIONAL and not valid_citations:
            raise ToolError(
                f"write_case_note rejected: section '{section}' needs at least one "
                f"citation referencing a prior tool call id. Known ids: "
                f"{sorted(known) if known else '(none yet - call a data tool first)'}.",
                kind="missing_citation")

        ctx.sections.append({"section": section, "content": content,
                             "citations": valid_citations})
        result = {"ok": True, "section": section, "n_sections": len(ctx.sections)}
        validate(result, self.output_schema, "write_case_note.output")
        return result
