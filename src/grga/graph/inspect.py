"""Keyword inspection over the shared graph (user-scoped observability).

``inspect_keyword`` answers "before I open ``--cross-scope`` on this seed,
how does it distribute across my corpora and is it likely polysemous?".

It is a pure read over the v9 keyword-primary asset tables:

* ``docs_by_scope``  - how many documents in each indexed scope contain the
  keyword (via ``keyword_docs``).
* ``top_cooccurring_keywords`` - the highest-weight neighbours of the
  keyword, together with the scopes they were observed in (via
  ``keyword_pairs``).
* ``suspected_polysemy`` - if the per-scope sets of top co-occurring
  keywords barely overlap (pairwise Jaccard < 0.2), the keyword probably
  means different things in different corpora and the caller should think
  twice before enabling cross-scope expansion.

The command never touches an LLM and never writes.
"""
from __future__ import annotations

import sqlite3
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from grga.graph.schema import connect, ensure_schema

DEFAULT_LIMIT = 20
DEFAULT_COOCCUR_LIMIT = 10
DEFAULT_SIMILAR_LIMIT = 10
# Default cap on ``top_docs`` returned in the indexed branch. Small enough
# to keep JSON reports terse even for hub keywords (``total_docs`` in the
# thousands), large enough to give the caller a useful "which files hold
# this term" starting point without a round-trip to ``search``. ``0``
# disables the field entirely.
DEFAULT_DOCS_LIMIT = 5
# Below this length a substring match becomes noise (e.g. seed "a" would
# match anything containing "a"). The not-indexed vocab probe silently
# skips shorter seeds instead of returning a firehose.
SIMILAR_MIN_SEED_LEN = 2
POLYSEMY_JACCARD_THRESHOLD = 0.2


def _fold(term: str) -> str:
    return term.casefold().strip()


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def inspect_keyword(
    keyword: str,
    *,
    db_path: Union[str, Path, None] = None,
    limit: int = DEFAULT_LIMIT,
    cooccur_limit: int = DEFAULT_COOCCUR_LIMIT,
    similar_limit: int = DEFAULT_SIMILAR_LIMIT,
    docs_limit: int = DEFAULT_DOCS_LIMIT,
    scope_names: Optional[Sequence[str]] = None,
    tag_names: Optional[Sequence[str]] = None,
    tag_logic: str = "or",
) -> dict:
    """Return a scope-distribution + polysemy report for ``keyword``.

    Args:
        keyword: the term to inspect (case-insensitive).
        db_path: SQLite path (defaults to the shared deep-search DB).
        limit: cap on ``top_cooccurring_keywords`` returned.
        cooccur_limit: how many top neighbours to consider *per scope* when
            estimating polysemy (keeps the Jaccard comparison focused on the
            characteristic head of each scope).
        similar_limit: cap on ``similar_keywords`` in the not-indexed branch
            (see below). Ignored on the indexed branch.
        docs_limit: cap on ``top_docs`` in the indexed branch. ``0``
            suppresses the field entirely. Ignored on the not-indexed /
            knowledge-only branches (both lack local ``keyword_docs``
            evidence — a ``top_docs`` field there would be misleading).
        scope_names: optional list of logical scope names (``--scope``).
            ``None`` / empty = cross-scope union. Feeds the same
            ``keyword_pairs.scope_id IN (...)`` / ``keyword_docs`` filter
            path as :func:`grga.related.related_keywords`.
        tag_names: optional list of scope-tag labels (``--tag``, v11).
            ``None`` / empty = no tag filter.
        tag_logic: ``"or"`` (default) or ``"and"`` — how multiple ``tag_names``
            values combine. Mirrors ``grga related --tag-logic``.

    ``scope_names`` and ``tag_names`` are **intersected** when both are
    provided (name ∩ tag), matching ``related``. An explicit filter that
    resolves to zero scopes returns a dedicated ``scope_filter_hint`` and
    empties every scope-scoped field — never a silent fall-back to
    cross-scope (which would violate the caller's narrow-down intent).

    Returns a dict::

        {
          "keyword": str,
          "total_docs": int,
          "docs_by_scope": {scope: count, ...},
          "top_docs": [                       # indexed branch only
            {"scope": str, "doc_path": str,
             "occurrence_count": int, "chunk_count": int},
            ...
          ],
          "top_docs_truncated": bool,         # indexed branch only
          "top_cooccurring_keywords": [
            {"keyword": str, "weight": float, "shared_scopes": [scope, ...]},
            ...
          ],
          "suspected_polysemy": bool,
          "polysemy_hint": str,
        }

    ``top_docs`` is a locate-me shortcut: reads ``keyword_docs`` (already
    scanned for the doc-count aggregate) and returns the top ``docs_limit``
    document paths per keyword, sorted by
    ``(-occurrence_count, -chunk_count, scope, doc_path)`` across scopes.
    This saves the caller a round-trip to ``search`` when the intent is
    just "which files hold this term?". ``top_docs_truncated`` is ``True``
    when ``total_docs`` exceeds ``docs_limit``.

    The not-indexed branch (``total_docs == 0``) instead returns an
    ``index_hint`` field and **omits** ``polysemy_hint`` /
    ``suspected_polysemy``: a keyword that never entered the graph cannot
    be polysemous, and the naming ironclad (design §1.5) forbids putting
    an index-layer message under a ``polysemy_*`` name. The two hints are
    independent fields — ``polysemy_hint`` describes a cross-scope meaning
    collision, ``index_hint`` describes an indexing gap — so a caller keys
    off field *presence* to tell the two situations apart.

    The not-indexed branch additionally includes ``similar_keywords``: a
    list of vocabulary entries whose text contains the folded seed
    (``kind = "superstring"``) or is contained in it (``kind =
    "substring"``). This surfaces the objective fact "the graph knows
    a related spelling" so the caller can retry with a corrected seed
    (e.g. ``related 苍坤`` empty → ``similar_keywords: [{kw: 苍坤上人,
    kind: superstring}]`` → retry). Empty list when the seed is too short
    (< 2 chars) or nothing matches.
    """
    if tag_logic not in ("and", "or"):
        raise ValueError(
            f"tag_logic must be 'and' or 'or', got {tag_logic!r}"
        )
    folded = _fold(keyword)
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        scope_ids, filter_requested, filter_desc = _resolve_scope_filter(
            conn, scope_names, tag_names, tag_logic,
        )
        if filter_requested and not scope_ids:
            # Explicit --scope / --tag that matched nothing: bail out
            # with a distinct hint rather than degrading to cross-scope.
            return {
                "keyword": keyword,
                "total_docs": 0,
                "docs_by_scope": {},
                "top_cooccurring_keywords": [],
                "scope_filter_hint": (
                    f"no scope matched the requested filter ({filter_desc}). "
                    f"Run `grga scope list` to inspect available scopes / "
                    f"tags, or drop the filter for a cross-scope view."
                ),
            }
        return _inspect(
            conn, keyword, folded, limit, cooccur_limit, similar_limit,
            docs_limit, scope_ids if filter_requested else None,
        )
    finally:
        conn.close()


def _resolve_scope_filter(
    conn: sqlite3.Connection,
    scope_names: Optional[Sequence[str]],
    tag_names: Optional[Sequence[str]],
    tag_logic: str,
) -> tuple[List[int], bool, str]:
    """Resolve ``--scope`` / ``--tag`` into a concrete ``scope_id`` list.

    Mirrors :func:`grga.related._related_keywords_impl` so both verbs
    agree on filter semantics:

    * name-only  → ``SELECT id FROM scopes WHERE name IN (...)``
    * tag-only   → :func:`grga.graph.scope.resolve_scope_ids_by_tags`
    * both       → intersection (a scope must satisfy BOTH)
    * neither    → ``(_, False, "")`` = cross-scope (no WHERE clause)

    Returns ``(scope_ids, filter_requested, filter_desc)``.
    ``filter_requested`` is ``True`` iff the caller passed at least one
    non-empty ``--scope`` / ``--tag`` — used to distinguish "no filter"
    (union across all scopes) from "filter that matched zero scopes"
    (bail out with a hint). ``filter_desc`` is a compact human-readable
    summary used in the empty-match hint (never surfaced when the filter
    resolves to a non-empty set).
    """
    have_scope = scope_names is not None and any(s for s in scope_names)
    have_tag = tag_names is not None and any(t for t in tag_names)
    if not have_scope and not have_tag:
        return [], False, ""

    name_scope_ids: Optional[set] = None
    tag_scope_ids: Optional[set] = None
    parts: List[str] = []

    if have_scope:
        names = [s for s in scope_names if s]  # type: ignore[union-attr]
        placeholders = ",".join("?" for _ in names)
        rows = conn.execute(
            f"SELECT id FROM scopes WHERE name IN ({placeholders})",
            names,
        ).fetchall()
        name_scope_ids = {int(r[0]) for r in rows}
        parts.append(f"scope={sorted(names)}")

    if have_tag:
        from grga.graph.scope import resolve_scope_ids_by_tags
        tags = [t for t in tag_names if t]  # type: ignore[union-attr]
        tag_scope_ids = set(
            resolve_scope_ids_by_tags(conn, tags, logic=tag_logic)
        )
        parts.append(f"tag={sorted(tags)} logic={tag_logic}")

    if name_scope_ids is not None and tag_scope_ids is not None:
        merged = name_scope_ids & tag_scope_ids
    elif name_scope_ids is not None:
        merged = name_scope_ids
    else:
        assert tag_scope_ids is not None
        merged = tag_scope_ids

    return sorted(merged), True, ", ".join(parts)


def _inspect(
    conn: sqlite3.Connection,
    original: str,
    folded: str,
    limit: int,
    cooccur_limit: int,
    similar_limit: int,
    docs_limit: int,
    scope_ids: Optional[Sequence[int]],
) -> dict:
    # Not-indexed branch: return a dedicated ``index_hint`` and omit the
    # polysemy fields entirely (a term that never entered the graph is
    # by definition not polysemous, and re-purposing ``polysemy_hint`` for
    # an indexing message violates the naming ironclad — see §1.5.1).
    # ``suspected_polysemy`` is also omitted so callers key on field
    # *presence* to distinguish the two branches; a stray ``False`` here
    # was previously read by SKILL.md as "checked and cleared", which is
    # not what "keyword never seen" means.
    empty = {
        "keyword": original,
        "total_docs": 0,
        "docs_by_scope": {},
        "top_cooccurring_keywords": [],
        "index_hint": (
            f"'{original}' is not indexed in the graph. It may still appear "
            f"in the corpus text — try `grga search \"{original}\"` for "
            f"full-text hits, or `grga index build` to (re)index a scope."
        ),
    }
    if not folded:
        empty["similar_keywords"] = []
        return empty

    # Documents (per scope) that contain the keyword. ``scope_ids`` is
    # threaded into every scope-aware helper so an explicit --scope /
    # --tag filter narrows every field consistently (docs_by_scope,
    # top_docs, top_cooccurring_keywords, knowledge_scopes,
    # similar_keywords). None ⇒ cross-scope.
    docs_by_scope, _seed_docs_ignored, doc_hits = _seed_docs(
        conn, folded, scope_ids=scope_ids
    )
    if not docs_by_scope:
        # v10 knowledge-only branch (plan-2026-07-14 §9.4): when the seed
        # exists in ``keyword_stats`` (knowledge layer) but has zero
        # ``keyword_docs`` rows (evidence layer), this is a scope that
        # was populated by ``grga index import`` and not yet re-``build``ed
        # on this machine. Fall back to a cooccurrence-only report so the
        # caller still gets useful graph expansion, plus a distinct
        # ``evidence_hint`` explaining why doc counts are 0.
        knowledge_scopes = _knowledge_only_scopes(
            conn, folded, scope_ids=scope_ids
        )
        if knowledge_scopes:
            cooccur_weight, cooccur_scopes, _ = _cooccurrences(
                conn, folded, scope_ids=scope_ids,
            )
            top_cooccurring = sorted(
                cooccur_weight.items(), key=lambda item: (-item[1], item[0])
            )[:limit]
            top_cooccurring_out: List[dict] = [
                {
                    "keyword": kw,
                    "weight": round(weight, 6),
                    "shared_scopes": sorted(cooccur_scopes.get(kw, set())),
                }
                for kw, weight in top_cooccurring
            ]
            return {
                "keyword": original,
                "total_docs": 0,
                "docs_by_scope": {},
                "knowledge_scopes": sorted(knowledge_scopes),
                "top_cooccurring_keywords": top_cooccurring_out,
                "evidence_hint": (
                    f"'{original}' has knowledge in "
                    f"{sorted(knowledge_scopes)} but no local corpus "
                    f"evidence. Run `grga index build --scope <name> "
                    f"--directory <path>` to attach local documents, or "
                    f"use `grga related --scope <name>` for pure "
                    f"knowledge-graph expansion (evidence not required)."
                ),
            }
        empty["similar_keywords"] = _similar_keywords(
            conn, folded, similar_limit, scope_ids=scope_ids
        )
        return empty
    total_docs = sum(docs_by_scope.values())

    # Co-occurring keywords: neighbours of the seed in the co-occurrence
    # graph (``keyword_pairs``, chunk-level cooccur_count materialised at
    # build time). Per-scope breakdown feeds the polysemy heuristic.
    cooccur_weight, cooccur_scopes, per_scope_cooccur = _cooccurrences(
        conn, folded, scope_ids=scope_ids,
    )

    top_cooccurring = sorted(
        cooccur_weight.items(), key=lambda item: (-item[1], item[0])
    )[:limit]
    top_cooccurring_out: List[dict] = [
        {
            "keyword": kw,
            "weight": round(weight, 6),
            "shared_scopes": sorted(cooccur_scopes.get(kw, set())),
        }
        for kw, weight in top_cooccurring
    ]

    suspected, hint = _detect_polysemy(
        docs_by_scope, per_scope_cooccur, cooccur_limit
    )

    result = {
        "keyword": original,
        "total_docs": total_docs,
        "docs_by_scope": dict(
            sorted(docs_by_scope.items(), key=lambda item: (-item[1], item[0]))
        ),
        "top_cooccurring_keywords": top_cooccurring_out,
        "suspected_polysemy": suspected,
        "polysemy_hint": hint,
    }
    # ``top_docs``: "which files hold this term" locate-me shortcut. Reads
    # off the same ``keyword_docs`` scan already done above so it costs
    # nothing extra. Sorted globally (not per-scope) by occurrence density
    # so the caller sees the strongest hits first regardless of which
    # corpus they live in; ``scope`` is on each row for routing back.
    # Suppressed when ``docs_limit <= 0`` — caller opt-out for JSON size.
    if docs_limit > 0 and doc_hits:
        ordered = sorted(
            doc_hits,
            key=lambda h: (-h["occurrence_count"], -h["chunk_count"], h["scope"], h["doc_path"]),
        )
        result["top_docs"] = ordered[:docs_limit]
        result["top_docs_truncated"] = len(ordered) > docs_limit
    return result


def _resolve_kw_ids(
    conn: sqlite3.Connection,
    folded_keywords: Sequence[str],
) -> Dict[str, int]:
    """Resolve casefolded keywords to their global kw_id in one query.

    ``keywords`` has a unique index on ``kw`` so the batched IN lookup is
    O(log N) per key. Missing keywords are simply absent from the result.
    """
    uniq = [kw for kw in dict.fromkeys(folded_keywords) if kw]
    if not uniq:
        return {}
    placeholders = ",".join("?" for _ in uniq)
    out: Dict[str, int] = {}
    for r in conn.execute(
        f"SELECT id, kw FROM keywords WHERE kw IN ({placeholders})",
        uniq,
    ):
        out[str(r["kw"])] = int(r["id"])
    return out


def _scope_filter_clause(
    column: str,
    scope_ids: Optional[Sequence[int]],
) -> tuple[str, List[int]]:
    """Build a ``AND {column} IN (?, ?, ...)`` fragment and its params.

    Central so every scope-aware helper here (``_seed_docs``,
    ``_cooccurrences_batch``, ``_knowledge_only_scopes``,
    ``_similar_keywords``) narrows on ``scope_id`` the same way and
    survives the same escape/placeholder rules. Returns ``("", [])``
    when ``scope_ids`` is ``None`` (cross-scope). An empty *list* is
    treated the same as ``None`` and never surfaces as ``IN ()`` — the
    caller upstream is responsible for handling "explicit filter, zero
    matches" (see :func:`_resolve_scope_filter`).
    """
    if not scope_ids:
        return "", []
    ids = list(scope_ids)
    placeholders = ",".join("?" for _ in ids)
    return f" AND {column} IN ({placeholders})", ids


def _cooccurrences(
    conn: sqlite3.Connection,
    folded: str,
    *,
    scope_ids: Optional[Sequence[int]] = None,
) -> tuple[Dict[str, float], Dict[str, set], Dict[str, Dict[str, float]]]:
    """Aggregate keywords co-occurring with ``folded`` via ``keyword_pairs``.

    v9 note: chunk-level co-occurrence is now materialised into
    ``keyword_pairs.cooccur_count`` at build time (design D6). Reading
    those edges directly is the canonical way to answer "who does this
    keyword keep company with?" — the pre-v9 ``keyword_chunks`` self-join
    is gone.

    Post-review (F4): the query is now on kw_id equality (``kw_a_id = ?``
    / ``kw_b_id = ?``), so SQLite can use the two ``(dir_id, kw_a_id, ...)``
    / ``(dir_id, kw_b_id, ...)`` indexes — no more full-table scan on
    text-column OR. For batch use, see :func:`_cooccurrences_batch`.

    ``scope_ids`` narrows the read to the given scope shard(s). ``None``
    or empty = cross-scope union.

    Returns ``(cooccur_weight, cooccur_scopes, per_scope_cooccur)``.
    """
    kw_ids = _resolve_kw_ids(conn, [folded])
    seed_kw_id = kw_ids.get(folded)
    if seed_kw_id is None:
        return {}, {}, {}
    batch = _cooccurrences_batch(
        conn, {folded: seed_kw_id}, scope_ids=scope_ids
    )
    return batch.get(folded, ({}, {}, {}))


def _cooccurrences_batch(
    conn: sqlite3.Connection,
    seed_kw_ids: Dict[str, int],
    *,
    scope_ids: Optional[Sequence[int]] = None,
) -> Dict[str, tuple[Dict[str, float], Dict[str, set], Dict[str, Dict[str, float]]]]:
    """Batched :func:`_cooccurrences` — one scan resolves many keywords.

    ``seed_kw_ids`` maps folded keyword → kw_id (as produced by
    :func:`_resolve_kw_ids`). Returns
    ``{folded_keyword: (cooccur_weight, cooccur_scopes, per_scope_cooccur)}``
    for every requested seed. Missing seeds are simply absent from the
    result.

    F4 (review): pre-fix, ``compute_suspected_polysemy_batch`` looped
    :func:`_cooccurrences` per keyword — turning a K-keyword batch into
    K full-table scans. This batched form issues one indexed query per
    endpoint side (a-side + b-side) using ``kw_a_id IN (...)`` / ``kw_b_id
    IN (...)`` so the whole batch costs two index probes.
    """
    if not seed_kw_ids:
        return {}
    id_to_folded: Dict[int, str] = {v: k for k, v in seed_kw_ids.items()}
    if len(id_to_folded) != len(seed_kw_ids):
        # Same kw_id resolves for multiple folded keys — shouldn't happen
        # (kw column is unique) but stay defensive.
        pass

    out: Dict[str, tuple[Dict[str, float], Dict[str, set], Dict[str, Dict[str, float]]]] = {
        folded: ({}, {}, {}) for folded in seed_kw_ids
    }

    id_list = list(id_to_folded.keys())
    if not id_list:
        return out

    # F4: two index-friendly IN queries — a-side and b-side of the pair.
    # Chunk-partition to dodge SQLITE_MAX_VARIABLE_NUMBER.
    scope_clause, scope_params = _scope_filter_clause("p.scope_id", scope_ids)
    for i in range(0, len(id_list), 900):
        piece = id_list[i:i + 900]
        ph = ",".join("?" for _ in piece)
        # a-side: seed is kw_a_id; other is kw_b_id
        for row in conn.execute(
            f"""
            SELECT s.name AS scope,
                   p.kw_a_id AS seed_id,
                   ok.kw     AS other_kw,
                   p.cooccur_count AS cooccur_count
            FROM keyword_pairs p
            JOIN keywords ok ON ok.id = p.kw_b_id
            JOIN scopes s ON s.id = p.scope_id
            WHERE p.source = 'chunk_cooccur'
              AND p.kw_a_id IN ({ph})
            {scope_clause}
            """,
            [*piece, *scope_params],
        ):
            _absorb_cooccur_row(row, id_to_folded, out)
        # b-side: seed is kw_b_id; other is kw_a_id
        for row in conn.execute(
            f"""
            SELECT s.name AS scope,
                   p.kw_b_id AS seed_id,
                   ok.kw     AS other_kw,
                   p.cooccur_count AS cooccur_count
            FROM keyword_pairs p
            JOIN keywords ok ON ok.id = p.kw_a_id
            JOIN scopes s ON s.id = p.scope_id
            WHERE p.source = 'chunk_cooccur'
              AND p.kw_b_id IN ({ph})
            {scope_clause}
            """,
            [*piece, *scope_params],
        ):
            _absorb_cooccur_row(row, id_to_folded, out)
    return out


def _absorb_cooccur_row(row, id_to_folded, out) -> None:
    """Fold one (seed_id, other_kw, directory, cooccur_count) row into ``out``.

    Shared by the two IN queries in :func:`_cooccurrences_batch` so the
    aggregation logic stays in one place.
    """
    seed_id = int(row["seed_id"])
    folded_seed = id_to_folded.get(seed_id)
    if folded_seed is None:
        return
    kw = _fold(row["other_kw"])
    if not kw or kw == folded_seed:
        return
    scope = row["scope"]
    weight = float(row["cooccur_count"])
    cooccur_weight, cooccur_scopes, per_scope_cooccur = out[folded_seed]
    cooccur_weight[kw] = cooccur_weight.get(kw, 0.0) + weight
    cooccur_scopes.setdefault(kw, set()).add(scope)
    scope_map = per_scope_cooccur.setdefault(scope, {})
    scope_map[kw] = scope_map.get(kw, 0.0) + weight

def _similar_keywords(
    conn: sqlite3.Connection,
    folded: str,
    limit: int,
    *,
    scope_ids: Optional[Sequence[int]] = None,
) -> List[dict]:
    """Vocabulary-level fallback for the not-indexed branch.

    Returns keywords whose text either contains the folded seed
    (``superstring``, e.g. seed ``苍坤`` → ``苍坤上人``) or is contained
    in the folded seed (``substring``, e.g. seed ``韩立道友`` → ``韩立``).
    Restricted to keywords that actually have at least one indexed doc
    (JOIN on ``keyword_docs``) — a pure ``keywords`` row without stats
    would be misleading as a "the graph knows this" signal.

    Cost model: ``keywords.kw`` is the only intern column so a substring
    LIKE against it is O(vocab). At typical corpus sizes (10K–100K
    tokens) this is a few ms; even at 1M tokens under 100 ms. The path
    is only reached when the seed did not resolve to any doc, so it
    never touches hot ``related`` / ``search`` code paths.

    Args:
        conn: open SQLite connection at v9 schema.
        folded: case-folded seed (empty string is the caller's problem).
        limit: max rows returned. ``<= 0`` disables the probe.

    Returns:
        List of ``{"kw": str, "kind": "superstring"|"substring",
        "chunk_df": int}``, sorted by ``(-chunk_df, kw)``. Empty when the
        seed is shorter than :data:`SIMILAR_MIN_SEED_LEN` (would fire on
        too much of the vocab), the disable flag is set, or nothing
        matches.
    """
    if limit <= 0:
        return []
    if len(folded) < SIMILAR_MIN_SEED_LEN:
        return []
    # Escape LIKE metacharacters so underscore/percent-bearing seeds
    # (common in code identifiers, e.g. ``max_file_kb``) match literally
    # rather than as SQL wildcards. Without this, ``LIMIT`` is applied in
    # SQL *before* the Python literal-substring re-filter below, so over-
    # matched rows could crowd out real matches and shrink the result.
    # We use ``\`` as ESCAPE and mirror the escape on both sides of the
    # OR (a-side pattern is user-controlled, b-side is stored ``k.kw``).
    esc = (
        folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    like_pattern = f"%{esc}%"
    # ``kd.scope_id`` narrows the vocab probe to the requested scopes so
    # a --scope-constrained "seed not found here" fallback only lists
    # spellings that actually live in those scopes.
    scope_clause, scope_params = _scope_filter_clause("kd.scope_id", scope_ids)
    rows = conn.execute(
        rf"""
        SELECT k.kw AS kw, COALESCE(SUM(kd.chunk_count), 0) AS chunk_df
        FROM keywords k
        JOIN keyword_docs kd ON kd.kw_id = k.id
        WHERE k.kw != ?
          AND (
            k.kw LIKE ? ESCAPE '\'
            OR ? LIKE '%' || REPLACE(REPLACE(REPLACE(k.kw, '\', '\\'),
                                             '%', '\%'),
                                     '_', '\_') || '%' ESCAPE '\'
          )
          {scope_clause}
        GROUP BY k.id
        ORDER BY chunk_df DESC, kw ASC
        LIMIT ?
        """,
        (folded, like_pattern, folded, *scope_params, int(limit)),
    ).fetchall()
    out: List[dict] = []
    for row in rows:
        kw = str(row["kw"])
        if folded in kw:
            kind = "superstring"
        elif kw and kw in folded:
            kind = "substring"
        else:
            continue
        out.append({
            "kw": kw,
            "kind": kind,
            "chunk_df": int(row["chunk_df"] or 0),
        })
    return out


def _knowledge_only_scopes(
    conn: sqlite3.Connection,
    folded: str,
    *,
    scope_ids: Optional[Sequence[int]] = None,
) -> List[str]:
    """Return scope names where ``folded`` has knowledge but no evidence.

    v10 (plan-2026-07-14 §9.4): a scope populated by ``grga index import``
    carries ``keyword_stats`` / ``keyword_pairs`` (knowledge) but no
    ``keyword_docs`` / ``docs`` (evidence). This helper answers "does the
    seed exist in the knowledge layer of any scope?" so :func:`_inspect`
    can distinguish that from a genuinely unknown seed. Reads
    ``keyword_stats`` (the per-scope knowledge presence signal).
    """
    scope_clause, scope_params = _scope_filter_clause("ks.scope_id", scope_ids)
    rows = conn.execute(
        f"""
        SELECT s.name AS scope
        FROM keyword_stats ks
        JOIN keywords k ON k.id = ks.kw_id
        JOIN scopes s ON s.id = ks.scope_id
        WHERE k.kw = ?
          {scope_clause}
        """,
        (folded, *scope_params),
    ).fetchall()
    return [str(row["scope"]) for row in rows]


def _seed_docs(
    conn: sqlite3.Connection,
    folded: str,
    *,
    scope_ids: Optional[Sequence[int]] = None,
) -> tuple[Dict[str, int], Dict[str, set], List[dict]]:
    """Return ``(docs_by_scope, seed_docs_by_scope, doc_hits)`` for a keyword.

    v9: reads the doc-level ``keyword_docs`` index. Shared by
    :func:`_inspect` and :func:`compute_suspected_polysemy` so the seed
    set is resolved identically at both entry points.

    ``doc_hits`` is the flat per-document list carrying the same
    ``occurrence_count`` / ``chunk_count`` columns that :func:`_inspect`
    surfaces as ``top_docs``. Kept here (rather than re-queried in
    ``_inspect``) so the single ``keyword_docs`` scan already fans out
    all three views. Callers that only need the aggregate (e.g.
    :func:`compute_suspected_polysemy`) simply ignore the third slot.

    ``scope_ids`` narrows the read to the given scope shard(s). ``None``
    or empty = cross-scope union.
    """
    scope_clause, scope_params = _scope_filter_clause("kd.scope_id", scope_ids)
    seed_rows = conn.execute(
        f"""
        SELECT s.name AS scope,
               d.doc_path AS doc_path,
               kd.occurrence_count AS occurrence_count,
               kd.chunk_count AS chunk_count
        FROM keyword_docs kd
        JOIN keywords k ON k.id = kd.kw_id
        JOIN docs d ON d.id = kd.doc_id
        JOIN scopes s ON s.id = d.scope_id
        WHERE k.kw = ?
          {scope_clause}
        """,
        (folded, *scope_params),
    ).fetchall()
    docs_by_scope: Dict[str, int] = {}
    seed_docs_by_scope: Dict[str, set] = {}
    doc_hits: List[dict] = []
    for row in seed_rows:
        scope = row["scope"]
        docs_by_scope[scope] = docs_by_scope.get(scope, 0) + 1
        seed_docs_by_scope.setdefault(scope, set()).add(row["doc_path"])
        doc_hits.append({
            "scope": scope,
            "doc_path": row["doc_path"],
            "occurrence_count": int(row["occurrence_count"] or 0),
            "chunk_count": int(row["chunk_count"] or 0),
        })
    return docs_by_scope, seed_docs_by_scope, doc_hits


def _seed_docs_batch(
    conn: sqlite3.Connection,
    folded_keywords: Sequence[str],
) -> Dict[str, Dict[str, int]]:
    """Batched :func:`_seed_docs` — one scan resolves many keywords at once.

    v9: reads the doc-level ``keyword_docs`` index. Returns
    ``{folded_keyword: docs_by_scope}`` for every requested keyword that
    has at least one row.
    """
    uniq = [kw for kw in dict.fromkeys(folded_keywords) if kw]
    if not uniq:
        return {}
    placeholders = ",".join("?" for _ in uniq)
    rows = conn.execute(
        f"""
        SELECT k.kw AS kw, s.name AS scope, d.doc_path AS doc_path
        FROM keyword_docs kd
        JOIN keywords k ON k.id = kd.kw_id
        JOIN docs d ON d.id = kd.doc_id
        JOIN scopes s ON s.id = d.scope_id
        WHERE k.kw IN ({placeholders})
        """,
        uniq,
    ).fetchall()
    out: Dict[str, Dict[str, int]] = {}
    for row in rows:
        kw = _fold(row["kw"])
        docs_by_scope = out.setdefault(kw, {})
        scope = row["scope"]
        docs_by_scope[scope] = docs_by_scope.get(scope, 0) + 1
    return out


def compute_suspected_polysemy(
    conn: sqlite3.Connection,
    keyword: str,
    cooccur_limit: int = DEFAULT_COOCCUR_LIMIT,
) -> tuple[bool, str]:
    """Standalone polysemy check for a single ``keyword``.

    Extracted from :func:`_inspect` so callers outside ``graph inspect``
    (e.g. ``compute_crossed_scopes_preview``) can annotate bridge keywords
    without reproducing the co-occurrence + Jaccard heuristic. Returns the
    same ``(suspected, hint)`` pair the full report exposes. A keyword found
    in fewer than two scopes is never flagged (``(False, "")``).
    """
    folded = _fold(keyword)
    if not folded:
        return False, ""
    docs_by_scope, _, _ = _seed_docs(conn, folded)
    if not docs_by_scope:
        return False, ""
    _, _, per_scope_cooccur = _cooccurrences(conn, folded)
    return _detect_polysemy(docs_by_scope, per_scope_cooccur, cooccur_limit)


def compute_suspected_polysemy_batch(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    cooccur_limit: int = DEFAULT_COOCCUR_LIMIT,
) -> Dict[str, bool]:
    """Batched polysemy verdict for many keywords: ``{keyword: suspected}``.

    F4 (review): pre-fix, this looped :func:`_cooccurrences` per keyword,
    turning a K-keyword batch into K full-table scans and defeating the
    :func:`_seed_docs_batch` batching. Now every keyword's neighbour set
    is resolved in one indexed pass via :func:`_cooccurrences_batch`.

    The verdict is identical to calling :func:`compute_suspected_polysemy`
    individually. Missing keywords map to ``False``. Returned dict is
    keyed on the *folded* keyword.
    """
    folded = [_fold(kw) for kw in keywords]
    seed_map = _seed_docs_batch(conn, folded)
    kw_ids = _resolve_kw_ids(conn, folded)
    cooccur_map = _cooccurrences_batch(conn, kw_ids)
    out: Dict[str, bool] = {}
    for kw in dict.fromkeys(folded):
        if not kw:
            continue
        docs_by_scope = seed_map.get(kw, {})
        if not docs_by_scope:
            out[kw] = False
            continue
        _, _, per_scope_cooccur = cooccur_map.get(kw, ({}, {}, {}))
        suspected, _ = _detect_polysemy(
            docs_by_scope, per_scope_cooccur, cooccur_limit
        )
        out[kw] = suspected
    return out


def _detect_polysemy(
    docs_by_scope: Dict[str, int],
    per_scope_cooccur: Dict[str, Dict[str, float]],
    cooccur_limit: int,
) -> tuple[bool, str]:
    """Heuristic: multi-scope keyword whose per-scope neighbour heads diverge.

    We take the top-``cooccur_limit`` co-occurring keywords of each scope and
    compute pairwise Jaccard similarity between the scope head-sets. If the
    *minimum* similarity across any scope pair drops below
    ``POLYSEMY_JACCARD_THRESHOLD`` we flag suspected polysemy: the keyword
    keeps very different company in different corpora, the tell-tale sign of
    a same-name-different-meaning collision.
    """
    scopes = list(per_scope_cooccur.keys())
    if len(scopes) < 2:
        return False, ""

    head_sets: Dict[str, set] = {}
    for scope in scopes:
        top = sorted(
            per_scope_cooccur[scope].items(),
            key=lambda item: (-item[1], item[0]),
        )[:cooccur_limit]
        head_sets[scope] = {kw for kw, _ in top}

    min_sim = 1.0
    for a, b in combinations(scopes, 2):
        sim = _jaccard(head_sets[a], head_sets[b])
        if sim < min_sim:
            min_sim = sim

    if min_sim < POLYSEMY_JACCARD_THRESHOLD:
        hint = (
            f"This keyword appears in {len(scopes)} scopes with largely "
            f"disjoint top-cooccurring keywords (min Jaccard "
            f"{min_sim:.2f} < {POLYSEMY_JACCARD_THRESHOLD}). Consider "
            f"disambiguation before enabling --cross-scope."
        )
        return True, hint
    return False, ""
