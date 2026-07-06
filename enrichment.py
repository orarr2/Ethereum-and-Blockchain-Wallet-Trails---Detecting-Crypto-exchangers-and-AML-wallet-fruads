"""
Modular identity-enrichment layer.

Design goals
------------
1. Pluggable: every source implements one `enrich(addresses) -> {addr: {...}}`
   method, so new sources drop in without touching the caller.
2. Free-first: the sources wired into `DEFAULT_SOURCES` cost nothing - public
   label sets, public Ethereum RPCs, and an on-chain oracle read. This matches
   the project rule "integrate what is free, drop what costs money".
3. Honest about IP: real-world IP attribution is NOT available from on-chain
   data. It requires off-chain infrastructure (a listening node capturing the
   originating peer of a transaction) or a paid intelligence provider. That
   capability is represented here by a *documented, non-wired* stub that raises
   on use - it is deliberately excluded from `DEFAULT_SOURCES` so no paid or
   privacy-sensitive call ever happens by default.

Nothing in DEFAULT_SOURCES makes a paid API call. Sources that need a BigQuery
client (exchange anchor) accept one explicitly and are opt-in.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Dict, Iterable, List, Optional

Address = str
Record = Dict[str, object]


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class EnrichmentSource:
    """Abstract enrichment source. Subclasses implement `enrich`."""

    name: str = "base"
    cost: str = "free"          # "free" | "paid" | "infra"
    requires: tuple = ()        # human-readable prerequisites

    def enrich(self, addresses: Iterable[Address]) -> Dict[Address, Record]:
        raise NotImplementedError

    def available(self) -> bool:
        """Cheap check whether this source can run right now."""
        return True


# ---------------------------------------------------------------------------
# FREE sources (wired by default)
# ---------------------------------------------------------------------------
class PublicLabelsSource(EnrichmentSource):
    """Community Etherscan label set (brianleect/etherscan-labels). Free HTTP."""

    name = "public_labels"
    URL = ("https://raw.githubusercontent.com/brianleect/etherscan-labels/"
           "main/data/etherscan/combined/combinedAllLabels.json")

    def __init__(self, labels: Optional[dict] = None, timeout: int = 60):
        self._labels = {k.lower(): v for k, v in labels.items()} if labels else None
        self._timeout = timeout

    def _load(self) -> dict:
        if self._labels is None:
            with urllib.request.urlopen(self.URL, timeout=self._timeout) as r:
                self._labels = {k.lower(): v for k, v in json.load(r).items()}
        return self._labels

    def enrich(self, addresses: Iterable[Address]) -> Dict[Address, Record]:
        labels = self._load()
        out: Dict[Address, Record] = {}
        for a in addresses:
            meta = labels.get(str(a).lower())
            if meta and (meta.get("name") or meta.get("labels")):
                out[a] = {"label_name": meta.get("name", ""),
                          "label_tags": list(meta.get("labels", []))}
        return out


class ChainalysisOracleSource(EnrichmentSource):
    """On-chain `isSanctioned(address)` read against Chainalysis' public oracle
    contract via a public Ethereum RPC. Free (a `view` call, no gas)."""

    name = "chainalysis_oracle"
    requires = ("web3",)
    ORACLE = "0x40C57923924B5c5c5455c48D93317139ADDaC8fb"
    RPCS = ("https://ethereum-rpc.publicnode.com", "https://cloudflare-eth.com",
            "https://rpc.ankr.com/eth", "https://eth.merkle.io")

    def __init__(self, rpc_urls: Optional[List[str]] = None, cap: int = 2000):
        self.rpc_urls = list(rpc_urls) if rpc_urls else list(self.RPCS)
        self.cap = cap

    def _connect(self):
        try:
            from web3 import Web3
        except ImportError:
            return None, None
        for rpc in self.rpc_urls:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
                if w3.is_connected():
                    return w3, Web3
            except Exception:
                continue
        return None, None

    def available(self) -> bool:
        w3, _ = self._connect()
        return w3 is not None

    def enrich(self, addresses: Iterable[Address]) -> Dict[Address, Record]:
        w3, Web3 = self._connect()
        if w3 is None:
            return {}
        abi = [{"inputs": [{"type": "address", "name": "addr"}],
                "name": "isSanctioned",
                "outputs": [{"type": "bool", "name": ""}],
                "stateMutability": "view", "type": "function"}]
        c = w3.eth.contract(address=Web3.to_checksum_address(self.ORACLE), abi=abi)
        out: Dict[Address, Record] = {}
        for a in list(addresses)[: self.cap]:
            try:
                cs = Web3.to_checksum_address(a)
                if c.functions.isSanctioned(cs).call():
                    out[a] = {"chainalysis_sanctioned": True}
            except Exception:
                continue
        return out


class ENSSource(EnrichmentSource):
    """Reverse-resolve ETH address -> primary .eth name via public RPC. Free."""

    name = "ens"
    requires = ("web3",)
    RPCS = ("https://ethereum-rpc.publicnode.com", "https://cloudflare-eth.com",
            "https://rpc.ankr.com/eth", "https://eth.merkle.io", "https://1rpc.io/eth")

    def __init__(self, rpc_urls: Optional[List[str]] = None, max_addresses: int = 100):
        self.rpc_urls = list(rpc_urls) if rpc_urls else list(self.RPCS)
        self.max_addresses = max_addresses

    def enrich(self, addresses: Iterable[Address]) -> Dict[Address, Record]:
        try:
            from web3 import Web3
            from ens import ENS
        except ImportError:
            return {}
        w3 = None
        for url in self.rpc_urls:
            try:
                cand = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
                if cand.is_connected():
                    w3 = cand
                    break
            except Exception:
                continue
        if w3 is None:
            return {}
        ns = ENS.from_web3(w3)
        out: Dict[Address, Record] = {}
        for a in list(addresses)[: self.max_addresses]:
            try:
                name = ns.name(Web3.to_checksum_address(a))
                if name:
                    out[a] = {"ens_name": name}
            except Exception:
                continue
        return out


# ---------------------------------------------------------------------------
# OPT-IN source (free but needs a BigQuery client)
# ---------------------------------------------------------------------------
class ExchangeAnchorSource(EnrichmentSource):
    """Direct USDT transfers between a candidate and a labelled exchange - the
    KYC/subpoena anchor. Free data, but needs a BigQuery client, so it is NOT
    in DEFAULT_SOURCES; wire it explicitly when you have a client."""

    name = "exchange_anchor"
    cost = "free"
    requires = ("bigquery client", "exchange address set")

    def __init__(self, run_query, exchange_addrs, usdt_contract, lookback_days=90, decimals=6):
        self.run_query = run_query
        self.exchange_addrs = set(exchange_addrs)
        self.usdt = usdt_contract
        self.lookback_days = lookback_days
        self.decimals = decimals

    def enrich(self, addresses: Iterable[Address]) -> Dict[Address, Record]:
        cand = list(addresses)
        if not cand or not self.exchange_addrs:
            return {}
        cand_s = ", ".join(repr(a) for a in cand)
        exch_s = ", ".join(repr(a) for a in self.exchange_addrs)
        sql = f"""
        DECLARE usdt STRING DEFAULT '{self.usdt}';
        DECLARE since TIMESTAMP DEFAULT TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {self.lookback_days} DAY);
        SELECT IF(from_address IN ({cand_s}), from_address, to_address) AS candidate,
               IF(from_address IN ({exch_s}), from_address, to_address) AS exchange_addr,
               COUNT(*) AS n_tx,
               SUM(SAFE_CAST(value AS BIGNUMERIC)/POW(10,{self.decimals})) AS usdt
        FROM `bigquery-public-data.crypto_ethereum.token_transfers`
        WHERE token_address=usdt AND block_timestamp>=since
          AND ((from_address IN ({cand_s}) AND to_address IN ({exch_s}))
            OR (from_address IN ({exch_s}) AND to_address IN ({cand_s})))
        GROUP BY candidate, exchange_addr
        ORDER BY usdt DESC
        """
        df = self.run_query(sql)
        out: Dict[Address, Record] = {}
        for r in df.itertuples(index=False):
            out.setdefault(r.candidate, {"exchange_anchor": []})
            out[r.candidate]["exchange_anchor"].append(
                {"exchange": r.exchange_addr, "n_tx": int(r.n_tx), "usdt": float(r.usdt)})
        return out


# ---------------------------------------------------------------------------
# DOCUMENTED, NON-WIRED: IP attribution (not on-chain; paid/infra)
# ---------------------------------------------------------------------------
class IPAttributionSource(EnrichmentSource):
    """IP attribution is NOT derivable from on-chain data.

    A blockchain ledger records value flow, never the network origin of the
    broadcasting peer. Recovering an IP requires one of:
      * running listening full nodes that log the first peer to relay each tx
        (probabilistic, heavy infra, legally sensitive), or
      * a paid intelligence provider (Chainalysis, TRM, Arkham, Nansen) that
        sells such attribution under contract.

    Per the project rule "drop what costs money", this source is deliberately
    NOT in DEFAULT_SOURCES and raises on use. It exists only to make the
    boundary explicit and to give a place to plug a licensed provider later.
    """

    name = "ip_attribution"
    cost = "paid/infra"
    requires = ("listening-node infrastructure OR a licensed intelligence API",)

    def available(self) -> bool:
        return False

    def enrich(self, addresses: Iterable[Address]) -> Dict[Address, Record]:
        raise NotImplementedError(
            "IP attribution is off-chain only. Wire a licensed provider or a "
            "node-level mempool collector here; it is intentionally not enabled "
            "by default (paid / privacy-sensitive)."
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
DEFAULT_SOURCES = (PublicLabelsSource, ChainalysisOracleSource, ENSSource)


class Enricher:
    """Run a set of enrichment sources over an address list and merge results
    into one {address: merged_record} dict."""

    def __init__(self, sources: Optional[List[EnrichmentSource]] = None):
        self.sources = list(sources) if sources is not None else [s() for s in DEFAULT_SOURCES]

    def enrich(self, addresses: Iterable[Address]) -> Dict[Address, Record]:
        addresses = list(addresses)
        merged: Dict[Address, Record] = {}
        for src in self.sources:
            try:
                if not src.available():
                    continue
                for addr, rec in src.enrich(addresses).items():
                    merged.setdefault(addr, {}).update(rec)
                    merged[addr].setdefault("_sources", []).append(src.name)
            except Exception as e:
                print(f"[enrichment] source {src.name} failed: {type(e).__name__}: {e}")
        return merged

    def enrich_frame(self, df, wallet_col: str = "wallet"):
        """Return a copy of `df` with enrichment columns attached."""
        recs = self.enrich(df[wallet_col].tolist())
        out = df.copy()
        out["ens_name"] = out[wallet_col].map(lambda a: (recs.get(a) or {}).get("ens_name", ""))
        out["label_name"] = out[wallet_col].map(lambda a: (recs.get(a) or {}).get("label_name", ""))
        out["chainalysis_sanctioned"] = out[wallet_col].map(
            lambda a: bool((recs.get(a) or {}).get("chainalysis_sanctioned", False)))
        out["enrichment_sources"] = out[wallet_col].map(
            lambda a: ", ".join((recs.get(a) or {}).get("_sources", [])))
        return out
