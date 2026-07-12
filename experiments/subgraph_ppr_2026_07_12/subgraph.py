"""
Lazy K-hop subgraph builder over grga's keyword co-occurrence graph.

Reads keyword_pairs directly (id-native, no JOIN back to keywords until the
end) so we can compare against the current in-tree full-graph PPR path
without touching production code.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple


CHUNK_COOCCUR_SOURCE = "chunk_cooccur"


@dataclass
class SubgraphStats:
    """Instrumentation for one build_subgraph() call."""

    hop_new_nodes: List[int] = field(default_factory=list)  # nodes added at each hop
    hop_edges_fetched: List[int] = field(default_factory=list)  # rows fetched per hop
    hop_sql_ms: List[float] = field(default_factory=list)  # SQL wall time per hop
    frontier_after_prune: List[int] = field(default_factory=list)  # frontier size after top-M prune
    total_nodes: int = 0
    total_edges: int = 0
    build_ms: float = 0.0
    text_lookup_ms: float = 0.0


def build_subgraph(
    conn: sqlite3.Connection,
    seed_kw_ids: List[int],
    *,
    scope_dir_id: Optional[int] = None,
    max_hops: int = 3,
    top_m_per_node: int = 100,
    min_npmi: float = 0.2,
):
    """Grow a K-hop subgraph from ``seed_kw_ids`` on the co-occurrence graph.

    Returns (nodes_set, edge_dict, stats).

    * ``nodes_set``: Set[int] of kw_ids in the subgraph.
    * ``edge_dict``: Dict[(int,int)->(cooccur, npmi, source)] with a < b.
    * ``stats``: instrumentation (SubgraphStats).

    Semantics:

    * BFS by hop. At each hop, the "frontier" is the set of nodes discovered
      in the previous hop. For each frontier node, we ask the DB for its
      "top-M" neighbours (by edge weight, taking cooccur as a proxy for
      simplicity — matches the P0/P1 PPR weighting priority). All fetched
      neighbours + their edges are recorded regardless of top-M cap for
      *outgoing* pruning — the cap only limits how many neighbours we walk
      to next hop, so edge coverage inside the subgraph stays dense.
    * ``min_npmi`` mirrors the production filter.
    * All queries are id-native (no JOIN to keywords). One SQL call per hop
      (not per node) via ``kw_a_id IN (...) OR kw_b_id IN (...)``.
    """
    stats = SubgraphStats()
    t_start = time.perf_counter()

    nodes: Set[int] = set(seed_kw_ids)
    edges: Dict[Tuple[int, int], Tuple[float, float, str]] = {}
    frontier: Set[int] = set(seed_kw_ids)

    for hop in range(max_hops):
        if not frontier:
            break
        # Batched edge query: all edges with at least one endpoint in the
        # current frontier, honouring min_npmi.
        placeholders = ",".join("?" * len(frontier))
        params: List = list(frontier) + list(frontier)
        where = (
            f"(kw_a_id IN ({placeholders}) OR kw_b_id IN ({placeholders}))"
        )
        if scope_dir_id is not None:
            where = f"dir_id = ? AND " + where
            params = [scope_dir_id] + params
        # min_npmi filter (llm_promoted rows have NULL npmi -> keep them)
        where += " AND (source != ? OR npmi >= ?)"
        params.append(CHUNK_COOCCUR_SOURCE)
        params.append(min_npmi)

        sql = (
            "SELECT kw_a_id, kw_b_id, cooccur_count, npmi, source "
            f"FROM keyword_pairs WHERE {where}"
        )
        t = time.perf_counter()
        rows = conn.execute(sql, params).fetchall()
        stats.hop_sql_ms.append((time.perf_counter() - t) * 1000)
        stats.hop_edges_fetched.append(len(rows))

        # Per-node fanout accumulator for top-M pruning.
        per_node: Dict[int, List[Tuple[int, float, float, str]]] = {}
        for r in rows:
            a, b, co, npmi, src = int(r[0]), int(r[1]), float(r[2]), r[3], r[4]
            npmi = float(npmi) if npmi is not None else 0.0
            if a == b:
                continue
            key = (a, b) if a < b else (b, a)
            edges.setdefault(key, (co, npmi, src))
            # For frontier growth: aggregate neighbours seen from each frontier node
            if a in frontier:
                per_node.setdefault(a, []).append((b, co, npmi, src))
            if b in frontier:
                per_node.setdefault(b, []).append((a, co, npmi, src))

        # Compute next frontier: per frontier node take top-M neighbours by
        # weight (cooccur as coarse proxy; matches P0/P1's edge_weight primary
        # term for chunk_cooccur, and log1p(cooccur) for llm_promoted).
        next_frontier: Set[int] = set()
        for kwid, nbrs in per_node.items():
            # Sort by (npmi desc, cooccur desc) for chunk_cooccur, else cooccur only.
            nbrs.sort(key=lambda t: (-t[2], -t[1]))
            for (nbr, co, npmi, src) in nbrs[:top_m_per_node]:
                if nbr not in nodes:
                    next_frontier.add(nbr)
        # Add all fetched neighbours to nodes (unpruned, so we get full edges
        # among discovered nodes at final assembly).
        new_this_hop: Set[int] = set()
        for r in rows:
            a, b = int(r[0]), int(r[1])
            if a not in nodes:
                new_this_hop.add(a)
            if b not in nodes:
                new_this_hop.add(b)
        nodes.update(new_this_hop)
        stats.hop_new_nodes.append(len(new_this_hop))
        stats.frontier_after_prune.append(len(next_frontier))
        frontier = next_frontier

    # One extra "closure" query: fetch edges internal to the current node set
    # that we haven't already collected (frontier-only fetch misses edges where
    # both endpoints joined at the same hop but neither was in that hop's
    # frontier). Bounded by node-set size.
    if nodes and max_hops > 0:
        node_list = list(nodes)
        # Chunk IN() to stay under SQLITE_MAX_VARIABLE_NUMBER (~999)
        # For our sizes (typically < 5000 nodes) we chunk to 900 per query.
        chunk = 900
        t = time.perf_counter()
        closure_rows = 0
        for i in range(0, len(node_list), chunk):
            piece = node_list[i:i + chunk]
            ph = ",".join("?" * len(piece))
            params = list(piece) + list(piece) + [CHUNK_COOCCUR_SOURCE, min_npmi]
            if scope_dir_id is not None:
                sql = (
                    "SELECT kw_a_id, kw_b_id, cooccur_count, npmi, source "
                    "FROM keyword_pairs "
                    "WHERE dir_id = ? "
                    f"AND kw_a_id IN ({ph}) AND kw_b_id IN ({ph}) "
                    "AND (source != ? OR npmi >= ?)"
                )
                params = [scope_dir_id] + params
            else:
                sql = (
                    "SELECT kw_a_id, kw_b_id, cooccur_count, npmi, source "
                    "FROM keyword_pairs "
                    f"WHERE kw_a_id IN ({ph}) AND kw_b_id IN ({ph}) "
                    "AND (source != ? OR npmi >= ?)"
                )
            for r in conn.execute(sql, params):
                a, b = int(r[0]), int(r[1])
                if a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                if key not in edges:
                    co = float(r[2])
                    npmi = float(r[3]) if r[3] is not None else 0.0
                    edges[key] = (co, npmi, r[4])
                    closure_rows += 1
        stats.hop_sql_ms.append((time.perf_counter() - t) * 1000)
        stats.hop_edges_fetched.append(closure_rows)

    stats.total_nodes = len(nodes)
    stats.total_edges = len(edges)
    stats.build_ms = (time.perf_counter() - t_start) * 1000
    return nodes, edges, stats
