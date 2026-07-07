"""Graph schema management (Phase 4.1).

``ensure_schema(conn)`` creates the v2 tables (``keyword_docs``,
``heading_docs``, ``llm_extension_votes``) without ever touching the v1
tables that ``grga.deep_search.KeywordLibrary`` already manages
(``keyword_counts``, ``keyword_pairs``, ``keyword_sources``). Upgrading an
old database is safe: this module only ever runs ``CREATE TABLE IF NOT
EXISTS`` and ``CREATE INDEX IF NOT EXISTS`` statements plus a lightweight
``PRAGMA user_version`` bump, so no v1 rows are moved or destroyed.

The schema version is stored on the SQLite ``user_version`` pragma so we
can layer future migrations without disturbing the v1 counterparts. See
``docs/design/v2-design.md`` §6.2 for the table shapes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

SCHEMA_VERSION = 2

# Preserved v1 tables. Never dropped, altered, or renamed.
V1_TABLES = ("keyword_counts", "keyword_pairs", "keyword_sources")

# v2 tables owned by this module.
V2_TABLES = ("keyword_docs", "heading_docs", "llm_extension_votes")


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


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Idempotently create v2 tables and stamp the schema version.

    Safe to call on:

    * a brand-new empty database (creates every v2 table);
    * a v1-only database (only the v2 tables are added, v1 tables are
      untouched);
    * a database that already reflects ``SCHEMA_VERSION`` (no-op fast path).

    Returns the final ``user_version`` after the call.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS keyword_docs (
            scope TEXT NOT NULL DEFAULT '',
            directory TEXT NOT NULL,
            keyword TEXT NOT NULL,
            doc_path TEXT NOT NULL,
            occurrence_count REAL NOT NULL DEFAULT 0,
            first_line INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (directory, keyword, doc_path)
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
        CREATE INDEX IF NOT EXISTS idx_keyword_docs_kw
            ON keyword_docs (directory, keyword, occurrence_count DESC);
        CREATE INDEX IF NOT EXISTS idx_keyword_docs_doc
            ON keyword_docs (directory, doc_path);
        CREATE INDEX IF NOT EXISTS idx_heading_docs_head
            ON heading_docs (directory, heading_path);
        CREATE INDEX IF NOT EXISTS idx_heading_docs_doc
            ON heading_docs (directory, doc_path);
        CREATE INDEX IF NOT EXISTS idx_votes_dir
            ON llm_extension_votes (directory, vote_count DESC);
        """
    )
    current = read_schema_version(conn)
    if current < SCHEMA_VERSION:
        # ``?`` placeholders are not accepted for PRAGMA statements, but the
        # value is a hard-coded module constant so string formatting is safe.
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
    conn.commit()
    return read_schema_version(conn)


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return every user table name in the database (debug helper)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]
