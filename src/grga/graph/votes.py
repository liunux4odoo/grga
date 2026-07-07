"""L3 -> L2 feedback voting (Phase 5.2).

The ``suggest()`` API (L3, LLM-driven) never writes directly into the
co-occurrence graph. Instead every keyword pair the LLM proposes is
*recorded as a vote* in the ``llm_extension_votes`` table (see
:mod:`grga.graph.schema`). Only after a pair accumulates enough votes does
``grga index promote`` (Phase 5.3) merge it into the v1 ``keyword_pairs``
graph. This is the anti-pollution gate described in
``docs/design/v2-design.md`` §4.4: a single hallucinated pair can never
poison the graph on its own.

Design constraints:

* **Additive**: this module only ever touches ``llm_extension_votes``. The
  v1 tables (``keyword_pairs`` / ``keyword_counts`` / ``keyword_sources``)
  are left untouched here; promotion happens in a separate offline step.
* **Idempotent + deduplicated**: pairs are canonicalised (folded + sorted)
  so ``(A, B)`` and ``(B, A)`` share a single row, and self-pairs are
  dropped. Repeated recording accumulates ``vote_count``.
* **Raw records are never cleared by promotion** so the vote history stays
  auditable and the refeed is reversible.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from grga.graph.schema import connect, ensure_schema, resolve_scope_key, scope_write_key


def _fold(term: str) -> str:
    return str(term).casefold().strip()


def _canonical_pair(a: str, b: str) -> Optional[Tuple[str, str]]:
    """Return a folded, order-normalised ``(kw_a, kw_b)`` or ``None``.

    ``None`` is returned when either side is empty or the two sides fold to
    the same token (a self-pair carries no co-occurrence signal).
    """
    fa, fb = _fold(a), _fold(b)
    if not fa or not fb or fa == fb:
        return None
    return (fa, fb) if fa < fb else (fb, fa)


@dataclass
class VoteResult:
    """Summary of a vote-recording call."""

    pairs_seen: int = 0
    pairs_recorded: int = 0
    votes_added: float = 0.0

    def to_dict(self) -> dict:
        return {
            "pairs_seen": self.pairs_seen,
            "pairs_recorded": self.pairs_recorded,
            "votes_added": round(self.votes_added, 4),
        }


def _record_pairs(
    conn: sqlite3.Connection,
    pairs: Iterable[Tuple[str, str]],
    *,
    directory: str,
    weight: float,
) -> VoteResult:
    result = VoteResult()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    seen: set = set()
    for raw_a, raw_b in pairs:
        result.pairs_seen += 1
        canonical = _canonical_pair(raw_a, raw_b)
        if canonical is None:
            continue
        if canonical in seen:
            # De-duplicate within a single call so one suggestion emitting
            # the same pair twice still counts as one vote.
            continue
        seen.add(canonical)
        kw_a, kw_b = canonical
        conn.execute(
            """
            INSERT INTO llm_extension_votes
                (directory, keyword_a, keyword_b, vote_count, last_voted)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(directory, keyword_a, keyword_b) DO UPDATE SET
                vote_count = vote_count + excluded.vote_count,
                last_voted = excluded.last_voted
            """,
            (directory, kw_a, kw_b, float(weight), now),
        )
        result.pairs_recorded += 1
        result.votes_added += float(weight)
    return result


def record_vote(
    keyword_a: str,
    keyword_b: str,
    *,
    directory: Optional[Union[str, Path]] = None,
    weight: float = 1.0,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> VoteResult:
    """Record a single L3 keyword-pair vote.

    Args:
        keyword_a / keyword_b: the two keywords the LLM linked.
        directory: scope key (usually the searched directory). ``None`` or
            ``""`` both record the vote as *unscoped* (stored as ``""``),
            matching :func:`grga.graph.schema.scope_write_key`.
        weight: increment applied to ``vote_count`` (default ``1.0``).
        db_path: SQLite path; defaults to the shared deep-search DB.
        conn: an existing connection to reuse (schema is still ensured). The
            caller owns and commits/closes it. When omitted, a connection is
            opened, committed, and closed internally.

    Returns:
        A :class:`VoteResult`. Self-pairs and empty tokens are skipped and
        reflected in ``pairs_seen`` vs ``pairs_recorded``.
    """
    return record_votes(
        [(keyword_a, keyword_b)],
        directory=directory,
        weight=weight,
        db_path=db_path,
        conn=conn,
    )


def record_votes(
    pairs: Sequence[Tuple[str, str]],
    *,
    directory: Optional[Union[str, Path]] = None,
    weight: float = 1.0,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> VoteResult:
    """Record many keyword-pair votes in one transaction.

    See :func:`record_vote` for argument semantics. Duplicate pairs within
    ``pairs`` collapse to a single vote for this call.
    """
    scope_key = scope_write_key(directory)
    if conn is not None:
        ensure_schema(conn)
        result = _record_pairs(conn, pairs, directory=scope_key, weight=weight)
        conn.commit()
        return result

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        ensure_schema(owned)
        result = _record_pairs(owned, pairs, directory=scope_key, weight=weight)
        owned.commit()
        return result
    finally:
        owned.close()


def record_votes_from_keywords(
    keywords: Sequence[str],
    *,
    directory: Optional[Union[str, Path]] = None,
    weight: float = 1.0,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> VoteResult:
    """Vote for every unordered pair among ``keywords``.

    A convenience wrapper used by :func:`grga.suggest.suggest`: when the LLM
    proposes a *set* of related keywords for a query, each co-mention is a
    weak "these belong together" signal, so we vote for the full clique.
    """
    folded: List[str] = []
    seen: set = set()
    for kw in keywords:
        fk = _fold(kw)
        if not fk or fk in seen:
            continue
        seen.add(fk)
        folded.append(fk)
    if len(folded) < 2:
        return VoteResult()
    return record_votes(
        list(combinations(folded, 2)),
        directory=directory,
        weight=weight,
        db_path=db_path,
        conn=conn,
    )


def get_votes(
    *,
    directory: Optional[Union[str, Path]] = None,
    min_votes: float = 0.0,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[dict]:
    """Return vote rows (debug / promotion helper), sorted by vote_count desc.

    ``directory`` follows the shared convention:

    * ``None`` (default) - no scope filter, all rows.
    * ``""`` (or whitespace) - restrict to *unscoped* votes only.
    * a path - restrict to that resolved directory.
    """
    scope_key = resolve_scope_key(directory)

    def _query(c: sqlite3.Connection) -> List[dict]:
        ensure_schema(c)
        where = "vote_count >= ?"
        params: List = [float(min_votes)]
        if scope_key is not None:
            where += " AND directory = ?"
            params.append(scope_key)
        rows = c.execute(
            f"""
            SELECT directory, keyword_a, keyword_b, vote_count, last_voted
            FROM llm_extension_votes
            WHERE {where}
            ORDER BY vote_count DESC, keyword_a, keyword_b
            """,
            params,
        ).fetchall()
        return [
            {
                "directory": row["directory"],
                "keyword_a": row["keyword_a"],
                "keyword_b": row["keyword_b"],
                "vote_count": float(row["vote_count"]),
                "last_voted": row["last_voted"],
            }
            for row in rows
        ]

    if conn is not None:
        return _query(conn)

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        return _query(owned)
    finally:
        owned.close()
