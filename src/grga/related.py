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

# Query-side ``--tag`` combining logic. ``or`` is the default: two ``--tag``
# flags typically read as "either counts", matching how ``--scope`` already
# treats multiple values as a union. ``and`` requires every listed tag on
# the same scope.
VALID_TAG_LOGIC = ("and", "or")
DEFAULT_TAG_LOGIC = "or"


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


def _normalise_inner_whitespace(text: str) -> str:
    """Collapse runs of whitespace inside ``text`` into single spaces.

    Used by the phrase-or-split heuristic so that ``"nano  center"`` (double
    space) is still recognised as the stored phrase ``"nano center"``.
    """
    return " ".join(text.split())


def _phrase_in_evidence(conn: sqlite3.Connection, phrase: str) -> bool:
    """Whether the exact casefolded phrase has doc-level evidence.

    Checks ``keyword_docs`` (joined to ``keywords``). Scope-independent:
    a phrase that is attested in *any* scope counts as a phrase. This is
    the same evidence table ``graph inspect`` uses for "does the graph
    know this keyword?" (not-indexed branch / ``top_docs``).
    """
    row = conn.execute(
        "SELECT 1 FROM keyword_docs kd "
        "JOIN keywords k ON k.id = kd.kw_id "
        "WHERE k.kw = ? LIMIT 1",
        (phrase,),
    ).fetchone()
    return row is not None


def _resolve_seed_args(
    conn: sqlite3.Connection,
    raw_seeds: Sequence[str],
) -> List[str]:
    """Expand user-supplied seed args into concrete folded graph seeds.

    A single argument can carry interior whitespace in two different
    ways, and the graph has no way to tell them apart from the arg
    boundary alone:

    * **space-joined phrase** — the build-time ngram detector re-joins
      ASCII phrases with a space (e.g. ``nano center``), so the string
      *is* one keyword in the index.
    * **accidental concatenation** — CJK terms have no spaces, so a
      quoted string like ``"楚光 小柒"`` is actually two seeds that
      happen to have been passed as one shell argument.

    Resolve per-argument with an evidence probe: if the exact string
    has doc-level evidence (``keyword_docs``), keep it as one phrase
    seed; otherwise split on whitespace into multiple seeds. The
    resulting list is case-folded, de-duplicated (first-seen order
    preserved), and empty entries are dropped.

    Args:
        conn: open SQLite connection (schema already ensured).
        raw_seeds: seed arguments as supplied by the caller (pre-fold).

    Returns:
        A list of folded, de-duplicated seed strings.
    """
    out: List[str] = []
    seen: set = set()
    for raw in raw_seeds:
        if raw is None:
            continue
        folded_phrase = _normalise_inner_whitespace(_fold(raw))
        if not folded_phrase:
            continue
        if " " in folded_phrase and not _phrase_in_evidence(conn, folded_phrase):
            candidates = folded_phrase.split()
        else:
            candidates = [folded_phrase]
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


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


def _normalise_tags(
    tags: Optional[Sequence[str]],
) -> Optional[List[str]]:
    """Normalise a user-supplied tag list to unique, non-empty labels.

    Mirrors :func:`_normalise_scopes`:

    * ``None`` -> ``None`` (no tag filter).
    * empty sequence / only-blank items -> ``None`` (treat as "no filter",
      so a forwarded unset field never raises).
    * one or more real labels -> stripped + deduped list, order preserved.

    Tags are stripped but NOT casefolded — they are opaque labels whose
    casing must match what was written at build time (same contract as
    ``scopes.name``).
    """
    if tags is None:
        return None
    seen: set = set()
    out: List[str] = []
    for tag in tags:
        if tag is None:
            continue
        text = str(tag).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
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
    tags: Optional[Sequence[str]] = None,
    tag_logic: str = DEFAULT_TAG_LOGIC,
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
    * ``scopes`` is a **multi-select** per-scope-name filter (v10): only
      pairs whose ``scope_id`` is in the resolved set are returned.
      ``None`` (or empty) = no name filter.
    * ``tags`` is a **multi-select** tag filter (v11): scope ids are
      resolved from ``scope_tags`` under ``tag_logic`` (``or`` = any tag,
      ``and`` = every tag), then intersected with the ``scopes`` filter
      (if any). Both empty = cross-scope union. See
      plan-2026-07-14-scope-tags.
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
        tags:           optional multi-select scope-tag filter.
        tag_logic:      ``"or"`` (default) or ``"and"`` — how multiple
                        ``tags`` combine.

    Returns:
        A :class:`RelatedKeywordsResult` whose ``results`` is the flat list
        of :class:`RelatedKeyword`, sorted by ``(via_seed, -npmi, kw)``, and
        whose ``resolved_seeds`` reports per-seed vocabulary membership in
        first-seen input order.
    """
    if tag_logic not in VALID_TAG_LOGIC:
        raise ValueError(
            f"tag_logic must be one of {VALID_TAG_LOGIC}, got {tag_logic!r}"
        )
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_names = _normalise_scopes(scopes)
    tag_names = _normalise_tags(tags)
    effective_min_npmi = (
        _default_min_npmi() if min_npmi is None else float(min_npmi)
    )
    # Fast path: if every raw seed is empty after basic folding, there is
    # nothing to resolve and no need to open a DB connection. This
    # preserves the pre-fallback zero-side-effect behaviour for empty
    # inputs (which callers may use as a defensive guard).
    if not any(_fold(s) for s in seeds):
        return RelatedKeywordsResult(results=[], resolved_seeds=[])
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        seed_folded = _resolve_seed_args(conn, seeds)
        if not seed_folded:
            return RelatedKeywordsResult(results=[], resolved_seeds=[])
        return _related_keywords_impl(
            conn,
            seed_folded,
            limit_per_seed=limit_per_seed,
            min_npmi=effective_min_npmi,
            scope_names=scope_names,
            tag_names=tag_names,
            tag_logic=tag_logic,
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
        # No ORDER BY here, so ``scope_id IN (...)`` still uses the
        # composite index (a-side / b-side PK prefix) without a temp
        # sort. Safe to keep the IN form; per-scope loop is not needed.
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
    tag_names: Optional[Sequence[str]] = None,
    tag_logic: str = DEFAULT_TAG_LOGIC,
) -> RelatedKeywordsResult:
    """Core query path for :func:`related_keywords`.

    Split out so tests can hand-inject a connection. ``seeds`` is
    expected casefold-normalised and de-duplicated (order preserved).
    ``scope_names`` is a validated list of logical scope names or
    ``None`` (no name filter). ``tag_names`` is a validated list of
    scope-tag labels or ``None`` (no tag filter). When both are set,
    their resolved ``scope_id`` sets are **intersected** (design:
    plan-2026-07-14-scope-tags §4).
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

    # Resolve scope_id filter from --scope names and/or --tag labels.
    # * name-only → SELECT id FROM scopes WHERE name IN (...)
    # * tag-only  → scope_tags lookup under tag_logic (and/or)
    # * both      → intersection (a scope must satisfy BOTH)
    # * neither   → cross-scope union (empty scope_ids ⇒ no filter clause)
    #
    # When either filter is present but resolves empty, the intersection
    # is empty by construction — return early with an empty result set
    # rather than falling through to cross-scope (which would violate the
    # user's explicit "narrow down" intent).
    scope_ids: List[int] = []
    have_scope_filter = scope_names is not None and len(scope_names) > 0
    have_tag_filter = tag_names is not None and len(tag_names) > 0
    name_scope_ids: Optional[set] = None
    tag_scope_ids: Optional[set] = None

    if have_scope_filter:
        s_placeholders = ",".join("?" for _ in scope_names)
        rows = conn.execute(
            f"SELECT id FROM scopes WHERE name IN ({s_placeholders})",
            list(scope_names),
        ).fetchall()
        name_scope_ids = {int(r[0]) for r in rows}

    if have_tag_filter:
        # Delegate to graph.scope so the SQL shape (and its and/or
        # semantics) live in one place. The helper returns [] on empty
        # input; ``have_tag_filter`` already guaranteed at least one
        # non-empty label, so a [] here means "no scope carries the tag".
        from grga.graph.scope import resolve_scope_ids_by_tags
        tag_scope_ids = set(resolve_scope_ids_by_tags(
            conn, tag_names, logic=tag_logic,
        ))

    if name_scope_ids is not None and tag_scope_ids is not None:
        merged = name_scope_ids & tag_scope_ids
    elif name_scope_ids is not None:
        merged = name_scope_ids
    elif tag_scope_ids is not None:
        merged = tag_scope_ids
    else:
        merged = None  # cross-scope

    if merged is not None:
        scope_ids = sorted(merged)
        if not scope_ids:
            # An explicit filter was requested but nothing matched. Bail
            # out rather than degrading to the cross-scope path.
            return RelatedKeywordsResult(
                results=[], resolved_seeds=resolved_seeds
            )

    seed_ids: List[Tuple[int, str]] = []
    for s in seeds:
        if s in text_to_id:
            seed_ids.append((text_to_id[s], s))
    if not seed_ids:
        return RelatedKeywordsResult(results=[], resolved_seeds=resolved_seeds)

    # Perf: the composite indexes ``idx_kp_{a,b}_npmi (scope_id, kw_?_id,
    # npmi DESC)`` support an ordered-LIMIT walk ONLY when ``scope_id`` is
    # an equality predicate on a single value. Two patterns lose that
    # fast path:
    #
    #   * ``scope_id IN (a, b, ...)``: SQLite index-searches each scope
    #     but must materialise every matching row into a temp b-tree and
    #     sort before applying LIMIT (a few ms per scope but grows with
    #     matched-row count).
    #   * no ``scope_id`` clause at all (cross-scope): SQLite falls back
    #     to a full index scan over ``idx_kp_b_npmi`` and a temp-sort,
    #     which on a ~76M-row ``keyword_pairs`` table wedges the query
    #     for tens of seconds (regression report 2026-07-16).
    #
    # Fix: always resolve the query to a concrete list of scope ids
    # (populate from ``scopes`` table on the cross-scope path), then run
    # per-scope ``scope_id = ?`` queries in a Python loop and merge in
    # ``merged`` below. N queries of O(limit_per_seed) index-ordered rows
    # beat one query of O(pairs-touching-seed) even at N ≈ dozens of
    # scopes, and preserves index-ordered LIMIT semantics for the write
    # side of the pair invariant.
    if not scope_ids:
        scope_ids = [
            int(r[0]) for r in conn.execute("SELECT id FROM scopes")
        ]
        if not scope_ids:
            return RelatedKeywordsResult(
                results=[], resolved_seeds=resolved_seeds,
            )

    seen_kw_ids: set = set()
    # Per-seed candidate rows: (nbr_id, source, npmi, cooccur, scope_ids set).
    per_seed_rows: List[
        Tuple[str, List[Tuple[int, str, float, float, "frozenset[int]"]]]
    ] = []
    # Every scope_id that appeared on any surviving edge — used at the end
    # for one batched scope_id → name reverse-lookup.
    hit_scope_ids: set = set()

    for (seed_id, seed_text) in seed_ids:
        rows_a: List = []
        rows_b: List = []
        rows_llm_a: List = []
        rows_llm_b: List = []
        for sid in scope_ids:
            rows_a.extend(conn.execute(
                "SELECT kw_b_id, npmi, cooccur_count, source, scope_id "
                "FROM keyword_pairs "
                "WHERE scope_id = ? AND kw_a_id = ? AND source = ? "
                "AND npmi >= ? "
                "ORDER BY npmi DESC LIMIT ?",
                (sid, seed_id, CHUNK_COOCCUR_SOURCE,
                 float(min_npmi), int(limit_per_seed)),
            ).fetchall())
            rows_b.extend(conn.execute(
                "SELECT kw_a_id, npmi, cooccur_count, source, scope_id "
                "FROM keyword_pairs "
                "WHERE scope_id = ? AND kw_b_id = ? AND source = ? "
                "AND npmi >= ? "
                "ORDER BY npmi DESC LIMIT ?",
                (sid, seed_id, CHUNK_COOCCUR_SOURCE,
                 float(min_npmi), int(limit_per_seed)),
            ).fetchall())
            # llm_promoted: no npmi filter (may be NULL). Small set, no LIMIT.
            # Without ANALYZE stats, SQLite picks the composite PK
            # ``(scope_id, kw_a_id, kw_b_id, source)`` for the b-side query
            # and scans the whole (scope_id=?) partition — pathological on
            # a 76M-row table (5s+ on the ``工作`` scope, 2026-07-16). The
            # a-side query uses the PK's ``kw_a_id`` prefix and is fine;
            # only the b-side needs the ``INDEXED BY`` hint to force
            # ``idx_kp_b_npmi (scope_id, kw_b_id, ...)``, which narrows to
            # the seed's b-side rows in one covering-index seek before
            # the tiny source filter.
            rows_llm_a.extend(conn.execute(
                "SELECT kw_b_id, npmi, cooccur_count, source, scope_id "
                "FROM keyword_pairs "
                "WHERE scope_id = ? AND kw_a_id = ? AND source = ?",
                (sid, seed_id, "llm_promoted"),
            ).fetchall())
            rows_llm_b.extend(conn.execute(
                "SELECT kw_a_id, npmi, cooccur_count, source, scope_id "
                "FROM keyword_pairs INDEXED BY idx_kp_b_npmi "
                "WHERE scope_id = ? AND kw_b_id = ? AND source = ?",
                (sid, seed_id, "llm_promoted"),
            ).fetchall())

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
    # ``scope_ids`` is guaranteed non-empty at this point — the cross-scope
    # path populated it from ``scopes`` above, and an empty ``scopes``
    # table returned early. No fallback branch is needed; the
    # ``scope_id IN (...)`` predicate keeps ``keyword_stats`` restricted
    # to the caller's query range, which matters for the hub-word
    # ``chunk_df`` signal (plan-2026-07-14 §4.2).
    sp = ",".join("?" for _ in scope_ids)
    for i in range(0, len(id_list), chunk):
        piece = id_list[i:i + chunk]
        ph = ",".join("?" for _ in piece)
        for r in conn.execute(
            f"SELECT id, kw FROM keywords WHERE id IN ({ph})", piece
        ):
            kw_text[int(r[0])] = str(r[1])
        for r in conn.execute(
            f"SELECT kw_id, SUM(chunk_df) FROM keyword_stats "
            f"WHERE kw_id IN ({ph}) AND scope_id IN ({sp}) "
            f"GROUP BY kw_id",
            (*piece, *scope_ids),
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

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    scope_name = resolve_scope_name(scope)
    # Fast path: nothing to do with all-empty seeds; skip the connection.
    if not any(_fold(k) for k in keywords):
        return []
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        seeds = _resolve_seed_args(conn, keywords)
        if not seeds:
            return []
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

