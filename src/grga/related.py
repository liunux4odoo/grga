"""``related()`` API — L2 graph neighbours (v10 scope-aware).

Two entry points, both read-only, deterministic, LLM-free:

* :func:`related_keywords` — graph-expansion primitive. Takes seed
  keywords, returns a :class:`RelatedKeywordsResult` bundling the flat
  candidate stream (``results``: ``kw``/``npmi``/``cooccur``/``chunk_df``/
  ``via_seed``/``source``) with a per-seed vocabulary probe
  (``resolved_seeds``: which seeds are known to the index). No PPR, no
  full-graph load. Multi-seed queries expand each seed independently
  without fusion (caller decides). The probe disambiguates the "empty
  results" case: unknown seed vs. isolated seed.

  v10 (plan-2026-07-14): the ``directory`` scope filter became a
  **multi-select** ``scopes`` list of logical names. ``scope_id IN (...)``
  keeps the composite-PK index usable per scope; ``None`` = cross-scope
  union. See design §4.

* :func:`related` — legacy doc-oriented API, now supports only
  ``mode="heading"`` (documents that share a Markdown ``heading_path``
  with a seed-containing document).
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from grga.graph.schema import (
    connect,
    ensure_schema,
    resolve_scope_name,
    CHUNK_COOCCUR_SOURCE,
)

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

    ``doc_path`` is relative to whatever directory the scope was
    ``grga index build``-ed under on this machine. ``scope`` is the
    logical scope name (v10) — the caller looks up
    ``scope_directories`` when it needs to resolve to a filesystem path
    for ripgrep follow-up.

    v10 note: pre-v10 ``directory`` field (an absolute path) is renamed
    to ``scope`` (logical name). The pre-v10 ``qualified_path`` helper
    is gone — resolve the physical path via ``grga.graph.ids
    .IdInterner.list_directories(scope_id)`` when needed.
    """

    doc_path: str
    score: float
    mode: str
    reason: str = ""
    keywords: List[str] = field(default_factory=list)
    scope: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "doc_path": self.doc_path,
            "score": round(self.score, 6),
            "mode": self.mode,
            "reason": self.reason,
            "keywords": list(self.keywords),
            "scope": self.scope,
        }


def _fold(term: str) -> str:
    return term.casefold().strip()


def _normalise_scopes(
    scopes: Optional[Sequence[str]],
) -> Optional[List[str]]:
    """Normalise a user-supplied scope list to unique, valid names.

    * ``None`` -> ``None`` (no scope filter, cross-scope union).
    * empty sequence -> ``None`` (same as above; treat empty as "all").
    * a sequence containing only empty/whitespace items -> ``None``
      (documented "empty → all" fallback; empty items are dropped, not
      an error — read-side API callers that forward an unset field must
      not hit a ``ValueError``).
    * one or more real names -> validated + deduped list, order preserved.

    Empty/whitespace *items* inside the list are silently skipped rather
    than raising, so ``related_keywords(scopes=[""])`` behaves like
    ``scopes=None``. See plan-2026-07-14 §4.
    """
    if scopes is None:
        return None
    seen: set = set()
    out: List[str] = []
    for name in scopes:
        # Drop empty/whitespace items before the strict normaliser so a
        # forwarded blank field degrades to "all scopes" instead of an
        # uncaught ValueError from resolve_scope_name.
        if name is None or not str(name).strip():
            continue
        resolved = resolve_scope_name(name)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out or None


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
                       time — see design §3.2). When the query touches
                       multiple scopes, this is the **max** npmi across
                       those scopes (index-ordered LIMIT semantics); use
                       :attr:`scopes` to see which scopes contributed.
        cooccur_count: raw chunk co-occurrence count. Same "max across
                       returned scopes" caveat as npmi.
        via_seed:      the seed that expanded to this candidate. Preserved
                       so callers can decide how to fuse (they can, we do
                       not).
        chunk_df:      candidate's own scope-level chunk df — hub-word
                       discriminator (design §related-signals-v3 §chunk_df).
                       Cross-scope query sums this across the queried
                       scopes (SUM(chunk_df) via keyword_stats).
        source:        ``chunk_cooccur`` (npmi-scored) or ``llm_promoted``
                       (npmi may be NULL upstream but is stored 0.0 here for
                       consistent typing).
        scopes:        v3.1 (plan-2026-07-14 §5.3): the scope names within
                       the caller's query range that carry this pair. Lets
                       an agent route follow-up ripgrep at the right scope
                       binding without a per-candidate ``inspect`` round-trip.
                       Cross-scope query (``scopes=None``): all scopes that
                       memorised the pair. Single-scope query: the singleton
                       list of that scope. Always populated (never empty
                       when the row is returned).
    """

    kw: str
    npmi: float
    cooccur_count: float
    via_seed: str
    chunk_df: int
    source: str
    scopes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kw": self.kw,
            "npmi": round(float(self.npmi), 6),
            "cooccur_count": float(self.cooccur_count),
            "via_seed": self.via_seed,
            "chunk_df": int(self.chunk_df),
            "source": self.source,
            "scopes": list(self.scopes),
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
    scopes: Optional[Sequence[str]] = None,
) -> RelatedKeywordsResult:
    """Graph-expansion primitive: seeds → candidate keywords + evidence.

    For each seed keyword, load its top ``limit_per_seed`` neighbours from
    ``keyword_pairs`` (ordered by stored ``npmi DESC``), tagged with which
    seed they came from. **No PPR, no full-graph load, no adjacency cache.**
    The index ``(scope_id, kw_a_id, npmi DESC)`` + its mirror on ``kw_b_id``
    turn the double lookup into ``O(limit_per_seed)`` per seed.

    Semantics:

    * ``min_npmi`` may only *tighten* the write-time floor
      (:data:`BUILD_MIN_NPMI` = 0.2). Passing a lower value has no effect.
    * Multi-seed queries return per-seed rows without fusion.
    * ``scopes`` is a **multi-select** per-scope filter (v10): only pairs
      whose ``scope_id`` is in the resolved set are returned. ``None`` (or
      empty) = no filter (cross-scope union). See plan-2026-07-14 §4.
    * ``resolved_seeds`` reports, for each distinct queried seed, whether
      it exists in the vocabulary (``in_index``).

    Args:
        seeds:          query seed keywords (casefold-normalised internally).
        limit_per_seed: max candidates per seed. Default 30.
        min_npmi:       optional read-side floor. ``None`` (default)
                        resolves to :data:`DEFAULT_MIN_NPMI` or
                        ``GRGA_PPR_MIN_NPMI``.
        db_path:        SQLite path (defaults to shared deep-search DB).
        scopes:         optional multi-select scope-name filter.

    Returns:
        A :class:`RelatedKeywordsResult` whose ``results`` is the flat list
        of :class:`RelatedKeyword`, sorted by ``(via_seed, -npmi, kw)``, and
        whose ``resolved_seeds`` reports per-seed vocabulary membership in
        first-seen input order.
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
    scope_names = _normalise_scopes(scopes)
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
            scope_names=scope_names,
        )
    finally:
        conn.close()



def _complete_scope_sets(
    conn: sqlite3.Connection,
    seed_id: int,
    ranked: List[Tuple[int, str, float, float, "frozenset[int]"]],
    scope_ids: List[int],
) -> List[Tuple[int, str, float, float, "frozenset[int]"]]:
    """Refill ``scopes`` on each ranked candidate with the FULL scope set.

    Per-SELECT ``ORDER BY npmi DESC LIMIT ?`` in the caller can drop rows
    for the same ``(nbr, source)`` pair in a lower-npmi scope before the
    merge dict sees them. Without a follow-up, ``RelatedKeyword.scopes``
    silently mis-reports the scope set — violating the v3.1 contract that
    promises "cross-scope query: every scope in DB that carries this
    pair" and "multi-scope IN: the subset that actually memorised it".

    This runs after the main SELECTs have chosen the top-k winners and
    re-fetches the complete scope membership for those winners only,
    respecting the same ``scope_ids`` filter as the main query. Cost is
    O(surviving_pairs) index-only lookups on the composite PK
    ``(scope_id, kw_a_id, kw_b_id, source)`` mirror indexes — a covered
    scan of at most ``limit_per_seed`` rows per side per source.

    The pair invariant ``kw_a_id < kw_b_id`` means each ``(seed, nbr)``
    lives on exactly one side of the PK: nbr > seed → a-side; nbr < seed
    → b-side. We split the surviving neighbours accordingly and issue
    one query per (direction, source) with an ``IN (...)`` clause on
    ``nbr``, keeping the total at ≤ 4 statements per seed.
    """
    if not ranked:
        return ranked

    # Group surviving nbr_ids by (direction, source). Direction: 'a' when
    # nbr > seed (row stored as kw_a=seed, kw_b=nbr), 'b' when nbr < seed.
    from collections import defaultdict
    buckets: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    for (nbr_id, src, _npmi, _cooccur, _scopes) in ranked:
        direction = "a" if nbr_id > seed_id else "b"
        buckets[(direction, src)].append(nbr_id)

    scope_filter = ""
    scope_params: List = []
    if len(scope_ids) == 1:
        scope_filter = " AND scope_id = ?"
        scope_params = [scope_ids[0]]
    elif len(scope_ids) >= 2:
        sp = ",".join("?" for _ in scope_ids)
        scope_filter = f" AND scope_id IN ({sp})"
        scope_params = list(scope_ids)

    # scope_map[(nbr_id, src)] = complete scope set for this candidate.
    scope_map: Dict[Tuple[int, str], set] = defaultdict(set)
    for (direction, src), nbrs in buckets.items():
        # Chunk to stay under SQLITE_MAX_VARIABLE_NUMBER.
        for i in range(0, len(nbrs), 800):
            piece = nbrs[i:i + 800]
            ph = ",".join("?" for _ in piece)
            if direction == "a":
                # seed on a-side: WHERE kw_a_id = seed AND kw_b_id IN (...)
                sql = (
                    f"SELECT kw_b_id AS nbr_id, scope_id "
                    f"FROM keyword_pairs "
                    f"WHERE kw_a_id = ? AND source = ? "
                    f"AND kw_b_id IN ({ph}){scope_filter}"
                )
                params = (seed_id, src, *piece, *scope_params)
            else:
                sql = (
                    f"SELECT kw_a_id AS nbr_id, scope_id "
                    f"FROM keyword_pairs "
                    f"WHERE kw_b_id = ? AND source = ? "
                    f"AND kw_a_id IN ({ph}){scope_filter}"
                )
                params = (seed_id, src, *piece, *scope_params)
            for row in conn.execute(sql, params):
                scope_map[(int(row[0]), src)].add(int(row[1]))

    # Rewrite ranked with the complete scope sets. When the follow-up
    # returned no rows for a candidate (should not happen — the pair is
    # by construction in the DB), fall back to the truncated set rather
    # than empty, so the caller never sees an empty ``scopes`` list.
    out: List[Tuple[int, str, float, float, "frozenset[int]"]] = []
    for (nbr_id, src, npmi_val, cooccur, prev_scopes) in ranked:
        full = scope_map.get((nbr_id, src))
        chosen = frozenset(full) if full else prev_scopes
        out.append((nbr_id, src, npmi_val, cooccur, chosen))
    return out


def _related_keywords_impl(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    *,
    limit_per_seed: int,
    min_npmi: float,
    scope_names: Optional[Sequence[str]],
) -> RelatedKeywordsResult:
    """Core query path for :func:`related_keywords`.

    Split out so tests can hand-inject a connection. ``seeds`` is
    expected casefold-normalised and de-duplicated (order preserved).
    ``scope_names`` is a validated list of logical scope names or
    ``None`` (cross-scope).
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

    # Resolve scope_id filter (if any). Unknown scopes are dropped; if a
    # non-empty scope list resolves to zero known scopes, no candidate
    # can match — return early (the vocabulary probe above still stands).
    scope_ids: List[int] = []
    if scope_names is not None:
        s_placeholders = ",".join("?" for _ in scope_names)
        rows = conn.execute(
            f"SELECT id FROM scopes WHERE name IN ({s_placeholders})",
            list(scope_names),
        ).fetchall()
        scope_ids = [int(r[0]) for r in rows]
        if not scope_ids:
            return RelatedKeywordsResult(
                results=[], resolved_seeds=resolved_seeds
            )

    seed_ids: List[Tuple[int, str]] = []
    for s in seeds:
        if s in text_to_id:
            seed_ids.append((text_to_id[s], s))
    if not seed_ids:
        return RelatedKeywordsResult(results=[], resolved_seeds=resolved_seeds)

    # Scope-filter shape. The composite index
    # ``idx_kp_a_npmi (scope_id, kw_a_id, npmi DESC)`` supports an
    # ordered-LIMIT walk ONLY when the leading column is an equality
    # predicate. ``scope_id IN (a, b, ...)`` forces SQLite to materialise
    # every matching row across the listed scopes into a temp b-tree and
    # sort before applying LIMIT — losing the sub-millisecond fast path
    # for the very common single-scope call. Therefore:
    #
    #   * len == 1  → ``scope_id = ?`` (index-ordered LIMIT preserved)
    #   * len >= 2  → ``scope_id IN (...)`` (accepts the temp-sort cost)
    #   * scope_ids empty (cross-scope) → no clause (full-index scan)
    scope_filter = ""
    scope_params: List = []
    if len(scope_ids) == 1:
        scope_filter = "scope_id = ? AND "
        scope_params = [scope_ids[0]]
    elif len(scope_ids) >= 2:
        sp = ",".join("?" for _ in scope_ids)
        scope_filter = f"scope_id IN ({sp}) AND "
        scope_params = list(scope_ids)
    # TODO(perf): cross-scope path (scope_names is None) full-scans
    # idx_kp_b_npmi because the (scope_id, kw_a_id, npmi DESC) composite
    # cannot be used without a scope_id prefix. Acceptable for the rare
    # cross-scope case (see plan-2026-07-12-related-perf).

    seen_kw_ids: set = set()
    # Per-seed candidate rows: (nbr_id, source, npmi, cooccur, scope_ids set).
    per_seed_rows: List[
        Tuple[str, List[Tuple[int, str, float, float, "frozenset[int]"]]]
    ] = []
    # Every scope_id that appeared on any surviving edge — used at the end
    # for one batched scope_id → name reverse-lookup.
    hit_scope_ids: set = set()

    for (seed_id, seed_text) in seed_ids:
        rows_a = conn.execute(
            f"SELECT kw_b_id, npmi, cooccur_count, source, scope_id "
            f"FROM keyword_pairs "
            f"WHERE {scope_filter}kw_a_id = ? AND source = ? AND npmi >= ? "
            f"ORDER BY npmi DESC LIMIT ?",
            (*scope_params, seed_id, CHUNK_COOCCUR_SOURCE,
             float(min_npmi), int(limit_per_seed)),
        ).fetchall()
        rows_b = conn.execute(
            f"SELECT kw_a_id, npmi, cooccur_count, source, scope_id "
            f"FROM keyword_pairs "
            f"WHERE {scope_filter}kw_b_id = ? AND source = ? AND npmi >= ? "
            f"ORDER BY npmi DESC LIMIT ?",
            (*scope_params, seed_id, CHUNK_COOCCUR_SOURCE,
             float(min_npmi), int(limit_per_seed)),
        ).fetchall()
        # llm_promoted: no npmi filter (may be NULL).
        rows_llm_a = conn.execute(
            f"SELECT kw_b_id, npmi, cooccur_count, source, scope_id "
            f"FROM keyword_pairs "
            f"WHERE {scope_filter}kw_a_id = ? AND source = ?",
            (*scope_params, seed_id, "llm_promoted"),
        ).fetchall()
        rows_llm_b = conn.execute(
            f"SELECT kw_a_id, npmi, cooccur_count, source, scope_id "
            f"FROM keyword_pairs "
            f"WHERE {scope_filter}kw_b_id = ? AND source = ?",
            (*scope_params, seed_id, "llm_promoted"),
        ).fetchall()

        # Merge + dedupe within one seed. A pair with the same (kw_a, kw_b)
        # but different ``source`` values keeps both provenance rows.
        # ``merged`` value: (best_npmi, best_cooccur, scope_ids_set).
        # The npmi / cooccur are the MAX across scopes; the scope set
        # accumulates every scope this pair hit so callers can route
        # follow-up work at the right binding (design v3.1 §5.3).
        merged: Dict[Tuple[int, str], Tuple[float, float, set]] = {}
        for r in list(rows_a) + list(rows_b) + list(rows_llm_a) + list(rows_llm_b):
            nbr_id = int(r[0])
            npmi_val = float(r[1]) if r[1] is not None else 0.0
            cooccur = float(r[2])
            src = str(r[3])
            sid = int(r[4])
            key = (nbr_id, src)
            prev = merged.get(key)
            if prev is None:
                merged[key] = (npmi_val, cooccur, {sid})
            else:
                prev_npmi, prev_cooccur, prev_scopes = prev
                new_scopes = prev_scopes | {sid}
                if npmi_val > prev_npmi:
                    merged[key] = (npmi_val, cooccur, new_scopes)
                else:
                    # Keep the higher-npmi row's npmi/cooccur but still
                    # record that this scope carries the pair.
                    merged[key] = (prev_npmi, prev_cooccur, new_scopes)

        ranked = sorted(
            [
                (nbr_id, src, npmi_val, cooccur, frozenset(scopes))
                for (nbr_id, src), (npmi_val, cooccur, scopes) in merged.items()
            ],
            key=lambda t: (-t[2], t[0]),
        )[:limit_per_seed]

        # v3.1 D14 fix: refill each candidate's scope set from the DB so
        # that per-SELECT LIMIT truncation does not silently drop scopes
        # that also carry the pair with a lower npmi. Costs at most 4
        # extra index-covered lookups per seed.
        ranked = _complete_scope_sets(conn, seed_id, ranked, scope_ids)

        for (nbr_id, _src, _npmi_val, _cooccur, scope_set) in ranked:
            seen_kw_ids.add(nbr_id)
            hit_scope_ids.update(scope_set)
        per_seed_rows.append((seed_text, ranked))

    if not seen_kw_ids:
        return RelatedKeywordsResult(results=[], resolved_seeds=resolved_seeds)

    # Batched translate: nbr kw_id → text + chunk_df. Chunk-partitioned to
    # dodge SQLITE_MAX_VARIABLE_NUMBER. ``keyword_stats`` rows are per-scope;
    # a multi-scope (or cross-scope) query sums chunk_df across the selected
    # scopes (hub-word signal only; not a ranking input — plan-2026-07-14 §4.2).
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
        if scope_ids:
            sp = ",".join("?" for _ in scope_ids)
            for r in conn.execute(
                f"SELECT kw_id, SUM(chunk_df) FROM keyword_stats "
                f"WHERE kw_id IN ({ph}) AND scope_id IN ({sp}) "
                f"GROUP BY kw_id",
                (*piece, *scope_ids),
            ):
                chunk_df_map[int(r[0])] = int(r[1] or 0)
        else:
            for r in conn.execute(
                f"SELECT kw_id, SUM(chunk_df) FROM keyword_stats "
                f"WHERE kw_id IN ({ph}) GROUP BY kw_id",
                piece,
            ):
                chunk_df_map[int(r[0])] = int(r[1] or 0)

    # v3.1: batch-resolve scope_id → name for every scope that carried at
    # least one returned edge. One query per DB call regardless of how
    # many pairs — chunk-partitioned like the kw lookups above.
    scope_name_map: Dict[int, str] = {}
    hit_scope_list = list(hit_scope_ids)
    for i in range(0, len(hit_scope_list), 900):
        piece = hit_scope_list[i:i + 900]
        ph = ",".join("?" for _ in piece)
        for r in conn.execute(
            f"SELECT id, name FROM scopes WHERE id IN ({ph})", piece
        ):
            scope_name_map[int(r[0])] = str(r[1])

    all_results: List[RelatedKeyword] = []
    for seed_text, ranked in per_seed_rows:
        for (nbr_id, src, npmi_val, cooccur, scope_set) in ranked:
            kw = kw_text.get(nbr_id, "")
            if not kw:
                continue
            # Reverse-map the scope ids to names, sort ascending for a
            # stable ``--json`` contract. Unknown ids (shouldn't happen —
            # every id came from the batch we just looked up) are dropped.
            scopes = sorted(
                scope_name_map[sid]
                for sid in scope_set
                if sid in scope_name_map
            )
            all_results.append(
                RelatedKeyword(
                    kw=kw,
                    npmi=npmi_val,
                    cooccur_count=cooccur,
                    via_seed=seed_text,
                    chunk_df=chunk_df_map.get(nbr_id, 0),
                    source=src,
                    scopes=scopes,
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
    scope: Optional[str] = None,
    same_scope: bool = False,
) -> List[Related]:
    """Return documents related to ``keywords`` under the chosen ``mode``.

    v10: the single-scope filter is a logical scope name. The
    doc-returning modes are now just ``heading``. ``graph`` and ``dir``
    are removed; graph expansion returns keywords, not docs — call
    :func:`related_keywords` for that.

    Args:
        keywords: seed keywords (query terms).
        mode: currently only ``heading`` is supported.
        limit: maximum number of results.
        db_path: SQLite path (defaults to shared deep-search DB).
        scope: optional scope-name filter for seed lookup.
        same_scope: restrict heading matches to ``scope`` too.

    Raises:
        ValueError: if ``mode`` is not ``heading``.
    """
    if mode == "graph":
        raise ValueError(
            "mode='graph' was removed in v8 keyword-primary. "
            "Use grga.related.related_keywords() to expand seeds to "
            "candidate keywords + npmi/cooccur evidence."
        )
    if mode == "dir":
        raise ValueError(
            "mode='dir' was removed in v8 keyword-primary. "
            "Use `grga search --json` to enumerate seed-containing docs, "
            "then aggregate by directory client-side."
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
    scope_name = resolve_scope_name(scope)
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        return _related_heading(
            conn, seeds, limit,
            seed_scope_name=scope_name,
            match_scope_name=scope_name if same_scope else None,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chunk-level primitives shared by all modes.
# ---------------------------------------------------------------------------


def _docs_for_keywords_chunks(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    scope_name: Optional[str],
) -> Dict[Tuple[str, str], Tuple[float, set]]:
    """Aggregate ``keyword_docs`` to ``{(scope, doc_path): (weight, kws)}``.

    ``weight`` is the summed ``occurrence_count`` across the seed keywords
    for each doc. ``kws`` is the set of seed keywords that hit this doc.
    v10: the tuple key's first element is the logical scope name (not
    a filesystem path).
    """
    if not keywords:
        return {}
    placeholders = ",".join("?" for _ in keywords)
    params: List = list(keywords)
    where = f"k.kw IN ({placeholders})"
    if scope_name is not None:
        where += " AND s.name = ?"
        params.append(scope_name)
    rows = conn.execute(
        f"SELECT s.name AS scope, k.kw AS keyword, d.doc_path AS doc_path, "
        f"kd.occurrence_count AS total, kd.chunk_count AS chunks "
        f"FROM keyword_docs kd "
        f"JOIN keywords k ON k.id = kd.kw_id "
        f"JOIN docs d ON d.id = kd.doc_id "
        f"JOIN scopes s ON s.id = d.scope_id "
        f"WHERE {where}",
        params,
    ).fetchall()
    out: Dict[Tuple[str, str], Tuple[float, set]] = {}
    for row in rows:
        key = (row["scope"], row["doc_path"])
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
    seed_scope_name: Optional[str],
    match_scope_name: Optional[str],
) -> List[Related]:
    seed_docs = _docs_for_keywords_chunks(conn, seeds, seed_scope_name)
    if not seed_docs:
        return []
    seed_doc_set: set = set(seed_docs.keys())
    seed_scopes = {scope for scope, _ in seed_doc_set}

    seed_doc_paths = sorted({doc for _, doc in seed_doc_set})
    doc_ph = ",".join("?" for _ in seed_doc_paths)
    scope_ph = ",".join("?" for _ in seed_scopes)
    where = f"d.doc_path IN ({doc_ph}) AND s.name IN ({scope_ph})"
    params: List = [*seed_doc_paths, *sorted(seed_scopes)]
    rows = conn.execute(
        f"SELECT DISTINCT s.name AS scope, d.doc_path AS doc_path, "
        f"hd.heading_path AS heading_path "
        f"FROM heading_docs hd "
        f"JOIN docs d ON d.id = hd.doc_id "
        f"JOIN scopes s ON s.id = d.scope_id "
        f"WHERE {where}",
        params,
    ).fetchall()
    if not rows:
        return []

    seed_full_paths: List[str] = []
    seed_leaves: List[str] = []
    for row in rows:
        if (row["scope"], row["doc_path"]) not in seed_doc_set:
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
    if match_scope_name is not None:
        combined = f"({combined}) AND s.name = ?"
        params.append(match_scope_name)
    rows = conn.execute(
        f"SELECT s.name AS scope, hd.heading_path AS heading_path, "
        f"d.doc_path AS doc_path "
        f"FROM heading_docs hd "
        f"JOIN docs d ON d.id = hd.doc_id "
        f"JOIN scopes s ON s.id = d.scope_id "
        f"WHERE {combined}",
        params,
    ).fetchall()

    full_set = set(seed_full_paths)
    leaf_set = set(seed_leaves)
    doc_score: Dict[tuple, float] = {}
    doc_headings: Dict[tuple, set] = {}
    for row in rows:
        key = (row["scope"], row["doc_path"])
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
            keywords=sorted(doc_headings.get((scope, doc), set())),
            scope=scope,
        )
        for (scope, doc), score in doc_score.items()
    ]
    results.sort(key=lambda r: (-r.score, r.doc_path, r.scope or ""))
    return results[:limit]

