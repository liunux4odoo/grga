"""Personalized PageRank over the keyword co-occurrence graph.

The graph nodes are keywords; edges come from ``keyword_pairs`` (schema
v4), which after the chunk-level migration carries two row kinds:

* ``source='chunk_cooccur'`` — segment co-occurrence with a ``cooccur_count``
  and an ``npmi`` (Bouma 2009);
* ``source='llm_promoted'`` — pairs graduated from the vote buffer by
  ``grga index promote`` (``npmi`` may be NULL).

Given a small seed set (typically 1-5 keywords from the user query) PPR
returns the keywords most closely related on the graph, without ever
touching an LLM.

The default power-iteration implementation is a pure-Python hand-written
loop (design §5's "简单的 power iteration"). When numpy is available and
the graph is large enough (see :data:`NUMPY_MIN_EDGES`), the same
computation runs through a hand-written CSR ``np.bincount`` SpMV
(plan-2026-07-12-related-perf §2.3). numpy is an *optional* dependency
(``pip install grga[perf]``); grga still runs without it.
"""
from __future__ import annotations

import math
import os
import sqlite3
import itertools
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from grga.graph.schema import ensure_schema, CHUNK_COOCCUR_SOURCE

try:  # optional-dependency: numpy accelerates PPR on graphs >= NUMPY_MIN_EDGES
    import numpy as _np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover - environment dependent
    _np = None
    _HAS_NUMPY = False

DEFAULT_ALPHA = 0.15
DEFAULT_ITERS = 20
DEFAULT_TOP_K = 20
# Below this edge count the CSR/numpy overhead outweighs the Python loop
# (measured on the perf bench). Callers should not need to override this.
NUMPY_MIN_EDGES = int(os.environ.get("GRGA_PPR_NUMPY_MIN_EDGES", "50000"))


@dataclass
class Edge:
    """One undirected edge with an aggregated weight."""

    a: str
    b: str
    weight: float


def _fold(term: str) -> str:
    return term.casefold().strip()


# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 32766 (>=3.32) or 999
# (older). A large scope can touch >32k distinct keyword ids, so batch the
# ``id IN (...)`` translate query well under the conservative limit.
_SQL_IN_CHUNK = 900


def _fetch_id_to_text(
    conn: sqlite3.Connection, kw_ids: Sequence[int]
) -> Dict[int, str]:
    """``{kw_id: folded_kw}`` for ``kw_ids``, chunked to dodge the SQLite
    bound-variable limit on large scopes."""
    out: Dict[int, str] = {}
    for start in range(0, len(kw_ids), _SQL_IN_CHUNK):
        batch = kw_ids[start:start + _SQL_IN_CHUNK]
        ph = ",".join("?" for _ in batch)
        for r in conn.execute(
            f"SELECT id, kw FROM keywords WHERE id IN ({ph})", list(batch)
        ):
            out[int(r[0])] = _fold(r[1])
    return out


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def edge_weight(source: str, cooccur_count: float, npmi: Optional[float]) -> float:
    """Map a ``keyword_pairs`` row to a non-negative PPR edge weight.

    * ``chunk_cooccur``: weight = ``max(npmi, 0) + 0.01 * log1p(cooccur_count)``.
      npmi is the primary segment-level association strength (V5_npmi
      experiment). The small ``log1p(cooccur_count)`` floor rescues real
      co-occurrence edges whose npmi degenerates to exactly 0 on tiny
      corpora / ubiquitous keywords — a pure mathematical safeguard, no
      corpus-specific priors (HC2). The floor does *not* conflict with the
      P1.2 ``min_npmi`` filter: SQL WHERE drops sub-threshold edges before
      ``edge_weight`` sees them, so the floor cannot resurrect anything
      the threshold meant to exclude.
    * ``llm_promoted`` (npmi usually NULL): weight = ``log1p(cooccur_count)``
      — monotone in the LLM vote strength.

    Pure mathematical mapping — no corpus-specific priors (HC2).
    """
    count_w = math.log1p(max(float(cooccur_count), 0.0))
    if source == CHUNK_COOCCUR_SOURCE:
        npmi_w = max(float(npmi), 0.0) if npmi is not None else 0.0
        return npmi_w + 0.01 * count_w
    return count_w


def load_edges(
    conn: sqlite3.Connection,
    *,
    directory: Optional[str] = None,
    include_pairs: bool = True,
    min_weight: float = 0.0,
    min_npmi: float = 0.0,
) -> List[Edge]:
    """Load undirected edges from ``keyword_pairs`` (schema v5).

    All sources contribute; :func:`edge_weight` maps each row to a
    non-negative PPR weight. Rows are joined back to keyword text at
    load-time — the PPR graph's node type is a keyword string
    (:class:`IndexedGraph` uses the strings as node ids) so the callers'
    seed / result vocabulary stays contract-compatible.

    ``min_npmi`` (default 0.0) filters ``chunk_cooccur`` rows in SQL:
    edges with ``npmi < min_npmi`` are dropped. ``llm_promoted`` rows
    (npmi usually NULL) are unaffected. See plan-2026-07-12-related-perf
    §2.4.

    Legacy loader — the hot path uses :func:`load_edges_indexed` which
    skips the text JOIN and casefold entirely (plan §2.1).
    """
    ensure_schema(conn)
    edges: Dict[Tuple[str, str], float] = {}

    if include_pairs and _table_exists(conn, "keyword_pairs"):
        where_parts: List[str] = []
        params: List = []
        if directory is not None:
            # Resolve the scope path to a dir_id up front. An unknown
            # directory means no edges in this scope.
            row = conn.execute(
                "SELECT id FROM directories WHERE path = ?", (directory,)
            ).fetchone()
            if row is None:
                return []
            where_parts.append("p.dir_id = ?")
            params.append(int(row[0]))
        if min_npmi > 0.0:
            # llm_promoted rows have NULL npmi — keep them via source check.
            # Bind ``CHUNK_COOCCUR_SOURCE`` as a parameter so a schema-side
            # rename of the constant does not silently disable this filter
            # (drift trap flagged in review of plan-2026-07-12).
            where_parts.append(
                "(p.source != ? OR p.npmi >= ?)"
            )
            params.append(CHUNK_COOCCUR_SOURCE)
            params.append(float(min_npmi))
        where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
        rows = conn.execute(
            "SELECT ka.kw AS keyword_a, kb.kw AS keyword_b, "
            "       p.source AS source, p.cooccur_count AS cooccur_count, "
            "       p.npmi AS npmi "
            "FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id"
            f"{where}",
            params,
        ).fetchall()
        for row in rows:
            a, b = _fold(row["keyword_a"]), _fold(row["keyword_b"])
            if not a or not b or a == b:
                continue
            w = edge_weight(row["source"], row["cooccur_count"], row["npmi"])
            if w <= 0:
                continue
            key = tuple(sorted((a, b)))
            edges[key] = edges.get(key, 0.0) + w

    return [
        Edge(a=k[0], b=k[1], weight=w)
        for k, w in edges.items()
        if w > min_weight
    ]


def load_edges_indexed(
    conn: sqlite3.Connection,
    *,
    directory: Optional[str] = None,
    min_npmi: float = 0.0,
) -> "IndexedGraph":
    """id-native load: aggregate on int keyword ids, translate once at the end.

    Compared to :func:`load_edges` + :func:`build_indexed_graph`, this skips
    (1) two JOINs back to ``keywords`` per row, (2) 2·N ``casefold+strip``
    per edge, and (3) the intern-back step. On the ~3M-edge benchmark this
    replaces 34s of load + 14s of build with a single ~2s SQL fetch + a
    ~1s Python aggregate. See plan-2026-07-12-related-perf §2.1.

    The returned graph's ``node_ids`` array still holds keyword strings
    (a single batched ``SELECT id, kw FROM keywords WHERE id IN (...)``
    translates the used ids at the end), so trace / signals / output
    layers see zero change (契约兼容).
    """
    ensure_schema(conn)
    if not _table_exists(conn, "keyword_pairs"):
        return IndexedGraph(node_ids=[], node_index={}, neighbors=[], out_degree=[])

    where_parts: List[str] = []
    params: List = []
    if directory is not None:
        row = conn.execute(
            "SELECT id FROM directories WHERE path = ?", (directory,)
        ).fetchone()
        if row is None:
            return IndexedGraph(node_ids=[], node_index={}, neighbors=[], out_degree=[])
        where_parts.append("dir_id = ?")
        params.append(int(row[0]))
    if min_npmi > 0.0:
        # Same drift-guarded form as load_edges — bind the source constant.
        where_parts.append("(source != ? OR npmi >= ?)")
        params.append(CHUNK_COOCCUR_SOURCE)
        params.append(float(min_npmi))
    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    rows = conn.execute(
        "SELECT kw_a_id, kw_b_id, source, cooccur_count, npmi "
        f"FROM keyword_pairs{where}",
        params,
    ).fetchall()

    # Aggregate parallel edges on int-id pairs. Dense-assign local indices
    # via first-seen order so downstream numpy CSR (P3) sees contiguous
    # 0..n-1 ints.
    local_index: Dict[int, int] = {}
    neighbors: List[List[Tuple[int, float]]] = []
    out_degree: List[float] = []
    used_kw_ids: List[int] = []

    def _local(kwid: int) -> int:
        idx = local_index.get(kwid)
        if idx is None:
            idx = len(local_index)
            local_index[kwid] = idx
            neighbors.append([])
            out_degree.append(0.0)
            used_kw_ids.append(kwid)
        return idx

    agg: Dict[Tuple[int, int], float] = {}
    for row in rows:
        a_kw = int(row["kw_a_id"])
        b_kw = int(row["kw_b_id"])
        if a_kw == b_kw:
            continue
        w = edge_weight(row["source"], row["cooccur_count"], row["npmi"])
        if w <= 0:
            continue
        key = (a_kw, b_kw) if a_kw < b_kw else (b_kw, a_kw)
        agg[key] = agg.get(key, 0.0) + w

    for (a_kw, b_kw), w in agg.items():
        ia, ib = _local(a_kw), _local(b_kw)
        neighbors[ia].append((ib, w))
        neighbors[ib].append((ia, w))
        out_degree[ia] += w
        out_degree[ib] += w

    # One batched translate: kw_id -> folded kw text, produced in
    # local-index order so ``node_ids[i]`` matches ``neighbors[i]``. Chunked
    # through :func:`_fetch_id_to_text` because ``used_kw_ids`` can exceed
    # SQLITE_MAX_VARIABLE_NUMBER on large scopes.
    node_ids = [""] * len(used_kw_ids)
    if used_kw_ids:
        id_to_text = _fetch_id_to_text(conn, used_kw_ids)
        for local_idx, kwid in enumerate(used_kw_ids):
            node_ids[local_idx] = id_to_text.get(kwid, "")
    node_index = {name: i for i, name in enumerate(node_ids) if name}
    return IndexedGraph(
        node_ids=node_ids,
        node_index=node_index,
        neighbors=neighbors,
        out_degree=out_degree,
    )


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
        _csr_cache: lazy CSR materialisation for the numpy backend
            (:func:`_ppr_numpy`). Populated on first use and cached on the
            instance so subsequent PPR calls against the same
            ``adjacency_cache`` entry skip the O(nnz) rebuild. Treat as
            opaque — read via :func:`_get_csr`.
    """

    node_ids: List[str]
    node_index: Dict[str, int]
    neighbors: List[List[Tuple[int, float]]]
    out_degree: List[float]
    _csr_cache: Optional[Tuple] = None


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

    Dispatches between the pure-Python power iteration and the numpy CSR
    ``np.bincount`` SpMV backend based on :func:`_select_backend`
    (``GRGA_PPR_BACKEND=py|numpy|auto``, default ``auto``). Both backends
    produce byte-identical top-K output — dangling leak, seed exclusion,
    and tie-break rule ``(-score, node_id_string)`` are shared. See
    plan-2026-07-12-related-perf §2.3.
    """
    n = len(graph.node_ids)
    if n == 0:
        return []
    seed_folded = [s for s in (_fold(x) for x in seeds) if s]
    seed_ids = [graph.node_index[s] for s in seed_folded if s in graph.node_index]
    # Dedupe preserving first-seen order. Both backends previously
    # diverged on duplicate seeds: the Python dangling-leak loop
    # ``for sid in seed_ids: new_score[sid] += leak * personal[sid]``
    # double-counted, while numpy's vectorised ``new_score += leak * personal``
    # did not. Deduping up front eliminates the divergence and also
    # normalises ``personal.sum() == 1.0`` (writing ``1/len(seed_ids)``
    # twice to the same index otherwise leaves the personal vector
    # underweight). ``dict.fromkeys`` is C-level.
    seed_ids = list(dict.fromkeys(seed_ids))
    if not seed_ids:
        return []
    n_edges = sum(len(nb) for nb in graph.neighbors) // 2
    backend = _select_backend(n_edges)
    if backend == "numpy":
        return _ppr_numpy(graph, seed_ids, top_k=top_k, alpha=alpha, iters=iters)
    return _ppr_python(graph, seed_ids, top_k=top_k, alpha=alpha, iters=iters)


def _ppr_python(
    graph: IndexedGraph,
    seed_ids: Sequence[int],
    *,
    top_k: int,
    alpha: float,
    iters: int,
) -> List[Tuple[str, float]]:
    """Pure-Python power iteration. Reference implementation."""
    n = len(graph.node_ids)
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


def _select_backend(n_edges: int) -> str:
    """Choose ``py`` or ``numpy`` per ``GRGA_PPR_BACKEND`` and graph size."""
    env = os.environ.get("GRGA_PPR_BACKEND", "auto").lower()
    if env == "py":
        return "py"
    if env == "numpy":
        if not _HAS_NUMPY:
            raise RuntimeError(
                "GRGA_PPR_BACKEND=numpy but numpy is not importable — "
                "install with `pip install grga[perf]` or unset the variable"
            )
        return "numpy"
    # auto: numpy only when it wins (empirically ~50k edges)
    return "numpy" if _HAS_NUMPY and n_edges >= NUMPY_MIN_EDGES else "py"


def _get_csr(graph: IndexedGraph):
    """Return the numpy CSR view for ``graph``, populating the cache on first
    call. The cache lives on the :class:`IndexedGraph` instance (which is
    itself shared by :mod:`grga.graph.adjacency_cache`), so a warm-cache
    ``related --mode graph`` invocation reuses the CSR and skips the
    O(nnz) Python flatten. Fixes the review-flagged regression where every
    PPR call rebuilt CSR (plan-2026-07-12 §2.3 promise).

    Cached tuple layout: ``(n, indptr, indices, data, out_degree,
    row_of_edge, dangling_mask, node_ids_arr)``. All arrays are read-only
    numpy views — treat as immutable shared state.
    """
    if graph._csr_cache is not None:
        return graph._csr_cache
    assert _HAS_NUMPY, "_get_csr called without numpy available"
    n = len(graph.node_ids)
    row_lens = [len(nb) for nb in graph.neighbors]
    nnz = sum(row_lens)
    indptr = _np.empty(n + 1, dtype=_np.int64)
    indptr[0] = 0
    if n:
        indptr[1:] = _np.cumsum(row_lens, dtype=_np.int64)
    if nnz:
        # Flatten ``List[List[Tuple[int, float]]]`` in a single C-level
        # pass via ``chain.from_iterable`` + ``zip`` (selection A of the
        # 2026-07-12 review reply). Avoids the ~nnz Python ``list.append``
        # calls of the previous double-loop; on 43k-edge synthetic bench
        # this alone is ~2-3× faster, and the whole CSR is cached anyway
        # so this only pays on the first PPR call per warm graph.
        pairs = itertools.chain.from_iterable(graph.neighbors)
        idx_iter, w_iter = zip(*pairs)
        indices = _np.fromiter(idx_iter, dtype=_np.int64, count=nnz)
        data = _np.fromiter(w_iter, dtype=_np.float64, count=nnz)
    else:
        indices = _np.empty(0, dtype=_np.int64)
        data = _np.empty(0, dtype=_np.float64)
    out_degree = _np.asarray(graph.out_degree, dtype=_np.float64)
    row_of_edge = _np.repeat(_np.arange(n, dtype=_np.int64), _np.diff(indptr))
    dangling_mask = out_degree == 0
    # Object dtype so ``np.lexsort`` can compare Python strings.
    node_ids_arr = _np.asarray(graph.node_ids, dtype=object)
    csr = (
        n, indptr, indices, data, out_degree,
        row_of_edge, dangling_mask, node_ids_arr,
    )
    graph._csr_cache = csr
    return csr


def _ppr_numpy(
    graph: IndexedGraph,
    seed_ids: Sequence[int],
    *,
    top_k: int,
    alpha: float,
    iters: int,
) -> List[Tuple[str, float]]:
    """CSR SpMV backend.

    Uses ``np.bincount`` (indexed accumulation via C hot loop) rather than
    ``np.add.at`` (a 10-100× slower Python-level scatter): see
    ``experiments/related_perf_2026_07_12/FINDINGS.md`` §F2.

    Tie-break matches :func:`_ppr_python` **exactly** via a single
    ``np.lexsort`` over ``(node_ids_arr, -score)``. The earlier ``argsort``
    prefilter used array-index tie-break and diverged from the pure-Python
    ``(-score, node_id_string)`` sort at the prefilter boundary; lexsort
    bakes the string tie-break into the ordering itself and needs no
    prefilter (``lexsort`` on 100k items is sub-millisecond in numpy).
    """
    assert _HAS_NUMPY, "_ppr_numpy called without numpy available"
    (n, _indptr, indices, data, out_degree,
     row_of_edge, dangling_mask, node_ids_arr) = _get_csr(graph)

    personal = _np.zeros(n, dtype=_np.float64)
    personal[list(seed_ids)] = 1.0 / len(seed_ids)
    score = personal.copy()
    for _ in range(max(1, iters)):
        with _np.errstate(divide="ignore", invalid="ignore"):
            share = _np.where(
                out_degree > 0, (1.0 - alpha) * score / out_degree, 0.0
            )
        contrib = share[row_of_edge] * data
        # np.bincount handles duplicate indices in a C hot loop.
        new_score = alpha * personal + _np.bincount(
            indices, weights=contrib, minlength=n
        )
        leak = ((1.0 - alpha) * score * dangling_mask).sum()
        if leak:
            new_score += leak * personal
        score = new_score

    seed_id_set = set(seed_ids)
    # np.lexsort is stable and uses the *last* key as the primary. Ordering
    # by (-score, node_id_string) means primary = -score (higher first),
    # secondary = node_id ascending — identical to _ppr_python's
    # ``key=lambda item: (-item[1], item[0])`` sort.
    order = _np.lexsort((node_ids_arr, -score))
    out: List[Tuple[str, float]] = []
    for i in order:
        ii = int(i)
        if ii in seed_id_set or score[ii] <= 0:
            continue
        out.append((graph.node_ids[ii], float(score[ii])))
        if len(out) >= top_k:
            break
    return out


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
