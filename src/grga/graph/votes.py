"""L3 -> L2 feedback voting (v10 scope-aware).

The ``suggest()`` API (L3, LLM-driven) never writes directly into the
co-occurrence graph. Instead every keyword pair the LLM proposes is
*recorded as a vote* in the ``llm_extension_votes`` table (see
:mod:`grga.graph.schema`). Only after a pair accumulates enough votes
does ``grga index promote`` (see :mod:`grga.graph.promote`) merge it
into ``keyword_pairs``. This is the anti-pollution gate described in
``docs/design/v2-design.md`` §4.4: a single hallucinated pair can never
poison the graph on its own.

v10 scope semantics (plan-2026-07-14): a vote is **knowledge**, so it
must belong to a named logical scope. There is no "unscoped" sentinel
anymore. Callers pass ``scope`` as a logical name (not a filesystem
path):

* ``scope=None`` -> recording is **silently skipped** (the caller had
  no scope to attribute the vote to; knowledge without a home is not
  written). Read paths treat ``None`` as "no scope filter".
* ``scope="physics-cn"`` -> the vote is recorded under that scope,
  matching the scope a corpus was ``grga index build``-ed under.

Design constraints:

* **Additive**: this module only ever touches ``llm_extension_votes``.
* **Idempotent + deduplicated**: pairs are canonicalised (folded +
  sorted) so ``(A, B)`` and ``(B, A)`` share a row, and self-pairs are
  dropped. Repeated recording accumulates ``vote_count``.
* **Raw records are never cleared by promotion** so the vote history
  stays auditable and the refeed is reversible.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from grga.graph.ids import IdInterner
from grga.graph.schema import connect, ensure_schema, resolve_scope_name


def _fold(term: str) -> str:
    return str(term).casefold().strip()


def _canonical_pair(a: str, b: str) -> Optional[Tuple[str, str]]:
    """Return a folded, order-normalised ``(kw_a, kw_b)`` or ``None``.

    ``None`` is returned when either side is empty or the two sides fold
    to the same token (a self-pair carries no co-occurrence signal).
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
    scope_id: int,
    weight: float,
) -> VoteResult:
    result = VoteResult()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    seen: set = set()
    interner = IdInterner(conn)
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
        kw_a_id = interner.kw_id(kw_a)
        kw_b_id = interner.kw_id(kw_b)
        conn.execute(
            """
            INSERT INTO llm_extension_votes
                (scope_id, kw_a_id, kw_b_id, vote_count, last_voted)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope_id, kw_a_id, kw_b_id) DO UPDATE SET
                vote_count = vote_count + excluded.vote_count,
                last_voted = excluded.last_voted
            """,
            (scope_id, kw_a_id, kw_b_id, float(weight), now),
        )
        result.pairs_recorded += 1
        result.votes_added += float(weight)
    return result


def record_vote(
    keyword_a: str,
    keyword_b: str,
    *,
    scope: Optional[str] = None,
    weight: float = 1.0,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> VoteResult:
    """Record a single L3 keyword-pair vote.

    Args:
        keyword_a / keyword_b: the two keywords the LLM linked.
        scope: logical scope name to attribute the vote to. ``None``
            skips recording (no scope to attribute knowledge to).
        weight: increment applied to ``vote_count`` (default ``1.0``).
        db_path: SQLite path; defaults to the shared deep-search DB.
        conn: an existing connection to reuse (schema is still ensured).
            The caller owns and commits/closes it. When omitted, a
            connection is opened, committed, and closed internally.

    Returns:
        A :class:`VoteResult`. Self-pairs and empty tokens are skipped
        and reflected in ``pairs_seen`` vs ``pairs_recorded``. A
        ``scope=None`` call returns an all-zero result.
    """
    return record_votes(
        [(keyword_a, keyword_b)],
        scope=scope,
        weight=weight,
        db_path=db_path,
        conn=conn,
    )


def record_votes(
    pairs: Sequence[Tuple[str, str]],
    *,
    scope: Optional[str] = None,
    weight: float = 1.0,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> VoteResult:
    """Record many keyword-pair votes in one transaction.

    See :func:`record_vote` for argument semantics. Duplicate pairs
    within ``pairs`` collapse to a single vote for this call. A
    ``scope=None`` call is a no-op (returns an empty result) — knowledge
    is never written without a named scope.
    """
    scope_name = resolve_scope_name(scope)
    if scope_name is None:
        return VoteResult()

    def _do(c: sqlite3.Connection) -> VoteResult:
        ensure_schema(c)
        scope_id = IdInterner(c).scope_id(scope_name)
        return _record_pairs(c, pairs, scope_id=scope_id, weight=weight)

    if conn is not None:
        result = _do(conn)
        conn.commit()
        return result

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        result = _do(owned)
        owned.commit()
        return result
    finally:
        owned.close()


def record_votes_from_keywords(
    keywords: Sequence[str],
    *,
    scope: Optional[str] = None,
    weight: float = 1.0,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> VoteResult:
    """Vote for every unordered pair among ``keywords``.

    A convenience wrapper used by :func:`grga.suggest.suggest`: when the
    LLM proposes a *set* of related keywords for a query, each co-mention
    is a weak "these belong together" signal, so we vote for the full
    clique. ``scope=None`` is a no-op.
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
        scope=scope,
        weight=weight,
        db_path=db_path,
        conn=conn,
    )


def get_votes(
    *,
    scope: Optional[str] = None,
    min_votes: float = 0.0,
    db_path: Union[str, Path, None] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[dict]:
    """Return vote rows (debug / promotion helper), sorted by vote_count desc.

    ``scope`` follows the v10 convention:

    * ``None`` (default) - no scope filter, all rows.
    * a logical name - restrict to that scope.
    """
    scope_name = resolve_scope_name(scope)

    def _query(c: sqlite3.Connection) -> List[dict]:
        ensure_schema(c)
        where = "v.vote_count >= ?"
        params: List = [float(min_votes)]
        if scope_name is not None:
            scope_id = IdInterner(c).scope_id_ro(scope_name)
            if scope_id is None:
                # Unknown scope can never have votes — short-circuit.
                return []
            where += " AND v.scope_id = ?"
            params.append(int(scope_id))
        rows = c.execute(
            f"""
            SELECT s.name AS scope,
                   ka.kw AS keyword_a,
                   kb.kw AS keyword_b,
                   v.vote_count AS vote_count,
                   v.last_voted AS last_voted
            FROM llm_extension_votes v
            JOIN scopes s ON s.id = v.scope_id
            JOIN keywords ka ON ka.id = v.kw_a_id
            JOIN keywords kb ON kb.id = v.kw_b_id
            WHERE {where}
            ORDER BY v.vote_count DESC, ka.kw, kb.kw
            """,
            params,
        ).fetchall()
        return [
            {
                "scope": row["scope"],
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
