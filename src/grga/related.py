"""``related()`` API — L2 graph neighbours (chunk-level, v8 keyword-primary).

Two entry points, both read-only, deterministic, LLM-free:

* :func:`related_keywords` — v8 graph-expansion primitive. Takes seed
  keywords, returns a :class:`RelatedKeywordsResult` bundling the flat
  candidate stream (``results``: ``kw``/``npmi``/``cooccur``/``chunk_df``/
  ``via_seed``/``source``) with a per-seed vocabulary probe
  (``resolved_seeds``: which seeds are known to the index). No PPR, no
  full-graph load. Multi-seed queries expand each seed independently
  without fusion (caller decides). The probe disambiguates the "empty
  results" case: unknown seed vs. isolated seed.
  Design: ``docs/design/plan-2026-07-12-keyword-primary.md`` §4.

* :func:`related` — legacy doc-oriented API, now supports only
  ``mode="heading"`` (documents that share a Markdown ``heading_path``
  with a seed-containing document). The pre-v8 ``mode="graph"`` and
  ``mode="dir"`` were removed in Phase 3b-2 — call ``related_keywords``
  for graph expansion, or ``grga search --json`` for directory-level
  aggregation via a Unix pipeline.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from grga.graph.schema import connect, ensure_schema, CHUNK_COOCCUR_SOURCE

VALID_MODES = ("heading",)
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
class Related:
    """A single related document with provenance.

    ``doc_path`` is relative to its indexed scope. In cross-scope results
    the same relative path can occur in several scopes, so ``directory``
    disambiguates: it holds the resolved scope root the document was
    indexed under (``None`` when unknown / not tracked). ``qualified_path``
    joins the two into a filesystem-resolvable path.

    v8/v9 note: post ``mode='graph'`` removal (Phase 3b-2), the only
    remaining consumer of this class is ``mode='heading'`` (matched
    heading paths shared with the seed docs). The pre-v8 per-doc
    ``keyword_contributions`` block is gone — graph expansion now returns
    :class:`RelatedKeyword` records directly.
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
                       discriminator (design §related-signals-v3 §chunk_df).
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


@dataclass
class SeedResolution:
    """Per-seed vocabulary probe (envelope-level, multi-seed related).

    Answers the one question a caller cannot derive from ``results`` alone:
    *was this seed known to the index at all?* When a seed produces no
    candidate keywords, the empty ``results`` slice is ambiguous — the seed
    could be (a) absent from the vocabulary (misspelt / never indexed), or
    (b) present but isolated (all its pairs fell below the write-time npmi
    floor, or the read-side ``min_npmi`` filtered them). ``in_index``
    disambiguates (a) from (b): callers pick the next move (reformulate vs.
    swap seed vs. loosen ``--min-npmi``) without a per-seed ``inspect``
    round-trip.

    Deriving the rest is the caller's job (AGENTS.md §信号暴露原则): the set
    of seeds that *produced* candidates is ``{r.via_seed for r in results}``;
    a seed that is ``in_index`` but missing from that set is the isolated /
    over-filtered case (b).

    Fields:
        seed:     casefold-normalised seed (as queried).
        in_index: whether the seed resolves to a row in the ``keywords``
                  vocabulary table. Scope-independent: a seed can be
                  ``in_index=True`` yet yield no candidates within a
                  ``--directory`` scope.
    """

    seed: str
    in_index: bool

    def to_dict(self) -> dict:
        return {"seed": self.seed, "in_index": self.in_index}


@dataclass
class RelatedKeywordsResult:
    """Envelope returned by :func:`related_keywords`.

    Wraps the flat candidate stream (``results``) with a per-seed
    vocabulary probe (``resolved_seeds``). The probe is the only piece of
    envelope-level metadata the v3 graph contract exposes — everything else
    (fusion, dedup, hit counts) stays derivable on the caller side.
    """

    results: List[RelatedKeyword]
    resolved_seeds: List[SeedResolution]

    def to_dict(self) -> dict:
        return {
            "results": [r.to_dict() for r in self.results],
            "resolved_seeds": [s.to_dict() for s in self.resolved_seeds],
        }


DEFAULT_LIMIT_PER_SEED = 30


def related_keywords(
    seeds: Sequence[str],
    *,
    limit_per_seed: int = DEFAULT_LIMIT_PER_SEED,
    min_npmi: Optional[float] = None,
    db_path: Union[str, Path, None] = None,
    directory: Optional[Union[str, Path]] = None,
) -> RelatedKeywordsResult:
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
    * ``resolved_seeds`` reports, for each distinct queried seed, whether it
      exists in the vocabulary (``in_index``). This is the multi-seed probe:
      it separates an unknown seed from a known-but-isolated one when
      ``results`` is empty. See :class:`SeedResolution`.

    Args:
        seeds:          query seed keywords (casefold-normalised internally).
        limit_per_seed: max candidates per seed. Default 30.
        min_npmi:       optional read-side floor. ``None`` (default)
                        resolves to :data:`DEFAULT_MIN_NPMI` or
                        ``GRGA_PPR_MIN_NPMI``.
        db_path:        SQLite path (defaults to shared deep-search DB).
        directory:      optional scope filter (resolved absolute path).

    Returns:
        A :class:`RelatedKeywordsResult` whose ``results`` is the flat list
        of :class:`RelatedKeyword`, sorted by ``(via_seed, -npmi, kw)``, and
        whose ``resolved_seeds`` reports per-seed vocabulary membership in
        first-seen input order. ``results`` is empty when no seed resolves.
    """
    seed_folded: List[str] = []
    seen_seeds: set = set()
    for s in (_fold(k) for k in seeds):
        if s and s not in seen_seeds:
            seen_seeds.add(s)
            seed_folded.append(s)
    if not seed_folded:
        return RelatedKeywordsResult(results=[], resolved_seeds=[])

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
) -> RelatedKeywordsResult:
    """Core query path for :func:`related_keywords`.

    Split out so tests can hand-inject a connection without going through
    the DB-path resolver. The DB schema must already be at v8. ``seeds`` is
    expected casefold-normalised and de-duplicated (order preserved).
    """
    # Resolve seeds → kw_ids first (scope-independent) so the per-seed
    # ``resolved_seeds`` probe is populated even when the scope is unknown.
    placeholders = ",".join("?" for _ in seeds)
    seed_id_rows = list(
        conn.execute(
            f"SELECT id, kw FROM keywords WHERE kw IN ({placeholders})",
            list(seeds),
        )
    )
    text_to_id: Dict[str, int] = {str(r[1]): int(r[0]) for r in seed_id_rows}
    resolved_seeds = [
        SeedResolution(seed=s, in_index=s in text_to_id) for s in seeds
    ]

    # Resolve dir_id filter (if any). Unknown scope → zero candidates, but
    # the vocabulary probe above still stands.
    dir_id: Optional[int] = None
    if scope_key is not None:
        row = conn.execute(
            "SELECT id FROM directories WHERE path = ?", (scope_key,)
        ).fetchone()
        if row is None:
            return RelatedKeywordsResult(
                results=[], resolved_seeds=resolved_seeds
            )
        dir_id = int(row[0])

    seed_ids: List[Tuple[int, str]] = []
    for s in seeds:
        if s in text_to_id:
            seed_ids.append((text_to_id[s], s))
    if not seed_ids:
        return RelatedKeywordsResult(results=[], resolved_seeds=resolved_seeds)

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
        return RelatedKeywordsResult(results=[], resolved_seeds=resolved_seeds)

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
    return RelatedKeywordsResult(
        results=all_results, resolved_seeds=resolved_seeds
    )


# ---------------------------------------------------------------------------
# Legacy ``related()`` API (Phase 3b will retire ``mode='graph'`` / ``dir``).
# ---------------------------------------------------------------------------


def related(
    keywords: Sequence[str],
    *,
    mode: str = "heading",
    limit: int = DEFAULT_LIMIT,
    db_path: Union[str, Path, None] = None,
    directory: Optional[Union[str, Path]] = None,
    same_scope: bool = False,
) -> List[Related]:
    """Return documents related to ``keywords`` under the chosen ``mode``.

    v8 keyword-primary (Phase 3b-2): the doc-returning modes are now just
    ``heading``. ``graph`` and ``dir`` are removed; graph expansion returns
    keywords, not docs — call :func:`related_keywords` for that. Design:
    ``docs/design/plan-2026-07-12-keyword-primary.md`` §4.

    Args:
        keywords: seed keywords (query terms).
        mode: currently only ``heading`` is supported.
        limit: maximum number of results.
        db_path: SQLite path (defaults to shared deep-search DB).
        directory: optional scope filter for seed lookup.
        same_scope: restrict heading matches to ``directory`` too (reverts
            to pre-v2.2 default).

    Raises:
        ValueError: if ``mode`` is not ``heading``. ``graph`` and ``dir``
            surface an explicit hint pointing at :func:`related_keywords`.
    """
    if mode == "graph":
        raise ValueError(
            "mode='graph' was removed in v8 keyword-primary (Phase 3b-2). "
            "Use grga.related.related_keywords() to expand seeds to "
            "candidate keywords + npmi/cooccur evidence. See "
            "docs/design/plan-2026-07-12-keyword-primary.md §4."
        )
    if mode == "dir":
        raise ValueError(
            "mode='dir' was removed in v8 keyword-primary (Phase 3b-2). "
            "Use `grga search --json` to enumerate seed-containing docs, "
            "then aggregate by directory client-side (Unix pipeline)."
        )
    if mode not in VALID_MODES:
        raise ValueError(
            f"unknown related mode: {mode!r} (expected one of {VALID_MODES})"
        )

    seeds = [s for s in (_fold(k) for k in keywords) if s]
    if not seeds:
        return []

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_key = _resolve_directory(directory)
    conn = connect(target_db)
    try:
        ensure_schema(conn)
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
    """Aggregate ``keyword_docs`` to ``{(directory, doc_path): (weight, kws)}``.

    ``weight`` is the summed ``occurrence_count`` across the seed keywords
    for each doc. ``kws`` is the set of seed keywords that hit this doc.

    v9 note: reads the doc-level inverted index ``keyword_docs``. The
    pre-v9 ``keyword_chunks`` self-join is gone; each ``keyword_docs`` row
    already carries the per-doc occurrence total, so the lookup is a
    single indexed join.
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
        f"kd.occurrence_count AS total, kd.chunk_count AS chunks "
        f"FROM keyword_docs kd "
        f"JOIN keywords k ON k.id = kd.kw_id "
        f"JOIN docs d ON d.id = kd.doc_id "
        f"JOIN directories dir ON dir.id = d.dir_id "
        f"WHERE {where}",
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

