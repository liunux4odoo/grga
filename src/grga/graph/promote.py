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
  ``promoted_at`` marker column on the votes table records which pairs were
  already merged, so re-running ``promote`` does not double-count.
* **Dry-run**: ``dry_run=True`` reports what *would* be promoted without
  writing anything.

The ``keyword_pairs`` rows created here carry a distinct ``scope`` marker
(``PROMOTE_SCOPE``) so operators can tell LLM-refed edges apart from the
organically-learned deep-search co-occurrences.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from grga.graph.schema import connect, ensure_schema, resolve_scope_key

DEFAULT_THRESHOLD = 3
PROMOTE_SCOPE = "llm_promoted"


@dataclass
class PromotedPair:
    """One keyword pair that crossed the promotion threshold."""

    directory: str
    keyword_a: str
    keyword_b: str
    vote_count: float
    already_promoted: bool = False

    def to_dict(self) -> dict:
        return {
            "directory": self.directory,
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


def _ensure_promoted_marker(conn: sqlite3.Connection) -> None:
    """Add the ``promoted_at`` column to ``llm_extension_votes`` if absent.

    Kept here (rather than in ``schema.ensure_schema``) so the base schema
    stays minimal; the marker is only meaningful to the promotion path.
    ``ALTER TABLE ... ADD COLUMN`` is additive and preserves existing rows.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(llm_extension_votes)")}
    if "promoted_at" not in cols:
        conn.execute("ALTER TABLE llm_extension_votes ADD COLUMN promoted_at TEXT")


def _ensure_keyword_pairs_table(conn: sqlite3.Connection) -> None:
    """Make sure ``keyword_pairs`` exists before we try to write into it.

    :func:`grga.deep_search.KeywordLibrary` creates this table lazily on
    first use, but ``grga index promote`` may run against a database that
    has only ever seen ``build_graph_index`` (so no v1 table yet). We
    replicate the KeywordLibrary DDL verbatim; because it uses ``IF NOT
    EXISTS`` it never touches an existing v1 table.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS keyword_pairs (
            scope TEXT NOT NULL,
            directory TEXT NOT NULL,
            keyword_a TEXT NOT NULL,
            keyword_b TEXT NOT NULL,
            count REAL NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (scope, directory, keyword_a, keyword_b)
        );
        CREATE INDEX IF NOT EXISTS idx_pairs_dir ON keyword_pairs (directory, count);
        """
    )


def promote_votes(
    *,
    threshold: float = DEFAULT_THRESHOLD,
    directory: Optional[Union[str, Path]] = None,
    db_path: Union[str, Path, None] = None,
    dry_run: bool = False,
    repromote: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> PromoteStats:
    """Merge high-vote pairs from ``llm_extension_votes`` into ``keyword_pairs``.

    Args:
        threshold: minimum ``vote_count`` for a pair to be promoted.
        directory: scope filter following the shared convention
            (:func:`grga.graph.schema.resolve_scope_key`):

            * ``None`` (default) - promote across every scope in the votes
              table.
            * ``""`` (or whitespace) - promote only *unscoped* votes.
            * a path - promote only votes recorded for that resolved
              directory.

            The resulting ``keyword_pairs`` rows keep the same directory
            value as the source vote row, so per-scope semantics survive.
        db_path: SQLite path; defaults to the shared deep-search DB.
        dry_run: when ``True``, compute candidates but write nothing.
        repromote: when ``True``, promote qualifying pairs again even if they
            already carry a ``promoted_at`` marker (they still merge
            additively into ``keyword_pairs``). Default ``False`` skips them.
        conn: reuse an existing connection instead of opening one.

    Returns:
        A :class:`PromoteStats` describing candidates and what was written.
    """
    scope_key = resolve_scope_key(directory)

    def _run(c: sqlite3.Connection) -> PromoteStats:
        ensure_schema(c)
        _ensure_promoted_marker(c)
        _ensure_keyword_pairs_table(c)
        stats = PromoteStats(threshold=float(threshold), dry_run=dry_run)

        where = "vote_count >= ?"
        params: List = [float(threshold)]
        if scope_key is not None:
            where += " AND directory = ?"
            params.append(scope_key)
        rows = c.execute(
            f"""
            SELECT directory, keyword_a, keyword_b, vote_count, promoted_at
            FROM llm_extension_votes
            WHERE {where}
            ORDER BY vote_count DESC, keyword_a, keyword_b
            """,
            params,
        ).fetchall()

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for row in rows:
            stats.candidates += 1
            already = bool(row["promoted_at"])
            pair = PromotedPair(
                directory=row["directory"],
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
            # Canonical ordering already guaranteed by the votes writer, but
            # keyword_pairs stores its own (scope, directory) key so we merge
            # additively there.
            c.execute(
                """
                INSERT INTO keyword_pairs
                    (scope, directory, keyword_a, keyword_b, count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, directory, keyword_a, keyword_b) DO UPDATE SET
                    count = count + excluded.count,
                    updated_at = excluded.updated_at
                """,
                (
                    PROMOTE_SCOPE,
                    row["directory"],
                    row["keyword_a"],
                    row["keyword_b"],
                    float(row["vote_count"]),
                    now,
                ),
            )
            c.execute(
                """
                UPDATE llm_extension_votes SET promoted_at = ?
                WHERE directory = ? AND keyword_a = ? AND keyword_b = ?
                """,
                (now, row["directory"], row["keyword_a"], row["keyword_b"]),
            )
            stats.promoted += 1

        if not dry_run:
            c.commit()
        return stats

    if conn is not None:
        return _run(conn)

    from grga.deep_search import DEFAULT_DB_PATH

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    owned = connect(target_db)
    try:
        return _run(owned)
    finally:
        owned.close()
