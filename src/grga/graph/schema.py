"""Graph schema management (chunk-level migration, schema v4).

``ensure_schema(conn)`` creates the current graph tables and, when it finds
an older database, performs a **one-shot** migration to the chunk-level
co-occurrence model (``docs/design/plan-2026-07-11-chunk-migration.md``).

Current (v4) graph tables owned by this module:

* ``chunks``          — chunk metadata (line ranges, char counts) per doc.
* ``keyword_chunks``  — keyword → chunk inverted index (segment df原料).
* ``keyword_pairs``   — multi-source pair table: ``source='chunk_cooccur'``
  rows carry segment-level ``cooccur_count`` + ``npmi``;
  ``source='llm_promoted'`` rows come from ``grga index promote``.
* ``heading_docs``    — Markdown chapter structure (unchanged).
* ``llm_extension_votes`` — LLM pair vote buffer (unchanged).

The v1 counters ``keyword_counts`` / ``keyword_sources`` are preserved and
never touched by this module.

Migration (v3 → v4), one atomic transaction:

* ``keyword_docs`` is **dropped** (doc-level graph proven structurally
  insufficient; see the plan §10).
* ``keyword_pairs`` is rebuilt with a ``source`` column in its primary key.
  Legacy rows written by the old promotion path (old ``scope='llm_promoted'``)
  are salvaged as ``source='llm_promoted'``; every other legacy row (v1
  deep-search / doc-level derived) is dropped — segment build recomputes it.
* ``chunks`` / ``keyword_chunks`` are created.

The migration is idempotent: an already-v4 database is a no-op fast path.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional, Union

SCHEMA_VERSION = 4

# Source markers for ``keyword_pairs`` rows. These are the *only* two
# allowed values (plan HC4 / §3.3).
CHUNK_COOCCUR_SOURCE = "chunk_cooccur"
LLM_PROMOTED_SOURCE = "llm_promoted"

# Preserved v1 counter tables. Never dropped, altered, or renamed.
V1_TABLES = ("keyword_counts", "keyword_sources")

# Graph tables owned by this module (post-migration shape).
GRAPH_TABLES = ("chunks", "keyword_chunks", "keyword_pairs", "heading_docs",
                "llm_extension_votes")


def resolve_scope_key(directory: Optional[Union[str, Path]]) -> Optional[str]:
    """Canonicalise a directory scope for the graph tables.

    Single source of truth shared by :mod:`grga.graph.votes` and
    :mod:`grga.graph.promote` so the "unscoped" concept never drifts:

    * ``None`` -> ``None`` meaning *no scope filter* (match every scope).
    * ``""`` (or whitespace) -> ``""`` meaning the *unscoped* rows, i.e.
      votes recorded without a directory. This is a real, filterable key.
    * a real path -> its resolved absolute string.

    Callers store rows using the ``""``-vs-path distinction and filter with
    ``scope_key is None`` (no ``WHERE``) vs ``scope_key == ""`` / a path
    (``WHERE directory = ?``). Because ``""`` round-trips instead of
    collapsing to ``None``, "promote only unscoped votes" is expressible.
    """
    if directory is None:
        return None
    text = str(directory).strip()
    if not text:
        return ""
    return str(Path(text).expanduser().resolve())


def scope_write_key(directory: Optional[Union[str, Path]]) -> str:
    """Return the value to *store* in a ``directory`` column.

    Writers always need a concrete string, so ``None`` and ``""`` both map
    to the unscoped sentinel ``""`` (whereas :func:`resolve_scope_key`
    keeps ``None`` distinct for read-time filtering).
    """
    key = resolve_scope_key(directory)
    return "" if key is None else key


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """Open ``path`` and enable ``sqlite3.Row`` factory.

    The parent directory is created on demand (matching v1
    ``KeywordLibrary`` semantics). The caller owns the connection and is
    responsible for closing it.
    """
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    return conn


def read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current ``user_version`` pragma value."""
    cur = conn.execute("PRAGMA user_version")
    row = cur.fetchone()
    if row is None:
        return 0
    return int(row[0])


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({name})")}


_CREATE_KEYWORD_PAIRS = """
CREATE TABLE IF NOT EXISTS keyword_pairs (
    scope TEXT NOT NULL DEFAULT '',
    directory TEXT NOT NULL,
    keyword_a TEXT NOT NULL,
    keyword_b TEXT NOT NULL,
    source TEXT NOT NULL,
    cooccur_count REAL NOT NULL DEFAULT 0,
    npmi REAL,
    updated_at TEXT,
    PRIMARY KEY (directory, keyword_a, keyword_b, source)
);
"""

_CREATE_GRAPH_TABLES = """
CREATE TABLE IF NOT EXISTS chunks (
    scope TEXT NOT NULL DEFAULT '',
    directory TEXT NOT NULL,
    doc_path TEXT NOT NULL,
    chunk_id INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    PRIMARY KEY (directory, doc_path, chunk_id)
);
CREATE TABLE IF NOT EXISTS keyword_chunks (
    directory TEXT NOT NULL,
    keyword TEXT NOT NULL,
    doc_path TEXT NOT NULL,
    chunk_id INTEGER NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (directory, keyword, doc_path, chunk_id)
);
CREATE TABLE IF NOT EXISTS heading_docs (
    directory TEXT NOT NULL,
    heading_path TEXT NOT NULL,
    doc_path TEXT NOT NULL,
    chapter_id TEXT NOT NULL DEFAULT '',
    start_line INTEGER NOT NULL DEFAULT 0,
    end_line INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (directory, heading_path, doc_path, chapter_id)
);
CREATE TABLE IF NOT EXISTS llm_extension_votes (
    directory TEXT NOT NULL,
    keyword_a TEXT NOT NULL,
    keyword_b TEXT NOT NULL,
    vote_count REAL NOT NULL DEFAULT 0,
    last_voted TEXT,
    PRIMARY KEY (directory, keyword_a, keyword_b)
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_keyword_chunks_kw
    ON keyword_chunks (directory, keyword);
CREATE INDEX IF NOT EXISTS idx_keyword_chunks_doc
    ON keyword_chunks (directory, doc_path, chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc
    ON chunks (directory, doc_path);
CREATE INDEX IF NOT EXISTS idx_keyword_pairs_a
    ON keyword_pairs (directory, keyword_a);
CREATE INDEX IF NOT EXISTS idx_keyword_pairs_b
    ON keyword_pairs (directory, keyword_b);
CREATE INDEX IF NOT EXISTS idx_heading_docs_head
    ON heading_docs (directory, heading_path);
CREATE INDEX IF NOT EXISTS idx_heading_docs_doc
    ON heading_docs (directory, doc_path);
CREATE INDEX IF NOT EXISTS idx_votes_dir
    ON llm_extension_votes (directory, vote_count DESC);
"""


def _split_sql(script: str) -> List[str]:
    """Split a ``;``-separated SQL script into individual statements.

    ``sqlite3.Connection.executescript`` issues an implicit ``COMMIT``
    before running the script, so it cannot be used inside our migration
    transaction. This helper feeds ``conn.execute`` statement by
    statement, preserving the outer BEGIN / COMMIT.
    """
    out: List[str] = []
    for chunk in script.split(";"):
        stmt = chunk.strip()
        if stmt:
            out.append(stmt)
    return out


def _create_current_tables(conn: sqlite3.Connection) -> None:
    """Create every current graph table + index (idempotent).

    Executes individual statements so the caller's transaction is
    preserved (:func:`sqlite3.Connection.executescript` would COMMIT).
    """
    for stmt in _split_sql(
        _CREATE_KEYWORD_PAIRS + _CREATE_GRAPH_TABLES + _CREATE_INDEXES
    ):
        conn.execute(stmt)


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Idempotently bring ``conn`` to the current chunk-level schema (v4).

    Safe to call on:

    * a brand-new empty database (creates every current table);
    * a v1-only database (``keyword_counts`` etc. preserved, graph tables
      added, legacy ``keyword_pairs`` restructured);
    * a v2/v3 database (``keyword_docs`` dropped, ``keyword_pairs``
      restructured, ``chunks`` / ``keyword_chunks`` created);
    * a database already at ``SCHEMA_VERSION`` (no-op fast path).

    Does **not** commit — the caller's transaction (implicit or explicit)
    is honoured throughout. This lets ``KeywordLibrary.add_keywords`` /
    ``promote_votes`` / any other multi-statement writer batch schema-setup
    with their own INSERTs atomically. Migration (v3→v4) *does* need its
    own atomic scope: :func:`_migrate_to_v4` opens a nested transaction
    only when the caller has none in progress, otherwise it participates
    in the caller's transaction (Python 3.12 sqlite3 forbids nesting
    ``BEGIN`` and would raise ``cannot start a transaction within a
    transaction`` if we opened one unconditionally).

    Returns the final ``user_version`` after the call.
    """
    current = read_schema_version(conn)
    if current >= SCHEMA_VERSION:
        # Fast path: DB is at the current schema. Assume migration created
        # every table when it stamped user_version = SCHEMA_VERSION, so we
        # avoid the 13 sqlite_master probes ``_create_current_tables``
        # would otherwise perform on every call.
        return current

    _migrate_to_v4(conn)
    conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
    return SCHEMA_VERSION


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """Bring ``conn`` from any pre-v4 shape to v4.

    Atomicity: when the caller has no in-flight transaction we open one
    (``BEGIN IMMEDIATE``) so the DROP / CREATE / salvage sequence is one
    all-or-nothing unit. When the caller *does* have an in-flight
    transaction we cannot open a nested one (Python's sqlite3 rejects
    it), so we participate in the caller's transaction; the caller then
    controls the commit/rollback boundary.

    Salvages old promotion rows (legacy ``scope='llm_promoted'``) as
    ``source='llm_promoted'`` and drops every other legacy ``keyword_pairs``
    row (doc-level / v1-derived — segment build recomputes them). Drops
    ``keyword_docs``. Creates the current graph tables.

    Legacy detection is re-run *inside* the transaction so a concurrent
    process that wins the migration race cannot leave us reading a stale
    "legacy pair table exists" view against the new schema.
    """
    owned_txn = not conn.in_transaction
    if owned_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check under the write lock (TOCTOU guard): another process may
        # have already migrated between the caller's read_schema_version()
        # and this point.
        if read_schema_version(conn) >= SCHEMA_VERSION:
            _create_current_tables(conn)
            if owned_txn:
                conn.execute("COMMIT")
            return

        legacy_pairs = (
            _table_exists(conn, "keyword_pairs")
            and "source" not in _table_columns(conn, "keyword_pairs")
        )
        legacy_docs = _table_exists(conn, "keyword_docs")

        salvaged = []
        if legacy_pairs:
            rows = conn.execute(
                "SELECT directory, keyword_a, keyword_b, count, updated_at "
                "FROM keyword_pairs WHERE scope = ?",
                (LLM_PROMOTED_SOURCE,),
            ).fetchall()
            salvaged = [
                (
                    row["directory"],
                    row["keyword_a"],
                    row["keyword_b"],
                    float(row["count"] or 0.0),
                    row["updated_at"] if "updated_at" in row.keys() else None,
                )
                for row in rows
            ]
            conn.execute("DROP TABLE keyword_pairs")

        if legacy_docs:
            conn.execute("DROP TABLE IF EXISTS keyword_docs")

        _create_current_tables(conn)

        if salvaged:
            conn.executemany(
                """
                INSERT OR IGNORE INTO keyword_pairs
                    (scope, directory, keyword_a, keyword_b, source,
                     cooccur_count, npmi, updated_at)
                VALUES ('', ?, ?, ?, ?, ?, NULL, ?)
                """,
                [
                    (directory, kw_a, kw_b, LLM_PROMOTED_SOURCE, count, updated)
                    for (directory, kw_a, kw_b, count, updated) in salvaged
                ],
            )

        if owned_txn:
            conn.execute("COMMIT")
    except Exception:
        if owned_txn:
            # Only attempt rollback for a transaction we ourselves opened;
            # a caller-owned txn is the caller's to roll back.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        raise


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return every user table name in the database (debug helper)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]
