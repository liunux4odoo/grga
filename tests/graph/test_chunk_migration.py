"""Chunk-level migration boundary tests (plan §6).

Trimmed in v8 Phase 3b-2 to the 5 tests that exercise chunk-mode behaviour
and schema/llm-promote flows independently of the removed graph mode. The
graph-expansion tests that used to live here are covered by
``tests/test_related_keywords.py`` against the v8 pair-primary API.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grga.graph.build import build_graph_index, split_chunks
from grga.graph.schema import (
    LLM_PROMOTED_SOURCE,
    SCHEMA_VERSION,
    connect,
    ensure_schema,
    read_schema_version,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))


# ---------------------------------------------------------------------------
# 空集 · scope with no chunks
# ---------------------------------------------------------------------------


def test_chars_mode_single_huge_line_becomes_one_chunk():
    text = "x" * 100_000
    chunks = split_chunks(text, mode="chars", min_chars=300, max_chars=600)
    assert len(chunks) == 1
    assert chunks[0].char_count == 100_000


# 降级路径 · Markdown heading + tiny paragraph
def test_chars_mode_short_heading_paragraph_not_merged():
    text = "# A\nx\n## B\ny\n"
    chunks = split_chunks(text, mode="chars", is_markdown=True, min_chars=100, max_chars=500)
    # heading forces cut even below min_chars
    heads = [c.text.splitlines()[0] for c in chunks]
    assert any(h.startswith("# A") for h in heads)
    assert any(h.startswith("## B") for h in heads)


# ---------------------------------------------------------------------------
# hub / domain / rare seed scenarios
# ---------------------------------------------------------------------------


def test_v3_migration_atomic_transaction(tmp_path):
    """A v3 database migrates in one shot: single ensure_schema call brings
    it to the current schema with keyword_docs dropped and llm_promoted rows
    salvaged (v3 → v4 → v5 chain, one atomic transaction)."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE keyword_docs (
            directory TEXT, keyword TEXT, doc_path TEXT, occurrence_count REAL,
            first_line INTEGER, updated_at TEXT, scope TEXT DEFAULT '',
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
    conn.execute(
        "INSERT INTO keyword_docs (directory, keyword, doc_path, occurrence_count, first_line) "
        "VALUES ('/d', 'alpha', 'a.md', 3, 1)"
    )
    conn.execute(
        "INSERT INTO keyword_pairs (scope, directory, keyword_a, keyword_b, count) "
        "VALUES ('llm_promoted', '/d', 'x', 'y', 5)"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit(); conn.close()

    c = connect(db)
    try:
        ensure_schema(c)
        assert read_schema_version(c) == SCHEMA_VERSION
        # Legacy keyword_docs gone
        assert c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='keyword_docs'"
        ).fetchone()[0] == 0
        # LLM-promoted rows salvaged
        rows = c.execute(
            "SELECT ka.kw AS keyword_a, kb.kw AS keyword_b, p.source AS source "
            "FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["source"] == LLM_PROMOTED_SOURCE
    finally:
        c.close()


def test_current_schema_migration_idempotent(tmp_path):
    db = tmp_path / "graph.db"
    c = connect(db)
    try:
        ensure_schema(c)
        first = c.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
        ensure_schema(c)  # no-op
        second = c.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
        assert [tuple(r) for r in first] == [tuple(r) for r in second]
    finally:
        c.close()


def test_llm_promote_flow_marks_source_correctly(tmp_path):
    """Promotion path writes source='llm_promoted' in the new schema."""
    from grga.graph.promote import promote_votes
    from grga.graph.votes import record_vote

    db = tmp_path / "graph.db"
    for _ in range(3):
        record_vote("alpha", "beta", db_path=db)
    stats = promote_votes(threshold=3, db_path=db)
    assert stats.promoted == 1

    c = connect(db)
    try:
        row = c.execute(
            "SELECT p.source AS source FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id "
            "WHERE ka.kw = 'alpha' AND kb.kw = 'beta'"
        ).fetchone()
        assert row is not None
        assert row["source"] == LLM_PROMOTED_SOURCE
    finally:
        c.close()


# ---------------------------------------------------------------------------
# graph_signals contract
# ---------------------------------------------------------------------------


