"""Personalized PageRank over the keyword co-occurrence graph (Phase 4.3).

The graph nodes are keywords sourced from ``keyword_pairs`` (v1) plus
optional edges from ``keyword_docs`` (v2, co-occurrence within a document).
Given a small seed set (typically 1-5 keywords from the user query) PPR
returns the keywords most closely related on the graph, without ever
touching an LLM.

We implement power iteration by hand (design §5's "简单的 power iteration")
because the graph is tiny (thousands of nodes at most for a personal
corpus) and depending on numpy would drag in a heavy transitive
dependency. Weights are kept as Python floats.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from grga.graph.schema import ensure_schema

DEFAULT_ALPHA = 0.15
DEFAULT_ITERS = 20
DEFAULT_TOP_K = 20


@dataclass
class Edge:
    """One undirected edge with an aggregated weight."""

    a: str
    b: str
    weight: float


def _fold(term: str) -> str:
    return term.casefold().strip()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def load_edges(
    conn: sqlite3.Connection,
    *,
    directory: Optional[str] = None,
    include_pairs: bool = True,
    include_docs: bool = True,
    min_weight: float = 0.0,
) -> List[Edge]:
    """Load undirected edges from the SQLite graph tables.

    ``keyword_pairs`` (v1) contributes co-occurrence counts directly.
    ``keyword_docs`` (v2) is used to synthesise pairwise weights when two
    keywords share a document; the synthetic weight is the minimum
    per-doc occurrence count (a cheap co-occurrence proxy).
    """
    ensure_schema(conn)
    edges: Dict[Tuple[str, str], float] = {}

    if include_pairs:
        where = ""
        params: List = []
        if directory is not None:
            where = " WHERE directory = ?"
            params.append(directory)
        if _table_exists(conn, "keyword_pairs"):
            rows = conn.execute(
                f"SELECT keyword_a, keyword_b, count FROM keyword_pairs{where}",
                params,
            ).fetchall()
            for row in rows:
                a, b = _fold(row["keyword_a"]), _fold(row["keyword_b"])
                if not a or not b or a == b:
                    continue
                key = tuple(sorted((a, b)))
                edges[key] = edges.get(key, 0.0) + float(row["count"])

    if include_docs:
        params = []
        if directory is not None:
            params.append(directory)
        # Aggregate intra-document keyword pairs in SQL via a self-join. This
        # keeps the O(D * K^2) work inside SQLite's C engine and returns one
        # row per (kw_a, kw_b) pair, instead of materialising millions of
        # transient Python tuples on every call. ``keyword_docs`` stores one
        # row per (folded keyword, doc), so ``LOWER(...)`` ordering yields
        # canonical unordered pairs without self-pairs or duplicates.
        where_clause = "WHERE a.directory = ?" if directory is not None else ""
        rows = conn.execute(
            f"""
            SELECT LOWER(a.keyword) AS kw_a,
                   LOWER(b.keyword) AS kw_b,
                   SUM(MIN(a.occurrence_count, b.occurrence_count)) AS weight
            FROM keyword_docs a
            JOIN keyword_docs b
              ON a.doc_path = b.doc_path
             AND a.directory = b.directory
             AND LOWER(a.keyword) < LOWER(b.keyword)
            {where_clause}
            GROUP BY kw_a, kw_b
            """,
            params,
        ).fetchall()
        for row in rows:
            a, b = _fold(row["kw_a"]), _fold(row["kw_b"])
            if not a or not b or a == b:
                continue
            key = tuple(sorted((a, b)))
            edges[key] = edges.get(key, 0.0) + float(row["weight"])

    return [
        Edge(a=k[0], b=k[1], weight=w)
        for k, w in edges.items()
        if w > min_weight
    ]


def build_adjacency(edges: Sequence[Edge]) -> Dict[str, Dict[str, float]]:
    """Return an undirected adjacency map: ``adj[node] = {nbr: weight}``."""
    adj: Dict[str, Dict[str, float]] = {}
    for edge in edges:
        if edge.weight <= 0:
            continue
        adj.setdefault(edge.a, {}).setdefault(edge.b, 0.0)
        adj[edge.a][edge.b] += edge.weight
        adj.setdefault(edge.b, {}).setdefault(edge.a, 0.0)
        adj[edge.b][edge.a] += edge.weight
    return adj


def personalized_pagerank(
    seeds: Iterable[str],
    adjacency: Dict[str, Dict[str, float]],
    *,
    top_k: int = DEFAULT_TOP_K,
    alpha: float = DEFAULT_ALPHA,
    iters: int = DEFAULT_ITERS,
) -> List[Tuple[str, float]]:
    """Run PPR power iteration and return ``(node, score)`` top-k.

    Args:
        seeds: query nodes; scores are personalised toward these.
        adjacency: undirected graph (see :func:`build_adjacency`).
        top_k: number of highest-scoring nodes to return.
        alpha: teleport / restart probability. ``0.15`` mirrors the
            classic PageRank prescription and gives a slow-diffusing walk
            that stays close to the seeds.
        iters: power iteration count; 20 is plenty for graphs on the
            order of ``10^3`` nodes.

    Seed nodes missing from the graph are dropped. If no seed is present
    the function returns an empty list (a sane degrade rather than a
    ValueError).
    """
    # ``seeds`` are folded exactly once here and never again inside the
    # power-iteration body — the inner loop must stay fold-free (see PERF
    # plan P0-3). Any caller that stores adjacency keys or seeds unfolded
    # is a bug in that caller, not in PPR.
    seed_set = [s for s in (_fold(x) for x in seeds) if s]
    if not seed_set or not adjacency:
        return []
    seed_in_graph_list = [s for s in seed_set if s in adjacency]
    if not seed_in_graph_list:
        return []
    seed_in_graph = set(seed_in_graph_list)

    # Personalisation vector: uniform over seeds present in the graph.
    p = 1.0 / len(seed_in_graph_list)
    personal: Dict[str, float] = {s: p for s in seed_in_graph_list}

    # Pre-compute per-node out-degree once. ``sum(neighbours.values())`` in
    # the inner loop was O(deg) per iteration per node — hoisting it turns
    # ``iters`` full sweeps into a single upfront pass. On the 600-doc
    # bench this halves PPR's own time.
    out_degree: Dict[str, float] = {
        node: sum(neighbours.values())
        for node, neighbours in adjacency.items()
    }

    scores: Dict[str, float] = dict(personal)

    for _ in range(max(1, iters)):
        next_scores: Dict[str, float] = {
            node: alpha * personal.get(node, 0.0) for node in adjacency
        }
        for node, mass in scores.items():
            neighbours = adjacency.get(node)
            if not neighbours:
                # Dangling nodes leak mass back to the personal vector.
                leak = (1.0 - alpha) * mass
                for target, weight in personal.items():
                    next_scores[target] = next_scores.get(target, 0.0) + leak * weight
                continue
            total = out_degree.get(node, 0.0)
            if total <= 0:
                continue
            share = (1.0 - alpha) * mass / total
            for nbr, weight in neighbours.items():
                next_scores[nbr] = next_scores.get(nbr, 0.0) + share * weight
        scores = next_scores

    # Filter out the seeds themselves so callers get novel neighbours.
    ranked = [
        (node, score)
        for node, score in scores.items()
        if node not in seed_in_graph and score > 0
    ]
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:top_k]
