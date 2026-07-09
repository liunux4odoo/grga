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
    load_edges,
    personalized_pagerank,
)
from grga.graph.schema import connect, ensure_schema

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
    where = f"LOWER(keyword) IN ({placeholders})"
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
    where = f"LOWER(keyword) IN ({placeholders})"
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
    results, _ = _related_graph_impl(
        conn, seeds, limit, scope_key, alpha, iters,
        cross_scope=cross_scope, db_path=db_path,
    )
    return results


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
    _use_unrestricted_ppr: bool = False,
) -> Tuple[List[Related], Dict[str, float]]:
    """Run PPR + doc collection and return ``(results, kw_score)``.

    Split out of :func:`_related_graph` so the preview path can reuse the
    same PPR result for both the main scope-restricted document list *and*
    the cross-scope bridge-keyword annotation, eliminating the second PPR
    that previously cost ~380 ms per preview call (see PERF plan P0-2).

    Args:
        _use_unrestricted_ppr: when True and ``scope_key`` is set, run PPR
            on the whole graph (unrestricted) but still filter final docs
            to ``scope_key``. Semantically equivalent to running a normal
            scoped query and then a *separate* unrestricted PPR for the
            preview, but at half the PPR cost. Used exclusively by
            :func:`related_graph_with_preview(preview=True)`.
    """
    # In cross-scope mode the seeds must still originate from ``scope_key``
    # (we do not want a keyword that only lives in another corpus to
    # bootstrap the walk), but PPR expansion and final document collection
    # are unrestricted. When ``scope_key`` is None (global query) cross_scope
    # is a no-op: the walk already spans the whole graph.
    if cross_scope and scope_key is not None:
        seed_docs = _docs_for_keywords(conn, seeds, scope_key)
        if not seed_docs:
            return [], {}
        edge_scope_key: Optional[str] = None
        doc_scope_key: Optional[str] = None
    elif _use_unrestricted_ppr and scope_key is not None:
        # Preview path: PPR spans whole graph so top_bridge_keywords is
        # relevant across scopes; main document list stays scope-limited.
        edge_scope_key = None
        doc_scope_key = scope_key
    else:
        edge_scope_key = scope_key
        doc_scope_key = scope_key

    # Adjacency is expensive to rebuild (~30% of the call) and unchanged
    # between successive queries against the same (db, scope). Route the
    # fetch through ``adjacency_cache`` when a ``db_path`` was threaded in
    # by the top-level ``related()``; fall back to a fresh build when
    # invoked without one (kept for legacy in-process callers that pass a
    # bare connection).
    if db_path is not None:
        from grga.graph.adjacency_cache import get_adjacency

        _, adjacency = get_adjacency(db_path, edge_scope_key)
    else:
        edges = load_edges(conn, directory=edge_scope_key)
        adjacency = build_adjacency(edges)
    ranked_keywords = personalized_pagerank(
        seeds, adjacency, top_k=max(limit * 3, limit), alpha=alpha, iters=iters
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
        return results[:limit], {}

    kw_score = {kw: score for kw, score in ranked_keywords}
    # Include the seeds so their own documents are represented.
    for seed in seeds:
        kw_score.setdefault(seed, max(kw_score.values()) if kw_score else 1.0)

    keyword_list = list(kw_score.keys())
    placeholders = ",".join("?" for _ in keyword_list)
    params: List = list(keyword_list)
    where = f"LOWER(keyword) IN ({placeholders})"
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
    return results[:limit], kw_score


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

    Extracted from :func:`compute_crossed_scopes_preview` so callers that
    already ran a PPR (see :func:`_related_graph_impl` with
    ``_use_unrestricted_ppr=True``) can reuse the same keyword rankings
    instead of paying a second PPR — that's the P0-2 halving of preview
    cost (see ``PERF_optimization_plan.md``).

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
        WHERE LOWER(keyword) IN ({placeholders}) AND directory != ?
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

    Standalone entry point: runs its own unrestricted PPR and then delegates
    the SQL/aggregation to :func:`_preview_from_kw_score`. Callers going
    through :func:`related_graph_with_preview` avoid the second PPR — the
    main scope-limited query is already run with
    ``_use_unrestricted_ppr=True`` and its ``kw_score`` is passed directly
    to :func:`_preview_from_kw_score`.
    """
    if scope_key is None:
        return None

    if db_path is not None:
        from grga.graph.adjacency_cache import get_adjacency

        _, adjacency = get_adjacency(db_path, None)
    else:
        edges = load_edges(conn, directory=None)
        adjacency = build_adjacency(edges)
    ranked_keywords = personalized_pagerank(
        seeds, adjacency, top_k=max(limit * 3, limit), alpha=alpha, iters=iters
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
) -> dict:
    """``mode=graph`` variant returning results plus cross-scope observability.

    Returns ``{"results": List[Related], "crossed_scopes_preview": dict|None}``.
    The preview is **opt-in** (``preview=True``) because it runs a
    full-graph PPR whose cost scales with *every other* scope in the shared
    DB — undesirable as an implicit tax on ordinary scoped queries. It is
    still ``None`` when no ``directory`` scope is active even if requested.
    Used by ``related --preview --json`` so a caller can see what
    ``--cross-scope`` would pull in before committing to it.
    """
    seeds = [s for s in (_fold(k) for k in keywords) if s]
    if not seeds:
        return {"results": [], "crossed_scopes_preview": None}

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_key = _resolve_directory(directory)
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        # When preview is requested and a scope is active, run *one*
        # unrestricted PPR whose ``kw_score`` feeds both the main
        # scope-limited document list and the cross-scope preview
        # annotation. Prior to P0-2 the preview path ran a separate second
        # PPR at ~380 ms extra per call.
        wants_shared_ppr = preview and scope_key is not None and not cross_scope
        results, kw_score = _related_graph_impl(
            conn, seeds, limit, scope_key, alpha, iters,
            cross_scope=cross_scope, db_path=target_db,
            _use_unrestricted_ppr=wants_shared_ppr,
        )
        crossed_preview = None
        if preview:
            if scope_key is None:
                # Nothing to preview: query already spans every scope.
                crossed_preview = None
            elif wants_shared_ppr:
                crossed_preview = _preview_from_kw_score(
                    conn, kw_score, scope_key
                )
            else:
                # cross_scope=True path: main PPR was already unrestricted
                # so its kw_score is usable for preview too, but the main
                # results are cross-scope by definition, so "would_add" is
                # not a meaningful escalation signal — fall back to the
                # legacy standalone preview call for a stable payload.
                crossed_preview = compute_crossed_scopes_preview(
                    conn, seeds, limit, scope_key, alpha, iters,
                    db_path=target_db,
                )
        return {"results": results, "crossed_scopes_preview": crossed_preview}
    finally:
        conn.close()
