"""L3 -> L2 refeed / promotion (Phase 5.3).

``promote_votes`` is the offline task that closes the refeed loop from
``docs/design/v2-design.md`` §4.4: keyword pairs the LLM has proposed
enough times (``vote_count >= threshold``) graduate from the
``llm_extension_votes`` staging table into the v1 ``keyword_pairs`` graph,
where ``related(mode="graph")`` picks them up for free on the next query.

Anti-pollution guarantees:

* **Threshold gate**: only pairs at or above ``threshold`` votes are
  promoted (default 3, per the design's suggested value).
* **Non-destructive**: the ``llm_extension_votes`` rows are *never* deleted
  by promotion, so the vote history stays auditable and a bad refeed can be
  rolled back by editing ``keyword_pairs`` directly.
* **Idempotent**: promotion merges into ``keyword_pairs`` with an additive
  ``ON CONFLICT`` only for pairs that have not been promoted before. A
  ``promoted_at`` marker column on the votes table (present natively in
  v5) records which pairs were already merged, so re-running ``promote``
  does not double-count.
* **Dry-run**: ``dry_run=True`` reports what *would* be promoted without
  writing anything.

The ``keyword_pairs`` rows created here carry a distinct ``source`` marker
(``LLM_PROMOTED_SOURCE``) so operators can tell LLM-refed edges apart from
the organically-learned chunk co-occurrences.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from grga.graph.ids import IdInterner
from grga.graph.schema import (
    LLM_PROMOTED_SOURCE,
    connect,
    ensure_schema,
    resolve_scope_name,
)

DEFAULT_THRESHOLD = 3
# Public re-export so external callers keep the old import surface working.
PROMOTE_SCOPE = LLM_PROMOTED_SOURCE


@dataclass
class PromotedPair:
    """One keyword pair that crossed the promotion threshold."""

    scope: str
    keyword_a: str
    keyword_b: str
    vote_count: float
    already_promoted: bool = False

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "keyword_a": self.keyword_a,
            "keyword_b": self.keyword_b,
            "vote_count": round(self.vote_count, 4),
            "already_promoted": self.already_promoted,
        }


@dataclass
class PromoteStats:
    """Summary produced by :func:`promote_votes`."""

    threshold: float
    dry_run: bool
    candidates: int = 0
    promoted: int = 0
    skipped_already_promoted: int = 0
    pairs: List[PromotedPair] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "dry_run": self.dry_run,
            "candidates": self.candidates,
            "promoted": self.promoted,
            "skipped_already_promoted": self.skipped_already_promoted,
            "pairs": [p.to_dict() for p in self.pairs],
        }


def _run_promote(
    conn: sqlite3.Connection,
    *,
    threshold: float,
    scope_name: Optional[str],
    dry_run: bool,
    repromote: bool,
) -> "PromoteStats":
    """Core promotion body. Extracted so ``promote_votes`` reads cleanly."""
    ensure_schema(conn)
    stats = PromoteStats(threshold=float(threshold), dry_run=dry_run)

    where = "v.vote_count >= ?"
    params: List = [float(threshold)]
    if scope_name is not None:
        # Resolve the scope to an id up front; if the scope has never
        # been seen there are no candidate votes.
        scope_id = IdInterner(conn).scope_id_ro(scope_name)
        if scope_id is None:
            return stats
        where += " AND v.scope_id = ?"
        params.append(int(scope_id))
    rows = conn.execute(
        f"""
        SELECT v.scope_id AS scope_id,
               v.kw_a_id AS kw_a_id,
               v.kw_b_id AS kw_b_id,
               s.name AS scope,
               ka.kw AS keyword_a,
               kb.kw AS keyword_b,
               v.vote_count AS vote_count,
               v.promoted_at AS promoted_at
        FROM llm_extension_votes v
        JOIN scopes s ON s.id = v.scope_id
        JOIN keywords ka ON ka.id = v.kw_a_id
        JOIN keywords kb ON kb.id = v.kw_b_id
        WHERE {where}
        ORDER BY v.vote_count DESC, ka.kw, kb.kw
        """,
        params,
    ).fetchall()

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for row in rows:
        stats.candidates += 1
        already = bool(row["promoted_at"])
        pair = PromotedPair(
            scope=row["scope"],
            keyword_a=row["keyword_a"],
            keyword_b=row["keyword_b"],
            vote_count=float(row["vote_count"]),
            already_promoted=already,
        )
        if already and not repromote:
            stats.skipped_already_promoted += 1
            stats.pairs.append(pair)
            continue
        stats.pairs.append(pair)
        if dry_run:
            stats.promoted += 1
            continue
        # v10 keyword_pairs: PK is (scope_id, kw_a_id, kw_b_id, source).
        # We write source='llm_promoted' with a NULL npmi (chunk-level
        # build owns the npmi channel; vote-derived pairs have no npmi
        # to report). ``cooccur_count`` reuses the vote count as a
        # "how strong is the LLM signal" number.
        conn.execute(
            """
            INSERT INTO keyword_pairs
                (scope_id, kw_a_id, kw_b_id, source,
                 cooccur_count, npmi, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(scope_id, kw_a_id, kw_b_id, source) DO UPDATE SET
                cooccur_count = cooccur_count + excluded.cooccur_count,
                updated_at = excluded.updated_at
            """,
            (
                row["scope_id"],
                row["kw_a_id"],
                row["kw_b_id"],
                LLM_PROMOTED_SOURCE,
                float(row["vote_count"]),
                now,
            ),
        )
        conn.execute(
            """
            UPDATE llm_extension_votes SET promoted_at = ?
            WHERE scope_id = ? AND kw_a_id = ? AND kw_b_id = ?
            """,
            (now, row["scope_id"], row["kw_a_id"], row["kw_b_id"]),
        )
        stats.promoted += 1

    if not dry_run:
        conn.commit()
    return stats


def promote_votes(
    *,
    threshold: float = DEFAULT_THRESHOLD,
    scope: Optional[str] = None,
    db_path: Union[str, Path, None] = None,
    dry_run: bool = False,
    repromote: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> PromoteStats:
    """Merge high-vote pairs from ``llm_extension_votes`` into ``keyword_pairs``.

    Args:
        threshold: minimum ``vote_count`` for a pair to be promoted.
        scope: logical scope name (v10). Follows the shared convention:

            * ``None`` (default) - promote across every scope in the
              votes table.
            * a name - promote only votes recorded for that scope.

            The resulting ``keyword_pairs`` rows keep the same scope as
            the source vote row, so per-scope semantics survive.
        db_path: SQLite path; defaults to the shared deep-search DB.
        dry_run: when ``True``, compute candidates but write nothing.
        repromote: when ``True``, promote qualifying pairs again even if
            they already carry a ``promoted_at`` marker (they still merge
            additively into ``keyword_pairs``). Default ``False`` skips
            them.
        conn: reuse an existing connection instead of opening one.

    Returns:
        A :class:`PromoteStats` describing candidates and what was written.
    """
    scope_name = resolve_scope_name(scope)
    if conn is not None:
        return _run_promote(
            conn, threshold=threshold, scope_name=scope_name,
            dry_run=dry_run, repromote=repromote,
        )

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        return _run_promote(
            owned, threshold=threshold, scope_name=scope_name,
            dry_run=dry_run, repromote=repromote,
        )
    finally:
        owned.close()
