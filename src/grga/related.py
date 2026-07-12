"""``related()`` API — L2 graph neighbours (chunk-level).

Three modes map to the three "adjacent document" definitions from
``docs/design/v2-design.md`` §7.3, refactored on top of the chunk-level
tables (schema v4):

* ``graph``  — run Personalized PageRank over the keyword pair graph
  (``keyword_pairs``), then resolve top keywords back to documents via
  ``keyword_chunks``.
* ``dir``    — documents living in the same directories as the documents
  that contain the seed keywords (directory-level proximity).
* ``heading``- documents that share a ``heading_path`` with a document
  containing the seed keywords (cross-document chapter match).

All modes are read-only, deterministic, and never touch an LLM. Each
returns a list of :class:`Related` records sorted by ``score`` desc.

The ``graph`` mode additionally reports a ``graph_signals`` metadata block
(seed segment-df, total-chunks-in-scope, top neighbour edges with npmi /
raw count / source) so callers can classify seeds (hub / domain / rare)
and re-rank neighbours themselves. See §4.1 of the chunk-migration plan.
"""
from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from grga.graph.ppr import (
    DEFAULT_ALPHA,
    DEFAULT_ITERS,
    personalized_pagerank_indexed,
)
from grga.graph.schema import connect, ensure_schema
from grga.trace import GraphAction

VALID_MODES = ("graph", "dir", "heading")
DEFAULT_LIMIT = 20
DEFAULT_GRAPH_SIGNALS_LIMIT = 30
# P1.2 (plan-2026-07-12-related-perf §2.4): default npmi threshold on
# chunk_cooccur edges. 0.2 empirically de-noises hub function-word edges
# without hurting top-K recall (experiment F4). Override via CLI
# ``--min-npmi`` or env ``GRGA_PPR_MIN_NPMI``.
DEFAULT_MIN_NPMI = 0.2


def _default_min_npmi() -> float:
    """Resolve the effective default ``min_npmi`` (env can override)."""
    raw = os.environ.get("GRGA_PPR_MIN_NPMI")
    if raw is None:
        return DEFAULT_MIN_NPMI
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_MIN_NPMI
    return max(0.0, val)


@dataclass
class Related:
    """A single related document (or keyword) with provenance.

    ``doc_path`` is relative to its indexed scope. In cross-scope results
    the same relative path can occur in several scopes, so ``directory``
    disambiguates: it holds the resolved scope root the document was
    indexed under (``None`` when unknown / not tracked). ``qualified_path``
    joins the two into a filesystem-resolvable path.
    """

    doc_path: str
    score: float
    mode: str
    reason: str = ""
    keywords: List[str] = field(default_factory=list)
    directory: Optional[str] = None
    # ``mode='graph'`` only: the chunk-level metadata block described in
    # the chunk-migration plan §4.1. ``None`` for other modes.
    graph_signals: Optional[dict] = None

    @property
    def qualified_path(self) -> str:
        """Scope-qualified path (``<directory>/<doc_path>`` when scoped)."""
        if self.directory:
            return str(Path(self.directory) / self.doc_path)
        return self.doc_path

    def to_dict(self) -> dict:
        out = {
            "doc_path": self.doc_path,
            "score": round(self.score, 6),
            "mode": self.mode,
            "reason": self.reason,
            "keywords": list(self.keywords),
            "directory": self.directory,
            "qualified_path": self.qualified_path,
        }
        if self.graph_signals is not None:
            out["graph_signals"] = self.graph_signals
        return out


def _fold(term: str) -> str:
    return term.casefold().strip()


def _resolve_directory(directory: Optional[Union[str, Path]]) -> Optional[str]:
    if directory is None:
        return None
    return str(Path(directory).expanduser().resolve())


def related(
    keywords: Sequence[str],
    *,
    mode: str = "graph",
    limit: int = DEFAULT_LIMIT,
    db_path: Union[str, Path, None] = None,
    directory: Optional[Union[str, Path]] = None,
    alpha: float = DEFAULT_ALPHA,
    iters: int = DEFAULT_ITERS,
    cross_scope: bool = False,
    same_scope: bool = False,
    graph_signals_limit: int = DEFAULT_GRAPH_SIGNALS_LIMIT,
    min_npmi: Optional[float] = None,
) -> List[Related]:
    """Return documents related to ``keywords`` under the chosen ``mode``.

    Args:
        keywords: seed keywords (query terms).
        mode: one of ``graph`` | ``dir`` | ``heading``.
        limit: maximum number of results.
        db_path: SQLite path (defaults to shared deep-search DB).
        directory: optional scope filter. In the user-scoped storage model
            this is a *query-time filter*, not a partition key.
        alpha / iters: PPR knobs (``graph`` mode only).
        cross_scope: ``mode=graph`` only.
        same_scope: ``mode=heading`` only.
        graph_signals_limit: cap on ``neighbor_edges`` inside the
            ``graph_signals`` metadata (``mode=graph`` only).
        min_npmi: ``mode=graph`` only. Drop ``chunk_cooccur`` edges with
            ``npmi < min_npmi`` before running PPR. ``None`` (default)
            resolves to :data:`DEFAULT_MIN_NPMI` (0.2) or the
            ``GRGA_PPR_MIN_NPMI`` env override.

    Raises:
        ValueError: if ``mode`` is not recognised.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"unknown related mode: {mode!r} (expected one of {VALID_MODES})")

    seeds = [s for s in (_fold(k) for k in keywords) if s]
    if not seeds:
        return []

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_key = _resolve_directory(directory)
    effective_min_npmi = _default_min_npmi() if min_npmi is None else float(min_npmi)
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        if mode == "graph":
            return _related_graph(
                conn, seeds, limit, scope_key, alpha, iters,
                cross_scope=cross_scope, db_path=target_db,
                graph_signals_limit=graph_signals_limit,
                min_npmi=effective_min_npmi,
            )
        if mode == "dir":
            return _related_dir(conn, seeds, limit, scope_key)
        return _related_heading(
            conn, seeds, limit,
            seed_scope_key=scope_key,
            match_scope_key=scope_key if same_scope else None,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chunk-level primitives shared by all modes.
# ---------------------------------------------------------------------------


def _docs_for_keywords_chunks(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    scope_key: Optional[str],
) -> Dict[Tuple[str, str], Tuple[float, set]]:
    """Aggregate ``keyword_chunks`` to ``{(directory, doc_path): (weight, kws)}``.

    ``weight`` is the summed ``occurrence_count`` across chunks and the
    seed keywords. ``kws`` is the set of seed keywords that hit this doc.

    Reads the normalized (v5) tables, joining ``keywords`` / ``docs`` /
    ``directories`` back to text so the returned mapping stays keyed on the
    scope path + relative doc path (contract-compatible).
    """
    if not keywords:
        return {}
    placeholders = ",".join("?" for _ in keywords)
    params: List = list(keywords)
    where = f"k.kw IN ({placeholders})"
    if scope_key is not None:
        where += " AND dir.path = ?"
        params.append(scope_key)
    rows = conn.execute(
        f"SELECT dir.path AS directory, k.kw AS keyword, d.doc_path AS doc_path, "
        f"SUM(kc.occurrence_count) AS total, COUNT(*) AS chunks "
        f"FROM keyword_chunks kc "
        f"JOIN keywords k ON k.id = kc.kw_id "
        f"JOIN docs d ON d.id = kc.doc_id "
        f"JOIN directories dir ON dir.id = d.dir_id "
        f"WHERE {where} "
        f"GROUP BY dir.path, k.kw, d.doc_path",
        params,
    ).fetchall()
    out: Dict[Tuple[str, str], Tuple[float, set]] = {}
    for row in rows:
        key = (row["directory"], row["doc_path"])
        prev_w, prev_kws = out.get(key, (0.0, set()))
        prev_kws = set(prev_kws)
        prev_kws.add(row["keyword"])
        out[key] = (prev_w + float(row["total"] or 0.0), prev_kws)
    return out


def _seed_chunk_df(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    scope_key: Optional[str],
) -> Dict[str, int]:
    """Return ``{keyword: chunk_df}`` — distinct chunks each keyword hits."""
    if not keywords:
        return {}
    placeholders = ",".join("?" for _ in keywords)
    params: List = list(keywords)
    where = f"k.kw IN ({placeholders})"
    if scope_key is not None:
        where += " AND d.dir_id = (SELECT id FROM directories WHERE path = ?)"
        params.append(scope_key)
    rows = conn.execute(
        f"SELECT k.kw AS keyword, COUNT(*) AS df "
        f"FROM keyword_chunks kc "
        f"JOIN keywords k ON k.id = kc.kw_id "
        f"JOIN docs d ON d.id = kc.doc_id "
        f"WHERE {where} "
        f"GROUP BY k.kw",
        params,
    ).fetchall()
    return {row["keyword"]: int(row["df"]) for row in rows}


def _total_chunks_in_scope(
    conn: sqlite3.Connection,
    scope_key: Optional[str],
) -> int:
    where = ""
    params: List = []
    if scope_key is not None:
        where = (
            " WHERE doc_id IN (SELECT d.id FROM docs d "
            "JOIN directories dir ON dir.id = d.dir_id WHERE dir.path = ?)"
        )
        params.append(scope_key)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM chunks{where}", params
    ).fetchone()
    return int(row["n"]) if row else 0


def _neighbor_edges_for_seeds(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    neighbor_keywords: Sequence[str],
    scope_key: Optional[str],
    limit: int,
) -> List[dict]:
    """Return ``keyword_pairs`` rows connecting seeds to their PPR neighbours.

    Each row carries ``a`` / ``b`` / ``npmi`` / ``cooccur_count`` /
    ``source``. Ordered by ``npmi DESC NULLS LAST`` then ``cooccur_count
    DESC`` for determinism, capped at ``limit``. Joins ``keywords`` back
    to text so ``a`` / ``b`` remain keyword strings (contract).
    """
    if not seeds or not neighbor_keywords or limit <= 0:
        return []
    seed_ph = ",".join("?" for _ in seeds)
    nbr_ph = ",".join("?" for _ in neighbor_keywords)
    params: List = [*seeds, *neighbor_keywords, *neighbor_keywords, *seeds]
    where = (
        f"((ka.kw IN ({seed_ph}) AND kb.kw IN ({nbr_ph})) "
        f" OR (ka.kw IN ({nbr_ph}) AND kb.kw IN ({seed_ph})))"
    )
    if scope_key is not None:
        where += " AND p.dir_id = (SELECT id FROM directories WHERE path = ?)"
        params.append(scope_key)
    rows = conn.execute(
        f"SELECT ka.kw AS keyword_a, kb.kw AS keyword_b, "
        f"p.npmi AS npmi, p.cooccur_count AS cooccur_count, p.source AS source "
        f"FROM keyword_pairs p "
        f"JOIN keywords ka ON ka.id = p.kw_a_id "
        f"JOIN keywords kb ON kb.id = p.kw_b_id "
        f"WHERE {where}",
        params,
    ).fetchall()
    seed_set = set(seeds)
    out: List[dict] = []
    for row in rows:
        a, b = row["keyword_a"], row["keyword_b"]
        # Orient (a, b) so ``a`` is the seed side (for caller convenience).
        if a in seed_set:
            oa, ob = a, b
        else:
            oa, ob = b, a
        npmi = row["npmi"]
        out.append(
            {
                "a": oa,
                "b": ob,
                "npmi": float(npmi) if npmi is not None else None,
                "cooccur_count": float(row["cooccur_count"] or 0.0),
                "source": row["source"],
            }
        )
    # Deterministic ordering: npmi desc (None last), then cooccur desc,
    # then (a, b) lexicographic.
    def _sort_key(edge: dict):
        npmi = edge["npmi"]
        # Non-None npmi should sort before None (higher priority); within
        # non-None, higher is better; ties broken by cooccur_count then a/b.
        return (
            0 if npmi is not None else 1,
            -(npmi if npmi is not None else 0.0),
            -edge["cooccur_count"],
            edge["a"],
            edge["b"],
        )
    out.sort(key=_sort_key)
    return out[:limit]


# ---------------------------------------------------------------------------
# mode = graph
# ---------------------------------------------------------------------------


def _related_graph(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    scope_key: Optional[str],
    alpha: float,
    iters: int,
    *,
    cross_scope: bool = False,
    db_path: Optional[Path] = None,
    graph_signals_limit: int = DEFAULT_GRAPH_SIGNALS_LIMIT,
    min_npmi: float = 0.0,
) -> List[Related]:
    results, _, _ = _related_graph_impl(
        conn, seeds, limit, scope_key, alpha, iters,
        cross_scope=cross_scope, db_path=db_path,
        graph_signals_limit=graph_signals_limit,
        min_npmi=min_npmi,
    )
    return results


def _build_graph_trace(
    indexed,
    seeds: Sequence[str],
    kw_score: Dict[str, float],
    results: Sequence[Related],
    *,
    fallback: bool = False,
) -> List["GraphAction"]:
    """Reconstruct a walk trace from a finished ``mode=graph`` computation.

    (Same algorithm as the doc-level implementation; only the underlying
    graph source changed. The trace contract remains a deterministic BFS
    tree over the co-occurrence graph.)
    """
    from collections import deque

    from grga import trace as _trace

    actions: List[_trace.GraphAction] = []
    seed_set = {s for s in seeds}

    if fallback:
        for r in results:
            file_node = _trace.encode_file(r.doc_path)
            for kw in sorted(r.keywords):
                if kw not in seed_set:
                    continue
                actions.append(
                    _trace.traverse_edge(
                        _trace.encode_keyword(kw), file_node,
                        edge_kind="keyword_doc", weight=r.score, hop=1,
                    )
                )
            actions.append(
                _trace.score_node(
                    file_node, algo="direct-seed",
                    score=r.score, hops_from_seed=1,
                )
            )
        return actions

    node_index = getattr(indexed, "node_index", {})
    node_ids = getattr(indexed, "node_ids", [])
    neighbors = getattr(indexed, "neighbors", [])

    wanted = {
        node_index[kw] for kw in kw_score if kw in node_index
    }
    seed_ids = [node_index[s] for s in seeds if s in node_index]
    seed_id_set = set(seed_ids)

    hop: Dict[int, int] = {sid: 0 for sid in seed_ids}
    parent: Dict[int, Tuple[int, float]] = {}
    dq: deque = deque(seed_ids)
    while dq:
        cur = dq.popleft()
        ordered = sorted(
            neighbors[cur],
            key=lambda t: (-t[1], node_ids[t[0]]),
        )
        for j, w in ordered:
            if j in hop:
                continue
            hop[j] = hop[cur] + 1
            parent[j] = (cur, w)
            dq.append(j)

    emitted: Dict[Tuple[int, int], Tuple[float, int]] = {}
    for target in wanted:
        if target in seed_id_set or target not in hop:
            continue
        node = target
        while node in parent:
            par, w = parent[node]
            key = (par, node)
            if key in emitted:
                break
            emitted[key] = (w, hop[node])
            node = par

    for (frm, to), (weight, h) in sorted(
        emitted.items(),
        key=lambda item: (item[1][1], node_ids[item[0][0]], node_ids[item[0][1]]),
    ):
        actions.append(
            _trace.traverse_edge(
                _trace.encode_keyword(node_ids[frm]),
                _trace.encode_keyword(node_ids[to]),
                edge_kind="co_occurrence", weight=weight, hop=h,
            )
        )

    hop_by_kw = {node_ids[idx]: h for idx, h in hop.items()}

    ranked = sorted(
        ((kw, sc) for kw, sc in kw_score.items() if kw not in seed_set),
        key=lambda item: (-item[1], item[0]),
    )
    for kw, sc in ranked:
        h = hop_by_kw.get(kw)
        if h is None:
            continue
        actions.append(
            _trace.score_node(
                _trace.encode_keyword(kw), algo="ppr",
                score=sc, hops_from_seed=h,
            )
        )

    for r in sorted(results, key=lambda x: (-x.score, x.doc_path, x.directory or "")):
        file_node = _trace.encode_file(r.doc_path)
        contributing = sorted(
            r.keywords,
            key=lambda kw: (-kw_score.get(kw, 0.0), kw),
        )
        min_kw_hop = None
        for kw in contributing:
            kw_hop = hop_by_kw.get(kw)
            edge_hop = (kw_hop + 1) if kw_hop is not None else 1
            if min_kw_hop is None or edge_hop < min_kw_hop:
                min_kw_hop = edge_hop
            actions.append(
                _trace.traverse_edge(
                    _trace.encode_keyword(kw), file_node,
                    edge_kind="keyword_doc",
                    weight=kw_score.get(kw, 0.0), hop=edge_hop,
                )
            )
        actions.append(
            _trace.score_node(
                file_node, algo="ppr",
                score=r.score,
                hops_from_seed=min_kw_hop if min_kw_hop is not None else 1,
            )
        )
    return actions


def _related_graph_impl(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    scope_key: Optional[str],
    alpha: float,
    iters: int,
    *,
    cross_scope: bool = False,
    db_path: Optional[Path] = None,
    collect_trace: bool = False,
    graph_signals_limit: int = DEFAULT_GRAPH_SIGNALS_LIMIT,
    min_npmi: float = 0.0,
) -> Tuple[List[Related], Dict[str, float], List["GraphAction"]]:
    """Run PPR + doc collection and return ``(results, kw_score, trace)``.

    Chunk-level: seed → doc resolution goes through ``keyword_chunks``
    instead of ``keyword_docs``; each result carries a ``graph_signals``
    metadata block with segment-df and neighbour-edge details.

    ``min_npmi`` (default 0.0 from the internal callers; public API
    resolves to :data:`DEFAULT_MIN_NPMI` via :func:`related`) filters
    ``chunk_cooccur`` edges before PPR — see plan-2026-07-12-related-perf
    §2.4.
    """
    # In cross-scope mode the seeds must still originate from ``scope_key``.
    if cross_scope and scope_key is not None:
        seed_docs = _docs_for_keywords_chunks(conn, seeds, scope_key)
        if not seed_docs:
            return [], {}, []
        edge_scope_key: Optional[str] = None
        doc_scope_key: Optional[str] = None
        signals_scope_key: Optional[str] = None
    else:
        edge_scope_key = scope_key
        doc_scope_key = scope_key
        signals_scope_key = scope_key

    if db_path is not None:
        from grga.graph.adjacency_cache import get_adjacency

        _, _, indexed = get_adjacency(
            db_path, edge_scope_key, min_npmi=min_npmi,
        )
        ranked_keywords = personalized_pagerank_indexed(
            seeds, indexed, top_k=max(limit * 3, limit), alpha=alpha, iters=iters
        )
    else:
        from grga.graph.ppr import load_edges_indexed

        indexed = load_edges_indexed(
            conn, directory=edge_scope_key, min_npmi=min_npmi,
        )
        ranked_keywords = personalized_pagerank_indexed(
            seeds, indexed, top_k=max(limit * 3, limit), alpha=alpha, iters=iters
        )

    # Segment-df / total-chunks / neighbour-edges are computed once per
    # call. All results in a single call share the same ``graph_signals``
    # block by reference (immutable dict) so the JSON payload stays compact.
    seed_chunk_df = _seed_chunk_df(conn, seeds, signals_scope_key)
    total_chunks = _total_chunks_in_scope(conn, signals_scope_key)
    if ranked_keywords:
        neighbor_kws = [kw for kw, _ in ranked_keywords]
    else:
        neighbor_kws = []
    neighbor_edges = _neighbor_edges_for_seeds(
        conn, seeds, neighbor_kws, edge_scope_key, graph_signals_limit,
    )
    graph_signals = {
        "seed_chunk_df": {kw: int(seed_chunk_df.get(kw, 0)) for kw in seeds},
        "total_chunks_in_scope": int(total_chunks),
        "neighbor_edges": neighbor_edges,
    }

    if not ranked_keywords:
        # No graph signal: fall back to documents that directly contain the
        # seed keywords via ``keyword_chunks``.
        direct = _docs_for_keywords_chunks(conn, seeds, doc_scope_key)
        results = [
            Related(
                doc_path=doc,
                score=score,
                mode="graph",
                reason="direct-seed",
                keywords=sorted(kws),
                directory=directory,
                graph_signals=graph_signals,
            )
            for (directory, doc), (score, kws) in direct.items()
        ]
        results.sort(key=lambda r: (-r.score, r.doc_path, r.directory or ""))
        capped = results[:limit]
        trace = (
            _build_graph_trace(indexed, seeds, {}, capped, fallback=True)
            if collect_trace else []
        )
        return capped, {}, trace

    kw_score = {kw: score for kw, score in ranked_keywords}
    for seed in seeds:
        kw_score.setdefault(seed, max(kw_score.values()) if kw_score else 1.0)

    doc_results = _aggregate_docs(
        conn, kw_score, doc_scope_key, graph_signals=graph_signals,
    )
    doc_results.sort(key=lambda r: (-r.score, r.doc_path, r.directory or ""))
    capped = doc_results[:limit]
    trace = (
        _build_graph_trace(indexed, seeds, kw_score, capped)
        if collect_trace else []
    )
    return capped, kw_score, trace


def _aggregate_docs(
    conn: sqlite3.Connection,
    kw_score: Dict[str, float],
    doc_scope_key: Optional[str],
    *,
    graph_signals: Optional[dict],
) -> List["Related"]:
    """Dispatch to id-native (P2.3, default) or legacy text-IN path.

    Rollback: set ``GRGA_DOC_AGG_LEGACY=1`` to force the pre-P2.3 SQL
    (``k.kw IN (text)`` with the docs+keywords double-JOIN).
    """
    if os.environ.get("GRGA_DOC_AGG_LEGACY"):
        return _aggregate_docs_text_legacy(
            conn, kw_score, doc_scope_key, graph_signals=graph_signals,
        )
    return _aggregate_docs_id_native(
        conn, kw_score, doc_scope_key, graph_signals=graph_signals,
    )


def _aggregate_docs_id_native(
    conn: sqlite3.Connection,
    kw_score: Dict[str, float],
    doc_scope_key: Optional[str],
    *,
    graph_signals: Optional[dict],
) -> List["Related"]:
    """P2.3 doc aggregation — aggregate on ``kw_id`` (integer PK).

    Replaces ``k.kw IN (60 text)`` with ``kw_id IN (60 int)``. That change
    lets SQLite walk the ``keyword_chunks`` composite PK
    ``(kw_id, doc_id, chunk_id)`` as a range scan instead of falling off
    the PK path and doing a full ``docs × chunks`` traversal. Benchmark
    delta: 11s → 53ms on 3.5M keyword_chunks (~210×). See
    ``docs/design/plan-2026-07-12-related-perf.md`` §2.2 and
    ``experiments/related_perf_2026_07_12/FINDINGS.md`` §F1.

    The output is byte-identical to :func:`_aggregate_docs_text_legacy`
    (verified in experiments as Jaccard=1.0 on top-20).
    """
    if not kw_score:
        return []

    # Step 1: batch-translate kw text -> kw_id. One indexed IN over
    # ``keywords`` — for ~60 kws this is single-digit ms.
    kw_texts = list(kw_score.keys())
    ph = ",".join("?" for _ in kw_texts)
    kw_id_to_text: Dict[int, str] = {
        int(r["id"]): _fold(r["kw"])
        for r in conn.execute(
            f"SELECT id, kw FROM keywords WHERE kw IN ({ph})", kw_texts
        )
    }
    if not kw_id_to_text:
        return []
    kw_ids = list(kw_id_to_text.keys())
    ph_id = ",".join("?" for _ in kw_ids)

    # Step 2: id-native aggregation. ``kw_id IN`` walks the composite
    # PK; the ``docs``/``directories`` joins are required to (a) filter
    # to ``doc_scope_key`` and (b) surface the human-readable directory
    # path (contract). Both joins are covering-index reads at scale.
    params: List = list(kw_ids)
    where = f"kc.kw_id IN ({ph_id})"
    if doc_scope_key is not None:
        where += " AND dir.path = ?"
        params.append(doc_scope_key)
    rows = conn.execute(
        f"SELECT kc.kw_id AS kw_id, "
        f"       dir.path AS directory, d.doc_path AS doc_path, "
        f"       SUM(kc.occurrence_count) AS total "
        f"FROM keyword_chunks kc "
        f"JOIN docs d ON d.id = kc.doc_id "
        f"JOIN directories dir ON dir.id = d.dir_id "
        f"WHERE {where} "
        f"GROUP BY dir.path, d.doc_path, kc.kw_id",
        params,
    ).fetchall()

    doc_score: Dict[tuple, float] = {}
    doc_keywords: Dict[tuple, set] = {}
    for row in rows:
        kw = kw_id_to_text.get(int(row["kw_id"]))
        if kw is None:
            continue
        weight = kw_score.get(kw, 0.0)
        if weight <= 0:
            continue
        key = (row["directory"], row["doc_path"])
        contribution = weight * math.log1p(float(row["total"] or 0.0))
        doc_score[key] = doc_score.get(key, 0.0) + contribution
        doc_keywords.setdefault(key, set()).add(kw)

    return [
        Related(
            doc_path=doc,
            score=score,
            mode="graph",
            reason="ppr",
            keywords=sorted(doc_keywords.get((directory, doc), set())),
            directory=directory,
            graph_signals=graph_signals,
        )
        for (directory, doc), score in doc_score.items()
    ]


def _aggregate_docs_text_legacy(
    conn: sqlite3.Connection,
    kw_score: Dict[str, float],
    doc_scope_key: Optional[str],
    *,
    graph_signals: Optional[dict],
) -> List["Related"]:
    """Pre-P2.3 doc aggregation — ``k.kw IN (text)`` path.

    Kept for rollback via ``GRGA_DOC_AGG_LEGACY=1`` and as the ground-truth
    reference in :file:`tests/test_related_doc_agg.py`.
    """
    if not kw_score:
        return []
    keyword_list = list(kw_score.keys())
    placeholders = ",".join("?" for _ in keyword_list)
    params: List = list(keyword_list)
    where = f"k.kw IN ({placeholders})"
    if doc_scope_key is not None:
        where += " AND dir.path = ?"
        params.append(doc_scope_key)
    rows = conn.execute(
        f"SELECT dir.path AS directory, k.kw AS keyword, d.doc_path AS doc_path, "
        f"SUM(kc.occurrence_count) AS total, COUNT(*) AS chunk_count "
        f"FROM keyword_chunks kc "
        f"JOIN keywords k ON k.id = kc.kw_id "
        f"JOIN docs d ON d.id = kc.doc_id "
        f"JOIN directories dir ON dir.id = d.dir_id "
        f"WHERE {where} "
        f"GROUP BY dir.path, k.kw, d.doc_path",
        params,
    ).fetchall()

    doc_score: Dict[tuple, float] = {}
    doc_keywords: Dict[tuple, set] = {}
    for row in rows:
        kw = _fold(row["keyword"])
        weight = kw_score.get(kw, 0.0)
        if weight <= 0:
            continue
        key = (row["directory"], row["doc_path"])
        contribution = weight * math.log1p(float(row["total"] or 0.0))
        doc_score[key] = doc_score.get(key, 0.0) + contribution
        doc_keywords.setdefault(key, set()).add(kw)

    return [
        Related(
            doc_path=doc,
            score=score,
            mode="graph",
            reason="ppr",
            keywords=sorted(doc_keywords.get((directory, doc), set())),
            directory=directory,
            graph_signals=graph_signals,
        )
        for (directory, doc), score in doc_score.items()
    ]


# ---------------------------------------------------------------------------
# mode = dir
# ---------------------------------------------------------------------------


def _related_dir(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    scope_key: Optional[str],
) -> List[Related]:
    seed_docs = _docs_for_keywords_chunks(conn, seeds, scope_key)
    if not seed_docs:
        return []
    dir_weight: Dict[tuple, float] = {}
    seed_doc_keys = set(seed_docs.keys())
    for (scope, doc), (weight, _kws) in seed_docs.items():
        parent = str(Path(doc).parent)
        dir_weight[(scope, parent)] = dir_weight.get((scope, parent), 0.0) + weight
    if not dir_weight:
        return []

    where = ""
    params: List = []
    if scope_key is not None:
        where = " WHERE dir.path = ?"
        params.append(scope_key)
    # dir-mode preserves the v4 semantics of "only docs that carry at
    # least one indexed keyword" by joining through ``keyword_chunks``
    # (the ``docs`` intern table also holds zero-keyword files).
    rows = conn.execute(
        f"SELECT DISTINCT dir.path AS directory, d.doc_path AS doc_path "
        f"FROM keyword_chunks kc "
        f"JOIN docs d ON d.id = kc.doc_id "
        f"JOIN directories dir ON dir.id = d.dir_id"
        f"{where}",
        params,
    ).fetchall()

    results: List[Related] = []
    for row in rows:
        scope = row["directory"]
        doc = row["doc_path"]
        parent = str(Path(doc).parent)
        weight = dir_weight.get((scope, parent))
        if weight is None:
            continue
        if (scope, doc) in seed_doc_keys:
            continue
        results.append(
            Related(
                doc_path=doc,
                score=weight,
                mode="dir",
                reason=f"dir:{parent}",
                keywords=list(seeds),
                directory=scope,
            )
        )
    results.sort(key=lambda r: (-r.score, r.doc_path, r.directory or ""))
    return results[:limit]


# ---------------------------------------------------------------------------
# mode = heading
# ---------------------------------------------------------------------------


def _related_heading(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    *,
    seed_scope_key: Optional[str],
    match_scope_key: Optional[str],
) -> List[Related]:
    seed_docs = _docs_for_keywords_chunks(conn, seeds, seed_scope_key)
    if not seed_docs:
        return []
    seed_doc_set: set = set(seed_docs.keys())
    seed_directories = {directory for directory, _ in seed_doc_set}

    seed_doc_paths = sorted({doc for _, doc in seed_doc_set})
    doc_ph = ",".join("?" for _ in seed_doc_paths)
    dir_ph = ",".join("?" for _ in seed_directories)
    where = f"d.doc_path IN ({doc_ph}) AND dir.path IN ({dir_ph})"
    params: List = [*seed_doc_paths, *sorted(seed_directories)]
    rows = conn.execute(
        f"SELECT DISTINCT dir.path AS directory, d.doc_path AS doc_path, "
        f"hd.heading_path AS heading_path "
        f"FROM heading_docs hd "
        f"JOIN docs d ON d.id = hd.doc_id "
        f"JOIN directories dir ON dir.id = d.dir_id "
        f"WHERE {where}",
        params,
    ).fetchall()
    if not rows:
        return []

    seed_full_paths: List[str] = []
    seed_leaves: List[str] = []
    for row in rows:
        if (row["directory"], row["doc_path"]) not in seed_doc_set:
            continue
        heading_path = row["heading_path"] or ""
        if not heading_path:
            continue
        seed_full_paths.append(heading_path)
        leaf = heading_path.rsplit(" > ", 1)[-1].strip()
        if leaf:
            seed_leaves.append(leaf)
    if not seed_full_paths:
        return []

    where_parts: List[str] = []
    params = []
    if seed_full_paths:
        ph = ",".join("?" for _ in seed_full_paths)
        where_parts.append(f"hd.heading_path IN ({ph})")
        params.extend(seed_full_paths)
    if seed_leaves:
        ph = ",".join("?" for _ in seed_leaves)
        leaf_clause = (
            "hd.heading_path IN (" + ph + ") OR "
            + " OR ".join(["hd.heading_path LIKE ?" for _ in seed_leaves])
        )
        where_parts.append("(" + leaf_clause + ")")
        params.extend(seed_leaves)
        params.extend([f"% > {leaf}" for leaf in seed_leaves])
    combined = " OR ".join(f"({part})" for part in where_parts)
    if match_scope_key is not None:
        combined = f"({combined}) AND dir.path = ?"
        params.append(match_scope_key)
    rows = conn.execute(
        f"SELECT dir.path AS directory, hd.heading_path AS heading_path, "
        f"d.doc_path AS doc_path "
        f"FROM heading_docs hd "
        f"JOIN docs d ON d.id = hd.doc_id "
        f"JOIN directories dir ON dir.id = d.dir_id "
        f"WHERE {combined}",
        params,
    ).fetchall()

    full_set = set(seed_full_paths)
    leaf_set = set(seed_leaves)
    doc_score: Dict[tuple, float] = {}
    doc_headings: Dict[tuple, set] = {}
    for row in rows:
        key = (row["directory"], row["doc_path"])
        if key in seed_doc_set:
            continue
        heading_path = row["heading_path"] or ""
        leaf = heading_path.rsplit(" > ", 1)[-1].strip()
        weight = 0.0
        if heading_path in full_set:
            weight += 1.0
        if leaf in leaf_set:
            weight += 0.5
        if weight <= 0:
            continue
        doc_score[key] = doc_score.get(key, 0.0) + weight
        doc_headings.setdefault(key, set()).add(heading_path)

    results = [
        Related(
            doc_path=doc,
            score=score,
            mode="heading",
            reason="shared-heading",
            keywords=sorted(doc_headings.get((directory, doc), set())),
            directory=directory,
        )
        for (directory, doc), score in doc_score.items()
    ]
    results.sort(key=lambda r: (-r.score, r.doc_path, r.directory or ""))
    return results[:limit]


# ---------------------------------------------------------------------------
# --cross-scope preview
# ---------------------------------------------------------------------------


def _preview_from_kw_score(
    conn: sqlite3.Connection,
    kw_score: Dict[str, float],
    scope_key: str,
) -> dict:
    """Build a ``crossed_scopes_preview`` dict from an existing PPR ``kw_score``.

    Reads ``keyword_chunks`` to count other-scope reach (post-migration).
    """
    if not kw_score:
        return {"would_add": 0, "top_other_scopes": [], "top_bridge_keywords": []}

    keyword_list = list(kw_score.keys())
    placeholders = ",".join("?" for _ in keyword_list)
    params: List = list(keyword_list)
    params.append(scope_key)
    rows = conn.execute(
        f"""
        SELECT dir.path AS directory, k.kw AS keyword, d.doc_path AS doc_path,
               SUM(kc.occurrence_count) AS total
        FROM keyword_chunks kc
        JOIN keywords k ON k.id = kc.kw_id
        JOIN docs d ON d.id = kc.doc_id
        JOIN directories dir ON dir.id = d.dir_id
        WHERE k.kw IN ({placeholders}) AND dir.path != ?
        GROUP BY dir.path, k.kw, d.doc_path
        """,
        params,
    ).fetchall()

    scope_counts: Dict[str, set] = {}
    bridge_weight: Dict[str, float] = {}
    seen_docs: set = set()
    for row in rows:
        directory = row["directory"]
        doc_key = (directory, row["doc_path"])
        kw = _fold(row["keyword"])
        weight = kw_score.get(kw, 0.0)
        if weight <= 0:
            continue
        if doc_key not in seen_docs:
            seen_docs.add(doc_key)
            scope_counts.setdefault(directory, set()).add(row["doc_path"])
        bridge_weight[kw] = bridge_weight.get(kw, 0.0) + weight * math.log1p(float(row["total"] or 0.0))

    top_scopes = sorted(
        ((scope, len(docs)) for scope, docs in scope_counts.items()),
        key=lambda item: (-item[1], item[0]),
    )[:5]
    top_bridges = sorted(
        bridge_weight.items(), key=lambda item: (-item[1], item[0])
    )[:5]
    from grga.graph.inspect import compute_suspected_polysemy_batch

    bridge_keywords = [kw for kw, _ in top_bridges]
    polysemy_map = compute_suspected_polysemy_batch(conn, bridge_keywords)
    top_bridge_out: List[dict] = []
    for kw, weight in top_bridges:
        top_bridge_out.append(
            {
                "keyword": kw,
                "cross_weight": round(weight, 6),
                "suspected_polysemy": bool(polysemy_map.get(_fold(kw), False)),
            }
        )
    return {
        "would_add": len(seen_docs),
        "top_other_scopes": [{"scope": s, "count": c} for s, c in top_scopes],
        "top_bridge_keywords": top_bridge_out,
    }


def resolve_edge_quantile_cutoff(
    *,
    db_path: Union[str, Path, None],
    directory: Optional[Union[str, Path]],
    quantile: float,
    sample_size: int = 100_000,
) -> float:
    """P1.4a: translate ``edge_quantile`` into an equivalent ``min_npmi``.

    "Keep the top ``quantile`` fraction of edges by npmi" → the npmi value
    at that rank. Convenience sugar over :func:`related`'s ``min_npmi``
    (plan-2026-07-12-related-perf §2.7); the two are mutually exclusive at
    the CLI. ``keyword_pairs`` is a ``WITHOUT ROWID`` table, so sampling
    uses ``(kw_a_id + kw_b_id) % step`` (deterministic, ~uniform because
    intern ids are dense) rather than ``rowid % step``.

    Returns 0.0 (no filtering) when the scope has no edges.
    """
    from grga.deep_search import DEFAULT_DB_PATH

    if not 0.0 < quantile <= 1.0:
        raise ValueError("quantile must be in the open interval (0, 1]")

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_key = _resolve_directory(directory)
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        dir_id: Optional[int] = None
        if scope_key is not None:
            row = conn.execute(
                "SELECT id FROM directories WHERE path = ?", (scope_key,)
            ).fetchone()
            if row is None:
                return 0.0
            dir_id = int(row[0])

        where = "npmi IS NOT NULL"
        params: List = []
        if dir_id is not None:
            where += " AND dir_id = ?"
            params.append(dir_id)
        n = int(
            conn.execute(
                f"SELECT COUNT(*) FROM keyword_pairs WHERE {where}", params
            ).fetchone()[0]
        )
        if n == 0:
            return 0.0

        if n <= sample_size:
            # Contract: "keep top `quantile` fraction of edges by npmi", used
            # downstream as ``npmi >= cutoff``. Retrieving rank ``int(n*q)``
            # (0-indexed) paired with ``>=`` keeps ``int(n*q)+1`` rows, so
            # use ``ceil(n*q) - 1`` to actually keep ``int(n*q)`` rows on
            # non-trivial cases. E.g. n=100 q=0.1 → keep 10, not 11.
            target_rank = max(1, int(math.ceil(n * quantile)))
            off = min(n - 1, target_rank - 1)
            row = conn.execute(
                f"SELECT npmi FROM keyword_pairs WHERE {where} "
                f"ORDER BY npmi DESC LIMIT 1 OFFSET ?",
                params + [off],
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0

        step = max(1, n // sample_size)
        sample_where = where + " AND ((kw_a_id + kw_b_id) % ?) = 0"
        rows = conn.execute(
            f"SELECT npmi FROM keyword_pairs WHERE {sample_where}",
            params + [step],
        ).fetchall()
        vals = sorted(
            (float(r[0]) for r in rows if r[0] is not None), reverse=True
        )
        if not vals:
            return 0.0
        target_rank = max(1, int(math.ceil(len(vals) * quantile)))
        idx = min(len(vals) - 1, target_rank - 1)
        return vals[idx]
    finally:
        conn.close()


def compute_crossed_scopes_preview(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    scope_key: Optional[str],
    alpha: float,
    iters: int,
    *,
    db_path: Optional[Path] = None,
    min_npmi: float = 0.0,
) -> Optional[dict]:
    """Estimate what opening ``--cross-scope`` would pull into a graph query."""
    if scope_key is None:
        return None

    if db_path is not None:
        from grga.graph.adjacency_cache import get_adjacency

        _, _, indexed = get_adjacency(db_path, None, min_npmi=min_npmi)
        ranked_keywords = personalized_pagerank_indexed(
            seeds, indexed, top_k=max(limit * 3, limit), alpha=alpha, iters=iters
        )
    else:
        from grga.graph.ppr import load_edges_indexed

        indexed = load_edges_indexed(conn, directory=None, min_npmi=min_npmi)
        ranked_keywords = personalized_pagerank_indexed(
            seeds, indexed, top_k=max(limit * 3, limit), alpha=alpha, iters=iters
        )
    kw_score = {kw: score for kw, score in ranked_keywords}
    for seed in seeds:
        kw_score.setdefault(seed, max(kw_score.values()) if kw_score else 1.0)
    return _preview_from_kw_score(conn, kw_score, scope_key)


def related_graph_with_preview(
    keywords: Sequence[str],
    *,
    limit: int = DEFAULT_LIMIT,
    db_path: Union[str, Path, None] = None,
    directory: Optional[Union[str, Path]] = None,
    alpha: float = DEFAULT_ALPHA,
    iters: int = DEFAULT_ITERS,
    cross_scope: bool = False,
    preview: bool = False,
    collect_trace: bool = False,
    graph_signals_limit: int = DEFAULT_GRAPH_SIGNALS_LIMIT,
    min_npmi: Optional[float] = None,
) -> dict:
    """``mode=graph`` variant returning results plus cross-scope observability.

    Returns ``{"results": List[Related], "crossed_scopes_preview": dict|None,
    "graph_actions": List[GraphAction]}``.

    ``min_npmi`` follows the same semantics as :func:`related`: ``None``
    resolves to :data:`DEFAULT_MIN_NPMI` / ``GRGA_PPR_MIN_NPMI`` env.
    """
    seeds = [s for s in (_fold(k) for k in keywords) if s]
    if not seeds:
        return {"results": [], "crossed_scopes_preview": None, "graph_actions": []}

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_key = _resolve_directory(directory)
    effective_min_npmi = _default_min_npmi() if min_npmi is None else float(min_npmi)
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        results, _, trace = _related_graph_impl(
            conn, seeds, limit, scope_key, alpha, iters,
            cross_scope=cross_scope, db_path=target_db,
            collect_trace=collect_trace,
            graph_signals_limit=graph_signals_limit,
            min_npmi=effective_min_npmi,
        )
        crossed_preview = None
        if preview and scope_key is not None:
            crossed_preview = compute_crossed_scopes_preview(
                conn, seeds, limit, scope_key, alpha, iters,
                db_path=target_db, min_npmi=effective_min_npmi,
            )
        return {
            "results": results,
            "crossed_scopes_preview": crossed_preview,
            "graph_actions": trace,
        }
    finally:
        conn.close()
