"""
graph_expand - bounded, budget-gated neighbourhood expansion from BigQuery.

Per the operator's decision this tool is BigQuery-only: if no BQ project is
configured it returns a clean `bigquery_unavailable` error and the agent
continues with the other tools (no local fallback).

It reuses the exact edge-query shapes the notebook uses (USDT ERC-20
token_transfers for ethereum, decoded Transfer events for tron, and the
transactions inputs/outputs join for bitcoin), so results match the base
pipeline's graph.

Budget wiring: every level's query is dry-run first; the projected dollar cost
is checked against the remaining BQ budget BEFORE the query runs. A level that
would exceed the budget is skipped and the result is marked `truncated`.
"""
from __future__ import annotations

import os

import pandas as pd

from .base import Context, Tool, ToolError, validate

_IN_SCHEMA = {
    "type": "object",
    "properties": {
        "address": {"type": "string", "description": "Seed wallet (hex for eth/tron, base58/bech32 for btc)."},
        "chain": {"type": "string", "enum": ["ethereum", "tron", "bitcoin"]},
        "hops": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
        "direction": {"type": "string", "enum": ["in", "out", "both"], "default": "both"},
        "max_edges": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 300},
    },
    "required": ["address", "chain"],
    "additionalProperties": False,
}

_OUT_SCHEMA = {
    "type": "object",
    "properties": {
        "edges": {"type": "array"},
        "n_edges": {"type": "integer"},
        "n_nodes": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "hops_fetched": {"type": "integer"},
    },
    "required": ["edges", "truncated"],
}


def _lookback_days(chain: str) -> int:
    import config as base
    if chain == "ethereum":
        return int(base.LOOKBACK_DAYS)
    if chain == "tron":
        return int(base.TRON_LOOKBACK_DAYS)
    return int(os.environ.get("BTC_LOOKBACK_DAYS", "30"))


def _quote_list(addrs) -> str:
    # addresses are controlled (hex / base58); still, quote-escape defensively
    return ", ".join("'" + str(a).replace("'", "") + "'" for a in addrs)


def _sql_for(chain: str, frontier: list, direction: str) -> str:
    import config as base
    days = _lookback_days(chain)
    in_list = _quote_list(frontier)

    if chain == "ethereum":
        if direction == "in":
            touch = f"to_address IN ({in_list})"
        elif direction == "out":
            touch = f"from_address IN ({in_list})"
        else:
            touch = f"(from_address IN ({in_list}) OR to_address IN ({in_list}))"
        return f"""
        DECLARE usdt STRING DEFAULT '{base.USDT_ERC20}';
        DECLARE since TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY);
        SELECT from_address, to_address,
               SUM(SAFE_CAST(value AS BIGNUMERIC)/POW(10,{base.USDT_DECIMALS})) AS value,
               COUNT(*) AS tx_count,
               MIN(block_timestamp) AS first_ts, MAX(block_timestamp) AS last_ts
        FROM `{base.ETH_TOKEN_TRANSFERS}`
        WHERE token_address = usdt AND block_timestamp >= since AND {touch}
        GROUP BY from_address, to_address
        ORDER BY value DESC
        """

    if chain == "tron":
        f0 = "JSON_VALUE(args,'$[0]')"
        f1 = "JSON_VALUE(args,'$[1]')"
        if direction == "in":
            touch = f"{f1} IN ({in_list})"
        elif direction == "out":
            touch = f"{f0} IN ({in_list})"
        else:
            touch = f"({f0} IN ({in_list}) OR {f1} IN ({in_list}))"
        return f"""
        DECLARE since TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY);
        SELECT {f0} AS from_address, {f1} AS to_address,
               SUM(SAFE_CAST(JSON_VALUE(args,'$[2]') AS BIGNUMERIC)/POW(10,{base.USDT_DECIMALS})) AS value,
               COUNT(*) AS tx_count,
               MIN(block_timestamp) AS first_ts, MAX(block_timestamp) AS last_ts
        FROM `{base.TRON_EVENTS}`
        WHERE address='{base.USDT_TRC20_HEX}'
          AND event_signature='Transfer(address,address,uint256)'
          AND block_timestamp >= since AND {touch}
        GROUP BY from_address, to_address
        ORDER BY value DESC
        """

    if chain == "bitcoin":
        # touch on the output side (recipient) for "in", input side for "out".
        return f"""
        DECLARE since     TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY);
        DECLARE since_mon DATE DEFAULT DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY), MONTH);
        WITH outs AS (
          SELECT t.`hash` AS tx_hash, o_addr AS recipient, o.value AS amt
          FROM `bigquery-public-data.crypto_bitcoin.transactions` t,
               UNNEST(t.outputs) o, UNNEST(o.addresses) o_addr
          WHERE t.block_timestamp_month >= since_mon AND t.block_timestamp >= since
            AND NOT t.is_coinbase AND o.value > 0
        ),
        ins AS (
          SELECT t.`hash` AS tx_hash, i_addr AS sender
          FROM `bigquery-public-data.crypto_bitcoin.transactions` t,
               UNNEST(t.inputs) i, UNNEST(i.addresses) i_addr
          WHERE t.block_timestamp_month >= since_mon AND t.block_timestamp >= since
            AND NOT t.is_coinbase
        )
        SELECT i.sender AS from_address, o.recipient AS to_address,
               SUM(o.amt) AS value, COUNT(*) AS tx_count,
               CAST(NULL AS TIMESTAMP) AS first_ts, CAST(NULL AS TIMESTAMP) AS last_ts
        FROM outs o JOIN ins i USING (tx_hash)
        WHERE i.sender IS NOT NULL AND o.recipient IS NOT NULL
          AND (i.sender IN ({in_list}) OR o.recipient IN ({in_list}))
        GROUP BY from_address, to_address
        ORDER BY value DESC
        """

    raise ToolError(f"graph_expand: unsupported chain {chain!r}", kind="bad_chain")


class GraphExpandTool(Tool):
    name = "graph_expand"
    description = (
        "Fetch USDT/BTC transfer edges around an address from BigQuery, one hop "
        "at a time up to `hops`, capped at `max_edges`. Each level is dry-run "
        "priced and skipped if it would exceed the BigQuery budget. Edges are "
        "accumulated on the shared sub-graph for the mixer/bridge/ofac tools."
    )
    input_schema = _IN_SCHEMA
    output_schema = _OUT_SCHEMA

    def _client(self, ctx: Context):
        if ctx.bq_client is not None:
            return ctx.bq_client
        import config as base
        if not base.BQ_PROJECT:
            return None
        ctx.bq_client = base.make_bq_client()
        return ctx.bq_client

    def run(self, args: dict, ctx: Context) -> dict:
        validate(args, self.input_schema, "graph_expand.input")
        import config as base

        client = self._client(ctx)
        if client is None:
            raise ToolError(
                "bigquery_unavailable: BQ_PROJECT is not set. Set it in .env to "
                "enable graph expansion; the rest of the tools still work.",
                kind="bigquery_unavailable")

        chain = args["chain"]
        hops = min(int(args.get("hops", 1)), ctx.budget.hops - ctx.budget.hops_used or 1)
        hops = max(1, hops)
        direction = args.get("direction", "both")
        max_edges = int(args.get("max_edges", 300))

        frontier = [args["address"]]
        collected = []
        truncated = False
        hops_fetched = 0

        for level in range(hops):
            sql = _sql_for(chain, frontier, direction)
            # dry-run price gate
            try:
                gb = base.estimate_cost(client, sql, max_gb=base.BQ_MAX_GB)
            except Exception as e:
                raise ToolError(f"graph_expand: dry-run failed: {type(e).__name__}: {e}",
                                kind="bq_dryrun_failed")
            from .. import config as C
            projected_usd = gb / 1024.0 * C.BQ_USD_PER_TB
            if ctx.budget.would_exceed_bq(projected_usd):
                truncated = True
                break

            df = base.run_query(client, sql, max_gb=base.BQ_MAX_GB)
            ctx.budget.charge_bq(projected_usd)
            ctx.budget.charge_hop(1)
            hops_fetched += 1

            if df is None or not len(df):
                break
            df = df[["from_address", "to_address", "value", "tx_count", "first_ts", "last_ts"]].copy()
            collected.append(df)

            # next frontier = newly seen counterparties, minus what we already have
            new_nodes = set(df["from_address"].astype(str)) | set(df["to_address"].astype(str))
            frontier = list(new_nodes - ctx.seen_nodes)
            ctx.add_edges(df)

            if sum(len(d) for d in collected) >= max_edges:
                truncated = True
                break
            if not frontier:
                break

        all_edges = (pd.concat(collected, ignore_index=True) if collected
                     else pd.DataFrame(columns=["from_address", "to_address", "value",
                                                "tx_count", "first_ts", "last_ts"]))
        if len(all_edges) > max_edges:
            all_edges = all_edges.head(max_edges)
            truncated = True

        edge_records = [
            {"from": str(r.from_address), "to": str(r.to_address),
             "value": float(r.value) if pd.notna(r.value) else None,
             "tx_count": int(r.tx_count) if pd.notna(r.tx_count) else None}
            for r in all_edges.itertuples(index=False)
        ]
        result = {
            "edges": edge_records[:max_edges],
            "n_edges": len(edge_records),
            "n_nodes": len(set([e["from"] for e in edge_records] + [e["to"] for e in edge_records])),
            "truncated": truncated,
            "hops_fetched": hops_fetched,
        }
        validate(result, self.output_schema, "graph_expand.output")
        return result
