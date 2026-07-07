"""Tests for :mod:`grga.graph.schema` (Phase 4.1).

Focus: v2 tables are created, the schema version is stamped, and an
existing v1 database is upgraded without losing any rows.
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


def test_ensure_schema_on_empty_db(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        version = ensure_schema(conn)
        assert version == SCHEMA_VERSION
        tables = set(list_tables(conn))
        for name in V2_TABLES:
            assert name in tables
    finally:
        conn.close()


def test_ensure_schema_is_idempotent(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        first = list_tables(conn)
        ensure_schema(conn)
        second = list_tables(conn)
        assert first == second
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
