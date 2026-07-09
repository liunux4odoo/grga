"""``related()`` API - L2 graph neighbours (Phase 4.4).

Three modes map to the three "adjacent document" definitions from
``docs/design/v2-design.md`` §7.3:

* ``graph``  - run Personalized PageRank over the keyword co-occurrence
  graph (``keyword_pairs`` + ``keyword_docs``), then resolve the top
  keywords back to the documents that contain them.
* ``dir``    - documents living in the same directories as the documents
  that contain the seed keywords (directory-level proximity).
* ``heading``- documents that share a ``heading_path`` with a document
  containing the seed keywords (cross-document chapter match).

All modes are read-only, deterministic, and never touch an LLM. Each
returns a list of :class:`Related` records sorted by ``score`` desc.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from grga.graph.ppr import (
    DEFAULT_ALPHA,
    DEFAULT_ITERS,
    build_adjacency,
    build_indexed_graph,
    load_edges,
    personalized_pagerank,
    personalized_pagerank_indexed,
)
from grga.graph.schema import connect, ensure_schema
from grga.trace import GraphAction

VALID_MODES = ("graph", "dir", "heading")
DEFAULT_LIMIT = 20


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

    @property
    def qualified_path(self) -> str:
        """Scope-qualified path (``<directory>/<doc_path>`` when scoped)."""
        if self.directory:
            return str(Path(self.directory) / self.doc_path)
        return self.doc_path

    def to_dict(self) -> dict:
        return {
            "doc_path": self.doc_path,
            "score": round(self.score, 6),
            "mode": self.mode,
            "reason": self.reason,
            "keywords": list(self.keywords),
            "directory": self.directory,
            "qualified_path": self.qualified_path,
        }


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
) -> List[Related]:
    """Return documents related to ``keywords`` under the chosen ``mode``.

    Args:
        keywords: seed keywords (query terms).
        mode: one of ``graph`` | ``dir`` | ``heading``.
        limit: maximum number of results.
        db_path: SQLite path (defaults to shared deep-search DB).
        directory: optional scope filter. In the user-scoped storage model
            this is a *query-time filter*, not a partition key: all
            ``index build`` invocations share the same DB and rows are
            grouped by their ``directory`` column.
        alpha / iters: PPR knobs (``graph`` mode only).
        cross_scope: ``mode=graph`` only. When ``True`` and ``directory``
            is given, seeds are validated against ``directory`` but PPR
            expansion and final doc collection cross into every other
            scope in the shared DB. Ignored for ``mode=dir`` (directory
            proximity is intrinsically scope-local) and for
            ``mode=heading`` (which is cross-scope by default; use
            ``same_scope=True`` to override).
        same_scope: ``mode=heading`` only. When ``True``, restrict heading
            matches to ``directory`` (the pre-v2.2 default). Otherwise the
            heading branch searches across every indexed scope so a
            shared heading path like ``Install > macOS`` surfaces
            documents from other corpora.

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
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        if mode == "graph":
            return _related_graph(
                conn, seeds, limit, scope_key, alpha, iters,
                cross_scope=cross_scope, db_path=target_db,
            )
        if mode == "dir":
            return _related_dir(conn, seeds, limit, scope_key)
        # heading: seeds are always sourced from --directory (when given) so
        # the walk starts in the caller's project, but the second-stage
        # heading match is cross-scope by default so callers can discover
        # sibling sections in other corpora. ``same_scope=True`` restricts
        # the second stage to --directory too (legacy behaviour).
        return _related_heading(
            conn, seeds, limit,
            seed_scope_key=scope_key,
            match_scope_key=scope_key if same_scope else None,
        )
    finally:
        conn.close()


def _docs_for_keywords(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    scope_key: Optional[str],
) -> Dict[str, float]:
    """Return ``{doc_path: summed occurrence_count}`` for the given keywords."""
    if not keywords:
        return {}
    placeholders = ",".join("?" for _ in keywords)
    params: List = list(keywords)
    where = f"keyword IN ({placeholders})"
    if scope_key is not None:
        where += " AND directory = ?"
        params.append(scope_key)
    rows = conn.execute(
        f"SELECT doc_path, occurrence_count FROM keyword_docs WHERE {where}",
        params,
    ).fetchall()
    docs: Dict[str, float] = {}
    for row in rows:
        docs[row["doc_path"]] = docs.get(row["doc_path"], 0.0) + float(row["occurrence_count"])
    return docs


def _scoped_docs_for_keywords(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    scope_key: Optional[str],
) -> Dict[tuple, float]:
    """Like :func:`_docs_for_keywords` but keyed on ``(directory, doc_path)``.

    Needed whenever results may span more than one scope (cross-scope or
    global queries), where a bare relative ``doc_path`` is ambiguous.
    """
    if not keywords:
        return {}
    placeholders = ",".join("?" for _ in keywords)
    params: List = list(keywords)
    where = f"keyword IN ({placeholders})"
    if scope_key is not None:
        where += " AND directory = ?"
        params.append(scope_key)
    rows = conn.execute(
        f"SELECT directory, doc_path, occurrence_count FROM keyword_docs WHERE {where}",
        params,
    ).fetchall()
    docs: Dict[tuple, float] = {}
    for row in rows:
        key = (row["directory"], row["doc_path"])
        docs[key] = docs.get(key, 0.0) + float(row["occurrence_count"])
    return docs


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
) -> List[Related]:
    """Public-facing wrapper preserving the historical ``List[Related]`` shape."""
    results, _, _ = _related_graph_impl(
        conn, seeds, limit, scope_key, alpha, iters,
        cross_scope=cross_scope, db_path=db_path,
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

    PPR is a diffusion, not a literal walk, so there is no ground-truth
    edge sequence to report. We reconstruct a *deterministic BFS tree*
    over the co-occurrence graph (restricted to the keywords PPR actually
    scored) to give viewers a stable, explainable traversal:

    * ``traverse_edge`` — one per BFS tree edge (keyword→keyword), then one
      per (contributing keyword → document) edge.
    * ``score_node`` — one per PPR-ranked keyword and one per result doc.

    ``fallback=True`` marks the "no graph signal" branch, where results are
    direct-seed hits: we emit only seed→file edges (hop 1) plus file scores.
    """
    from collections import deque

    from grga import trace as _trace

    actions: List[_trace.GraphAction] = []
    seed_set = {s for s in seeds}

    if fallback:
        # Direct-seed degrade: seeds point straight at the docs that contain
        # them; no keyword graph was walked.
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

    # ``wanted`` is the set of keywords PPR scored (``kw_score`` includes the
    # seeds via setdefault upstream). We BFS the *full* graph — not just the
    # scored subset — so a scored node reached only via an unscored
    # intermediate (seed → A(unscored) → B(scored)) still gets a correct
    # ``hops_from_seed`` and a connecting edge. Restricting the walk to
    # ``wanted`` used to orphan such nodes (missing ``traverse_edge`` +
    # ``hops_from_seed=-1``), which broke the "every walked edge is
    # reported" contract (trace-signals.md §5.1).
    wanted = {
        node_index[kw] for kw in kw_score if kw in node_index
    }
    seed_ids = [node_index[s] for s in seeds if s in node_index]
    seed_id_set = set(seed_ids)

    hop: Dict[int, int] = {sid: 0 for sid in seed_ids}
    parent: Dict[int, Tuple[int, float]] = {}  # child -> (parent, edge weight)
    dq: deque = deque(seed_ids)
    while dq:
        cur = dq.popleft()
        # Deterministic neighbour order: heaviest edge first, then node id.
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

    # Emit only the edges on shortest paths from a seed to a scored keyword:
    # walk parent pointers up from each reachable ``wanted`` node, collecting
    # each tree edge once. This keeps the trace bounded to the result-relevant
    # subtree while staying connected (no dangling scored node).
    emitted: Dict[Tuple[int, int], Tuple[float, int]] = {}
    for target in wanted:
        if target in seed_id_set or target not in hop:
            continue
        node = target
        while node in parent:
            par, w = parent[node]
            key = (par, node)
            if key in emitted:
                break  # ancestor chain already recorded
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

    # score_node for every PPR-ranked (non-seed) keyword, in descending
    # score then keyword order so the trace is byte-stable. A keyword with
    # positive PPR score is by construction reachable from a seed, so
    # ``hop_by_kw`` has an entry; the ``continue`` guard is defensive only.
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

    # Keyword→document edges + document scores.
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
) -> Tuple[List[Related], Dict[str, float], List["GraphAction"]]:
    """Run PPR + doc collection and return ``(results, kw_score, trace)``.

    .. note::
       Return arity changed in the trace-signals v1 rollout: this helper
       now yields a **3-tuple** ``(results, kw_score, trace)`` (was a
       2-tuple). ``trace`` is an empty list unless ``collect_trace=True``.
       Internal callers only; external code should use :func:`related` or
       :func:`related_graph_with_preview`.

    Split out of :func:`_related_graph` so callers that also need the
    keyword-score dict (e.g. potential future observability code) can get
    it without a second traversal. The primary caller,
    :func:`_related_graph`, drops the second element.

    The ``kw_score`` we return here is always the **scope-restricted** one
    — mixing it with cross-scope PPR results was tried once (P0-2 draft)
    and abandoned because it silently changed which docs came back
    whenever the caller passed ``--preview``. The preview annotation now
    runs its own unrestricted PPR via
    :func:`compute_crossed_scopes_preview` (whose adjacency is cached, so
    the extra pass is cheap after the first call).
    """
    # In cross-scope mode the seeds must still originate from ``scope_key``
    # (we do not want a keyword that only lives in another corpus to
    # bootstrap the walk), but PPR expansion and final document collection
    # are unrestricted. When ``scope_key`` is None (global query) cross_scope
    # is a no-op: the walk already spans the whole graph.
    if cross_scope and scope_key is not None:
        seed_docs = _docs_for_keywords(conn, seeds, scope_key)
        if not seed_docs:
            return [], {}, []
        edge_scope_key: Optional[str] = None
        doc_scope_key: Optional[str] = None
    else:
        edge_scope_key = scope_key
        doc_scope_key = scope_key

    # Adjacency is expensive to rebuild (~30% of the call) and unchanged
    # between successive queries against the same (db, scope). Route the
    # fetch through ``adjacency_cache`` when a ``db_path`` was threaded in
    # by the top-level ``related()``; fall back to a fresh build when
    # invoked without one (kept for legacy in-process callers that pass a
    # bare connection). The indexed graph drives the fast PPR path.
    if db_path is not None:
        from grga.graph.adjacency_cache import get_adjacency

        _, _, indexed = get_adjacency(db_path, edge_scope_key)
        ranked_keywords = personalized_pagerank_indexed(
            seeds, indexed, top_k=max(limit * 3, limit), alpha=alpha, iters=iters
        )
    else:
        edges = load_edges(conn, directory=edge_scope_key)
        indexed = build_indexed_graph(edges)
        ranked_keywords = personalized_pagerank_indexed(
            seeds, indexed, top_k=max(limit * 3, limit), alpha=alpha, iters=iters
        )
    if not ranked_keywords:
        # No graph signal: fall back to documents that directly contain the
        # seed keywords so the caller still gets something useful.
        direct = _scoped_docs_for_keywords(conn, seeds, doc_scope_key)
        results = [
            Related(
                doc_path=doc,
                score=score,
                mode="graph",
                reason="direct-seed",
                keywords=list(seeds),
                directory=directory,
            )
            for (directory, doc), score in direct.items()
        ]
        results.sort(key=lambda r: (-r.score, r.doc_path, r.directory or ""))
        capped = results[:limit]
        trace = (
            _build_graph_trace(indexed, seeds, {}, capped, fallback=True)
            if collect_trace else []
        )
        return capped, {}, trace

    kw_score = {kw: score for kw, score in ranked_keywords}
    # Include the seeds so their own documents are represented.
    for seed in seeds:
        kw_score.setdefault(seed, max(kw_score.values()) if kw_score else 1.0)

    keyword_list = list(kw_score.keys())
    placeholders = ",".join("?" for _ in keyword_list)
    params: List = list(keyword_list)
    where = f"keyword IN ({placeholders})"
    if doc_scope_key is not None:
        where += " AND directory = ?"
        params.append(doc_scope_key)
    rows = conn.execute(
        f"SELECT directory, keyword, doc_path, occurrence_count FROM keyword_docs WHERE {where}",
        params,
    ).fetchall()

    # Key on (directory, doc_path): the same relative path can exist in
    # several scopes when the query spans more than one, so collapsing on
    # doc_path alone would merge unrelated documents.
    doc_score: Dict[tuple, float] = {}
    doc_keywords: Dict[tuple, set] = {}
    for row in rows:
        kw = _fold(row["keyword"])
        weight = kw_score.get(kw, 0.0)
        if weight <= 0:
            continue
        key = (row["directory"], row["doc_path"])
        contribution = weight * float(row["occurrence_count"])
        doc_score[key] = doc_score.get(key, 0.0) + contribution
        doc_keywords.setdefault(key, set()).add(kw)

    results = [
        Related(
            doc_path=doc,
            score=score,
            mode="graph",
            reason="ppr",
            keywords=sorted(doc_keywords.get((directory, doc), set())),
            directory=directory,
        )
        for (directory, doc), score in doc_score.items()
    ]
    results.sort(key=lambda r: (-r.score, r.doc_path, r.directory or ""))
    capped = results[:limit]
    trace = (
        _build_graph_trace(indexed, seeds, kw_score, capped)
        if collect_trace else []
    )
    return capped, kw_score, trace


def _related_dir(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    scope_key: Optional[str],
) -> List[Related]:
    seed_docs = _scoped_docs_for_keywords(conn, seeds, scope_key)
    if not seed_docs:
        return []
    # Directory of each seed doc -> aggregated seed relevance.
    # ``scope`` here is the indexed scope root; ``parent`` is the doc's
    # filesystem parent *inside* that scope. We key on the tuple so two
    # scopes cannot silently share a `parent`.
    dir_weight: Dict[tuple, float] = {}
    seed_doc_keys = set(seed_docs.keys())
    for (scope, doc), weight in seed_docs.items():
        parent = str(Path(doc).parent)
        dir_weight[(scope, parent)] = dir_weight.get((scope, parent), 0.0) + weight
    if not dir_weight:
        return []

    where = ""
    params: List = []
    if scope_key is not None:
        where = " WHERE directory = ?"
        params.append(scope_key)
    rows = conn.execute(
        f"SELECT DISTINCT directory, doc_path FROM keyword_docs{where}",
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
            continue  # skip the seed docs themselves
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


def _related_heading(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    *,
    seed_scope_key: Optional[str],
    match_scope_key: Optional[str],
) -> List[Related]:
    # Seed selection and match diffusion are two independent scope concepts.
    # Seeds are limited to ``seed_scope_key`` (``--directory`` if given) so
    # the walk starts in the caller's project. The second stage decides
    # whether matching heading docs must also live in ``--directory``
    # (``match_scope_key`` set) or may come from any scope in the shared DB
    # (``match_scope_key=None``, the cross-scope default).
    #
    # Track seed docs as (directory, doc_path) so identically-named files in
    # different scopes are not conflated when the query spans multiple
    # scopes (the default for mode=heading in the user-scoped model).
    seed_docs = _scoped_docs_for_keywords(conn, seeds, seed_scope_key)
    if not seed_docs:
        return []
    seed_doc_set: set = set(seed_docs.keys())  # {(directory, doc_path), ...}
    seed_directories = {directory for directory, _ in seed_doc_set}

    # Heading paths present in the seed documents. Restrict the lookup to
    # the *seed scopes* (a subset of the DB) so identically-named files in
    # unrelated scopes cannot inject stray heading paths into the seed set.
    seed_doc_paths = sorted({doc for _, doc in seed_doc_set})
    doc_ph = ",".join("?" for _ in seed_doc_paths)
    dir_ph = ",".join("?" for _ in seed_directories)
    where = f"doc_path IN ({doc_ph}) AND directory IN ({dir_ph})"
    params: List = [*seed_doc_paths, *sorted(seed_directories)]
    rows = conn.execute(
        f"SELECT DISTINCT directory, doc_path, heading_path FROM heading_docs WHERE {where}",
        params,
    ).fetchall()
    if not rows:
        return []

    # Keep only heading rows attached to *actual* seed docs (the doc_path IN
    # + directory IN filter above is a cross-product; discard the mismatched
    # cells here so we do not pull heading paths from an unrelated scope).
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

    # Match on both the full serialised path (exact chapter reuse) and its
    # leaf heading (cross-document section reuse, e.g. two files each having
    # a "macOS" subsection under different parents). We assign the leaf
    # signal a lower weight so exact chapter matches float to the top.
    where_parts: List[str] = []
    params = []
    if seed_full_paths:
        ph = ",".join("?" for _ in seed_full_paths)
        where_parts.append(f"heading_path IN ({ph})")
        params.extend(seed_full_paths)
    if seed_leaves:
        ph = ",".join("?" for _ in seed_leaves)
        leaf_clause = (
            "heading_path IN (" + ph + ") OR "
            + " OR ".join(["heading_path LIKE ?" for _ in seed_leaves])
        )
        where_parts.append("(" + leaf_clause + ")")
        params.extend(seed_leaves)
        params.extend([f"% > {leaf}" for leaf in seed_leaves])
    combined = " OR ".join(f"({part})" for part in where_parts)
    if match_scope_key is not None:
        combined = f"({combined}) AND directory = ?"
        params.append(match_scope_key)
    rows = conn.execute(
        f"SELECT directory, heading_path, doc_path FROM heading_docs WHERE {combined}",
        params,
    ).fetchall()

    full_set = set(seed_full_paths)
    leaf_set = set(seed_leaves)
    # Score on (directory, doc_path) so an identically-named file in a
    # different scope is a distinct result, not a merge.
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


def _preview_from_kw_score(
    conn: sqlite3.Connection,
    kw_score: Dict[str, float],
    scope_key: str,
) -> dict:
    """Build a ``crossed_scopes_preview`` dict from an existing PPR ``kw_score``.

    Extracted from :func:`compute_crossed_scopes_preview` so the SQL /
    aggregation / polysemy-annotation stage is shared and independently
    testable. ``kw_score`` is expected to come from an *unrestricted* PPR
    (the preview is about cross-scope reach), which
    :func:`compute_crossed_scopes_preview` computes.

    ``scope_key`` MUST be a real scope string (never ``None``); the caller
    is responsible for the "no scope filter → no preview" short-circuit.
    """
    if not kw_score:
        return {"would_add": 0, "top_other_scopes": [], "top_bridge_keywords": []}

    keyword_list = list(kw_score.keys())
    placeholders = ",".join("?" for _ in keyword_list)
    params: List = list(keyword_list)
    params.append(scope_key)
    rows = conn.execute(
        f"""
        SELECT directory, keyword, doc_path, occurrence_count
        FROM keyword_docs
        WHERE keyword IN ({placeholders}) AND directory != ?
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
        bridge_weight[kw] = bridge_weight.get(kw, 0.0) + weight * float(row["occurrence_count"])

    top_scopes = sorted(
        ((scope, len(docs)) for scope, docs in scope_counts.items()),
        key=lambda item: (-item[1], item[0]),
    )[:5]
    top_bridges = sorted(
        bridge_weight.items(), key=lambda item: (-item[1], item[0])
    )[:5]
    # Annotate each bridge keyword with the same polysemy heuristic
    # ``graph inspect`` uses. This collapses the SKILL.md upgrade sequence
    # (inside → preview → inspect → enable) into three steps: the caller
    # no longer has to loop ``grga graph inspect`` over the top bridges to
    # detect same-name-different-meaning collisions before flipping
    # ``--cross-scope``. The batched helper resolves all top-N seed sets in
    # one ``keyword IN (...)`` scan of ``keyword_docs`` (previously N full
    # scans, one per bridge — the annotation was silently linear in total
    # corpus size).
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


def compute_crossed_scopes_preview(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    scope_key: Optional[str],
    alpha: float,
    iters: int,
    *,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Estimate what opening ``--cross-scope`` would pull into a graph query.

    Returns ``None`` when there is nothing to preview (no scope filter is
    active, so the query already spans the whole graph). Otherwise returns::

        {
          "would_add": <int docs reachable in *other* scopes>,
          "top_other_scopes": [{"scope": str, "count": int}, ...],
          "top_bridge_keywords": [{"keyword": str, "cross_weight": float}, ...],
        }

    Runs an unrestricted PPR (over the whole graph) and delegates the
    SQL / aggregation / polysemy-annotation stage to
    :func:`_preview_from_kw_score`. Called by
    :func:`related_graph_with_preview` when ``preview=True``, and safe to
    call standalone by external code. The adjacency is fetched through
    :mod:`grga.graph.adjacency_cache` so the extra PPR is cheap after the
    first call within a session.
    """
    if scope_key is None:
        return None

    if db_path is not None:
        from grga.graph.adjacency_cache import get_adjacency

        _, _, indexed = get_adjacency(db_path, None)
        ranked_keywords = personalized_pagerank_indexed(
            seeds, indexed, top_k=max(limit * 3, limit), alpha=alpha, iters=iters
        )
    else:
        edges = load_edges(conn, directory=None)
        indexed = build_indexed_graph(edges)
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
) -> dict:
    """``mode=graph`` variant returning results plus cross-scope observability.

    Returns ``{"results": List[Related], "crossed_scopes_preview": dict|None,
    "graph_actions": List[GraphAction]}``. ``graph_actions`` is populated
    only when ``collect_trace=True`` (the ``--json`` path); otherwise it is
    an empty list. The preview is **opt-in** (``preview=True``) because it
    runs a full-graph PPR whose cost scales with *every other* scope in the
    shared DB — undesirable as an implicit tax on ordinary scoped queries.
    It is still ``None`` when no ``directory`` scope is active even if
    requested. Used by ``related --preview --json`` so a caller can see what
    ``--cross-scope`` would pull in before committing to it.
    """
    seeds = [s for s in (_fold(k) for k in keywords) if s]
    if not seeds:
        return {"results": [], "crossed_scopes_preview": None, "graph_actions": []}

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_key = _resolve_directory(directory)
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        # Main result list ALWAYS uses the scope-restricted PPR so callers
        # get identical results with or without ``preview``. Prior to the
        # 2026-07-09 review-followup this branch used
        # ``_use_unrestricted_ppr=True`` and re-scored the in-scope docs
        # with cross-scope keyword weights — silently changing ~30% of the
        # top-20 depending on the preview flag. That broke the observability
        # contract (``--preview`` must not perturb the results it's
        # describing). Now the preview annotation runs its own unrestricted
        # PPR via the standalone ``compute_crossed_scopes_preview`` (its
        # adjacency is cached by :mod:`grga.graph.adjacency_cache`, so the
        # second PPR is cheap after the first call).
        results, _, trace = _related_graph_impl(
            conn, seeds, limit, scope_key, alpha, iters,
            cross_scope=cross_scope, db_path=target_db,
            collect_trace=collect_trace,
        )
        crossed_preview = None
        if preview and scope_key is not None:
            crossed_preview = compute_crossed_scopes_preview(
                conn, seeds, limit, scope_key, alpha, iters,
                db_path=target_db,
            )
        return {
            "results": results,
            "crossed_scopes_preview": crossed_preview,
            "graph_actions": trace,
        }
    finally:
        conn.close()
