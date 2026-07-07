"""
Bridge-wallet detection.

An "articulation point" (or cut vertex) in an undirected graph is a node whose
removal DISCONNECTS the graph. In the funnel/exchanger context these are the
richest investigative leads: a single wallet that couples two otherwise
independent subgraphs is almost never coincidental - it is a common operator,
a nested exchange, or an OTC desk bridging two networks.

Algorithm
---------
Naive: for each AP, remove it and recount connected components. That is
O(|APs| * (V+E)) and becomes minutes on graphs of ~10^5 nodes.

We use the **block-cut tree** (BCT) instead:
  1) One O(V+E) call to `nx.biconnected_components` + `nx.articulation_points`.
  2) Build a bipartite tree of block-nodes and AP-nodes.
  3) One O(|BCT|) rooted-DFS gives every node's subtree weight (in original
     graph node units).
  4) For each AP, the sizes of the components that appear when we remove it
     are `subtree_size(child)` for each child, plus one "up-side" piece.

Total cost: O(V+E). Empirically ~100-1000x faster than the per-AP approach
on graphs where every wallet has a handful of counterparties.
"""
from __future__ import annotations
import networkx as nx
import pandas as pd


_EMPTY_COLS = ["wallet", "components_bridged", "component_sizes",
               "total_bridged_size", "degree", "is_bridge_wallet"]


def find_bridge_wallets(graph: nx.Graph, min_component_size: int = 2) -> pd.DataFrame:
    """Return articulation-point wallets ranked by how many components they
    hold together and the total size of those components.

    Parameters
    ----------
    graph : nx.Graph or nx.DiGraph
        Any wallet graph; a DiGraph is folded to its undirected view.
    min_component_size : int
        Only count components of at least this size (default 2) when scoring;
        drops trivial 1-node "components" so the ranking reflects real bridges.
    """
    if graph is None or graph.number_of_nodes() == 0:
        return pd.DataFrame(columns=_EMPTY_COLS)
    ug = graph.to_undirected(as_view=True) if graph.is_directed() else graph
    aps = set(nx.articulation_points(ug))
    if not aps:
        return pd.DataFrame(columns=_EMPTY_COLS)

    # (1) Biconnected components; each block is a set of original graph nodes.
    #     Non-AP nodes count once per block they live in (they only live in one).
    #     APs live in multiple blocks; we add them exactly once as their own BCT
    #     node with weight 1.
    bccs = list(nx.biconnected_components(ug))
    block_weight = [len(bcc - aps) for bcc in bccs]

    # (2) Block-cut tree: block-nodes are ("B", i), AP-nodes are ("A", wallet).
    T = nx.Graph()
    for i, bcc in enumerate(bccs):
        b_node = ("B", i)
        T.add_node(b_node, weight=block_weight[i])
        for n in bcc:
            if n in aps:
                a_node = ("A", n)
                if a_node not in T:
                    T.add_node(a_node, weight=1)
                T.add_edge(b_node, a_node)

    # (3) Rooted DFS per tree in the BCT forest: compute parent + subtree weight.
    parent: dict = {}
    subtree_size: dict = {}
    tree_total: dict = {}
    for root in list(T.nodes):
        if root in parent:
            continue
        parent[root] = None
        # iterative DFS, record traversal order for a post-order pass
        stack = [root]
        order = []
        while stack:
            n = stack.pop()
            order.append(n)
            for m in T.neighbors(n):
                if m not in parent:
                    parent[m] = n
                    stack.append(m)
        for n in reversed(order):
            s = T.nodes[n]["weight"]
            for m in T.neighbors(n):
                if parent.get(m) == n:
                    s += subtree_size[m]
            subtree_size[n] = s
        total = subtree_size[root]
        for n in order:
            tree_total[n] = total

    # (4) For each AP, read off component sizes from the precomputed subtree
    #     sizes. `up_size` is the everything-except-my-subtree side of the tree.
    rows = []
    for w in aps:
        a_node = ("A", w)
        if a_node not in T:
            continue
        comps = []
        for m in T.neighbors(a_node):
            if parent.get(m) == a_node:                       # child branch
                if subtree_size[m] >= min_component_size:
                    comps.append(subtree_size[m])
        if parent.get(a_node) is not None:                    # "up" branch
            up_size = tree_total[a_node] - subtree_size[a_node]
            if up_size >= min_component_size:
                comps.append(up_size)
        if len(comps) < 2:
            continue
        comps.sort(reverse=True)
        rows.append({
            "wallet": w,
            "components_bridged": len(comps),
            "component_sizes": comps[:10],
            "total_bridged_size": int(sum(comps)),
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
    out["is_bridge_wallet"] = (out["is_bridge_wallet"].fillna(False)
                                                     .infer_objects(copy=False)
                                                     .astype(bool))
    out["components_bridged"] = out["components_bridged"].fillna(0).astype(int)
    return out


def bridges_across_graphs(graphs: dict, min_component_size: int = 2) -> pd.DataFrame:
    """Run find_bridge_wallets over {chain: graph} and stack results with a
    `chain` column, so ETH/Tron/BTC bridges rank in one table."""
    parts = []
    for chain, g in graphs.items():
        b = find_bridge_wallets(g, min_component_size=min_component_size)
        if len(b):
            b["chain"] = chain
            parts.append(b)
    if not parts:
        return pd.DataFrame(columns=_EMPTY_COLS + ["chain"])
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values(["components_bridged", "total_bridged_size"],
                           ascending=False).reset_index(drop=True)
