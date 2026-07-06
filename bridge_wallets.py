"""
Bridge-wallet detection.

An "articulation point" (or cut vertex) in an undirected graph is a node whose
removal DISCONNECTS the graph. In the funnel/exchanger context these are the
richest investigative leads: a single wallet that couples two otherwise
independent subgraphs is almost never coincidental - it is a common operator,
a nested exchange, or an OTC desk bridging two networks.

We extract articulation points on the co-funding / edges graph, score them by
how many distinct components they hold together, and expose a triage-friendly
table. This is the "wallets that bridge two or more cliques" signal.
"""
from __future__ import annotations
from typing import Iterable
import networkx as nx
import pandas as pd


def find_bridge_wallets(graph: nx.Graph, min_component_size: int = 2) -> pd.DataFrame:
    """Return a frame of articulation-point wallets ranked by how many components
    they hold together and the total size of those components.

    Parameters
    ----------
    graph : nx.Graph or nx.DiGraph
        Any wallet graph; a DiGraph is folded to its undirected view.
    min_component_size : int
        Only count components of at least this size (default 2) when scoring;
        drops trivial 1-node "components" so the ranking reflects real bridges.

    Returns
    -------
    DataFrame with columns:
      wallet, components_bridged, component_sizes (list), total_bridged_size,
      degree, is_bridge_wallet=True. Sorted by (components_bridged desc,
      total_bridged_size desc).
    """
    if graph is None or graph.number_of_nodes() == 0:
        return pd.DataFrame(columns=[
            "wallet", "components_bridged", "component_sizes",
            "total_bridged_size", "degree", "is_bridge_wallet"])
    ug = graph.to_undirected(as_view=True) if graph.is_directed() else graph
    aps = list(nx.articulation_points(ug))
    rows = []
    for w in aps:
        neigh = list(ug.neighbors(w))
        if not neigh:
            continue
        h = ug.subgraph([n for n in ug.nodes if n != w]).copy()
        comps = [c for c in nx.connected_components(h) if any(n in c for n in neigh)]
        comps = [c for c in comps if len(c) >= min_component_size]
        if len(comps) < 2:
            continue
        sizes = sorted((len(c) for c in comps), reverse=True)
        rows.append({
            "wallet": w,
            "components_bridged": len(comps),
            "component_sizes": sizes[:10],
            "total_bridged_size": int(sum(sizes)),
            "degree": ug.degree(w),
            "is_bridge_wallet": True,
        })
    df = pd.DataFrame(rows)
    if not len(df):
        return df
    return df.sort_values(["components_bridged", "total_bridged_size"],
                          ascending=False).reset_index(drop=True)


def annotate_bridge_wallets(df: pd.DataFrame, bridges: pd.DataFrame,
                            wallet_col: str = "wallet") -> pd.DataFrame:
    """Left-join `bridges` onto a candidate frame, adding `is_bridge_wallet`
    and `components_bridged` columns (default False / 0)."""
    if bridges is None or not len(bridges):
        out = df.copy()
        out["is_bridge_wallet"] = False
        out["components_bridged"] = 0
        return out
    keep = bridges[["wallet", "components_bridged", "total_bridged_size", "is_bridge_wallet"]]
    out = df.merge(keep, on=wallet_col, how="left")
    out["is_bridge_wallet"] = out["is_bridge_wallet"].fillna(False)
    out["components_bridged"] = out["components_bridged"].fillna(0).astype(int)
    return out


def bridges_across_graphs(graphs: dict, min_component_size: int = 2) -> pd.DataFrame:
    """Convenience: run find_bridge_wallets over {chain: graph} and stack results
    with a `chain` column, so ETH/Tron/BTC bridges rank in one table."""
    parts = []
    for chain, g in graphs.items():
        b = find_bridge_wallets(g, min_component_size=min_component_size)
        if len(b):
            b["chain"] = chain
            parts.append(b)
    if not parts:
        return pd.DataFrame(columns=[
            "wallet", "chain", "components_bridged", "component_sizes",
            "total_bridged_size", "degree", "is_bridge_wallet"])
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values(["components_bridged", "total_bridged_size"],
                           ascending=False).reset_index(drop=True)
