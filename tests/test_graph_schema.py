"""Tests for :mod:`grga.graph.schema` (Phase 4.1).

Focus: v2 tables are created, the schema version is stamped, and an
existing v1 database is upgraded without losing any rows. Also covers
the v2 → v3 migration (PERF plan §P1-2) that casefolds every
``keyword_docs.keyword`` value so read paths can drop the ``LOWER()``
wrapper.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grga.graph.schema import (
    SCHEMA_VERSION,
    V1_TABLES,
    V2_TABLES,
    connect,
    ensure_schema,
    list_tables,
    read_schema_version,
)


def _make_v1_db(path: Path) -> None:
    """Create a database that looks like a v1 KeywordLibrary artefact."""
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
    conn.execute(
        "INSERT INTO keyword_pairs (scope, directory, keyword_a, keyword_b, count) "
        "VALUES ('g', '/d', 'alpha', 'beta', 3)"
    )
    conn.commit()
    conn.close()


def _make_v2_db(path: Path) -> None:
    """Build a v2 database with mixed-case ``keyword_docs`` rows.

    Reproduces the shape of pre-P1-2 corpora: keywords stored in original
    casing, ``user_version = 2``. Used by the v2 → v3 migration tests.
    """
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
        """
    )
    conn.executemany(
        "INSERT INTO keyword_docs (directory, keyword, doc_path, occurrence_count, first_line) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("/proj_a", "Install", "readme.md", 3, 2),
            ("/proj_a", "install", "guide.md", 5, 4),   # different doc_path — no PK collision
            ("/proj_a", "Config", "readme.md", 2, 10),
            # Two rows that WILL collide after folding: same (dir, doc) with
            # only case differing. Merge must sum occurrence_count.
            ("/proj_b", "API", "spec.md", 4, 1),
            ("/proj_b", "api", "spec.md", 6, 3),
            # Unicode mixed-case (ß.casefold() -> "ss").
            ("/proj_c", "Straße", "docs.md", 2, 1),
        ],
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


def test_ensure_schema_on_empty_db(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        version = ensure_schema(conn)
        assert version == SCHEMA_VERSION
        tables = set(list_tables(conn))
        for name in V2_TABLES:
            assert name in tables, f"missing v2 table {name}"
    finally:
        conn.close()


def test_ensure_schema_is_idempotent(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        ensure_schema(conn)  # second call must not raise or duplicate rows.
        conn.execute("INSERT INTO keyword_docs (directory, keyword, doc_path) VALUES ('/d', 'k', 'a.md')")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM keyword_docs").fetchone()[0] == 1
        assert read_schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_upgrade_preserves_v1_data(tmp_path: Path):
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
        for name in V2_TABLES:
            assert name in tables

        count = conn.execute("SELECT count, term FROM keyword_counts").fetchone()
        assert count["term"] == "alpha" and count["count"] == 5
        pair = conn.execute(
            "SELECT keyword_a, keyword_b, count FROM keyword_pairs"
        ).fetchone()
        assert pair["keyword_a"] == "alpha" and pair["count"] == 3
    finally:
        conn.close()


def test_v2_tables_writable_after_ensure(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO keyword_docs (directory, keyword, doc_path, occurrence_count, first_line) "
            "VALUES ('/d', 'alpha', 'a.md', 4, 2)"
        )
        conn.execute(
            "INSERT INTO heading_docs (directory, heading_path, doc_path, chapter_id, start_line, end_line) "
            "VALUES ('/d', 'Install > macOS', 'a.md', 'install/macos', 10, 20)"
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM keyword_docs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM heading_docs").fetchone()[0] == 1
    finally:
        conn.close()


def test_schema_migration_v2_to_v3(tmp_path: Path):
    """v2 → v3 casefolds every ``keyword_docs.keyword`` (PERF plan §P1-2)."""
    db = tmp_path / "v2.db"
    _make_v2_db(db)

    conn = connect(db)
    try:
        assert read_schema_version(conn) == 2
        ensure_schema(conn)
        assert read_schema_version(conn) == SCHEMA_VERSION

        rows = conn.execute(
            "SELECT directory, keyword, doc_path, occurrence_count "
            "FROM keyword_docs ORDER BY directory, keyword, doc_path"
        ).fetchall()
        # Every stored keyword is casefolded.
        for row in rows:
            assert row["keyword"] == row["keyword"].casefold().strip(), (
                f"post-migration row still has mixed-case: {tuple(row)}"
            )
        # Collision "API"/"api" in (/proj_b, spec.md) merged into one row
        # with summed occurrence_count.
        merged = conn.execute(
            "SELECT occurrence_count FROM keyword_docs "
            "WHERE directory = '/proj_b' AND doc_path = 'spec.md' AND keyword = 'api'"
        ).fetchone()
        assert merged is not None, "merged 'api' row missing"
        assert merged["occurrence_count"] == 10, (
            f"expected 4+6=10 after collision merge, got {merged['occurrence_count']}"
        )
        # Unicode fold: 'Straße' → 'strasse'
        uni = conn.execute(
            "SELECT keyword FROM keyword_docs WHERE directory = '/proj_c'"
        ).fetchone()
        assert uni["keyword"] == "strasse"
        # 'Install'/'install' in /proj_a live under different doc_paths, so
        # both survive as folded rows (no merge across doc_path).
        proj_a_rows = conn.execute(
            "SELECT keyword, doc_path, occurrence_count FROM keyword_docs "
            "WHERE directory = '/proj_a' ORDER BY keyword, doc_path"
        ).fetchall()
        proj_a_pairs = [(r["keyword"], r["doc_path"], r["occurrence_count"]) for r in proj_a_rows]
        assert ("install", "guide.md", 5) in proj_a_pairs
        assert ("install", "readme.md", 3) in proj_a_pairs
    finally:
        conn.close()


def test_schema_migration_v3_is_idempotent(tmp_path: Path):
    """Running ensure_schema on an already-v3 DB is a no-op fast path."""
    db = tmp_path / "v3.db"
    _make_v2_db(db)

    conn = connect(db)
    try:
        ensure_schema(conn)  # first: migrate
        first_snapshot = conn.execute(
            "SELECT directory, keyword, doc_path, occurrence_count FROM keyword_docs ORDER BY 1,2,3"
        ).fetchall()
        ensure_schema(conn)  # second: no-op
        second_snapshot = conn.execute(
            "SELECT directory, keyword, doc_path, occurrence_count FROM keyword_docs ORDER BY 1,2,3"
        ).fetchall()
        assert [tuple(r) for r in first_snapshot] == [tuple(r) for r in second_snapshot]
    finally:
        conn.close()


def test_schema_migration_scope_merge_deterministic(tmp_path: Path):
    """On collision, ``scope`` merge is deterministic and doesn't drop values.

    Two v2 rows folding to the same ``(directory, keyword, doc_path)`` may
    carry different ``scope`` values (unusual but possible if writers
    ever diverge from the "``scope`` follows ``directory``" convention).
    The merge picks a non-empty ``scope`` deterministically (lex-max on
    ties) rather than silently keeping whichever row happened to be
    iterated first. This test seeds a collision where the row with the
    "smaller" scope comes first and asserts the merged row still carries
    the lex-max non-empty value regardless of iteration order.
    """
    db = tmp_path / "v2_scope_merge.db"
    conn = sqlite3.connect(str(db))
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
        """
    )
    # Two rows folding to the same key with distinct scopes.
    conn.executemany(
        "INSERT INTO keyword_docs (scope, directory, keyword, doc_path, occurrence_count, first_line) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            # Empty scope first — must be replaced by the non-empty one.
            ("",       "/d", "API", "spec.md", 4, 1),
            ("scope-b", "/d", "api", "spec.md", 6, 3),
        ],
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    conn = connect(db)
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT scope, occurrence_count FROM keyword_docs "
            "WHERE directory='/d' AND keyword='api' AND doc_path='spec.md'"
        ).fetchone()
        assert row is not None
        assert row["scope"] == "scope-b", (
            f"non-empty scope should win the merge; got {row['scope']!r}"
        )
        assert row["occurrence_count"] == 10, (
            f"occurrence_count should still be summed; got {row['occurrence_count']}"
        )
    finally:
        conn.close()


def test_schema_query_plan_uses_index_after_v3(tmp_path: Path):
    """After v3, ``WHERE directory=? AND keyword IN (...)`` seeks the index.

    v3's real speedup is that ``keyword_docs.keyword`` is stored casefolded
    so read queries drop the non-sargable ``LOWER(keyword)`` wrapper. With
    a ``directory`` predicate the query then seeks
    ``idx_keyword_docs_kw (directory, keyword, ...)`` directly — the SQL
    plan reports ``SEARCH ... USING INDEX``. This test pins that shape
    (SEARCH, not SCAN, and mentions the ``idx_keyword_docs_kw`` index).

    The *global* ``keyword IN (...)`` case (no ``directory`` filter) still
    walks the whole autoindex because every index leads with ``directory``;
    a dedicated ``(keyword, ...)`` index is deferred to PERF plan §P2-2.
    We assert only the scoped case here to avoid false confidence about
    the global lookup.
    """
    db = tmp_path / "v3plan.db"
    conn = connect(db)
    try:
        ensure_schema(conn)
        # Add enough rows to make the planner honest about SCAN vs SEARCH.
        conn.executemany(
            "INSERT INTO keyword_docs (directory, keyword, doc_path, occurrence_count, first_line) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (f"/dir_{d}", f"kw{k}", f"doc_{k}.md", 1, 1)
                for d in range(4)
                for k in range(200)
            ],
        )
        conn.commit()

        # Scoped query: must SEARCH using a composite index on
        # ``(directory, keyword, ...)``. SQLite may pick either the
        # explicit ``idx_keyword_docs_kw`` or the primary-key autoindex
        # ``sqlite_autoindex_keyword_docs_1`` (both have the same leading
        # columns and are equally selective for this query). The real
        # signal we need is (a) SEARCH not SCAN and (b) the seek includes
        # ``keyword=?``, not a range or a directory-only seek.
        scoped = conn.execute(
            "EXPLAIN QUERY PLAN SELECT directory, doc_path FROM keyword_docs "
            "WHERE directory = ? AND keyword IN ('kw10','kw20','kw30')",
            ("/dir_0",),
        ).fetchall()
        scoped_detail = " | ".join(row[-1] for row in scoped)
        assert "SEARCH" in scoped_detail, (
            f"scoped keyword IN (...) must SEARCH, not SCAN: {scoped_detail}"
        )
        assert "keyword=?" in scoped_detail, (
            f"scoped plan must seek on keyword (not just directory): "
            f"{scoped_detail}"
        )
        # ``keyword>?`` or ``keyword<?`` would signal the P1-2 regression
        # (planner range-scans on the keyword axis instead of seeking).
        assert "keyword>?" not in scoped_detail and "keyword<?" not in scoped_detail, (
            f"scoped plan must not range-scan keyword: {scoped_detail}"
        )
    finally:
        conn.close()


def test_self_join_uses_doc_index_not_keyword_range(tmp_path: Path):
    """load_edges' self-join must not range-scan idx_keyword_docs_kw.

    Regression guard: without the ``+column`` optimiser barrier in the
    ``+a.keyword < +b.keyword`` clause, dropping ``LOWER()`` in schema v3
    caused SQLite to switch to ``SEARCH ... USING idx_keyword_docs_kw
    (directory=? AND keyword>?)``, a range scan with huge fan-out that
    made cold ``load_edges`` ~7× slower (1830ms vs 240ms on the 3-scope /
    600-doc bench). The good plan joins on the ``(directory, doc_path)``
    axis instead: doc-level joins have tens of keywords per doc, keyword-
    range joins have thousands per bucket.

    The precise index name SQLite chooses (idx_keyword_docs_doc vs the
    covering PK ``sqlite_autoindex_keyword_docs_1``) depends on the row
    count and analyse stats. What we assert is the *absence* of the
    catastrophic ``keyword>?`` / ``keyword<?`` range predicate on either
    join side — the direct signature of the bad plan.
    """
    db = tmp_path / "join_plan.db"
    conn = connect(db)
    try:
        ensure_schema(conn)
        # Realistic corpus shape: multiple directories, dozens of docs
        # each, ~30 keywords per doc — mirrors the bench profile.
        conn.executemany(
            "INSERT INTO keyword_docs (directory, keyword, doc_path, occurrence_count, first_line) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (f"/dir_{d}", f"kw{k:04d}", f"doc_{docidx:03d}.md", 1, 1)
                for d in range(3)
                for docidx in range(80)
                for k in range(30)
            ],
        )
        conn.commit()
        plan_rows = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT a.keyword, b.keyword
            FROM keyword_docs a
            JOIN keyword_docs b
              ON a.doc_path = b.doc_path
             AND a.directory = b.directory
             AND +a.keyword < +b.keyword
            WHERE a.directory = '/dir_0'
            """
        ).fetchall()
        detail = " | ".join(row[-1] for row in plan_rows)
        assert "keyword>?" not in detail and "keyword<?" not in detail, (
            f"self-join must not range-scan on keyword (P1-2 regression!); "
            f"plan was: {detail}"
        )
    finally:
        conn.close()
