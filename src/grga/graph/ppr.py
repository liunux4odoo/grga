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
        # ``keyword_docs`` stores casefolded keyword strings (schema v3),
        # so the self-join comparison is a direct column compare — no
        # ``LOWER(...)`` wrapper needed. The ``+`` unary prefix on the
        # keyword comparison is a deliberate SQLite optimiser barrier: it
        # stops the planner from range-scanning ``idx_keyword_docs_kw``
        # (``keyword > ?``, a huge fan-out) and forces the selective
        # ``(directory, doc_path)`` join instead. Dropping ``LOWER`` had let
        # the planner "see" the keyword index and pick the ~30x slower plan;
        # the barrier keeps the join on doc_path where each doc has only a
        # handful of keywords.
        where_clause = "WHERE a.directory = ?" if directory is not None else ""
        rows = conn.execute(
            f"""
            SELECT a.keyword AS kw_a,
                   b.keyword AS kw_b,
                   SUM(MIN(a.occurrence_count, b.occurrence_count)) AS weight
            FROM keyword_docs a
            JOIN keyword_docs b
              ON a.doc_path = b.doc_path
             AND a.directory = b.directory
             AND +a.keyword < +b.keyword
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


@dataclass
class IndexedGraph:
    """Integer-indexed adjacency for allocation-free PPR iteration.

    The dict-of-dicts adjacency (:func:`build_adjacency`) drives PPR through
    millions of string-keyed ``dict.get`` calls — 64% of a ``related
    --mode graph`` call per the PERF baseline. This structure maps every
    node to a dense integer id so the power-iteration inner loop touches
    only ``list[int]`` indexing and float arithmetic, zero hashing.

    Fields:
        node_ids: ``node_ids[i]`` is the string keyword for index ``i``.
        node_index: inverse map ``keyword -> index``.
        neighbors: ``neighbors[i]`` is a list of ``(j, weight)`` edges.
        out_degree: ``out_degree[i]`` is the pre-summed total edge weight
            out of node ``i`` (a dangling node has ``0.0``).
    """

    node_ids: List[str]
    node_index: Dict[str, int]
    neighbors: List[List[Tuple[int, float]]]
    out_degree: List[float]


def build_indexed_graph(edges: Sequence[Edge]) -> IndexedGraph:
    """Build an :class:`IndexedGraph` directly from undirected ``edges``."""
    node_index: Dict[str, int] = {}
    neighbors: List[List[Tuple[int, float]]] = []

    def _id(name: str) -> int:
        idx = node_index.get(name)
        if idx is None:
            idx = len(node_index)
            node_index[name] = idx
            neighbors.append([])
        return idx

    # Aggregate parallel edges the same way build_adjacency does.
    agg: Dict[Tuple[int, int], float] = {}
    for edge in edges:
        if edge.weight <= 0:
            continue
        ia, ib = _id(edge.a), _id(edge.b)
        if ia == ib:
            continue
        agg[(ia, ib)] = agg.get((ia, ib), 0.0) + edge.weight

    out_degree = [0.0] * len(node_index)
    for (ia, ib), w in agg.items():
        neighbors[ia].append((ib, w))
        neighbors[ib].append((ia, w))
        out_degree[ia] += w
        out_degree[ib] += w

    node_ids = [""] * len(node_index)
    for name, idx in node_index.items():
        node_ids[idx] = name
    return IndexedGraph(
        node_ids=node_ids,
        node_index=node_index,
        neighbors=neighbors,
        out_degree=out_degree,
    )


def indexed_graph_from_adjacency(
    adjacency: Dict[str, Dict[str, float]],
) -> IndexedGraph:
    """Convert a pre-built dict adjacency into an :class:`IndexedGraph`.

    Used by the legacy :func:`personalized_pagerank` dict API so it can
    delegate to the indexed implementation without a second graph load.
    """
    node_ids = list(adjacency.keys())
    node_index = {name: i for i, name in enumerate(node_ids)}
    neighbors: List[List[Tuple[int, float]]] = []
    out_degree: List[float] = []
    for name in node_ids:
        nbrs = adjacency[name]
        # Every neighbour key is guaranteed present in ``adjacency`` because
        # build_adjacency inserts both endpoints of each edge.
        neighbors.append([(node_index[k], w) for k, w in nbrs.items()])
        out_degree.append(sum(nbrs.values()))
    return IndexedGraph(
        node_ids=node_ids,
        node_index=node_index,
        neighbors=neighbors,
        out_degree=out_degree,
    )


def personalized_pagerank_indexed(
    seeds: Iterable[str],
    graph: IndexedGraph,
    *,
    top_k: int = DEFAULT_TOP_K,
    alpha: float = DEFAULT_ALPHA,
    iters: int = DEFAULT_ITERS,
) -> List[Tuple[str, float]]:
    """PPR over an :class:`IndexedGraph`; semantics match :func:`personalized_pagerank`.

    Same power iteration, same teleport / dangling-leak handling, same
    seed-exclusion and top-k tie-breaking — only the data layout differs
    (dense integer arrays instead of nested string dicts).
    """
    n = len(graph.node_ids)
    if n == 0:
        return []
    seed_folded = [s for s in (_fold(x) for x in seeds) if s]
    seed_ids = [graph.node_index[s] for s in seed_folded if s in graph.node_index]
    if not seed_ids:
        return []

    p = 1.0 / len(seed_ids)
    personal = [0.0] * n
    for sid in seed_ids:
        personal[sid] = p
    seed_id_set = set(seed_ids)
    neighbors = graph.neighbors
    out_degree = graph.out_degree

    score = personal[:]
    for _ in range(max(1, iters)):
        new_score = [alpha * personal[i] for i in range(n)]
        for i in range(n):
            si = score[i]
            if si <= 0:
                continue
            deg = out_degree[i]
            nbrs = neighbors[i]
            if not nbrs or deg <= 0:
                # Dangling: leak mass back onto the seed personal vector.
                leak = (1.0 - alpha) * si
                for sid in seed_ids:
                    new_score[sid] += leak * personal[sid]
                continue
            share = (1.0 - alpha) * si / deg
            for j, w in nbrs:
                new_score[j] += share * w
        score = new_score

    ranked = [
        (graph.node_ids[i], s)
        for i, s in enumerate(score)
        if i not in seed_id_set and s > 0
    ]
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:top_k]


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

    This function is a thin compatibility shim on top of
    :func:`personalized_pagerank_indexed`: it materialises the dict
    adjacency into an :class:`IndexedGraph` first, then delegates. Hot-path
    callers should build the indexed graph once (via
    :func:`build_indexed_graph`) and call the indexed variant directly to
    skip the conversion.
    """
    if not adjacency:
        return []
    return personalized_pagerank_indexed(
        seeds,
        indexed_graph_from_adjacency(adjacency),
        top_k=top_k,
        alpha=alpha,
        iters=iters,
    )
