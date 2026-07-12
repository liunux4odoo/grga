"""Tracer bullet — related --mode graph performance experiment.

Contract: SPEC.md (related_perf_2026_07_12)

Self-contained reimplementation of the load_edges -> build_indexed_graph ->
PPR -> doc-aggregation pipeline, with every optimisation from the perf plan
as an independent toggle in ``BenchConfig``. This lets us measure each
optimisation's contribution and their cumulative effect on ONE database
without touching src/grga/.

Optimisation toggles (SPEC §3.1):
  P1.1  apply_stopword_union   -- drop hub function-word edges (simulated)
  P1.2  min_npmi               -- npmi threshold edge filter
  P1.4a edge_quantile          -- convenience: quantile -> cutoff npmi
  P2.1  skip_build_adjacency   -- don't build the never-used dict adjacency
  P2.2  id_native              -- build graph from kw ids, translate once
  P3    ppr_backend            -- py | numpy | auto

All timing via time.perf_counter (Q6). Read-only DB access (mode=ro).
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import numpy as _np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover - environment dependent
    _np = None
    _HAS_NUMPY = False

DB_PATH = os.environ.get(
    "GRGA_BENCH_DB", str(Path.home() / ".config" / "grga" / "deep_keywords.db")
)
SCOPE = os.environ.get("GRGA_BENCH_SCOPE", "/media/lqw/DATA/sync/temp")

DEFAULT_ALPHA = 0.15
DEFAULT_ITERS = 20
DEFAULT_LIMIT = 20
DEFAULT_GRAPH_SIGNALS_LIMIT = 30
CHUNK_COOCCUR_SOURCE = "chunk_cooccur"
NUMPY_MIN_EDGES = int(os.environ.get("GRGA_PPR_NUMPY_MIN_EDGES", "50000"))

# ---- stopword sets (mirror src/grga/graph/build.py) -----------------------
# P1.1 simulates the effect of unioning the index-layer stopwords with the
# 26-char detection boundary set, applied at edge-load time (SPEC §2.1, Q1).
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "have", "has",
        "not", "was", "are", "you", "your", "but", "any", "all", "can",
        "should", "would", "could", "into", "onto", "over", "under", "than",
        "then", "when", "where", "which", "while", "will", "using", "used",
        "use", "one", "two", "may", "also", "such", "only", "each", "some",
        "true", "false", "none", "null", "self", "type", "types", "value",
        "values", "return", "returns", "class", "def", "function", "code",
        "test", "tests", "line", "lines", "file", "files", "path", "paths",
        "的", "了", "和", "或", "与", "以及", "但是", "因为", "所以",
        "这个", "那个", "一个", "一些", "我们",
    }
)
_CORE_STOP_BOUNDARIES = frozenset(
    "的了和与或是在这那有被把将从对为之其而且但也就都还只很才又已"
)
_INDEX_STOPWORDS = _STOPWORDS | _CORE_STOP_BOUNDARIES


# Drift guards — bench.py deliberately hard-copies these constants (SPEC §5:
# self-contained tracer bullet, no imports from src/grga/ in the hot path).
# But the copies MUST match production, or P1.1's numbers reflect a stale
# filter. Any drift trips the run before it produces misleading data.
def _assert_no_drift_from_production() -> None:
    """Fail fast if the copied constants diverged from src/grga/."""
    import sys as _sys
    _repo_src = str(Path(__file__).resolve().parents[2] / "src")
    if _repo_src not in _sys.path:
        _sys.path.insert(0, _repo_src)
    try:
        from grga.graph.build import (
            _STOPWORDS as _PROD_STOPWORDS,
            _CORE_STOP_BOUNDARIES as _PROD_CORE_BOUNDARIES,
        )
        from grga.graph.schema import CHUNK_COOCCUR_SOURCE as _PROD_CCS
    except ImportError as e:  # experiment can run without repo checkout,
        # but if it can, we insist on parity.
        import warnings
        warnings.warn(f"drift guard: could not import production constants ({e}); "
                      "bench numbers may not reflect current main-branch behaviour",
                      stacklevel=2)
        return
    assert _STOPWORDS == _PROD_STOPWORDS, (
        "bench.py _STOPWORDS drifted from src/grga/graph/build.py::_STOPWORDS; "
        "re-sync before trusting P1.1 numbers"
    )
    assert _CORE_STOP_BOUNDARIES == _PROD_CORE_BOUNDARIES, (
        "bench.py _CORE_STOP_BOUNDARIES drifted from "
        "src/grga/graph/build.py::_CORE_STOP_BOUNDARIES; re-sync"
    )
    assert CHUNK_COOCCUR_SOURCE == _PROD_CCS, (
        "bench.py CHUNK_COOCCUR_SOURCE drifted from "
        "src/grga/graph/schema.py::CHUNK_COOCCUR_SOURCE; re-sync"
    )


_assert_no_drift_from_production()


@dataclass
class BenchConfig:
    """One point in the optimisation matrix (SPEC §3)."""

    apply_stopword_union: bool = False   # P1.1
    min_npmi: float = 0.0                # P1.2
    edge_quantile: Optional[float] = None  # P1.4a (mutually exclusive w/ min_npmi)
    skip_build_adjacency: bool = False   # P2.1
    id_native: bool = False              # P2.2
    doc_agg_id_native: bool = False      # P2.3 (F1 fix — text IN kills perf)
    ppr_backend: str = "py"              # P3: py | numpy | auto

    def __post_init__(self):
        if self.edge_quantile is not None and self.min_npmi > 0.0:
            raise ValueError("min_npmi and edge_quantile are mutually exclusive (Q5)")


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def connect_ro(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _scope_dir_id(conn: sqlite3.Connection, scope: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM directories WHERE path = ?", (scope,)).fetchone()
    return int(row[0]) if row else None


def _stopword_ids(conn: sqlite3.Connection) -> List[int]:
    ph = ",".join("?" for _ in _INDEX_STOPWORDS)
    rows = conn.execute(
        f"SELECT id FROM keywords WHERE kw IN ({ph})", tuple(_INDEX_STOPWORDS)
    ).fetchall()
    return [int(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# P1.4a: quantile -> cutoff npmi (sampled)
# ---------------------------------------------------------------------------

def resolve_quantile_cutoff(
    conn: sqlite3.Connection,
    dir_id: int,
    quantile: float,
    *,
    sample_size: int = 100_000,
) -> Tuple[float, float]:
    """Return ``(cutoff_npmi, elapsed_ms)`` for keeping the top ``quantile``
    fraction of edges by npmi (SPEC §4.4)."""
    t0 = time.perf_counter()
    n = int(
        conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs WHERE dir_id = ?", (dir_id,)
        ).fetchone()[0]
    )
    if n == 0:
        return 0.0, (time.perf_counter() - t0) * 1000.0
    if n <= sample_size:
        off = int(n * quantile)
        row = conn.execute(
            "SELECT npmi FROM keyword_pairs WHERE dir_id = ? "
            "ORDER BY npmi DESC LIMIT 1 OFFSET ?",
            (dir_id, off),
        ).fetchone()
        cutoff = float(row[0]) if row and row[0] is not None else 0.0
    else:
        # keyword_pairs is WITHOUT ROWID; sample on the indexed int columns
        # instead. (kw_a_id + kw_b_id) % step is deterministic and roughly
        # uniform because keyword ids are densely assigned by the interner.
        step = max(1, n // sample_size)
        rows = conn.execute(
            "SELECT npmi FROM keyword_pairs "
            "WHERE dir_id = ? AND ((kw_a_id + kw_b_id) % ?) = 0",
            (dir_id, step),
        ).fetchall()
        vals = sorted((float(r[0]) for r in rows if r[0] is not None), reverse=True)
        if not vals:
            cutoff = 0.0
        else:
            idx = min(len(vals) - 1, int(len(vals) * quantile))
            cutoff = vals[idx]
    return cutoff, (time.perf_counter() - t0) * 1000.0


# ---------------------------------------------------------------------------
# Edge weight (V0 keeps log1p floor; P1.2 removes it — SPEC §4.3)
# ---------------------------------------------------------------------------

def edge_weight(source, cooccur_count, npmi, *, drop_floor: bool) -> float:
    count_w = math.log1p(max(float(cooccur_count), 0.0))
    if source == CHUNK_COOCCUR_SOURCE:
        npmi_w = max(float(npmi), 0.0) if npmi is not None else 0.0
        if drop_floor:
            return npmi_w
        return npmi_w + 0.01 * count_w
    return count_w


def _fold(term: str) -> str:
    return term.casefold().strip()


# ---------------------------------------------------------------------------
# IndexedGraph (node_ids stays str even in id-native mode — SPEC §4.6 plan 2a)
# ---------------------------------------------------------------------------

@dataclass
class IndexedGraph:
    node_ids: List[str]
    node_index: Dict[str, int]
    neighbors: List[List[Tuple[int, float]]]
    out_degree: List[float]

    @property
    def n_edges(self) -> int:
        return sum(len(nb) for nb in self.neighbors) // 2


# ---------------------------------------------------------------------------
# V0 / P1 path: text-native load_edges (JOIN + casefold), then build graph
# ---------------------------------------------------------------------------

def load_build_text(
    conn: sqlite3.Connection,
    dir_id: int,
    cfg: BenchConfig,
    *,
    stopword_ids: Optional[List[int]],
) -> Tuple[IndexedGraph, Dict[str, float]]:
    """Legacy path: SELECT ... JOIN keywords, fold to text, then intern back.

    Returns (graph, step_ms). Mirrors main-branch load_edges + build_indexed_graph.
    """
    step_ms: Dict[str, float] = {}
    drop_floor = cfg.min_npmi > 0.0 or cfg.edge_quantile is not None

    where = ["p.dir_id = ?"]
    params: List = [dir_id]
    if cfg.min_npmi > 0.0:
        where.append("(p.source != 'chunk_cooccur' OR p.npmi >= ?)")
        params.append(float(cfg.min_npmi))
    if cfg.apply_stopword_union and stopword_ids:
        ph = ",".join("?" for _ in stopword_ids)
        where.append(f"p.kw_a_id NOT IN ({ph}) AND p.kw_b_id NOT IN ({ph})")
        params.extend(stopword_ids)
        params.extend(stopword_ids)

    t0 = time.perf_counter()
    rows = conn.execute(
        "SELECT ka.kw AS a, kb.kw AS b, p.source AS source, "
        "p.cooccur_count AS cc, p.npmi AS npmi "
        "FROM keyword_pairs p "
        "JOIN keywords ka ON ka.id = p.kw_a_id "
        "JOIN keywords kb ON kb.id = p.kw_b_id "
        f"WHERE {' AND '.join(where)}",
        params,
    ).fetchall()
    step_ms["sql_fetch"] = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    agg: Dict[Tuple[str, str], float] = {}
    for row in rows:
        a, b = _fold(row["a"]), _fold(row["b"])
        if not a or not b or a == b:
            continue
        w = edge_weight(row["source"], row["cc"], row["npmi"], drop_floor=drop_floor)
        if w <= 0:
            continue
        key = (a, b) if a < b else (b, a)
        agg[key] = agg.get(key, 0.0) + w
    step_ms["fold_aggregate"] = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    graph = _build_indexed_from_str_pairs(agg)
    step_ms["build_indexed_graph"] = (time.perf_counter() - t0) * 1000.0

    return graph, step_ms


def _build_indexed_from_str_pairs(
    agg: Dict[Tuple[str, str], float]
) -> IndexedGraph:
    node_index: Dict[str, int] = {}
    neighbors: List[List[Tuple[int, float]]] = []

    def _id(name: str) -> int:
        idx = node_index.get(name)
        if idx is None:
            idx = len(node_index)
            node_index[name] = idx
            neighbors.append([])
        return idx

    out_degree: List[float] = []
    for (a, b), w in agg.items():
        ia, ib = _id(a), _id(b)
        while len(out_degree) < len(node_index):
            out_degree.append(0.0)
        neighbors[ia].append((ib, w))
        neighbors[ib].append((ia, w))
        out_degree[ia] += w
        out_degree[ib] += w

    node_ids = [""] * len(node_index)
    for name, idx in node_index.items():
        node_ids[idx] = name
    return IndexedGraph(node_ids, node_index, neighbors, out_degree)


# ---------------------------------------------------------------------------
# P2.2 path: id-native load (no JOIN, no fold), translate ids once at the end
# ---------------------------------------------------------------------------

def load_build_id_native(
    conn: sqlite3.Connection,
    dir_id: int,
    cfg: BenchConfig,
    *,
    stopword_ids: Optional[List[int]],
) -> Tuple[IndexedGraph, Dict[str, float]]:
    """id-native: aggregate on int kw ids; translate to text only for the
    final node_ids array (SPEC §4.6, plan 2a). node_ids stays str so trace /
    signals / output are unaffected."""
    step_ms: Dict[str, float] = {}
    drop_floor = cfg.min_npmi > 0.0 or cfg.edge_quantile is not None

    where = ["dir_id = ?"]
    params: List = [dir_id]
    if cfg.min_npmi > 0.0:
        where.append("(source != 'chunk_cooccur' OR npmi >= ?)")
        params.append(float(cfg.min_npmi))
    # Symmetric with load_build_text: push stopword filter into SQL so P2.2
    # measures the JOIN-elimination gain, not "filter moved to Python".
    if cfg.apply_stopword_union and stopword_ids:
        ph = ",".join("?" for _ in stopword_ids)
        where.append(f"kw_a_id NOT IN ({ph}) AND kw_b_id NOT IN ({ph})")
        params.extend(stopword_ids)
        params.extend(stopword_ids)

    t0 = time.perf_counter()
    rows = conn.execute(
        "SELECT kw_a_id, kw_b_id, source, cooccur_count, npmi "
        f"FROM keyword_pairs WHERE {' AND '.join(where)}",
        params,
    ).fetchall()
    step_ms["sql_fetch"] = (time.perf_counter() - t0) * 1000.0

    # id_aggregate: purely the pair aggregation on int ids (comparable to
    # load_build_text.fold_aggregate).
    t0 = time.perf_counter()
    agg: Dict[Tuple[int, int], float] = {}
    for row in rows:
        a_kw = int(row["kw_a_id"])
        b_kw = int(row["kw_b_id"])
        if a_kw == b_kw:
            continue
        w = edge_weight(row["source"], row["cooccur_count"], row["npmi"],
                        drop_floor=drop_floor)
        if w <= 0:
            continue
        key = (a_kw, b_kw) if a_kw < b_kw else (b_kw, a_kw)
        agg[key] = agg.get(key, 0.0) + w
    step_ms["id_aggregate"] = (time.perf_counter() - t0) * 1000.0

    # build_indexed_graph: neighbours/out_degree materialisation (comparable
    # to load_build_text.build_indexed_graph). Split from id_aggregate so
    # attribution vs V0's fold_aggregate + build_indexed_graph is honest.
    t0 = time.perf_counter()
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

    for (a_kw, b_kw), w in agg.items():
        ia, ib = _local(a_kw), _local(b_kw)
        neighbors[ia].append((ib, w))
        neighbors[ib].append((ia, w))
        out_degree[ia] += w
        out_degree[ib] += w
    step_ms["build_indexed_graph"] = (time.perf_counter() - t0) * 1000.0

    # Translate the used ids to text in ONE batch query.
    t0 = time.perf_counter()
    node_ids = [""] * len(used_kw_ids)
    if used_kw_ids:
        ph = ",".join("?" for _ in used_kw_ids)
        id_to_text = {
            int(r[0]): _fold(r[1])
            for r in conn.execute(
                f"SELECT id, kw FROM keywords WHERE id IN ({ph})", used_kw_ids
            )
        }
        for local_idx, kwid in enumerate(used_kw_ids):
            node_ids[local_idx] = id_to_text.get(kwid, "")
    node_index = {name: i for i, name in enumerate(node_ids)}
    step_ms["translate_ids"] = (time.perf_counter() - t0) * 1000.0

    graph = IndexedGraph(node_ids, node_index, neighbors, out_degree)
    return graph, step_ms


# ---------------------------------------------------------------------------
# P2.1 simulation: cost of the (never-used) dict adjacency
# ---------------------------------------------------------------------------

def build_dict_adjacency(graph: IndexedGraph) -> Dict[str, Dict[str, float]]:
    """The dead code path. V0/other variants pay this cost; P2.1 skips it."""
    adj: Dict[str, Dict[str, float]] = {}
    for i, nbrs in enumerate(graph.neighbors):
        name = graph.node_ids[i]
        d = adj.setdefault(name, {})
        for j, w in nbrs:
            d[graph.node_ids[j]] = d.get(graph.node_ids[j], 0.0) + w
    return adj


# ---------------------------------------------------------------------------
# PPR — pure Python (V0) and numpy CSR (P3)
# ---------------------------------------------------------------------------

def ppr_py(
    seeds: Sequence[str],
    graph: IndexedGraph,
    *,
    top_k: int,
    alpha: float = DEFAULT_ALPHA,
    iters: int = DEFAULT_ITERS,
) -> List[Tuple[str, float]]:
    n = len(graph.node_ids)
    if n == 0:
        return []
    seed_ids = [graph.node_index[s] for s in (_fold(x) for x in seeds)
                if s in graph.node_index]
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
                leak = (1.0 - alpha) * si
                for sid in seed_ids:
                    new_score[sid] += leak * personal[sid]
                continue
            share = (1.0 - alpha) * si / deg
            for j, w in nbrs:
                new_score[j] += share * w
        score = new_score
    ranked = [(graph.node_ids[i], s) for i, s in enumerate(score)
              if i not in seed_id_set and s > 0]
    ranked.sort(key=lambda it: (-it[1], it[0]))
    return ranked[:top_k]


def _csr_from_indexed(graph: IndexedGraph):
    n = len(graph.node_ids)
    row_lens = [len(nb) for nb in graph.neighbors]
    nnz = sum(row_lens)
    indptr = _np.empty(n + 1, dtype=_np.int64)
    indptr[0] = 0
    if n:
        indptr[1:] = _np.cumsum(row_lens, dtype=_np.int64)
    # Materialise indices/data via flat lists → np.fromiter/np.asarray. The
    # previous per-element scalar assignment (indices[pos] = j) is O(nnz)
    # in Python and was being timed inside the PPR step; keep the loop
    # tight and let numpy do the buffer copy in one shot.
    flat_j: List[int] = []
    flat_w: List[float] = []
    for nbrs in graph.neighbors:
        for j, w in nbrs:
            flat_j.append(j)
            flat_w.append(w)
    indices = _np.asarray(flat_j, dtype=_np.int64) if flat_j else _np.empty(0, dtype=_np.int64)
    data = _np.asarray(flat_w, dtype=_np.float64) if flat_w else _np.empty(0, dtype=_np.float64)
    out_degree = _np.asarray(graph.out_degree, dtype=_np.float64)
    return n, indptr, indices, data, out_degree


def ppr_numpy(
    seeds: Sequence[str],
    graph: IndexedGraph,
    *,
    top_k: int,
    alpha: float = DEFAULT_ALPHA,
    iters: int = DEFAULT_ITERS,
    csr=None,
) -> List[Tuple[str, float]]:
    """PPR via CSR SpMV. If ``csr`` is provided, its build time is billed
    to the caller (csr_build step); otherwise this function builds+bills
    itself so the code is still callable ad-hoc.
    """
    if not _HAS_NUMPY:
        raise RuntimeError("numpy backend requested but numpy not importable")
    n = len(graph.node_ids)
    if n == 0:
        return []
    seed_ids = [graph.node_index[s] for s in (_fold(x) for x in seeds)
                if s in graph.node_index]
    if not seed_ids:
        return []
    if csr is None:
        csr = _csr_from_indexed(graph)
    _, indptr, indices, data, out_degree = csr
    personal = _np.zeros(n, dtype=_np.float64)
    personal[seed_ids] = 1.0 / len(seed_ids)
    row_of_edge = _np.repeat(_np.arange(n, dtype=_np.int64), _np.diff(indptr))
    dangling_mask = out_degree == 0
    score = personal.copy()
    for _ in range(max(1, iters)):
        with _np.errstate(divide="ignore", invalid="ignore"):
            share = _np.where(out_degree > 0,
                              (1.0 - alpha) * score / out_degree, 0.0)
        contrib = share[row_of_edge] * data
        # np.bincount is 10-100× faster than np.add.at for scatter-add with
        # duplicate indices — same math, different backing implementation.
        new_score = alpha * personal + _np.bincount(
            indices, weights=contrib, minlength=n)
        leak = ((1.0 - alpha) * score * dangling_mask).sum()
        if leak:
            new_score += leak * personal
        score = new_score
    seed_id_set = set(seed_ids)
    # Tie-break MUST match ppr_py: (-score, node_id_string) lexicographic.
    # np.argsort alone would break ties by integer index (insertion order),
    # which diverges from production ppr.py::personalized_pagerank_indexed
    # and can flip the top-k at the boundary. Prefilter with argsort to
    # bound the candidate set, then apply the exact py-side sort key.
    order = _np.argsort(-score, kind="stable")
    # Pull enough candidates to survive any tie fan-out at the top-k
    # boundary. 3× top_k mirrors related.py's own multiplier.
    prefilter_n = min(len(order), max(top_k * 6, 100))
    candidates: List[Tuple[str, float]] = []
    for i in order[:prefilter_n]:
        ii = int(i)
        if ii in seed_id_set or score[ii] <= 0:
            continue
        candidates.append((graph.node_ids[ii], float(score[ii])))
    candidates.sort(key=lambda it: (-it[1], it[0]))
    return candidates[:top_k]


def _select_backend(cfg: BenchConfig, n_edges: int) -> str:
    if cfg.ppr_backend == "py":
        return "py"
    if cfg.ppr_backend == "numpy":
        if not _HAS_NUMPY:
            raise RuntimeError("ppr_backend=numpy but numpy unavailable")
        return "numpy"
    return "numpy" if _HAS_NUMPY and n_edges >= NUMPY_MIN_EDGES else "py"


# ---------------------------------------------------------------------------
# doc aggregation (seed + PPR keywords -> docs)  — identical across variants
# ---------------------------------------------------------------------------

def aggregate_docs(
    conn: sqlite3.Connection,
    dir_id: int,
    kw_score: Dict[str, float],
) -> List[dict]:
    if not kw_score:
        return []
    keyword_list = list(kw_score.keys())
    ph = ",".join("?" for _ in keyword_list)
    params = list(keyword_list) + [dir_id]
    rows = conn.execute(
        "SELECT d.doc_path AS doc_path, k.kw AS kw, "
        "SUM(kc.occurrence_count) AS total "
        "FROM keyword_chunks kc "
        "JOIN keywords k ON k.id = kc.kw_id "
        "JOIN docs d ON d.id = kc.doc_id "
        f"WHERE k.kw IN ({ph}) AND d.dir_id = ? "
        "GROUP BY d.doc_path, k.kw",
        params,
    ).fetchall()
    doc_score: Dict[str, float] = {}
    for row in rows:
        kw = _fold(row["kw"])
        w = kw_score.get(kw, 0.0)
        if w <= 0:
            continue
        doc_score[row["doc_path"]] = doc_score.get(row["doc_path"], 0.0) + \
            w * math.log1p(float(row["total"] or 0.0))
    ranked = sorted(doc_score.items(), key=lambda it: (-it[1], it[0]))
    return [{"doc_path": d, "score": s} for d, s in ranked]


def aggregate_docs_id_native(
    conn: sqlite3.Connection,
    dir_id: int,
    kw_score: Dict[str, float],
) -> List[dict]:
    """P2.3: aggregate on ``kw_id`` instead of ``k.kw``.

    The critical difference is the WHERE clause: ``kw_id IN (int_list)``
    hits keyword_chunks' composite primary key ``(kw_id, doc_id, chunk_id)``
    as a range scan, while ``k.kw IN (text_list)`` forces SQLite off the
    primary-key path (JOIN-through-keywords). Concept-verified at
    ~904× on this DB.

    Doc-scope filter still joins ``docs`` to preserve cross-scope safety
    (the ``dir_id`` check on doc). The concept-verified variant without
    the JOIN is ~11ms vs 37ms with it — safety is worth ~26ms.

    Returned shape matches :func:`aggregate_docs` exactly (list of
    ``{"doc_path": str, "score": float}``).
    """
    if not kw_score:
        return []
    # Step 1: batch-translate kw text -> kw_id. One IN query, tiny cost.
    kw_texts = list(kw_score.keys())
    ph = ",".join("?" for _ in kw_texts)
    kw_id_to_text: Dict[int, str] = {
        int(r[0]): r[1]
        for r in conn.execute(
            f"SELECT id, kw FROM keywords WHERE kw IN ({ph})", kw_texts
        )
    }
    if not kw_id_to_text:
        return []
    kw_id_list = list(kw_id_to_text.keys())
    ph_id = ",".join("?" for _ in kw_id_list)
    # Step 2: id-native aggregation. Hits keyword_chunks primary key.
    rows = conn.execute(
        "SELECT kc.doc_id AS doc_id, kc.kw_id AS kw_id, "
        "SUM(kc.occurrence_count) AS total "
        "FROM keyword_chunks kc "
        "JOIN docs d ON d.id = kc.doc_id "
        f"WHERE kc.kw_id IN ({ph_id}) AND d.dir_id = ? "
        "GROUP BY kc.doc_id, kc.kw_id",
        kw_id_list + [dir_id],
    ).fetchall()
    doc_score: Dict[int, float] = {}
    for row in rows:
        kw_text = kw_id_to_text.get(int(row["kw_id"]))
        if kw_text is None:
            continue
        w = kw_score.get(kw_text, 0.0)
        if w <= 0:
            continue
        did = int(row["doc_id"])
        doc_score[did] = doc_score.get(did, 0.0) + \
            w * math.log1p(float(row["total"] or 0.0))
    if not doc_score:
        return []
    # Step 3: batch doc_id -> doc_path.
    doc_ids = list(doc_score.keys())
    ph_d = ",".join("?" for _ in doc_ids)
    doc_paths = {
        int(r[0]): r[1]
        for r in conn.execute(
            f"SELECT id, doc_path FROM docs WHERE id IN ({ph_d})", doc_ids
        )
    }
    ranked = sorted(
        ((doc_paths.get(did, ""), s) for did, s in doc_score.items()),
        key=lambda it: (-it[1], it[0]),
    )
    return [{"doc_path": d, "score": s} for d, s in ranked if d]


# ---------------------------------------------------------------------------
# One full related --mode graph run under a config
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    total_ms: float
    step_ms: Dict[str, float]
    n_edges: int
    n_nodes: int
    ppr_backend_used: str
    top_keywords: List[str]
    top_docs: List[dict]
    quantile_cutoff: Optional[float] = None


def run_related_graph(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    cfg: BenchConfig,
    *,
    scope: str = SCOPE,
    limit: int = DEFAULT_LIMIT,
    stopword_ids: Optional[List[int]] = None,
) -> RunResult:
    step_ms: Dict[str, float] = {}
    t_all = time.perf_counter()

    dir_id = _scope_dir_id(conn, scope)
    if dir_id is None:
        raise RuntimeError(f"scope not found: {scope}")

    cutoff = None
    if cfg.edge_quantile is not None:
        cutoff, q_ms = resolve_quantile_cutoff(conn, dir_id, cfg.edge_quantile)
        step_ms["quantile_cutoff"] = q_ms
        cfg = BenchConfig(**{**asdict(cfg), "edge_quantile": None,
                             "min_npmi": cutoff})

    if cfg.apply_stopword_union and stopword_ids is None:
        stopword_ids = _stopword_ids(conn)

    if cfg.id_native:
        graph, load_ms = load_build_id_native(
            conn, dir_id, cfg, stopword_ids=stopword_ids)
    else:
        graph, load_ms = load_build_text(
            conn, dir_id, cfg, stopword_ids=stopword_ids)
    step_ms.update(load_ms)

    if not cfg.skip_build_adjacency:
        t0 = time.perf_counter()
        _ = build_dict_adjacency(graph)
        step_ms["build_adjacency"] = (time.perf_counter() - t0) * 1000.0

    n_edges = graph.n_edges
    backend = _select_backend(cfg, n_edges)
    if backend == "numpy":
        # Time CSR construction as its own step; ppr numpy vs py otherwise
        # unfairly charged the O(nnz) Python array-fill to PPR itself.
        t0 = time.perf_counter()
        csr = _csr_from_indexed(graph)
        step_ms["csr_build"] = (time.perf_counter() - t0) * 1000.0
        t0 = time.perf_counter()
        ranked = ppr_numpy(seeds, graph, top_k=max(limit * 3, limit), csr=csr)
        step_ms["ppr"] = (time.perf_counter() - t0) * 1000.0
    else:
        t0 = time.perf_counter()
        ranked = ppr_py(seeds, graph, top_k=max(limit * 3, limit))
        step_ms["ppr"] = (time.perf_counter() - t0) * 1000.0

    kw_score = {kw: sc for kw, sc in ranked}
    for s in (_fold(x) for x in seeds):
        if kw_score:
            kw_score.setdefault(s, max(kw_score.values()))
        else:
            kw_score.setdefault(s, 1.0)

    t0 = time.perf_counter()
    if cfg.doc_agg_id_native:
        docs = aggregate_docs_id_native(conn, dir_id, kw_score)
    else:
        docs = aggregate_docs(conn, dir_id, kw_score)
    step_ms["doc_agg_sql"] = (time.perf_counter() - t0) * 1000.0

    total_ms = (time.perf_counter() - t_all) * 1000.0
    return RunResult(
        total_ms=total_ms,
        step_ms=step_ms,
        n_edges=n_edges,
        n_nodes=len(graph.node_ids),
        ppr_backend_used=backend,
        top_keywords=[kw for kw, _ in ranked[:limit]],
        top_docs=docs[:limit],
        quantile_cutoff=cutoff,
    )
