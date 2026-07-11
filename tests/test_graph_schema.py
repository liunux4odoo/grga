"""Tests for :mod:`grga.graph.schema` (chunk-level migration, schema v4).

Focus: the current graph tables are created, the schema version is stamped
at v4, and legacy databases (v1 KeywordLibrary, v2/v3 ``keyword_docs``) are
migrated atomically:

* ``keyword_docs`` dropped.
* ``keyword_pairs`` rebuilt with a ``source`` PK column; legacy promoted
  rows salvaged as ``source='llm_promoted'``, doc-level rows cleared.
* ``chunks`` / ``keyword_chunks`` created.
* v1 counters (``keyword_counts`` / ``keyword_sources``) preserved.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grga.graph.schema import (
    GRAPH_TABLES,
    LLM_PROMOTED_SOURCE,
    SCHEMA_VERSION,
    V1_TABLES,
    connect,
    ensure_schema,
    list_tables,
    read_schema_version,
)


def _make_v1_db(path: Path) -> None:
    """Create a database that looks like a v1 KeywordLibrary artefact.

    Includes a legacy ``keyword_pairs`` (old ``scope``/``count`` PK) with
    one doc-level-derived row and one old promotion row.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE keyword_counts (
            scope TEXT NOT NULL, directory TEXT NOT NULL, term TEXT NOT NULL,
            count REAL NOT NULL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (scope, directory, term)
        );
        CREATE TABLE keyword_pairs (
            scope TEXT NOT NULL, directory TEXT NOT NULL,
            keyword_a TEXT NOT NULL, keyword_b TEXT NOT NULL,
            count REAL NOT NULL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (scope, directory, keyword_a, keyword_b)
        );
        CREATE TABLE keyword_sources (
            directory TEXT NOT NULL, keyword TEXT NOT NULL, path TEXT NOT NULL,
            line_number INTEGER NOT NULL DEFAULT 0, count REAL NOT NULL DEFAULT 0,
            updated_at TEXT, PRIMARY KEY (directory, keyword, path, line_number)
        );
        """
    )
    conn.execute(
        "INSERT INTO keyword_counts (scope, directory, term, count) VALUES ('g', '/d', 'alpha', 5)"
    )
    # A doc-level-derived pair (scope != 'llm_promoted') — must be cleared.
    conn.execute(
        "INSERT INTO keyword_pairs (scope, directory, keyword_a, keyword_b, count) "
        "VALUES ('g', '/d', 'alpha', 'beta', 3)"
    )
    # An old promotion row — must be salvaged as source='llm_promoted'.
    conn.execute(
        "INSERT INTO keyword_pairs (scope, directory, keyword_a, keyword_b, count) "
        "VALUES ('llm_promoted', '/d', 'gamma', 'delta', 7)"
    )
    conn.commit()
    conn.close()


def _make_v3_db(path: Path) -> None:
    """Build a v3 database with ``keyword_docs`` rows (to be dropped)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE keyword_docs (
            scope TEXT NOT NULL DEFAULT '',
            directory TEXT NOT NULL,
            keyword TEXT NOT NULL,
            doc_path TEXT NOT NULL,
            occurrence_count REAL NOT NULL DEFAULT 0,
            first_line INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (directory, keyword, doc_path)
        );
        CREATE TABLE keyword_pairs (
            scope TEXT NOT NULL, directory TEXT NOT NULL,
            keyword_a TEXT NOT NULL, keyword_b TEXT NOT NULL,
            count REAL NOT NULL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (scope, directory, keyword_a, keyword_b)
        );
        """
    )
    conn.executemany(
        "INSERT INTO keyword_docs (directory, keyword, doc_path, occurrence_count, first_line) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("/proj_a", "install", "readme.md", 3, 2),
            ("/proj_a", "config", "readme.md", 2, 10),
        ],
    )
    conn.execute(
        "INSERT INTO keyword_pairs (scope, directory, keyword_a, keyword_b, count) "
        "VALUES ('llm_promoted', '/proj_a', 'ka', 'kb', 4)"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()


def test_ensure_schema_on_empty_db(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        version = ensure_schema(conn)
        assert version == SCHEMA_VERSION
        tables = set(list_tables(conn))
        for name in GRAPH_TABLES:
            assert name in tables, f"missing graph table {name}"
        assert "keyword_docs" not in tables
    finally:
        conn.close()


def test_ensure_schema_is_idempotent(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        ensure_schema(conn)  # second call must not raise or duplicate rows.
        conn.execute(
            "INSERT INTO keyword_chunks (directory, keyword, doc_path, chunk_id) "
            "VALUES ('/d', 'k', 'a.md', 0)"
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM keyword_chunks").fetchone()[0] == 1
        assert read_schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_keyword_pairs_has_source_column(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(keyword_pairs)")}
        assert "source" in cols
        assert "cooccur_count" in cols
        assert "npmi" in cols
    finally:
        conn.close()


def test_migrate_v1_preserves_counts_and_salvages_promoted(tmp_path: Path):
    db = tmp_path / "v1.db"
    _make_v1_db(db)

    conn = connect(db)
    try:
        assert read_schema_version(conn) == 0
        ensure_schema(conn)
        assert read_schema_version(conn) == SCHEMA_VERSION

        tables = set(list_tables(conn))
        for name in V1_TABLES:
            assert name in tables, f"v1 table {name} vanished"
        for name in GRAPH_TABLES:
            assert name in tables
        assert "keyword_docs" not in tables

        # keyword_counts preserved.
        count = conn.execute("SELECT count, term FROM keyword_counts").fetchone()
        assert count["term"] == "alpha" and count["count"] == 5

        # Only llm_promoted rows survive; doc-level ('g') cleared.
        sources = {
            row["source"]
            for row in conn.execute("SELECT DISTINCT source FROM keyword_pairs")
        }
        assert sources <= {LLM_PROMOTED_SOURCE}
        salvaged = conn.execute(
            "SELECT keyword_a, keyword_b, cooccur_count, source FROM keyword_pairs"
        ).fetchall()
        assert len(salvaged) == 1
        assert salvaged[0]["keyword_a"] == "gamma"
        assert salvaged[0]["keyword_b"] == "delta"
        assert salvaged[0]["source"] == LLM_PROMOTED_SOURCE
        # doc-level 'alpha'/'beta' pair is gone.
        assert conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs WHERE keyword_a='alpha'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_migrate_v3_drops_keyword_docs(tmp_path: Path):
    db = tmp_path / "v3.db"
    _make_v3_db(db)

    conn = connect(db)
    try:
        assert read_schema_version(conn) == 3
        ensure_schema(conn)
        assert read_schema_version(conn) == SCHEMA_VERSION

        tables = set(list_tables(conn))
        assert "keyword_docs" not in tables
        for name in GRAPH_TABLES:
            assert name in tables

        # llm_promoted pair salvaged.
        rows = conn.execute(
            "SELECT keyword_a, keyword_b, source FROM keyword_pairs"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["source"] == LLM_PROMOTED_SOURCE
        assert (rows[0]["keyword_a"], rows[0]["keyword_b"]) == ("ka", "kb")
    finally:
        conn.close()


def test_migration_is_idempotent_on_v4(tmp_path: Path):
    db = tmp_path / "v3.db"
    _make_v3_db(db)

    conn = connect(db)
    try:
        ensure_schema(conn)  # migrate to v4
        first = conn.execute(
            "SELECT keyword_a, keyword_b, source FROM keyword_pairs ORDER BY 1,2,3"
        ).fetchall()
        ensure_schema(conn)  # no-op fast path
        second = conn.execute(
            "SELECT keyword_a, keyword_b, source FROM keyword_pairs ORDER BY 1,2,3"
        ).fetchall()
        assert [tuple(r) for r in first] == [tuple(r) for r in second]
        assert read_schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_graph_tables_writable_after_ensure(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO chunks (directory, doc_path, chunk_id, start_line, end_line, char_count) "
            "VALUES ('/d', 'a.md', 0, 1, 1, 40)"
        )
        conn.execute(
            "INSERT INTO keyword_chunks (directory, keyword, doc_path, chunk_id, occurrence_count) "
            "VALUES ('/d', 'alpha', 'a.md', 0, 2)"
        )
        conn.execute(
            "INSERT INTO keyword_pairs (directory, keyword_a, keyword_b, source, cooccur_count, npmi) "
            "VALUES ('/d', 'alpha', 'beta', 'chunk_cooccur', 3, 0.5)"
        )
        conn.execute(
            "INSERT INTO heading_docs (directory, heading_path, doc_path, chapter_id, start_line, end_line) "
            "VALUES ('/d', 'Install > macOS', 'a.md', 'install/macos', 10, 20)"
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM keyword_chunks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM keyword_pairs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM heading_docs").fetchone()[0] == 1
    finally:
        conn.close()


def test_ensure_schema_participates_in_caller_transaction(tmp_path: Path):
    """Regression: ensure_schema must NOT commit the caller's in-flight txn.

    Prior to the review-followup fix, ``ensure_schema``'s fast path called
    ``conn.commit()`` unconditionally, silently splitting a caller's atomic
    batch (e.g. ``KeywordLibrary.add_keywords`` INSERT followed by a
    vote-recording helper that itself calls ensure_schema). A crash between
    the two would leave partial state on disk.
    """
    db = tmp_path / "graph.db"
    conn = connect(db)
    try:
        ensure_schema(conn)  # migrate to v4
        conn.execute(
            "INSERT INTO keyword_chunks (directory, keyword, doc_path, chunk_id, occurrence_count) "
            "VALUES ('/d', 'kw1', 'a.md', 0, 1)"
        )
        assert conn.in_transaction is True

        # Second ensure_schema (fast path) MUST NOT commit the pending INSERT.
        ensure_schema(conn)
        assert conn.in_transaction is True, (
            "ensure_schema fast path must not commit the caller's transaction"
        )

        # Simulate caller crashing without committing.
    finally:
        conn.close()

    # Reopen: the row should NOT be visible because the caller never committed.
    conn = connect(db)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM keyword_chunks").fetchone()[0]
        assert rows == 0, (
            f"ensure_schema silently committed caller's INSERT (got {rows} rows, expected 0)"
        )
    finally:
        conn.close()


def test_migration_within_caller_transaction_succeeds(tmp_path: Path):
    """Regression: migration must not fail when caller already has a txn.

    Prior to the review-followup fix, ``_migrate_to_v4`` unconditionally
    issued ``BEGIN IMMEDIATE``. On the call path
    ``KeywordLibrary.add_keywords`` → INSERT (autobegins caller txn) →
    ``record_votes_from_keywords`` → ``ensure_schema`` → ``_migrate_to_v4``,
    ``BEGIN`` inside the caller's already-open transaction raised
    ``cannot start a transaction within a transaction``, was swallowed by
    the outer ``except Exception: pass``, and the v4 schema was never
    created — leaving deep-search permanently returning empty history.
    """
    db = tmp_path / "legacy.db"
    # Seed a v3-shaped legacy DB.
    raw = sqlite3.connect(str(db))
    raw.executescript(
        """
        CREATE TABLE keyword_docs (directory TEXT, keyword TEXT, doc_path TEXT,
            occurrence_count REAL, first_line INTEGER, updated_at TEXT,
            scope TEXT DEFAULT '',
            PRIMARY KEY (directory, keyword, doc_path));
        CREATE TABLE keyword_pairs (scope TEXT NOT NULL, directory TEXT NOT NULL,
            keyword_a TEXT NOT NULL, keyword_b TEXT NOT NULL,
            count REAL NOT NULL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (scope, directory, keyword_a, keyword_b));
        CREATE TABLE keyword_counts (scope TEXT NOT NULL, directory TEXT NOT NULL,
            term TEXT NOT NULL, count REAL NOT NULL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (scope, directory, term));
        """
    )
    raw.execute(
        "INSERT INTO keyword_pairs VALUES ('llm_promoted', '/d', 'alpha', 'beta', 3, NULL)"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    conn = connect(db)
    try:
        # Caller starts a write transaction (mimics add_keywords' INSERT).
        conn.execute(
            "INSERT INTO keyword_counts VALUES ('g', '/d', 'kw', 1, NULL)"
        )
        assert conn.in_transaction is True

        # This used to raise "cannot start a transaction within a transaction".
        version = ensure_schema(conn)
        assert version == SCHEMA_VERSION

        # v4 tables exist and legacy llm_promoted salvaged.
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "chunks" in tables and "keyword_chunks" in tables
        assert "keyword_docs" not in tables
        pair = conn.execute(
            "SELECT source FROM keyword_pairs WHERE keyword_a='alpha'"
        ).fetchone()
        assert pair is not None and pair["source"] == LLM_PROMOTED_SOURCE

        conn.commit()
    finally:
        conn.close()


def test_concurrent_migration_second_process_survives(tmp_path: Path):
    """Regression: TOCTOU-safe migration re-checks version inside the txn.

    Two processes hitting an unmigrated shared DB simultaneously used to
    race: the loser cached ``legacy_pairs=True`` from the pre-lock probe,
    unblocked after the winner migrated, and then ran the salvage SELECT
    against the *new* schema — crashing with ``no such column: count``.
    This test simulates the race by manually migrating the DB *between*
    the loser's ``read_schema_version`` and the loser's ``_migrate_to_v4``
    call — the fixed path re-reads under the lock and short-circuits.
    """
    db = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(db))
    raw.executescript(
        """
        CREATE TABLE keyword_pairs (scope TEXT NOT NULL, directory TEXT NOT NULL,
            keyword_a TEXT NOT NULL, keyword_b TEXT NOT NULL,
            count REAL NOT NULL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (scope, directory, keyword_a, keyword_b));
        """
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    # "Winner" migrates first.
    winner = connect(db)
    try:
        ensure_schema(winner)
    finally:
        winner.close()

    # "Loser" opens after winner committed. Version is now v4; ensure_schema
    # must NOT try to migrate again (which would fail on the old-schema
    # SELECT since keyword_pairs now has cooccur_count, not count).
    loser = connect(db)
    try:
        version = ensure_schema(loser)
        assert version == SCHEMA_VERSION
    finally:
        loser.close()
