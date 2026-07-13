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
    it to the current schema with the legacy keyword_docs dropped, the v9
    keyword_docs recreated, and llm_promoted rows salvaged (v3→v4→v5→…→v9
    chain, one atomic transaction)."""
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
        # keyword_docs exists post-migration but is the *v9* asset table
        # (different column shape) — the legacy v3 keyword_docs was dropped
        # during v3→v4 and the DDL recreated the new one.
        assert c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='keyword_docs'"
        ).fetchone()[0] == 1
        cols = {r["name"] for r in c.execute("PRAGMA table_info(keyword_docs)")}
        assert cols >= {"dir_id", "kw_id", "doc_id", "occurrence_count", "chunk_count"}
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
# _prune_low_score_pairs (v3.2 scope-adaptive + independence gate)
# ---------------------------------------------------------------------------


def test_prune_low_score_pairs_scope_adaptive_and_independence(tmp_path):
    """The storage-contract pruner applies the three write-time gates:

    * Gate 1 (independence): co >= max(chunk_df_a, chunk_df_b) → drop
    * Gate 2 (scope-adaptive count floor): from scope_stats.n_chunks
    * Gate 3 (npmi floor): NULL or below BUILD_MIN_NPMI → drop

    Verified via direct SQL setup so we don't have to build a v7-shape
    fixture. The three gates operate independently — a single-scope table
    with rows crafted to hit each of them individually and one row that
    passes all three.
    """
    from grga.graph.schema import (
        CHUNK_COOCCUR_SOURCE, BUILD_MIN_NPMI,
        _prune_low_score_pairs, connect, ensure_schema,
    )

    db = tmp_path / "graph.db"
    conn = connect(db)
    try:
        ensure_schema(conn)
        # Set up one scope with n_chunks=50 → resolve_min_pair_count(50) = 2.
        conn.execute(
            "INSERT INTO directories (id, path) VALUES (1, ?)",
            (str(tmp_path),),
        )
        conn.execute(
            "INSERT INTO scope_stats (dir_id, n_chunks, n_docs, vocab_size, updated_at) "
            "VALUES (1, 50, 5, 10, '2026-07-13T00:00:00Z')"
        )
        # Vocabulary + per-scope stats. Values chosen to exercise each gate:
        #   kw 1..5 with chunk_df:  10, 10, 10, 10, 10
        kws = [(i, f"kw{i}") for i in range(1, 6)]
        conn.executemany(
            "INSERT INTO keywords (id, kw) VALUES (?, ?)", kws
        )
        conn.executemany(
            "INSERT INTO keyword_stats "
            "(dir_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at) "
            "VALUES (1, ?, 10, 5, 20, '2026-07-13T00:00:00Z')",
            [(i,) for (i, _) in kws],
        )
        # Pairs: (kw_a, kw_b, cooccur, npmi) — one per gate + one survivor.
        # A stored ``kw_a_id < kw_b_id`` invariant is enforced by the schema.
        conn.executemany(
            "INSERT INTO keyword_pairs "
            "(dir_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, '2026-07-13T00:00:00Z')",
            [
                # PASS: co=5 < max(10,10), co>=2 floor, npmi>=0.2
                (1, 2, CHUNK_COOCCUR_SOURCE, 5, 0.55),
                # DROP Gate 1: co=10 == max(10,10) → glued
                (1, 3, CHUNK_COOCCUR_SOURCE, 10, 0.99),
                # DROP Gate 2: co=1 below adaptive floor (2)
                (1, 4, CHUNK_COOCCUR_SOURCE, 1, 0.80),
                # DROP Gate 3a: npmi NULL
                (1, 5, CHUNK_COOCCUR_SOURCE, 5, None),
                # DROP Gate 3b: npmi below BUILD_MIN_NPMI
                (2, 3, CHUNK_COOCCUR_SOURCE, 5, 0.05),
                # SURVIVE (llm_promoted always exempt from all three gates)
                (2, 4, "llm_promoted", 1, None),
            ],
        )
        conn.commit()

        deleted = _prune_low_score_pairs(conn)
        conn.commit()
        assert deleted == 4

        surviving = [
            (r["kw_a_id"], r["kw_b_id"], r["source"])
            for r in conn.execute(
                "SELECT kw_a_id, kw_b_id, source FROM keyword_pairs "
                "ORDER BY kw_a_id, kw_b_id"
            )
        ]
        assert surviving == [
            (1, 2, CHUNK_COOCCUR_SOURCE),  # passed all three gates
            (2, 4, "llm_promoted"),        # llm_promoted exempt
        ]
    finally:
        conn.close()


def test_prune_low_score_pairs_missing_scope_stats_falls_back_to_fixed_floor(tmp_path):
    """Legacy DBs (or fresh installs seeded before scope_stats is populated)
    with no scope_stats row for a dir_id must fall back to the pre-v3.2
    fixed floor (BUILD_MIN_PAIR_COUNT=5), still applying Gates 1 and 3."""
    from grga.graph.schema import (
        CHUNK_COOCCUR_SOURCE, BUILD_MIN_PAIR_COUNT,
        _prune_low_score_pairs, connect, ensure_schema,
    )

    db = tmp_path / "graph.db"
    conn = connect(db)
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO directories (id, path) VALUES (1, ?)",
            (str(tmp_path),),
        )
        # NO scope_stats row → fallback path.
        kws = [(1, "kw1"), (2, "kw2"), (3, "kw3")]
        conn.executemany("INSERT INTO keywords (id, kw) VALUES (?, ?)", kws)
        conn.executemany(
            "INSERT INTO keyword_stats "
            "(dir_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at) "
            "VALUES (1, ?, 20, 10, 40, '2026-07-13T00:00:00Z')",
            [(i,) for (i, _) in kws],
        )
        conn.executemany(
            "INSERT INTO keyword_pairs "
            "(dir_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, '2026-07-13T00:00:00Z')",
            [
                # co=4 < fixed floor 5 → drop
                (1, 2, CHUNK_COOCCUR_SOURCE, 4, 0.55),
                # co=5 == floor 5, co < max(20,20), npmi ok → survive
                (1, 3, CHUNK_COOCCUR_SOURCE, 5, 0.55),
            ],
        )
        conn.commit()

        deleted = _prune_low_score_pairs(conn)
        conn.commit()
        assert deleted == 1
        assert BUILD_MIN_PAIR_COUNT == 5, "test assumes historic default"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# graph_signals contract
# ---------------------------------------------------------------------------


