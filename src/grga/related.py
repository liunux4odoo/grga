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
from typing import Dict, List, Optional, Sequence, Union

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
    """A single related document (or keyword) with provenance."""

    doc_path: str
    score: float
    mode: str
    reason: str = ""
    keywords: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "doc_path": self.doc_path,
            "score": round(self.score, 6),
            "mode": self.mode,
            "reason": self.reason,
            "keywords": list(self.keywords),
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
) -> List[Related]:
    """Return documents related to ``keywords`` under the chosen ``mode``.

    Args:
        keywords: seed keywords (query terms).
        mode: one of ``graph`` | ``dir`` | ``heading``.
        limit: maximum number of results.
        db_path: SQLite path (defaults to shared deep-search DB).
        directory: optional scope filter; when given, only rows built for
            that indexed scope are considered.
        alpha / iters: PPR knobs (``graph`` mode only).

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
            return _related_graph(conn, seeds, limit, scope_key, alpha, iters)
        if mode == "dir":
            return _related_dir(conn, seeds, limit, scope_key)
        return _related_heading(conn, seeds, limit, scope_key)
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


def _related_graph(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    scope_key: Optional[str],
    alpha: float,
    iters: int,
) -> List[Related]:
    edges = load_edges(conn, directory=scope_key)
    adjacency = build_adjacency(edges)
    ranked_keywords = personalized_pagerank(
        seeds, adjacency, top_k=max(limit * 3, limit), alpha=alpha, iters=iters
    )
    if not ranked_keywords:
        # No graph signal: fall back to documents that directly contain the
        # seed keywords so the caller still gets something useful.
        direct = _docs_for_keywords(conn, seeds, scope_key)
        results = [
            Related(doc_path=doc, score=score, mode="graph", reason="direct-seed", keywords=list(seeds))
            for doc, score in direct.items()
        ]
        results.sort(key=lambda r: (-r.score, r.doc_path))
        return results[:limit]

    kw_score = {kw: score for kw, score in ranked_keywords}
    # Include the seeds so their own documents are represented.
    for seed in seeds:
        kw_score.setdefault(seed, max(kw_score.values()) if kw_score else 1.0)

    keyword_list = list(kw_score.keys())
    placeholders = ",".join("?" for _ in keyword_list)
    params: List = list(keyword_list)
    where = f"LOWER(keyword) IN ({placeholders})"
    if scope_key is not None:
        where += " AND directory = ?"
        params.append(scope_key)
    rows = conn.execute(
        f"SELECT keyword, doc_path, occurrence_count FROM keyword_docs WHERE {where}",
        params,
    ).fetchall()

    doc_score: Dict[str, float] = {}
    doc_keywords: Dict[str, set] = {}
    for row in rows:
        kw = _fold(row["keyword"])
        weight = kw_score.get(kw, 0.0)
        if weight <= 0:
            continue
        contribution = weight * float(row["occurrence_count"])
        doc_score[row["doc_path"]] = doc_score.get(row["doc_path"], 0.0) + contribution
        doc_keywords.setdefault(row["doc_path"], set()).add(kw)

    results = [
        Related(
            doc_path=doc,
            score=score,
            mode="graph",
            reason="ppr",
            keywords=sorted(doc_keywords.get(doc, set())),
        )
        for doc, score in doc_score.items()
    ]
    results.sort(key=lambda r: (-r.score, r.doc_path))
    return results[:limit]


def _related_dir(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    scope_key: Optional[str],
) -> List[Related]:
    seed_docs = _docs_for_keywords(conn, seeds, scope_key)
    if not seed_docs:
        return []
    # Directory of each seed doc -> aggregated seed relevance.
    dir_weight: Dict[str, float] = {}
    for doc, weight in seed_docs.items():
        parent = str(Path(doc).parent)
        dir_weight[parent] = dir_weight.get(parent, 0.0) + weight
    if not dir_weight:
        return []

    where = ""
    params: List = []
    if scope_key is not None:
        where = " WHERE directory = ?"
        params.append(scope_key)
    rows = conn.execute(
        f"SELECT DISTINCT doc_path FROM keyword_docs{where}",
        params,
    ).fetchall()

    results: List[Related] = []
    for row in rows:
        doc = row["doc_path"]
        parent = str(Path(doc).parent)
        weight = dir_weight.get(parent)
        if weight is None:
            continue
        if doc in seed_docs:
            continue  # skip the seed docs themselves
        results.append(
            Related(doc_path=doc, score=weight, mode="dir", reason=f"dir:{parent}", keywords=list(seeds))
        )
    results.sort(key=lambda r: (-r.score, r.doc_path))
    return results[:limit]


def _related_heading(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    limit: int,
    scope_key: Optional[str],
) -> List[Related]:
    seed_docs = _docs_for_keywords(conn, seeds, scope_key)
    if not seed_docs:
        return []
    seed_doc_set = set(seed_docs.keys())

    # Heading paths present in the seed documents.
    placeholders = ",".join("?" for _ in seed_doc_set)
    params: List = list(seed_doc_set)
    where = f"doc_path IN ({placeholders})"
    if scope_key is not None:
        where += " AND directory = ?"
        params.append(scope_key)
    rows = conn.execute(
        f"SELECT DISTINCT heading_path FROM heading_docs WHERE {where}",
        params,
    ).fetchall()
    if not rows:
        return []

    # Match on both the full serialised path (exact chapter reuse) and its
    # leaf heading (cross-document section reuse, e.g. two files each having
    # a "macOS" subsection under different parents). We assign the leaf
    # signal a lower weight so exact chapter matches float to the top.
    seed_full_paths: List[str] = []
    seed_leaves: List[str] = []
    for row in rows:
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
        where_parts.append(f"heading_path IN ({ph})")
        params.extend(seed_full_paths)
    if seed_leaves:
        # Match on the leaf using GLOB (case-sensitive) plus an OR path for
        # the trivial single-segment case.
        ph = ",".join("?" for _ in seed_leaves)
        leaf_clause = (
            "heading_path IN (" + ph + ") OR "
            + " OR ".join(["heading_path LIKE ?" for _ in seed_leaves])
        )
        where_parts.append("(" + leaf_clause + ")")
        params.extend(seed_leaves)
        params.extend([f"% > {leaf}" for leaf in seed_leaves])
    combined = " OR ".join(f"({part})" for part in where_parts)
    if scope_key is not None:
        combined = f"({combined}) AND directory = ?"
        params.append(scope_key)
    rows = conn.execute(
        f"SELECT heading_path, doc_path FROM heading_docs WHERE {combined}",
        params,
    ).fetchall()

    full_set = set(seed_full_paths)
    leaf_set = set(seed_leaves)
    doc_score: Dict[str, float] = {}
    doc_headings: Dict[str, set] = {}
    for row in rows:
        doc = row["doc_path"]
        if doc in seed_doc_set:
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
        doc_score[doc] = doc_score.get(doc, 0.0) + weight
        doc_headings.setdefault(doc, set()).add(heading_path)

    results = [
        Related(
            doc_path=doc,
            score=score,
            mode="heading",
            reason="shared-heading",
            keywords=sorted(doc_headings.get(doc, set())),
        )
        for doc, score in doc_score.items()
    ]
    results.sort(key=lambda r: (-r.score, r.doc_path))
    return results[:limit]
