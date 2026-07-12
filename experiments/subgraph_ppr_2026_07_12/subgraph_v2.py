"""V2: 真正的局部展开。核心修正：

- top-M 同时限制"节点入图"和"前沿传播"。未被 top-M 选中的邻居只作
  为**已存在节点集内的邻边**参与最终 closure，不加入子图节点集。
- max_hops 默认降为 2（3 跳几乎必然把全图拉入）。
- 保留 closure 步骤：拉取当前节点集内部的所有边（一次 SQL）。
"""
from __future__ import annotations
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

CHUNK_COOCCUR_SOURCE = "chunk_cooccur"

@dataclass
class SubgraphStats:
    hop_new_nodes: List[int] = field(default_factory=list)
    hop_edges_fetched: List[int] = field(default_factory=list)
    hop_sql_ms: List[float] = field(default_factory=list)
    frontier_after_prune: List[int] = field(default_factory=list)
    total_nodes: int = 0
    total_edges: int = 0
    build_ms: float = 0.0
    closure_edges: int = 0


def build_subgraph(
    conn: sqlite3.Connection,
    seed_kw_ids: List[int],
    *,
    scope_dir_id: Optional[int] = None,
    max_hops: int = 2,
    top_m_per_node: int = 30,
    min_npmi: float = 0.2,
):
    stats = SubgraphStats()
    t_start = time.perf_counter()

    nodes: Set[int] = set(seed_kw_ids)
    edges: Dict[Tuple[int, int], Tuple[float, float, str]] = {}
    frontier: Set[int] = set(seed_kw_ids)

    for hop in range(max_hops):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        params: List = list(frontier) + list(frontier)
        where = f"(kw_a_id IN ({placeholders}) OR kw_b_id IN ({placeholders}))"
        if scope_dir_id is not None:
            where = "dir_id = ? AND " + where
            params = [scope_dir_id] + params
        where += " AND (source != ? OR npmi >= ?)"
        params.append(CHUNK_COOCCUR_SOURCE)
        params.append(min_npmi)
        sql = ("SELECT kw_a_id, kw_b_id, cooccur_count, npmi, source "
               f"FROM keyword_pairs WHERE {where}")
        t = time.perf_counter()
        rows = conn.execute(sql, params).fetchall()
        stats.hop_sql_ms.append((time.perf_counter() - t) * 1000)
        stats.hop_edges_fetched.append(len(rows))

        # Per frontier node: rank its neighbours; only top-M enter the node set.
        per_node: Dict[int, List[Tuple[int, float, float, str]]] = {}
        for r in rows:
            a, b, co = int(r[0]), int(r[1]), float(r[2])
            npmi = float(r[3]) if r[3] is not None else 0.0
            src = r[4]
            if a == b:
                continue
            # Record edge if BOTH endpoints already in nodes (closure will
            # add the rest later); actually just always record so we don't
            # need a re-fetch — closure only fills gaps that this hop misses.
            key = (a, b) if a < b else (b, a)
            edges.setdefault(key, (co, npmi, src))
            if a in frontier:
                per_node.setdefault(a, []).append((b, co, npmi, src))
            if b in frontier:
                per_node.setdefault(b, []).append((a, co, npmi, src))

        # Rank by weight (npmi primary for chunk_cooccur, cooccur secondary).
        next_frontier: Set[int] = set()
        newly_added: Set[int] = set()
        for kwid, nbrs in per_node.items():
            nbrs.sort(key=lambda t: (-t[2], -t[1]))
            for (nbr, co, npmi, src) in nbrs[:top_m_per_node]:
                if nbr not in nodes:
                    nodes.add(nbr)
                    newly_added.add(nbr)
                    next_frontier.add(nbr)
        # Filter edges: keep only those with both endpoints in nodes (unpruned
        # neighbours produced edges we don't want).
        edges = {k: v for k, v in edges.items()
                 if k[0] in nodes and k[1] in nodes}
        stats.hop_new_nodes.append(len(newly_added))
        stats.frontier_after_prune.append(len(next_frontier))
        frontier = next_frontier

    # Closure: fetch every edge whose both endpoints already sit in nodes.
    # Ensures density is intrinsic to the subgraph (no missing edges from
    # unpruned-neighbour drops above).
    if nodes:
        node_list = list(nodes)
        chunk = 900
        t = time.perf_counter()
        closure_added = 0
        for i in range(0, len(node_list), chunk):
            piece = node_list[i:i + chunk]
            ph = ",".join("?" * len(piece))
            base = list(piece) + list(piece) + [CHUNK_COOCCUR_SOURCE, min_npmi]
            if scope_dir_id is not None:
                sql = ("SELECT kw_a_id, kw_b_id, cooccur_count, npmi, source "
                       "FROM keyword_pairs WHERE dir_id = ? "
                       f"AND kw_a_id IN ({ph}) AND kw_b_id IN ({ph}) "
                       "AND (source != ? OR npmi >= ?)")
                params = [scope_dir_id] + base
            else:
                sql = ("SELECT kw_a_id, kw_b_id, cooccur_count, npmi, source "
                       f"FROM keyword_pairs WHERE kw_a_id IN ({ph}) "
                       f"AND kw_b_id IN ({ph}) AND (source != ? OR npmi >= ?)")
                params = base
            for r in conn.execute(sql, params):
                a, b = int(r[0]), int(r[1])
                if a == b: continue
                key = (a, b) if a < b else (b, a)
                if key not in edges:
                    co = float(r[2])
                    npmi = float(r[3]) if r[3] is not None else 0.0
                    edges[key] = (co, npmi, r[4])
                    closure_added += 1
        stats.hop_sql_ms.append((time.perf_counter() - t) * 1000)
        stats.hop_edges_fetched.append(closure_added)
        stats.closure_edges = closure_added

    stats.total_nodes = len(nodes)
    stats.total_edges = len(edges)
    stats.build_ms = (time.perf_counter() - t_start) * 1000
    return nodes, edges, stats
