"""Keyword inspection over the shared graph (user-scoped observability).

``inspect_keyword`` answers "before I open ``--cross-scope`` on this seed,
how does it distribute across my corpora and is it likely polysemous?".

It is a pure read over the chunk-level tables (schema v4):

* ``docs_by_scope``  - how many documents in each indexed scope contain the
  keyword. A keyword spread thinly across many unrelated scopes is the
  classic same-name-different-meaning ("attention", "model", "client")
  signature.
* ``top_cooccurring_keywords`` - the highest-weight neighbours of the
  keyword, together with the scopes they were observed in.
* ``suspected_polysemy`` - a cheap heuristic: if the per-scope sets of top
  co-occurring keywords barely overlap (pairwise Jaccard < 0.2), the
  keyword probably means different things in different corpora and the
  caller should think twice before enabling cross-scope expansion.

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
) -> dict:
    """Return a scope-distribution + polysemy report for ``keyword``.

    Args:
        keyword: the term to inspect (case-insensitive).
        db_path: SQLite path (defaults to the shared deep-search DB).
        limit: cap on ``top_cooccurring_keywords`` returned.
        cooccur_limit: how many top neighbours to consider *per scope* when
            estimating polysemy (keeps the Jaccard comparison focused on the
            characteristic head of each scope).

    Returns a dict::

        {
          "keyword": str,
          "total_docs": int,
          "docs_by_scope": {scope: count, ...},
          "top_cooccurring_keywords": [
            {"keyword": str, "weight": float, "shared_scopes": [scope, ...]},
            ...
          ],
          "suspected_polysemy": bool,
          "polysemy_hint": str,
        }

    The not-indexed branch (``total_docs == 0``) instead returns an
    ``index_hint`` field and **omits** ``polysemy_hint`` /
    ``suspected_polysemy``: a keyword that never entered the graph cannot
    be polysemous, and the naming ironclad (design §1.5) forbids putting
    an index-layer message under a ``polysemy_*`` name. The two hints are
    independent fields — ``polysemy_hint`` describes a cross-scope meaning
    collision, ``index_hint`` describes an indexing gap — so a caller keys
    off field *presence* to tell the two situations apart.
    """
    folded = _fold(keyword)
    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        return _inspect(conn, keyword, folded, limit, cooccur_limit)
    finally:
        conn.close()


def _inspect(
    conn: sqlite3.Connection,
    original: str,
    folded: str,
    limit: int,
    cooccur_limit: int,
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
        return empty

    # Documents (per scope) that contain the keyword.
    docs_by_scope, _seed_docs_ignored = _seed_docs(conn, folded)
    if not docs_by_scope:
        return empty
    total_docs = sum(docs_by_scope.values())

    # Co-occurring keywords: other keywords living in the same *chunk* as
    # the seed (chunk-level, not doc-level — a doc-level aggregation would
    # collapse every seed inside a document to the same neighbour set and
    # produce the "identical top-N across unrelated seeds" pathology).
    cooccur_weight, cooccur_scopes, per_scope_cooccur = _cooccurrences(
        conn, folded
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

    return {
        "keyword": original,
        "total_docs": total_docs,
        "docs_by_scope": dict(
            sorted(docs_by_scope.items(), key=lambda item: (-item[1], item[0]))
        ),
        "top_cooccurring_keywords": top_cooccurring_out,
        "suspected_polysemy": suspected,
        "polysemy_hint": hint,
    }


def _cooccurrences(
    conn: sqlite3.Connection,
    folded: str,
) -> tuple[Dict[str, float], Dict[str, set], Dict[str, Dict[str, float]]]:
    """Aggregate keywords co-occurring with ``folded`` at chunk level.

    Returns ``(cooccur_weight, cooccur_scopes, per_scope_cooccur)`` where
    weights are the raw sum of ``occurrence_count`` for every neighbour
    keyword row that lives in a chunk the seed also occupies. The join is
    on ``(directory, doc_path, chunk_id)`` — a doc-level aggregation would
    lump together every seed-bearing document's entire keyword histogram
    and yield the "identical top-N across unrelated seeds" pathology.
    Shared by :func:`_inspect` and :func:`compute_suspected_polysemy`.
    """
    cooccur_weight: Dict[str, float] = {}
    cooccur_scopes: Dict[str, set] = {}
    per_scope_cooccur: Dict[str, Dict[str, float]] = {}
    rows = conn.execute(
        """
        SELECT other.directory   AS directory,
               other.keyword     AS kw,
               SUM(other.occurrence_count) AS occurrence_count
        FROM keyword_chunks AS seed
        JOIN keyword_chunks AS other
          ON other.directory = seed.directory
         AND other.doc_path  = seed.doc_path
         AND other.chunk_id  = seed.chunk_id
        WHERE seed.keyword = ?
          AND other.keyword <> seed.keyword
        GROUP BY other.directory, other.keyword
        """,
        (folded,),
    ).fetchall()
    for row in rows:
        kw = _fold(row["kw"])
        if not kw or kw == folded:
            continue
        directory = row["directory"]
        weight = float(row["occurrence_count"])
        cooccur_weight[kw] = cooccur_weight.get(kw, 0.0) + weight
        cooccur_scopes.setdefault(kw, set()).add(directory)
        scope_map = per_scope_cooccur.setdefault(directory, {})
        scope_map[kw] = scope_map.get(kw, 0.0) + weight
    return cooccur_weight, cooccur_scopes, per_scope_cooccur


def _seed_docs(
    conn: sqlite3.Connection,
    folded: str,
) -> tuple[Dict[str, int], Dict[str, set]]:
    """Return ``(docs_by_scope, seed_docs_by_scope)`` for a single keyword.

    Shared by :func:`_inspect` and :func:`compute_suspected_polysemy` so the
    seed set is resolved identically at both entry points — otherwise the
    preview's ``suspected_polysemy`` flag could drift from the standalone
    ``graph inspect`` verdict on the same keyword. Returns empty maps when
    the keyword is absent.
    """
    seed_rows = conn.execute(
        """
        SELECT DISTINCT directory, doc_path
        FROM keyword_chunks
        WHERE keyword = ?
        """,
        (folded,),
    ).fetchall()
    docs_by_scope: Dict[str, int] = {}
    seed_docs_by_scope: Dict[str, set] = {}
    for row in seed_rows:
        directory = row["directory"]
        docs_by_scope[directory] = docs_by_scope.get(directory, 0) + 1
        seed_docs_by_scope.setdefault(directory, set()).add(row["doc_path"])
    return docs_by_scope, seed_docs_by_scope


def _seed_docs_batch(
    conn: sqlite3.Connection,
    folded_keywords: Sequence[str],
) -> Dict[str, Dict[str, int]]:
    """Batched :func:`_seed_docs` — one scan resolves many keywords at once.

    Used by :func:`compute_suspected_polysemy_batch` so annotating the top-N
    preview bridge keywords costs a single ``keyword IN (...)`` query instead
    of N separate full scans of ``keyword_chunks``. Returns
    ``{folded_keyword: docs_by_scope}`` for every requested keyword that has
    at least one row. Only ``docs_by_scope`` is materialised — the polysemy
    heuristic resolves per-scope co-occurrence via a separate chunk-level
    self-join and never consumes the seed doc set.
    """
    uniq = [kw for kw in dict.fromkeys(folded_keywords) if kw]
    if not uniq:
        return {}
    placeholders = ",".join("?" for _ in uniq)
    rows = conn.execute(
        f"""
        SELECT keyword AS kw, directory, doc_path
        FROM keyword_chunks
        WHERE keyword IN ({placeholders})
        GROUP BY keyword, directory, doc_path
        """,
        uniq,
    ).fetchall()
    out: Dict[str, Dict[str, int]] = {}
    for row in rows:
        kw = _fold(row["kw"])
        docs_by_scope = out.setdefault(kw, {})
        directory = row["directory"]
        docs_by_scope[directory] = docs_by_scope.get(directory, 0) + 1
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
    docs_by_scope, _ = _seed_docs(conn, folded)
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

    Resolves every keyword's seed set in one ``keyword IN (...)`` scan (see
    :func:`_seed_docs_batch`) rather than one full scan per keyword. The
    verdict is identical to calling :func:`compute_suspected_polysemy`
    individually. Missing keywords map to ``False``. The returned dict is
    keyed on the *folded* keyword.
    """
    folded = [_fold(kw) for kw in keywords]
    seed_map = _seed_docs_batch(conn, folded)
    out: Dict[str, bool] = {}
    for kw in dict.fromkeys(folded):
        if not kw:
            continue
        docs_by_scope = seed_map.get(kw, {})
        if not docs_by_scope:
            out[kw] = False
            continue
        _, _, per_scope_cooccur = _cooccurrences(conn, kw)
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
