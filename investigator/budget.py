"""
Budget - the hard ceiling on a single investigation.

Four independent dimensions, any one of which stops the loop:
  hops       - graph-traversal depth (charged by graph_expand)
  bq_usd     - BigQuery dollars (charged from the dry-run byte estimate)
  tokens     - LLM tokens in+out (charged after every model call)
  max_calls  - total tool calls (backstop against a faulty loop)

Every charge is additive and monotonic. `exhausted()` returns (True, reason)
the moment any dimension is spent, and the agent finalises a PARTIAL dossier.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Budget:
    # ceilings
    hops: int = 2
    bq_usd: float = 0.10
    tokens: int = 30_000
    max_calls: int = 25

    # spent-so-far (mutated in place)
    hops_used: int = 0
    bq_spent: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0

    # audit trail of every charge, for the dossier's cost appendix
    ledger: list = field(default_factory=list)

    def charge_bq(self, usd: float) -> None:
        self.bq_spent += max(0.0, float(usd))
        self.ledger.append({"kind": "bq_usd", "amount": float(usd), "total": self.bq_spent})

    def charge_tokens(self, in_toks: int, out_toks: int) -> None:
        self.tokens_in += int(in_toks)
        self.tokens_out += int(out_toks)
        self.ledger.append({"kind": "tokens", "in": int(in_toks), "out": int(out_toks),
                            "total": self.tokens_in + self.tokens_out})

    def charge_call(self) -> None:
        self.calls += 1

    def charge_hop(self, n: int = 1) -> None:
        self.hops_used += int(n)

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    def would_exceed_bq(self, projected_usd: float) -> bool:
        """Pre-flight check: would adding `projected_usd` blow the BQ budget?"""
        return (self.bq_spent + max(0.0, float(projected_usd))) > self.bq_usd

    def exhausted(self) -> tuple[bool, str]:
        if self.calls >= self.max_calls:
            return True, "max_calls"
        if self.bq_spent >= self.bq_usd:
            return True, "bq_usd"
        if self.tokens_total >= self.tokens:
            return True, "tokens"
        if self.hops_used >= self.hops:
            return True, "hops"
        return False, ""

    def snapshot(self) -> dict:
        done, reason = self.exhausted()
        return {
            "hops": {"used": self.hops_used, "cap": self.hops},
            "bq_usd": {"used": round(self.bq_spent, 6), "cap": self.bq_usd},
            "tokens": {"in": self.tokens_in, "out": self.tokens_out,
                       "total": self.tokens_total, "cap": self.tokens},
            "calls": {"used": self.calls, "cap": self.max_calls},
            "exhausted": done,
            "exhausted_reason": reason,
        }

    @classmethod
    def from_config(cls) -> "Budget":
        from . import config as C
        return cls(
            hops=C.HOPS_BUDGET,
            bq_usd=C.BQ_USD_BUDGET_PER_DOSSIER,
            tokens=C.LLM_TOKEN_BUDGET_PER_DOSSIER,
            max_calls=C.MAX_TOOL_CALLS_PER_DOSSIER,
        )
