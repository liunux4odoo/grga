"""Tests for :mod:`grga.graph.schema` (normalized migration, schema v5).

Focus: the current graph tables are created, the schema version is stamped
at v5, and legacy databases (v1 KeywordLibrary, v2/v3 ``keyword_docs``,
v4 chunk-level TEXT-keyed) all converge on v5 in one atomic hop:

* Intern tables ``directories`` / ``keywords`` / ``docs`` populated from
  the pre-existing TEXT columns.
* Business tables (``keyword_chunks`` / ``chunks`` / ``keyword_pairs`` /
  ``heading_docs`` / ``llm_extension_votes`` / ``keyword_counts`` /
  ``keyword_sources``) rewritten to reference the intern tables by id.
* ``keyword_docs`` dropped (v3 legacy).
* ``keyword_pairs`` legacy promoted rows salvaged as
  ``source='llm_promoted'``; every other legacy row cleared.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grga.graph.schema import (
    GRAPH_TABLES,
    INTERN_TABLES,
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
        for name in INTERN_TABLES:
            assert name in tables, f"missing intern table {name}"
        assert "keyword_docs" not in tables
    finally:
        conn.close()


def _insert_normalized_kw_chunk(conn: sqlite3.Connection, directory: str,
                                keyword: str, doc_path: str, chunk_id: int,
                                occ: int = 1) -> None:
    """Helper: insert a row into ``keyword_chunks`` via the intern tables."""
    conn.execute("INSERT OR IGNORE INTO directories(path) VALUES (?)", (directory,))
    conn.execute("INSERT OR IGNORE INTO keywords(kw) VALUES (?)", (keyword,))
    dir_id = conn.execute("SELECT id FROM directories WHERE path=?", (directory,)).fetchone()[0]
    kw_id = conn.execute("SELECT id FROM keywords WHERE kw=?", (keyword,)).fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO docs(dir_id, doc_path) VALUES (?, ?)",
        (dir_id, doc_path),
    )
    doc_id = conn.execute(
        "SELECT id FROM docs WHERE dir_id=? AND doc_path=?",
        (dir_id, doc_path),
    ).fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO keyword_chunks(kw_id, doc_id, chunk_id, occurrence_count) "
        "VALUES (?, ?, ?, ?)",
        (kw_id, doc_id, chunk_id, occ),
    )


def test_ensure_schema_is_idempotent(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        ensure_schema(conn)  # second call must not raise or duplicate rows.
        _insert_normalized_kw_chunk(conn, "/d", "k", "a.md", 0)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM keyword_chunks").fetchone()[0] == 1
        assert read_schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_keyword_pairs_has_id_and_source_columns(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(keyword_pairs)")}
        assert "source" in cols
        assert "cooccur_count" in cols
        assert "npmi" in cols
        assert "dir_id" in cols
        assert "kw_a_id" in cols
        assert "kw_b_id" in cols
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
        for name in INTERN_TABLES:
            assert name in tables
        assert "keyword_docs" not in tables

        # keyword_counts preserved (normalized: SELECT via joins).
        count = conn.execute(
            "SELECT c.count, k.kw AS term "
            "FROM keyword_counts c JOIN keywords k ON k.id = c.kw_id"
        ).fetchone()
        assert count["term"] == "alpha" and count["count"] == 5

        # Only llm_promoted rows survive; doc-level ('g') cleared.
        sources = {
            row["source"]
            for row in conn.execute("SELECT DISTINCT source FROM keyword_pairs")
        }
        assert sources <= {LLM_PROMOTED_SOURCE}
        salvaged = conn.execute(
            "SELECT ka.kw AS keyword_a, kb.kw AS keyword_b, "
            "       p.cooccur_count AS cooccur_count, p.source AS source "
            "FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id"
        ).fetchall()
        assert len(salvaged) == 1
        assert salvaged[0]["keyword_a"] == "gamma"
        assert salvaged[0]["keyword_b"] == "delta"
        assert salvaged[0]["source"] == LLM_PROMOTED_SOURCE
        # doc-level 'alpha'/'beta' pair is gone.
        assert conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "WHERE ka.kw='alpha'"
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
            "SELECT ka.kw AS keyword_a, kb.kw AS keyword_b, p.source AS source "
            "FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["source"] == LLM_PROMOTED_SOURCE
        assert (rows[0]["keyword_a"], rows[0]["keyword_b"]) == ("ka", "kb")
    finally:
        conn.close()


def test_migration_is_idempotent_on_current(tmp_path: Path):
    db = tmp_path / "v3.db"
    _make_v3_db(db)

    conn = connect(db)
    try:
        ensure_schema(conn)  # migrate to v5
        first = conn.execute(
            "SELECT ka.kw, kb.kw, p.source FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id=p.kw_a_id "
            "JOIN keywords kb ON kb.id=p.kw_b_id "
            "ORDER BY 1,2,3"
        ).fetchall()
        ensure_schema(conn)  # no-op fast path
        second = conn.execute(
            "SELECT ka.kw, kb.kw, p.source FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id=p.kw_a_id "
            "JOIN keywords kb ON kb.id=p.kw_b_id "
            "ORDER BY 1,2,3"
        ).fetchall()
        assert [tuple(r) for r in first] == [tuple(r) for r in second]
        assert read_schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_graph_tables_writable_after_ensure(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        conn.execute("INSERT INTO directories(path) VALUES ('/d')")
        conn.execute("INSERT INTO keywords(kw) VALUES ('alpha')")
        conn.execute("INSERT INTO keywords(kw) VALUES ('beta')")
        conn.execute(
            "INSERT INTO docs(dir_id, doc_path) VALUES "
            "((SELECT id FROM directories WHERE path='/d'), 'a.md')"
        )
        dir_id = conn.execute("SELECT id FROM directories WHERE path='/d'").fetchone()[0]
        doc_id = conn.execute(
            "SELECT id FROM docs WHERE dir_id=? AND doc_path='a.md'", (dir_id,)
        ).fetchone()[0]
        alpha_id = conn.execute("SELECT id FROM keywords WHERE kw='alpha'").fetchone()[0]
        beta_id = conn.execute("SELECT id FROM keywords WHERE kw='beta'").fetchone()[0]
        conn.execute(
            "INSERT INTO chunks (doc_id, chunk_id, start_line, end_line, char_count) "
            "VALUES (?, 0, 1, 1, 40)",
            (doc_id,),
        )
        conn.execute(
            "INSERT INTO keyword_chunks (kw_id, doc_id, chunk_id, occurrence_count) "
            "VALUES (?, ?, 0, 2)",
            (alpha_id, doc_id),
        )
        conn.execute(
            "INSERT INTO keyword_pairs (dir_id, kw_a_id, kw_b_id, source, "
            "cooccur_count, npmi) VALUES (?, ?, ?, 'chunk_cooccur', 3, 0.5)",
            (dir_id, alpha_id, beta_id),
        )
        conn.execute(
            "INSERT INTO heading_docs (doc_id, heading_path, chapter_id, start_line, end_line) "
            "VALUES (?, 'Install > macOS', 'install/macos', 10, 20)",
            (doc_id,),
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
        ensure_schema(conn)  # migrate to current
        _insert_normalized_kw_chunk(conn, "/d", "kw1", "a.md", 0)
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

    Same scenario as the v4 test but converging on v5.
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
    raw.execute("INSERT INTO keyword_counts VALUES ('g', '/d', 'seed', 2, NULL)")
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    conn = connect(db)
    try:
        # Caller starts a write transaction (mimics some ad-hoc INSERT). The
        # legacy v3 keyword_counts still has TEXT columns pre-migration, so
        # we open the txn with a harmless SELECT that autobegins nothing;
        # instead we hold a savepoint-style lock via a real INSERT into a
        # column-compatible legacy table.
        conn.execute(
            "INSERT INTO keyword_counts VALUES ('g', '/d', 'kw', 1, NULL)"
        )
        assert conn.in_transaction is True

        # This used to raise "cannot start a transaction within a transaction".
        version = ensure_schema(conn)
        assert version == SCHEMA_VERSION

        # v5 tables exist and legacy llm_promoted salvaged.
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "chunks" in tables and "keyword_chunks" in tables
        assert "keyword_docs" not in tables
        assert "directories" in tables and "keywords" in tables and "docs" in tables
        pair = conn.execute(
            "SELECT p.source FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id WHERE ka.kw = 'alpha'"
        ).fetchone()
        assert pair is not None and pair["source"] == LLM_PROMOTED_SOURCE

        conn.commit()
    finally:
        conn.close()


def test_concurrent_migration_second_process_survives(tmp_path: Path):
    """Regression: TOCTOU-safe migration re-checks version inside the txn."""
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

    # "Loser" opens after winner committed. Version is now current; ensure_schema
    # must NOT try to migrate again.
    loser = connect(db)
    try:
        version = ensure_schema(loser)
        assert version == SCHEMA_VERSION
    finally:
        loser.close()
