"""Tests for :mod:`grga.graph.schema` (v10 scope / directory split).

Focus:

* Fresh install creates every v10 table + stamps ``user_version = 10``.
* Idempotent fast path: second ``ensure_schema`` is a no-op.
* Caller-owned transaction semantics: fast path never commits, migration
  path only commits its own txn.
* Every pre-v10 shape is rejected with an actionable error (see
  ``docs/design/plan-2026-07-14-scope-directory-split.md`` §7):

  - v5-v9 (``directories`` intern table + ``user_version`` in 1..9)
  - pre-v5 ``KeywordLibrary`` (``keyword_counts`` / ``keyword_sources``
    with a TEXT ``directory`` column, no ``directories``, ``user_version=0``)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grga.graph.schema import (
    CHUNK_COOCCUR_SOURCE,
    LLM_PROMOTED_SOURCE,
    SCHEMA_VERSION,
    connect,
    ensure_schema,
    list_tables,
    read_schema_version,
    resolve_scope_name,
)


# Every table a fresh v10 install must ship. Mirrors the DDL in
# ``schema.py`` — if you add a table there, list it here so a missing
# CREATE is caught at test time.
V10_TABLES = {
    # Scope layer
    "scopes", "scope_directories",
    # Global intern
    "keywords", "meta",
    # Business tables
    "keyword_pairs", "keyword_stats", "scope_stats",
    "auto_ngram_dict", "llm_extension_votes",
    # Evidence layer
    "docs", "keyword_docs", "heading_docs",
    "keyword_counts", "keyword_sources",
}

# Tables that v10 explicitly does NOT ship (dropped or replaced).
V10_ABSENT_TABLES = {"directories", "chunks", "keyword_chunks"}


# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------


def test_ensure_schema_on_empty_db(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        version = ensure_schema(conn)
        assert version == SCHEMA_VERSION
        tables = set(list_tables(conn))
        missing = V10_TABLES - tables
        assert not missing, f"missing v10 tables: {missing}"
        surplus = V10_ABSENT_TABLES & tables
        assert not surplus, f"v10 must not have: {surplus}"
    finally:
        conn.close()


def test_ensure_schema_is_idempotent(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        ensure_schema(conn)  # second call must be a no-op fast path
        assert read_schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_keyword_pairs_has_scope_id_and_source_columns(tmp_path: Path):
    """Regression: v10 shard key is ``scope_id``, not the pre-v10 ``dir_id``."""
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(keyword_pairs)")}
        assert cols >= {"scope_id", "kw_a_id", "kw_b_id", "source",
                        "cooccur_count", "npmi"}
        # The pre-v10 ``dir_id`` must be gone.
        assert "dir_id" not in cols
    finally:
        conn.close()


def test_scope_directories_binding_table_exists(tmp_path: Path):
    """v10 evidence layer: ``scope_directories`` records local path bindings."""
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        cols = {row["name"] for row in conn.execute(
            "PRAGMA table_info(scope_directories)"
        )}
        assert cols >= {"scope_id", "directory", "indexed_at"}
    finally:
        conn.close()


def test_scope_stats_has_scope_id(tmp_path: Path):
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        cols = {row["name"] for row in conn.execute(
            "PRAGMA table_info(scope_stats)"
        )}
        assert cols >= {"scope_id", "n_chunks", "n_docs", "vocab_size"}
        assert "dir_id" not in cols
    finally:
        conn.close()


def test_graph_tables_writable_after_ensure(tmp_path: Path):
    """End-to-end sanity: manual INSERTs into every asset table succeed."""
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO scopes(name, kind, created_at) "
            "VALUES ('domain-x', 'test', datetime('now'))"
        )
        scope_id = conn.execute(
            "SELECT id FROM scopes WHERE name='domain-x'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO scope_directories(scope_id, directory, indexed_at) "
            "VALUES (?, '/tmp/x', datetime('now'))",
            (scope_id,),
        )
        conn.execute("INSERT INTO keywords(kw) VALUES ('alpha')")
        conn.execute("INSERT INTO keywords(kw) VALUES ('beta')")
        alpha_id = conn.execute(
            "SELECT id FROM keywords WHERE kw='alpha'"
        ).fetchone()[0]
        beta_id = conn.execute(
            "SELECT id FROM keywords WHERE kw='beta'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO docs(scope_id, doc_path) VALUES (?, 'a.md')",
            (scope_id,),
        )
        doc_id = conn.execute(
            "SELECT id FROM docs WHERE scope_id=? AND doc_path='a.md'",
            (scope_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO keyword_docs(scope_id, kw_id, doc_id, "
            "occurrence_count, chunk_count) VALUES (?, ?, ?, 2, 1)",
            (scope_id, alpha_id, doc_id),
        )
        conn.execute(
            "INSERT INTO keyword_pairs(scope_id, kw_a_id, kw_b_id, source, "
            "cooccur_count, npmi) "
            f"VALUES (?, ?, ?, '{CHUNK_COOCCUR_SOURCE}', 3, 0.5)",
            (scope_id, alpha_id, beta_id),
        )
        conn.execute(
            "INSERT INTO heading_docs(doc_id, heading_path, chapter_id, "
            "start_line, end_line) "
            "VALUES (?, 'Install > macOS', 'install/macos', 10, 20)",
            (doc_id,),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM keyword_docs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM keyword_pairs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM heading_docs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM scope_directories").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Transaction semantics
# ---------------------------------------------------------------------------


def test_ensure_schema_fast_path_does_not_commit_caller_txn(tmp_path: Path):
    """Regression: v10 fast path must not commit a caller's in-flight txn.

    A caller-owned transaction wrapping a write followed by a second
    ensure_schema call (typical for ``KeywordLibrary.add_keywords`` +
    ``record_votes_from_keywords``) must remain atomic — a crash before
    the caller commits must roll back everything.
    """
    db = tmp_path / "graph.db"
    conn = connect(db)
    try:
        ensure_schema(conn)  # first: creates schema, commits its own txn
        conn.execute(
            "INSERT INTO scopes(name) VALUES ('domain-x')"
        )
        assert conn.in_transaction is True
        # Second call is the fast-path branch — it must NOT commit.
        ensure_schema(conn)
        assert conn.in_transaction is True, (
            "fast-path ensure_schema silently committed caller's txn"
        )
        # Simulate caller crashing without committing.
    finally:
        conn.close()

    # Reopen: the INSERT must not be visible.
    conn = connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0]
        assert n == 0, f"fast-path ensure_schema leaked {n} row(s)"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Legacy DB rejection (plan-2026-07-14 §7)
# ---------------------------------------------------------------------------


def _seed_v5_v9_shape(db: Path, version: int) -> None:
    """Create a minimal v5-v9-shaped DB: has ``directories``, stamped."""
    raw = sqlite3.connect(str(db))
    raw.execute(
        "CREATE TABLE directories (id INTEGER PRIMARY KEY, path TEXT)"
    )
    raw.execute(f"PRAGMA user_version = {int(version)}")
    raw.commit()
    raw.close()


def _seed_prev5_shape(db: Path) -> None:
    """Create a pre-v5 ``KeywordLibrary`` shape: TEXT ``directory`` columns,
    no ``directories`` intern table, ``user_version = 0``."""
    raw = sqlite3.connect(str(db))
    raw.executescript(
        """
        CREATE TABLE keyword_counts (
            scope TEXT NOT NULL, directory TEXT NOT NULL,
            term TEXT NOT NULL, count REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (scope, directory, term)
        );
        CREATE TABLE keyword_sources (
            directory TEXT NOT NULL, keyword TEXT NOT NULL,
            path TEXT NOT NULL, line_number INTEGER,
            count REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (directory, keyword, path, line_number)
        );
        """
    )
    raw.commit()
    raw.close()


@pytest.mark.parametrize("version", [1, 3, 5, 7, 8, 9])
def test_reject_v5_to_v9_legacy_db(tmp_path: Path, version: int):
    """Every pre-v10 user_version stamp with the ``directories`` marker
    triggers the actionable rejection message, mentioning the DB file and
    the ``SELECT path FROM directories`` inspect hint."""
    db = tmp_path / f"legacy_v{version}.db"
    _seed_v5_v9_shape(db, version)
    conn = connect(db)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            ensure_schema(conn)
        message = str(exc_info.value)
        assert "scope / directory split" in message
        assert "SELECT path FROM directories" in message
        assert str(db) in message
    finally:
        conn.close()


def test_reject_prev5_keyword_library_shape(tmp_path: Path):
    """Pre-v5 ``KeywordLibrary`` DBs have no ``directories`` table and
    ``user_version = 0`` — they must still be caught, with a hint that
    doesn't dangle at a nonexistent ``directories`` table."""
    db = tmp_path / "prev5.db"
    _seed_prev5_shape(db)
    conn = connect(db)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            ensure_schema(conn)
        message = str(exc_info.value)
        assert "scope / directory split" in message
        # The v5-v9 hint that references a non-existent table must NOT
        # be present here — the pre-v5 branch uses the `.tables` /
        # `PRAGMA user_version` hint instead.
        assert "SELECT path FROM directories" not in message
        assert ".tables" in message or "PRAGMA user_version" in message
    finally:
        conn.close()


def test_reject_future_version_db(tmp_path: Path):
    """A DB stamped with a version higher than ``SCHEMA_VERSION`` must not
    be silently rewritten. ``ensure_schema`` rejects it via the legacy
    path (the check-and-reject is on "user_version not in (0, current)")."""
    db = tmp_path / "future.db"
    raw = sqlite3.connect(str(db))
    raw.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION) + 1}")
    raw.commit()
    raw.close()
    conn = connect(db)
    try:
        with pytest.raises(RuntimeError):
            ensure_schema(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# resolve_scope_name (pure-string, no filesystem touch)
# ---------------------------------------------------------------------------


def test_resolve_scope_name_none_passes_through():
    assert resolve_scope_name(None) is None


def test_resolve_scope_name_strips_whitespace():
    assert resolve_scope_name("  physics-cn  ") == "physics-cn"


def test_resolve_scope_name_empty_raises():
    with pytest.raises(ValueError):
        resolve_scope_name("")


def test_resolve_scope_name_whitespace_only_raises():
    with pytest.raises(ValueError):
        resolve_scope_name("   \t\n")


def test_resolve_scope_name_does_not_resolve_paths(tmp_path: Path):
    """v10 invariant: scope names are opaque handles. ``resolve_scope_name``
    does not call ``Path.resolve`` / ``expanduser``, so a name that happens
    to look like a path is stored verbatim (this is a design choice, not a
    footgun — ``KeywordLibrary`` has a separate path-shaped guard)."""
    tricky = str(tmp_path / "not-a-real-scope")
    assert resolve_scope_name(tricky) == tricky
