"""
Provider-agnostic LLM interface.

The rest of the layer talks to `LLMClient.chat(...)` and never imports a vendor
SDK directly. The provider is chosen by env var (`INVESTIGATOR_LLM_PROVIDER`),
so swapping Anthropic <-> OpenAI <-> a local mock is a one-line change with no
code edits.

`chat()` returns a normalised `ChatResult`:
  - text        : any assistant free-text in the turn
  - tool_calls  : [{id, name, args}]  (empty when the model chose to stop)
  - stop_reason : "tool_use" | "end_turn" | "max_tokens" | ...
  - usage       : {"in": int, "out": int}

Canonical message format is Anthropic-native (role + content blocks), because
Anthropic is the default provider. The OpenAI and mock providers translate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatResult:
    text: str = ""
    tool_calls: list = field(default_factory=list)   # [{id, name, args}]
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=lambda: {"in": 0, "out": 0})
    raw: Any = None


class LLMClient:
    """Thin dispatcher over provider back-ends."""

    def __init__(self, provider: str | None = None, model: str | None = None,
                 api_key: str | None = None, max_tokens: int = 1500):
        from . import config as C
        self.provider = (provider or C.LLM_PROVIDER).lower()
        self.model = model or C.LLM_MODEL
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._client = None   # lazily constructed vendor client

    # -- public ------------------------------------------------------------
    def chat(self, system: str, messages: list, tools: list) -> ChatResult:
        if self.provider == "anthropic":
            return self._chat_anthropic(system, messages, tools)
        if self.provider == "openai":
            return self._chat_openai(system, messages, tools)
        if self.provider == "mock":
            return self._chat_mock(system, messages, tools)
        raise ValueError(f"Unknown INVESTIGATOR_LLM_PROVIDER={self.provider!r}")

    def describe(self) -> dict:
        return {"provider": self.provider, "model": self.model}

    # -- anthropic ---------------------------------------------------------
    def _anthropic(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "anthropic SDK not installed. `pip install anthropic` or set "
                    "INVESTIGATOR_LLM_PROVIDER=mock for an offline dry run."
                ) from e
            from . import config as C
            key = self._api_key or C.ANTHROPIC_API_KEY
            if not key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Put it in .env, or set "
                    "INVESTIGATOR_LLM_PROVIDER=mock for an offline dry run."
                )
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def _chat_anthropic(self, system: str, messages: list, tools: list) -> ChatResult:
        client = self._anthropic()
        anth_tools = [{"name": t["name"], "description": t["description"],
                       "input_schema": t["input_schema"]} for t in tools]
        resp = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=anth_tools,
            messages=messages,
        )
        text_parts, tool_calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "args": block.input})
        usage = {"in": getattr(resp.usage, "input_tokens", 0),
                 "out": getattr(resp.usage, "output_tokens", 0)}
        return ChatResult(text="\n".join(text_parts), tool_calls=tool_calls,
                          stop_reason=resp.stop_reason, usage=usage, raw=resp)

    # -- openai ------------------------------------------------------------
    def _openai(self):
        if self._client is None:
            try:
                import openai
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("openai SDK not installed. `pip install openai`.") from e
            from . import config as C
            key = self._api_key or C.OPENAI_API_KEY
            if not key:
                raise RuntimeError("OPENAI_API_KEY is not set.")
            self._client = openai.OpenAI(api_key=key)
        return self._client

    def _chat_openai(self, system: str, messages: list, tools: list) -> ChatResult:
        client = self._openai()
        oa_tools = [{"type": "function",
                     "function": {"name": t["name"], "description": t["description"],
                                  "parameters": t["input_schema"]}} for t in tools]
        oa_messages = [{"role": "system", "content": system}] + _to_openai_messages(messages)
        resp = client.chat.completions.create(
            model=self.model, max_tokens=self.max_tokens,
            tools=oa_tools, messages=oa_messages)
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": tc.id, "name": tc.function.name, "args": args})
        usage = {"in": getattr(resp.usage, "prompt_tokens", 0),
                 "out": getattr(resp.usage, "completion_tokens", 0)}
        stop = "tool_use" if tool_calls else "end_turn"
        return ChatResult(text=msg.content or "", tool_calls=tool_calls,
                          stop_reason=stop, usage=usage, raw=resp)

    # -- mock --------------------------------------------------------------
    def _chat_mock(self, system: str, messages: list, tools: list) -> ChatResult:
        """Deterministic offline driver.

        Walks the mandatory investigation plan step-by-step so the whole layer
        (agent loop, tools, dossier writer) can be exercised end-to-end without
        an API key or any network beyond the tools' own calls. It inspects the
        conversation to see which tools have already run, then emits the next
        scripted tool call. No real reasoning - just a fixed, auditable path.
        """
        from ._mock_plan import next_mock_action
        return next_mock_action(system, messages, tools)


def _to_openai_messages(messages: list) -> list:
    """Best-effort translation of Anthropic-native messages to OpenAI chat
    format. Handles text, tool_use (assistant), and tool_result (user)."""
    out = []
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        text_bits, tool_calls, tool_results = [], [], []
        for block in content:
            bt = block.get("type")
            if bt == "text":
                text_bits.append(block["text"])
            elif bt == "tool_use":
                tool_calls.append({"id": block["id"], "type": "function",
                                   "function": {"name": block["name"],
                                                "arguments": json.dumps(block.get("input", {}))}})
            elif bt == "tool_result":
                tool_results.append(block)
        if role == "assistant":
            entry = {"role": "assistant", "content": "\n".join(text_bits) or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        else:
            if tool_results:
                for tr in tool_results:
                    c = tr.get("content")
                    if isinstance(c, list):
                        c = "\n".join(b.get("text", "") for b in c)
                    out.append({"role": "tool", "tool_call_id": tr.get("tool_use_id"),
                                "content": c or ""})
            if text_bits:
                out.append({"role": "user", "content": "\n".join(text_bits)})
    return out
