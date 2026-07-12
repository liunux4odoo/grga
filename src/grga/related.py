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

The ``graph`` mode additionally emits a query-level ``expansion`` block
(``seeds[]`` and ``expanded_keywords[]``, each carrying ``chunk_df``,
``ppr_score``, ``hops_from_seed``, and — for hop-1 neighbours — the
strongest direct-edge ``npmi`` / ``cooccur_count`` inlined) plus a
per-document ``keyword_contributions`` list. Together these let callers
classify seeds (hub / domain / rare) and filter noisy expansions
(e.g. rare-seed PPR pulling in generic hub words) without querying the
DB. See ``docs/design/related-signals-v2.md``.
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
from grga.graph.schema import connect, ensure_schema, CHUNK_COOCCUR_SOURCE

VALID_MODES = ("graph", "dir", "heading")
DEFAULT_LIMIT = 20
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
class KeywordContribution:
    """Per-``(document, keyword)`` evidence block for ``mode=graph``.

    Emitted inside :attr:`Related.keyword_contributions` so callers can
    tell "the seed hit this doc N times" apart from "a generic PPR
    expansion word grazed this doc once". Together with ``expansion``
    at the payload root, this is enough to filter false positives that
    ride in only on hub words (three-body-style noise).

    Fields:
        kw: the (case-folded) keyword string.
        occurrences: summed ``keyword_chunks.occurrence_count`` for this
            keyword in this document, within the current scope.
    """

    kw: str
    occurrences: int

    def to_dict(self) -> dict:
        return {"kw": self.kw, "occurrences": int(self.occurrences)}


@dataclass
class Related:
    """A single related document (or keyword) with provenance.

    ``doc_path`` is relative to its indexed scope. In cross-scope results
    the same relative path can occur in several scopes, so ``directory``
    disambiguates: it holds the resolved scope root the document was
    indexed under (``None`` when unknown / not tracked). ``qualified_path``
    joins the two into a filesystem-resolvable path.

    ``keyword_contributions`` (``mode=graph`` only) is the per-doc
    per-keyword occurrence breakdown documented in
    ``docs/design/related-signals-v2.md``. ``keywords`` remains populated in
    ``mode=dir`` (seeds) and ``mode=heading`` (matched heading paths)
    where per-keyword occurrence has no natural meaning.

    All query-level graph signals (``ppr_score`` / ``chunk_df`` /
    ``hops_from_seed`` / ``npmi`` / ``cooccur_count`` / ``source`` /
    ``via_seeds`` / ``total_chunks_in_scope``) now live once at the
    payload root under ``expansion``. The pre-v2.1 per-doc
    ``graph_signals`` block was removed as pure duplication (it was a
    query-level constant shared by every ``Related`` in a call).
    """

    doc_path: str
    score: float
    mode: str
    reason: str = ""
    keywords: List[str] = field(default_factory=list)
    directory: Optional[str] = None
    # ``mode='graph'`` only: per-keyword occurrence breakdown. ``None`` in
    # other modes so callers key on presence (design §related-signals-v2).
    keyword_contributions: Optional[List[KeywordContribution]] = None

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
        if self.keyword_contributions is not None:
            out["keyword_contributions"] = [
                c.to_dict() for c in self.keyword_contributions
            ]
        return out


def _fold(term: str) -> str:
    return term.casefold().strip()


def _resolve_directory(directory: Optional[Union[str, Path]]) -> Optional[str]:
    if directory is None:
        return None
    return str(Path(directory).expanduser().resolve())


# ---------------------------------------------------------------------------
# v8 keyword-primary API (Phase 3a — additive; Phase 3b will retire the
# doc-returning ``related(mode="graph")`` code path).
# Design: docs/design/plan-2026-07-12-keyword-primary.md §4.
# ---------------------------------------------------------------------------


@dataclass
class RelatedKeyword:
    """A single candidate keyword expanded from a seed via co-occurrence.

    The v8 ``related --mode graph`` return type. Replaces the pre-v8
    document-oriented :class:`Related` for that mode: the graph produces
    candidate *keywords* (with their association evidence), and the caller
    turns those into passages by running ripgrep. Multi-seed queries return
    per-seed rows (``via_seed`` disambiguates) — no cross-seed score fusion,
    per user decision (design §7 · decision D4).

    Fields:
        kw:            casefold candidate keyword.
        npmi:          Bouma npmi of the (via_seed, kw) chunk co-occurrence,
                       as stored at build time (never recomputed at query
                       time — see design §3.2).
        cooccur_count: raw chunk co-occurrence count.
        via_seed:      the seed that expanded to this candidate. Preserved
                       so callers can decide how to fuse (they can, we do
                       not).
        chunk_df:      candidate's own scope-level chunk df — hub-word
                       discriminator (design §related-signals-v2 §chunk_df).
        source:        ``chunk_cooccur`` (npmi-scored) or ``llm_promoted``
                       (npmi may be NULL upstream but is stored 0.0 here for
                       consistent typing).
    """

    kw: str
    npmi: float
    cooccur_count: float
    via_seed: str
    chunk_df: int
    source: str

    def to_dict(self) -> dict:
        return {
            "kw": self.kw,
            "npmi": round(float(self.npmi), 6),
            "cooccur_count": float(self.cooccur_count),
            "via_seed": self.via_seed,
            "chunk_df": int(self.chunk_df),
            "source": self.source,
        }


DEFAULT_LIMIT_PER_SEED = 30


def related_keywords(
    seeds: Sequence[str],
    *,
    limit_per_seed: int = DEFAULT_LIMIT_PER_SEED,
    min_npmi: Optional[float] = None,
    db_path: Union[str, Path, None] = None,
    directory: Optional[Union[str, Path]] = None,
) -> List[RelatedKeyword]:
    """v8 graph-expansion primitive: seeds → candidate keywords + evidence.

    For each seed keyword, load its top ``limit_per_seed`` neighbours from
    ``keyword_pairs`` (ordered by stored ``npmi DESC``), tagged with which
    seed they came from. **No PPR, no full-graph load, no adjacency cache.**
    The index ``(dir_id, kw_a_id, npmi DESC)`` + its mirror on ``kw_b_id``
    turn the double lookup into ``O(limit_per_seed)`` per seed, giving
    sub-millisecond hot-path even on hub seeds (fanout 1500+).

    Semantics:

    * ``min_npmi`` may only *tighten* the write-time floor
      (:data:`BUILD_MIN_NPMI` = 0.2). Passing a lower value has no effect —
      pairs below the write floor were never memorised (design §3.3).
    * Multi-seed queries return per-seed rows without fusion. Duplicate
      candidates (same ``kw`` reached from multiple seeds) appear multiple
      times with different ``via_seed`` values; caller decides how to
      aggregate.
    * ``directory`` is a per-scope filter: only pairs whose ``dir_id``
      matches the resolved scope are returned. ``None`` = no filter
      (cross-scope search).

    Args:
        seeds:          query seed keywords (casefold-normalised internally).
        limit_per_seed: max candidates per seed. Default 30.
        min_npmi:       optional read-side floor. ``None`` (default)
                        resolves to :data:`DEFAULT_MIN_NPMI` or
                        ``GRGA_PPR_MIN_NPMI``.
        db_path:        SQLite path (defaults to shared deep-search DB).
        directory:      optional scope filter (resolved absolute path).

    Returns:
        Flat list of :class:`RelatedKeyword`, sorted by
        ``(via_seed, -npmi, kw)``. Empty when no seed resolves.
    """
    seed_folded = [s for s in (_fold(k) for k in seeds) if s]
    if not seed_folded:
        return []

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_key = _resolve_directory(directory)
    effective_min_npmi = (
        _default_min_npmi() if min_npmi is None else float(min_npmi)
    )
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        return _related_keywords_impl(
            conn,
            seed_folded,
            limit_per_seed=limit_per_seed,
            min_npmi=effective_min_npmi,
            scope_key=scope_key,
        )
    finally:
        conn.close()


def _related_keywords_impl(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    *,
    limit_per_seed: int,
    min_npmi: float,
    scope_key: Optional[str],
) -> List[RelatedKeyword]:
    """Core query path for :func:`related_keywords`.

    Split out so tests can hand-inject a connection without going through
    the DB-path resolver. The DB schema must already be at v8.
    """
    # Resolve dir_id filter (if any). Unknown scope → zero results.
    dir_id: Optional[int] = None
    if scope_key is not None:
        row = conn.execute(
            "SELECT id FROM directories WHERE path = ?", (scope_key,)
        ).fetchone()
        if row is None:
            return []
        dir_id = int(row[0])

    # Resolve seeds → kw_ids in one batched query, preserving input order.
    placeholders = ",".join("?" for _ in seeds)
    seed_id_rows = list(
        conn.execute(
            f"SELECT id, kw FROM keywords WHERE kw IN ({placeholders})",
            list(seeds),
        )
    )
    text_to_id: Dict[str, int] = {str(r[1]): int(r[0]) for r in seed_id_rows}
    seed_ids: List[Tuple[int, str]] = []
    for s in seeds:
        if s in text_to_id:
            seed_ids.append((text_to_id[s], s))
    if not seed_ids:
        return []

    dir_filter = ""
    dir_params: List = []
    if dir_id is not None:
        dir_filter = "dir_id = ? AND "
        dir_params = [dir_id]
    # TODO(perf): cross-scope path (dir_id is None) currently full-scans
    # idx_keyword_pairs_b because the (dir_id, kw_a_id, npmi DESC) composite
    # cannot be used without a dir_id prefix. Warm cost ~700 ms on prod DB
    # vs ~14 ms with a scope. Acceptable for the rare --cross-scope case,
    # but worth revisiting (iterate known dir_ids and UNION, or add a
    # non-prefixed npmi index for cross-scope) if it becomes a hot path.

    seen_kw_ids: set = set()
    per_seed_rows: List[Tuple[str, List[Tuple[int, str, float, float]]]] = []

    for (seed_id, seed_text) in seed_ids:
        # chunk_cooccur a-side + b-side; each ORDER BY npmi DESC LIMIT
        # walks ``idx_kp_a_npmi`` / ``idx_kp_b_npmi`` in index order.
        rows_a = conn.execute(
            f"SELECT kw_b_id, npmi, cooccur_count, source "
            f"FROM keyword_pairs "
            f"WHERE {dir_filter}kw_a_id = ? AND source = ? AND npmi >= ? "
            f"ORDER BY npmi DESC LIMIT ?",
            (*dir_params, seed_id, CHUNK_COOCCUR_SOURCE,
             float(min_npmi), int(limit_per_seed)),
        ).fetchall()
        rows_b = conn.execute(
            f"SELECT kw_a_id, npmi, cooccur_count, source "
            f"FROM keyword_pairs "
            f"WHERE {dir_filter}kw_b_id = ? AND source = ? AND npmi >= ? "
            f"ORDER BY npmi DESC LIMIT ?",
            (*dir_params, seed_id, CHUNK_COOCCUR_SOURCE,
             float(min_npmi), int(limit_per_seed)),
        ).fetchall()
        # llm_promoted: no npmi filter (may be NULL).
        rows_llm_a = conn.execute(
            f"SELECT kw_b_id, npmi, cooccur_count, source "
            f"FROM keyword_pairs "
            f"WHERE {dir_filter}kw_a_id = ? AND source = ?",
            (*dir_params, seed_id, "llm_promoted"),
        ).fetchall()
        rows_llm_b = conn.execute(
            f"SELECT kw_a_id, npmi, cooccur_count, source "
            f"FROM keyword_pairs "
            f"WHERE {dir_filter}kw_b_id = ? AND source = ?",
            (*dir_params, seed_id, "llm_promoted"),
        ).fetchall()

        # Merge + dedupe within one seed. A pair with the same (kw_a, kw_b)
        # but different ``source`` values keeps both provenance rows.
        merged: Dict[Tuple[int, str], Tuple[float, float]] = {}
        for r in list(rows_a) + list(rows_b) + list(rows_llm_a) + list(rows_llm_b):
            nbr_id = int(r[0])
            npmi_val = float(r[1]) if r[1] is not None else 0.0
            cooccur = float(r[2])
            src = str(r[3])
            key = (nbr_id, src)
            prev = merged.get(key)
            if prev is None or npmi_val > prev[0]:
                merged[key] = (npmi_val, cooccur)

        ranked = sorted(
            [
                (nbr_id, src, npmi_val, cooccur)
                for (nbr_id, src), (npmi_val, cooccur) in merged.items()
            ],
            key=lambda t: (-t[2], t[0]),
        )[:limit_per_seed]

        for (nbr_id, _src, _npmi_val, _cooccur) in ranked:
            seen_kw_ids.add(nbr_id)
        per_seed_rows.append((seed_text, ranked))

    if not seen_kw_ids:
        return []

    # Batched translate: nbr kw_id → text + chunk_df. Chunk-partitioned to
    # dodge SQLITE_MAX_VARIABLE_NUMBER. ``keyword_stats`` rows are per-scope;
    # cross-scope (dir_id is None) sums across scopes.
    kw_text: Dict[int, str] = {}
    chunk_df_map: Dict[int, int] = {}
    id_list = list(seen_kw_ids)
    chunk = 900
    for i in range(0, len(id_list), chunk):
        piece = id_list[i:i + chunk]
        ph = ",".join("?" for _ in piece)
        for r in conn.execute(
            f"SELECT id, kw FROM keywords WHERE id IN ({ph})", piece
        ):
            kw_text[int(r[0])] = str(r[1])
        if dir_id is not None:
            for r in conn.execute(
                f"SELECT kw_id, chunk_df FROM keyword_stats "
                f"WHERE kw_id IN ({ph}) AND dir_id = ?",
                (*piece, dir_id),
            ):
                chunk_df_map[int(r[0])] = int(r[1])
        else:
            for r in conn.execute(
                f"SELECT kw_id, SUM(chunk_df) FROM keyword_stats "
                f"WHERE kw_id IN ({ph}) GROUP BY kw_id",
                piece,
            ):
                chunk_df_map[int(r[0])] = int(r[1] or 0)

    all_results: List[RelatedKeyword] = []
    for seed_text, ranked in per_seed_rows:
        for (nbr_id, src, npmi_val, cooccur) in ranked:
            kw = kw_text.get(nbr_id, "")
            if not kw:
                continue
            all_results.append(
                RelatedKeyword(
                    kw=kw,
                    npmi=npmi_val,
                    cooccur_count=cooccur,
                    via_seed=seed_text,
                    chunk_df=chunk_df_map.get(nbr_id, 0),
                    source=src,
                )
            )
    all_results.sort(key=lambda r: (r.via_seed, -r.npmi, r.kw))
    return all_results


# ---------------------------------------------------------------------------
# Legacy ``related()`` API (Phase 3b will retire ``mode='graph'`` / ``dir``).
# ---------------------------------------------------------------------------


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


def _chunk_df_for_kws(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    scope_key: Optional[str],
) -> Dict[str, int]:
    """Return ``{keyword: chunk_df}`` — distinct chunks each keyword hits.

    Used for both the seed side and the PPR-expanded neighbour side of the
    ``expansion`` block (v2.1: 2026-07-12). ``chunk_df`` is the direct
    hub-word signal callers use to filter noisy expansions — a rare seed
    like ``苍坤`` (df=3) versus an omnipresent neighbour like ``上古``
    (df~45k) is the discriminator that ``ppr_score`` alone cannot expose.
    """
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


def _edges_by_neighbor(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    neighbor_keywords: Sequence[str],
    scope_key: Optional[str],
) -> Dict[str, List[dict]]:
    """Return ``{neighbor_kw: [{seed, npmi, cooccur_count, source}, ...]}``.

    Internal helper. Edge lists per neighbour are sorted deterministically
    by ``(npmi desc NULLS last, cooccur_count desc, seed asc)`` so that
    :func:`_build_expansion` can trust ``edges[0]`` as "the strongest
    direct edge" and inline its ``npmi`` / ``cooccur_count`` onto each
    expanded keyword (v2.2 flattening).

    Neighbours with no direct seed edge (hop >= 2 in the PPR expansion,
    or all edges filtered by ``min_npmi``) simply do not appear in the
    returned dict; callers treat a missing key as an empty edge list.
    """
    if not seeds or not neighbor_keywords:
        return {}
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
    by_neighbor: Dict[str, List[dict]] = {}
    for row in rows:
        a, b = row["keyword_a"], row["keyword_b"]
        # Orient so the seed lands on the ``seed`` field.
        if a in seed_set:
            seed_kw, neighbor_kw = a, b
        else:
            seed_kw, neighbor_kw = b, a
        npmi = row["npmi"]
        by_neighbor.setdefault(neighbor_kw, []).append(
            {
                "seed": seed_kw,
                "npmi": float(npmi) if npmi is not None else None,
                "cooccur_count": int(row["cooccur_count"] or 0),
                "source": row["source"],
            }
        )

    def _sort_key(edge: dict):
        npmi = edge["npmi"]
        return (
            0 if npmi is not None else 1,
            -(npmi if npmi is not None else 0.0),
            -edge["cooccur_count"],
            edge["seed"],
        )

    for edges in by_neighbor.values():
        edges.sort(key=_sort_key)
    return by_neighbor


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
    min_npmi: float = 0.0,
) -> List[Related]:
    results, _, _ = _related_graph_impl(
        conn, seeds, limit, scope_key, alpha, iters,
        cross_scope=cross_scope, db_path=db_path,
        min_npmi=min_npmi,
    )
    return results


def _compute_hops(
    indexed,
    seeds: Sequence[str],
    kw_score: Dict[str, float],
) -> Dict[str, int]:
    """BFS on the indexed co-occurrence graph, returning ``{kw: hops_from_seed}``.

    Only keywords the caller actually asks about (i.e. present in
    ``kw_score`` plus the seeds themselves) are returned. Traversal is
    deterministic: neighbours are visited in ``(-weight, node_id)`` order,
    matching the ordering used everywhere else in this module so hop
    numbers stay stable across runs.

    Replaces the pre-2026-07-12 ``_build_graph_trace`` which reconstructed
    a full BFS-tree action log for the (now-removed) trace-signals v1
    contract. See ``docs/design/related-signals-v2.md`` for the rationale
    behind the switch to per-keyword ``hops_from_seed`` in
    :func:`related_graph_with_preview`'s ``expansion`` block.
    """
    from collections import deque

    node_index = getattr(indexed, "node_index", {})
    node_ids = getattr(indexed, "node_ids", [])
    neighbors = getattr(indexed, "neighbors", [])

    wanted = {node_index[kw] for kw in kw_score if kw in node_index}
    seed_ids = [node_index[s] for s in seeds if s in node_index]
    wanted.update(seed_ids)

    hop: Dict[int, int] = {sid: 0 for sid in seed_ids}
    dq: deque = deque(seed_ids)
    while dq:
        cur = dq.popleft()
        ordered = sorted(
            neighbors[cur],
            key=lambda t: (-t[1], node_ids[t[0]]),
        )
        for j, _w in ordered:
            if j in hop:
                continue
            hop[j] = hop[cur] + 1
            dq.append(j)

    return {node_ids[idx]: h for idx, h in hop.items() if idx in wanted}


def _build_expansion(
    seeds: Sequence[str],
    kw_score: Dict[str, float],
    hops_by_kw: Dict[str, int],
    chunk_df: Dict[str, int],
    edges_by_neighbor: Dict[str, List[dict]],
    total_chunks_in_scope: int,
) -> dict:
    """Assemble the query-level ``expansion`` block (contract v2.2).

    Consolidates every per-keyword signal into one flat object per
    keyword. ``seeds[i]`` and ``expanded_keywords[j]`` are the only two
    places any keyword-level graph signal lives (see
    ``docs/design/related-signals-v2.md``).

    v2.2 flattens the pre-existing ``edges_to_seeds`` list. For a hop-1
    neighbour the *strongest* direct edge (npmi desc) is inlined as
    ``npmi`` / ``cooccur_count`` on the entry itself, dropping the
    single-element list wrapper and the redundant ``seed`` echo that
    dominated single-seed queries. ``source`` and ``via_seeds`` follow
    zero-value / single-value omission rules so the common case stays
    terse.

    Sort orders (deterministic):
    * ``seeds``: verbatim caller order (case-folded upstream).
    * ``expanded_keywords``: ``(-ppr_score, kw)``.

    Fields per entry:
    * ``kw``           - the (case-folded) keyword string.
    * ``ppr_score``    - PPR score (expanded only).
    * ``hops_from_seed`` - BFS distance from any seed (expanded only,
                           omitted when unreachable via the filtered graph).
    * ``chunk_df``     - distinct chunks the keyword hits in-scope.
                         Omitted when the keyword is absent from
                         ``keyword_chunks`` under the scope (rare; PPR
                         reached it via an ``llm_promoted`` edge that
                         carries no chunk).
    * ``npmi`` / ``cooccur_count`` - the strongest direct seed edge.
                         Present whenever a direct edge exists in
                         ``keyword_pairs`` (which ignores ``min_npmi``,
                         see trace-signals archive §7.1), independent of
                         ``hops_from_seed``. Absence means "no direct
                         edge at all"; presence with ``hops_from_seed > 1``
                         means "direct edge exists but PPR routed around
                         it because ``min_npmi`` filtered it out — likely
                         weak / noisy". ``npmi`` may be ``null`` on
                         ``llm_promoted`` edges.
    * ``source``       - edge provenance, **only emitted when it is not
                         the default ``"chunk_cooccur"``** (i.e. only for
                         ``llm_promoted``); absence means chunk_cooccur.
    * ``via_seeds``    - **only in multi-seed queries** and only when the
                         neighbour has direct edges to more than one seed;
                         lists those seed strings (npmi desc). Single-seed
                         queries omit it (the seed is
                         ``seeds[0].kw``). A single-edge neighbour in a
                         multi-seed query omits it too (unambiguous).
    """
    seed_list = list(seeds)
    multi_seed = len(set(seed_list)) > 1
    seed_entries = []
    for kw in seed_list:
        entry: Dict[str, object] = {"kw": kw}
        df = chunk_df.get(kw)
        if df is not None:
            entry["chunk_df"] = int(df)
        seed_entries.append(entry)

    seed_set = set(seed_list)
    expanded = []
    for kw, sc in sorted(
        ((kw, sc) for kw, sc in kw_score.items() if kw not in seed_set),
        key=lambda item: (-item[1], item[0]),
    ):
        entry: Dict[str, object] = {
            "kw": kw,
            "ppr_score": round(float(sc), 6),
        }
        h = hops_by_kw.get(kw)
        if h is not None:
            entry["hops_from_seed"] = int(h)
        df = chunk_df.get(kw)
        if df is not None:
            entry["chunk_df"] = int(df)

        # Flatten the strongest direct seed edge onto the entry. Edges are
        # already sorted (npmi desc NULLS last, cooccur desc, seed asc) by
        # :func:`_edges_by_neighbor`, so the first one is the strongest.
        edges = edges_by_neighbor.get(kw, [])
        if edges:
            best = edges[0]
            entry["npmi"] = best["npmi"]
            entry["cooccur_count"] = best["cooccur_count"]
            # Zero-value omission: only emit source when it deviates from
            # the overwhelmingly common ``chunk_cooccur``.
            if best["source"] != "chunk_cooccur":
                entry["source"] = best["source"]
            # Multi-seed bridge signal: only when >1 seed is directly
            # linked (single-seed queries and single-edge neighbours omit).
            if multi_seed and len(edges) > 1:
                entry["via_seeds"] = [e["seed"] for e in edges]
        expanded.append(entry)
    return {
        "seeds": seed_entries,
        "expanded_keywords": expanded,
        "total_chunks_in_scope": int(total_chunks_in_scope),
    }


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
    min_npmi: float = 0.0,
) -> Tuple[List[Related], Dict[str, float], dict]:
    """Run PPR + doc collection and return ``(results, kw_score, expansion)``.

    Chunk-level: seed -> doc resolution goes through ``keyword_chunks``
    instead of ``keyword_docs``. Each result carries a
    ``keyword_contributions`` per-doc breakdown; every keyword-level
    graph signal (``chunk_df``, ``ppr_score``, ``hops_from_seed``, and
    the strongest direct-edge ``npmi`` / ``cooccur_count``) is collected
    once into the query-level ``expansion`` block, not per-doc (see
    ``docs/design/related-signals-v2.md``).

    ``min_npmi`` (default 0.0 from the internal callers; public API
    resolves to :data:`DEFAULT_MIN_NPMI` via :func:`related`) filters
    ``chunk_cooccur`` edges before PPR - see plan-2026-07-12-related-perf
    §2.4.
    """
    def _empty_expansion() -> dict:
        return _build_expansion(seeds, {}, {}, {}, {}, 0)

    # In cross-scope mode the seeds must still originate from ``scope_key``.
    if cross_scope and scope_key is not None:
        seed_docs = _docs_for_keywords_chunks(conn, seeds, scope_key)
        if not seed_docs:
            return [], {}, _empty_expansion()
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

    total_chunks = _total_chunks_in_scope(conn, signals_scope_key)
    neighbor_kws = [kw for kw, _ in ranked_keywords]
    # One batched chunk_df query covers seeds + neighbours in a single SQL.
    all_kws_for_df = list({*seeds, *neighbor_kws})
    chunk_df_map = _chunk_df_for_kws(conn, all_kws_for_df, signals_scope_key)
    edges_by_nbr = _edges_by_neighbor(
        conn, seeds, neighbor_kws, edge_scope_key,
    )

    if not ranked_keywords:
        # No graph signal: fall back to documents that directly contain
        # the seed keywords via ``keyword_chunks``. Build per-doc
        # keyword_contributions from the seed-only aggregate.
        direct = _docs_for_keywords_chunks(conn, seeds, doc_scope_key)
        seed_kw_score = {s: 1.0 for s in seeds}
        seed_contributions = _keyword_contributions_by_doc(
            conn, seed_kw_score, doc_scope_key,
        )
        results: List[Related] = []
        for (directory, doc), (score, kws) in direct.items():
            key = (directory, doc)
            contribs = seed_contributions.get(key, [])
            results.append(
                Related(
                    doc_path=doc,
                    score=score,
                    mode="graph",
                    reason="direct-seed",
                    keywords=sorted(kws),
                    directory=directory,
                    keyword_contributions=contribs,
                )
            )
        results.sort(key=lambda r: (-r.score, r.doc_path, r.directory or ""))
        capped = results[:limit]
        expansion = _build_expansion(
            seeds, {}, {}, chunk_df_map, edges_by_nbr, total_chunks,
        )
        return capped, {}, expansion

    kw_score = {kw: score for kw, score in ranked_keywords}
    for seed in seeds:
        kw_score.setdefault(seed, max(kw_score.values()) if kw_score else 1.0)

    doc_results = _aggregate_docs(conn, kw_score, doc_scope_key)
    doc_results.sort(key=lambda r: (-r.score, r.doc_path, r.directory or ""))
    capped = doc_results[:limit]
    hops_by_kw = _compute_hops(indexed, seeds, kw_score)
    expansion = _build_expansion(
        seeds, kw_score, hops_by_kw, chunk_df_map, edges_by_nbr, total_chunks,
    )
    return capped, kw_score, expansion


def _aggregate_docs(
    conn: sqlite3.Connection,
    kw_score: Dict[str, float],
    doc_scope_key: Optional[str],
) -> List["Related"]:
    """Dispatch to id-native (P2.3, default) or legacy text-IN path.

    Rollback: set ``GRGA_DOC_AGG_LEGACY=1`` to force the pre-P2.3 SQL
    (``k.kw IN (text)`` with the docs+keywords double-JOIN).
    """
    if os.environ.get("GRGA_DOC_AGG_LEGACY"):
        return _aggregate_docs_text_legacy(conn, kw_score, doc_scope_key)
    return _aggregate_docs_id_native(conn, kw_score, doc_scope_key)


def _contribs_from_rowmap(
    rowmap: Dict[Tuple[Optional[str], str], Dict[str, int]],
) -> Dict[Tuple[Optional[str], str], List["KeywordContribution"]]:
    """Turn ``{doc_key: {kw: occurrences}}`` into sorted contribution lists.

    Sort key is ``(-occurrences, kw)`` so the strongest evidence surfaces
    first inside each ``Related``. The ordering is stable across runs and
    across the id-native / legacy aggregation paths, which is what the
    equivalence contract in ``tests/test_related_doc_agg.py`` relies on.
    """
    out: Dict[Tuple[Optional[str], str], List["KeywordContribution"]] = {}
    for key, kw_map in rowmap.items():
        contribs = [
            KeywordContribution(kw=kw, occurrences=int(occ))
            for kw, occ in kw_map.items()
        ]
        contribs.sort(key=lambda c: (-c.occurrences, c.kw))
        out[key] = contribs
    return out


def _keyword_contributions_by_doc(
    conn: sqlite3.Connection,
    kw_score: Dict[str, float],
    doc_scope_key: Optional[str],
) -> Dict[Tuple[Optional[str], str], List["KeywordContribution"]]:
    """Return ``{(directory, doc_path): [KeywordContribution, ...]}``.

    Used by the ``direct-seed`` fallback branch of :func:`_related_graph_impl`
    where no PPR ranking is available (isolated seeds) — the main
    aggregation paths already have the same rowmap in hand and build
    contributions inline for zero extra SQL.
    """
    if not kw_score:
        return {}
    kw_texts = list(kw_score.keys())
    ph = ",".join("?" for _ in kw_texts)
    kw_id_to_text: Dict[int, str] = {
        int(r["id"]): _fold(r["kw"])
        for r in conn.execute(
            f"SELECT id, kw FROM keywords WHERE kw IN ({ph})", kw_texts
        )
    }
    if not kw_id_to_text:
        return {}
    kw_ids = list(kw_id_to_text.keys())
    ph_id = ",".join("?" for _ in kw_ids)
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
    rowmap: Dict[Tuple[Optional[str], str], Dict[str, int]] = {}
    for row in rows:
        kw = kw_id_to_text.get(int(row["kw_id"]))
        if kw is None:
            continue
        key = (row["directory"], row["doc_path"])
        rowmap.setdefault(key, {})[kw] = int(row["total"] or 0)
    return _contribs_from_rowmap(rowmap)


def _aggregate_docs_id_native(
    conn: sqlite3.Connection,
    kw_score: Dict[str, float],
    doc_scope_key: Optional[str],
) -> List["Related"]:
    """P2.3 doc aggregation — aggregate on ``kw_id`` (integer PK).

    Replaces ``k.kw IN (60 text)`` with ``kw_id IN (60 int)``. That change
    lets SQLite walk the ``keyword_chunks`` composite PK
    ``(kw_id, doc_id, chunk_id)`` as a range scan instead of falling off
    the PK path and doing a full ``docs × chunks`` traversal. Benchmark
    delta: 11s → 53ms on 3.5M keyword_chunks (~210×). See
    ``docs/design/plan-2026-07-12-related-perf.md`` §2.2 and
    ``experiments/related_perf_2026_07_12/FINDINGS.md`` §F1.

    The output score is byte-identical to
    :func:`_aggregate_docs_text_legacy` (verified in experiments as
    Jaccard=1.0 on top-20). ``keyword_contributions`` is populated from
    the same rowmap the aggregation walks — free per-doc evidence for
    the caller (see ``docs/design/related-signals-v2.md``).
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
    doc_rowmap: Dict[tuple, Dict[str, int]] = {}
    for row in rows:
        kw = kw_id_to_text.get(int(row["kw_id"]))
        if kw is None:
            continue
        weight = kw_score.get(kw, 0.0)
        if weight <= 0:
            continue
        total = int(row["total"] or 0)
        key = (row["directory"], row["doc_path"])
        contribution = weight * math.log1p(float(total))
        doc_score[key] = doc_score.get(key, 0.0) + contribution
        doc_rowmap.setdefault(key, {})[kw] = total

    contribs_by_doc = _contribs_from_rowmap(doc_rowmap)
    return [
        Related(
            doc_path=doc,
            score=score,
            mode="graph",
            reason="ppr",
            keywords=sorted(doc_rowmap[(directory, doc)].keys()),
            directory=directory,
            keyword_contributions=contribs_by_doc.get((directory, doc), []),
        )
        for (directory, doc), score in doc_score.items()
    ]


def _aggregate_docs_text_legacy(
    conn: sqlite3.Connection,
    kw_score: Dict[str, float],
    doc_scope_key: Optional[str],
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
    doc_rowmap: Dict[tuple, Dict[str, int]] = {}
    for row in rows:
        kw = _fold(row["keyword"])
        weight = kw_score.get(kw, 0.0)
        if weight <= 0:
            continue
        total = int(row["total"] or 0)
        key = (row["directory"], row["doc_path"])
        contribution = weight * math.log1p(float(total))
        doc_score[key] = doc_score.get(key, 0.0) + contribution
        doc_rowmap.setdefault(key, {})[kw] = total

    contribs_by_doc = _contribs_from_rowmap(doc_rowmap)
    return [
        Related(
            doc_path=doc,
            score=score,
            mode="graph",
            reason="ppr",
            keywords=sorted(doc_rowmap[(directory, doc)].keys()),
            directory=directory,
            keyword_contributions=contribs_by_doc.get((directory, doc), []),
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
    min_npmi: Optional[float] = None,
) -> dict:
    """``mode=graph`` variant returning results plus cross-scope observability.

    Returns ``{"results": List[Related], "crossed_scopes_preview": dict|None,
    "expansion": dict}``. The ``expansion`` block carries the query-level
    keyword-expansion evidence (seeds + PPR-scored neighbours; hop-1
    neighbours inline their strongest direct-edge ``npmi`` /
    ``cooccur_count``, and ``source`` / ``via_seeds`` follow zero-value
    omission rules for the common single-seed / chunk_cooccur case) that
    callers use — together with each result's ``keyword_contributions``
    — to judge relevance and filter hub-word false positives. See
    ``docs/design/related-signals-v2.md``.

    ``min_npmi`` follows the same semantics as :func:`related`: ``None``
    resolves to :data:`DEFAULT_MIN_NPMI` / ``GRGA_PPR_MIN_NPMI`` env.
    """
    seeds = [s for s in (_fold(k) for k in keywords) if s]
    if not seeds:
        return {
            "results": [],
            "crossed_scopes_preview": None,
            "expansion": {
                "seeds": [],
                "expanded_keywords": [],
                "total_chunks_in_scope": 0,
            },
        }

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_key = _resolve_directory(directory)
    effective_min_npmi = _default_min_npmi() if min_npmi is None else float(min_npmi)
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        results, _, expansion = _related_graph_impl(
            conn, seeds, limit, scope_key, alpha, iters,
            cross_scope=cross_scope, db_path=target_db,
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
            "expansion": expansion,
        }
    finally:
        conn.close()
