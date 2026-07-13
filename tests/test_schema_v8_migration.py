"""Tests for the v7 → v8 keyword-primary migration (schema.py).

Focus areas:

* fresh empty DB migrates directly to v8 with all new asset tables present;
* a fully-populated v7 database backfills ``keyword_stats`` / ``scope_stats``
  correctly (per-scope chunk_df / doc_df / n_chunks match hand-computed values);
* ``keyword_pairs`` rows below the v8 admission threshold
  (``BUILD_MIN_PAIR_COUNT`` / ``BUILD_MIN_NPMI``) are pruned; above-threshold
  rows and every ``llm_promoted`` row survive;
* migration is idempotent — a re-run is a fast no-op;
* the deprecated ``chunks`` / ``keyword_chunks`` tables **are retained**
  in Phase 1 (they will be dropped in a later phase).

Design: docs/design/plan-2026-07-12-keyword-primary.md §6.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grga.graph.schema import (
    BUILD_MIN_NPMI,
    BUILD_MIN_PAIR_COUNT,
    CHUNK_COOCCUR_SOURCE,
    LLM_PROMOTED_SOURCE,
    SCHEMA_VERSION,
    _table_exists,
    connect,
    ensure_schema,
    read_schema_version,
)


def _populate_v7_fixture(conn: sqlite3.Connection) -> dict:
    """Populate a v5-shape (== v7 for graph tables) DB with test data.

    Returns a dict of expected values for later assertions. Uses two scopes
    to prove per-scope aggregation:

    * scope A (/corpus_a): 2 docs, 4 chunks. Keywords: alpha (kw_id=1)
      appears in 3 chunks across both docs. beta (kw_id=2) appears in 2
      chunks of doc 1 only. gamma (kw_id=3) appears once in doc 2.
    * scope B (/corpus_b): 1 doc, 2 chunks. Keywords: alpha (kw_id=1) once
      in chunk 0. delta (kw_id=4) once in chunk 1.

    keyword_pairs seeded with a mix of pass/prune rows:

    * (alpha, beta) co=6 npmi=0.35 in scope A — passes (both gates)
    * (alpha, gamma) co=3 npmi=0.5 in scope A — pruned (co<5)
    * (alpha, beta) co=8 npmi=0.15 in scope A — pruned (npmi<0.2)
    * (alpha, gamma) co=10 npmi=NULL in scope A source=llm_promoted — survives
    * (alpha, delta) co=7 npmi=0.4 in scope B — passes
    """
    # Bring the DB up to at least v5 shape so intern tables + v7 columns exist.
    # ``ensure_schema`` will run its usual chain; user_version starts at 0.
    ensure_schema(conn)
    # Roll user_version back to 7 so the v7→v8→v9 chain runs. ensure_schema
    # (called by connect path elsewhere) produced the v9 shape which no
    # longer has chunks / keyword_chunks — recreate them in their v7 shape
    # so the v7→v8 backfill + v8→v9 drop have real data to work on. This
    # mimics a genuine on-disk v7 database.
    conn.execute("PRAGMA user_version = 7")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            doc_id INTEGER NOT NULL,
            chunk_id INTEGER NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            char_count INTEGER NOT NULL,
            PRIMARY KEY (doc_id, chunk_id)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keyword_chunks (
            kw_id INTEGER NOT NULL,
            doc_id INTEGER NOT NULL,
            chunk_id INTEGER NOT NULL,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (kw_id, doc_id, chunk_id)
        ) WITHOUT ROWID
        """
    )
    conn.commit()

    # Directories
    conn.execute("INSERT INTO directories(id, path) VALUES (1, '/corpus_a')")
    conn.execute("INSERT INTO directories(id, path) VALUES (2, '/corpus_b')")
    # Keywords
    conn.execute("INSERT INTO keywords(id, kw) VALUES (1, 'alpha')")
    conn.execute("INSERT INTO keywords(id, kw) VALUES (2, 'beta')")
    conn.execute("INSERT INTO keywords(id, kw) VALUES (3, 'gamma')")
    conn.execute("INSERT INTO keywords(id, kw) VALUES (4, 'delta')")
    # Docs
    conn.execute(
        "INSERT INTO docs(id, dir_id, doc_path) VALUES (1, 1, 'a.txt')"
    )
    conn.execute(
        "INSERT INTO docs(id, dir_id, doc_path) VALUES (2, 1, 'b.txt')"
    )
    conn.execute(
        "INSERT INTO docs(id, dir_id, doc_path) VALUES (3, 2, 'c.txt')"
    )
    # Chunks: doc 1 has 3 chunks, doc 2 has 1 chunk, doc 3 has 2 chunks.
    for (did, cid) in [(1, 0), (1, 1), (1, 2), (2, 0), (3, 0), (3, 1)]:
        conn.execute(
            "INSERT INTO chunks(doc_id, chunk_id, start_line, end_line, "
            "char_count) VALUES (?, ?, ?, ?, 50)",
            (did, cid, cid * 10 + 1, cid * 10 + 10),
        )
    # keyword_chunks:
    # scope A: alpha in (d1,c0), (d1,c1), (d2,c0)  => chunk_df=3, doc_df=2, occ_total=1+2+1=4
    #          beta  in (d1,c0), (d1,c1)           => chunk_df=2, doc_df=1, occ_total=1+1=2
    #          gamma in (d2,c0)                    => chunk_df=1, doc_df=1, occ_total=1
    # scope B: alpha in (d3,c0)                    => chunk_df=1, doc_df=1, occ_total=1
    #          delta in (d3,c1)                    => chunk_df=1, doc_df=1, occ_total=1
    for (kw_id, doc_id, chunk_id, occ) in [
        (1, 1, 0, 1), (1, 1, 1, 2), (1, 2, 0, 1),
        (2, 1, 0, 1), (2, 1, 1, 1),
        (3, 2, 0, 1),
        (1, 3, 0, 1),
        (4, 3, 1, 1),
    ]:
        conn.execute(
            "INSERT INTO keyword_chunks(kw_id, doc_id, chunk_id, "
            "occurrence_count) VALUES (?, ?, ?, ?)",
            (kw_id, doc_id, chunk_id, occ),
        )
    # keyword_pairs — data-consistent with the keyword_chunks above so the
    # v3.2 independence gate (df_a == df_b == co, strict three-way equality)
    # is exercised against realistic values (co can never exceed
    # min(df_a, df_b) in a real corpus). scope A chunk_df: alpha=3, beta=2,
    # gamma=1; scope B: alpha=1, delta=1. scope A n_chunks(with-kw)=3 →
    # adaptive floor=2; scope B=2 → floor=2.
    #
    #   (1,1,2) alpha-beta co=2: df=(3,2,co=2) → not 3-way equal → indep✓;
    #           co=2≥floor2 ✓; npmi≥0.2 ✓                                  → SURVIVE
    #   (1,1,3) alpha-gamma co=1: co=1<floor2 → PRUNE (count floor)
    #   (1,1,3) llm_promoted:    exempt from all gates                     → SURVIVE
    #   (2,1,4) alpha-delta co=1: df=(1,1,co=1) → glued (3-way equal)      → PRUNE (independence)
    conn.execute(
        "INSERT INTO keyword_pairs(dir_id, kw_a_id, kw_b_id, source, "
        "cooccur_count, npmi) VALUES (1, 1, 2, ?, 2, 0.35)",
        (CHUNK_COOCCUR_SOURCE,),
    )
    conn.execute(
        "INSERT INTO keyword_pairs(dir_id, kw_a_id, kw_b_id, source, "
        "cooccur_count, npmi) VALUES (1, 1, 3, ?, 1, 0.5)",
        (CHUNK_COOCCUR_SOURCE,),
    )
    # Reuse (1,3) pair distinguished by source column to test admission gates.
    # Actually two chunk_cooccur rows with same key would violate PK; use
    # source='llm_promoted' for the third row instead.
    conn.execute(
        "INSERT INTO keyword_pairs(dir_id, kw_a_id, kw_b_id, source, "
        "cooccur_count, npmi) VALUES (1, 1, 3, ?, 10, NULL)",
        (LLM_PROMOTED_SOURCE,),
    )
    # alpha-delta in scope B: df_alpha=1, df_delta=1, co=1 → glued
    # (df == df == co, three-way equal) → independence prune.
    conn.execute(
        "INSERT INTO keyword_pairs(dir_id, kw_a_id, kw_b_id, source, "
        "cooccur_count, npmi) VALUES (2, 1, 4, ?, 1, 0.9)",
        (CHUNK_COOCCUR_SOURCE,),
    )
    conn.commit()

    return {
        "expected_stats": {
            # (dir_id, kw_id): (chunk_df, doc_df, total_occurrences)
            (1, 1): (3, 2, 4),
            (1, 2): (2, 1, 2),
            (1, 3): (1, 1, 1),
            (2, 1): (1, 1, 1),
            (2, 4): (1, 1, 1),
        },
        "expected_scope_stats": {
            # dir_id: (n_chunks, n_docs, vocab_size)
            # v9 semantics: n_chunks counts *chunks-with-keywords* (the npmi
            # denominator), not raw chunks. Scope A has 4 raw chunks but
            # chunk (doc1, chunk2) has no keyword_chunks row → 3 with-kw.
            1: (3, 2, 3),  # 3 chunks-with-kw across docs 1+2, 2 docs, 3 kws
            2: (2, 1, 2),  # 2 chunks-with-kw in scope B, 1 doc, 2 kws
        },
        "expected_surviving_pairs": {
            # (dir_id, kw_a_id, kw_b_id, source): (cooccur, npmi_or_None)
            (1, 1, 2, CHUNK_COOCCUR_SOURCE): (2.0, 0.35),
            (1, 1, 3, LLM_PROMOTED_SOURCE): (10.0, None),
        },
        "expected_pruned_pairs": {
            # These must be gone after v8 migration.
            (1, 1, 3, CHUNK_COOCCUR_SOURCE),  # co=1 < adaptive floor 2
            (2, 1, 4, CHUNK_COOCCUR_SOURCE),  # glued: df=df=co=1 (Gate 1)
        },
    }


def test_v7_to_v8_backfills_keyword_stats(tmp_path: Path) -> None:
    """keyword_stats should reflect the per-scope aggregate of keyword_chunks."""
    conn = connect(tmp_path / "graph.db")
    try:
        expected = _populate_v7_fixture(conn)
        # Trigger v7→v8 hop.
        conn.execute("PRAGMA user_version = 7")
        ensure_schema(conn)
        assert read_schema_version(conn) == SCHEMA_VERSION

        got = {
            (row["dir_id"], row["kw_id"]): (
                row["chunk_df"], row["doc_df"], row["total_occurrences"]
            )
            for row in conn.execute(
                "SELECT dir_id, kw_id, chunk_df, doc_df, total_occurrences "
                "FROM keyword_stats"
            )
        }
        assert got == expected["expected_stats"]
    finally:
        conn.close()


def test_v7_to_v8_backfills_scope_stats(tmp_path: Path) -> None:
    """scope_stats.n_chunks / n_docs / vocab_size should match hand-computed."""
    conn = connect(tmp_path / "graph.db")
    try:
        expected = _populate_v7_fixture(conn)
        conn.execute("PRAGMA user_version = 7")
        ensure_schema(conn)

        got = {
            row["dir_id"]: (row["n_chunks"], row["n_docs"], row["vocab_size"])
            for row in conn.execute(
                "SELECT dir_id, n_chunks, n_docs, vocab_size FROM scope_stats"
            )
        }
        assert got == expected["expected_scope_stats"]
    finally:
        conn.close()


def test_v7_to_v8_prunes_low_score_pairs(tmp_path: Path) -> None:
    """Sub-threshold chunk_cooccur pairs are removed; llm_promoted rows kept."""
    conn = connect(tmp_path / "graph.db")
    try:
        expected = _populate_v7_fixture(conn)
        conn.execute("PRAGMA user_version = 7")
        ensure_schema(conn)

        surviving = {
            (row["dir_id"], row["kw_a_id"], row["kw_b_id"], row["source"]): (
                row["cooccur_count"], row["npmi"]
            )
            for row in conn.execute(
                "SELECT dir_id, kw_a_id, kw_b_id, source, cooccur_count, npmi "
                "FROM keyword_pairs"
            )
        }
        # All expected rows survive with values intact.
        for key, (co, npmi) in expected["expected_surviving_pairs"].items():
            assert key in surviving, f"expected surviving pair missing: {key}"
            got_co, got_npmi = surviving[key]
            assert got_co == pytest.approx(co)
            if npmi is None:
                assert got_npmi is None
            else:
                assert got_npmi == pytest.approx(npmi)
        # All expected-pruned rows are gone.
        for key in expected["expected_pruned_pairs"]:
            assert key not in surviving, f"pruned pair survived: {key}"

        # Sanity: threshold constants match the spec.
        assert BUILD_MIN_PAIR_COUNT == 5
        assert BUILD_MIN_NPMI == 0.2
    finally:
        conn.close()


def test_v7_to_v9_drops_deprecated_tables(tmp_path: Path) -> None:
    """v7→v9 chain removes ``chunks`` and ``keyword_chunks``.

    Phase 1's ``keep them for compat`` clause is superseded by Phase 3b-3
    (docs/design/plan-2026-07-12-keyword-primary.md §4.3): once the build /
    read paths no longer touch these tables, keeping them is dead weight.
    ``keyword_docs`` (the doc-level replacement inverted index) takes over.
    """
    conn = connect(tmp_path / "graph.db")
    try:
        _populate_v7_fixture(conn)
        conn.execute("PRAGMA user_version = 7")
        ensure_schema(conn)

        assert not _table_exists(conn, "chunks")
        assert not _table_exists(conn, "keyword_chunks")
        # keyword_docs must have been backfilled from the v7 chunk data.
        assert _table_exists(conn, "keyword_docs")
        n_kd = conn.execute("SELECT COUNT(*) FROM keyword_docs").fetchone()[0]
        assert n_kd > 0
    finally:
        conn.close()


def test_v8_new_indexes_created(tmp_path: Path) -> None:
    """The two new npmi-ordered indexes on keyword_pairs must exist."""
    conn = connect(tmp_path / "graph.db")
    try:
        _populate_v7_fixture(conn)
        conn.execute("PRAGMA user_version = 7")
        ensure_schema(conn)

        idx_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='keyword_pairs'"
            )
        }
        assert "idx_kp_a_npmi" in idx_names
        assert "idx_kp_b_npmi" in idx_names
        # And the keyword_stats index:
        ks_idx = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='keyword_stats'"
            )
        }
        assert "idx_keyword_stats_scope" in ks_idx
    finally:
        conn.close()


def test_v7_to_v8_is_idempotent(tmp_path: Path) -> None:
    """Re-running ensure_schema on an already-v8 DB is a no-op."""
    conn = connect(tmp_path / "graph.db")
    try:
        _populate_v7_fixture(conn)
        conn.execute("PRAGMA user_version = 7")
        ensure_schema(conn)
        # Snapshot after first migration.
        stats1 = list(conn.execute(
            "SELECT dir_id, kw_id, chunk_df, doc_df, total_occurrences "
            "FROM keyword_stats ORDER BY dir_id, kw_id"
        ))
        pairs1 = list(conn.execute(
            "SELECT dir_id, kw_a_id, kw_b_id, source, cooccur_count, npmi "
            "FROM keyword_pairs ORDER BY dir_id, kw_a_id, kw_b_id, source"
        ))
        v1 = read_schema_version(conn)

        # Re-run.
        ensure_schema(conn)
        assert read_schema_version(conn) == v1

        stats2 = list(conn.execute(
            "SELECT dir_id, kw_id, chunk_df, doc_df, total_occurrences "
            "FROM keyword_stats ORDER BY dir_id, kw_id"
        ))
        pairs2 = list(conn.execute(
            "SELECT dir_id, kw_a_id, kw_b_id, source, cooccur_count, npmi "
            "FROM keyword_pairs ORDER BY dir_id, kw_a_id, kw_b_id, source"
        ))
        assert stats1 == stats2
        assert pairs1 == pairs2
    finally:
        conn.close()


def test_fresh_db_gets_v8_directly(tmp_path: Path) -> None:
    """A brand-new empty DB should land on v8 with all asset tables present."""
    conn = connect(tmp_path / "graph.db")
    try:
        ensure_schema(conn)
        assert read_schema_version(conn) == SCHEMA_VERSION
        assert _table_exists(conn, "keyword_stats")
        assert _table_exists(conn, "scope_stats")
        # keyword_stats + scope_stats start empty on a fresh DB.
        assert conn.execute(
            "SELECT COUNT(*) FROM keyword_stats"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM scope_stats"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_v7_to_v8_emits_lossy_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v7 → v8 must warn users about the lossy pair prune when data is at risk.

    Contract points (docs/design/plan-2026-07-12-keyword-primary.md §6):

    * Warning fires when the DB has existing ``keyword_pairs`` rows (data
      truly at risk of the prune DELETE).
    * Warning fires regardless of DB size (small DBs must get it too — the
      20 MB "may take a while" banner is a separate concern).
    * Warning mentions ``keyword_pairs`` (so users know what's being deleted).
    * Warning names the numeric threshold (so users can see what survives).
    * Warning states the migration is not reversible.
    * ``GRGA_QUIET_MIGRATION=1`` silences it (test-only escape hatch).
    """
    # Ensure the announce path is not silenced (may inherit from parent env).
    monkeypatch.delenv("GRGA_QUIET_MIGRATION", raising=False)

    conn = connect(tmp_path / "graph.db")
    try:
        _populate_v7_fixture(conn)  # seeds keyword_pairs rows
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        capsys.readouterr()  # discard any fixture-time output
        ensure_schema(conn)
        stderr = capsys.readouterr().err
        assert "v7→v8" in stderr or "v7\\u2192v8" in stderr, \
            f"expected v7→v8 label in stderr; got: {stderr!r}"
        assert "keyword_pairs" in stderr, \
            f"warning should mention keyword_pairs; got: {stderr!r}"
        assert "5" in stderr and "0.2" in stderr, \
            f"warning should cite thresholds (5, 0.2); got: {stderr!r}"
        assert "reversible" in stderr.lower() or "roll back" in stderr.lower(), \
            f"warning should flag irreversibility; got: {stderr!r}"
    finally:
        conn.close()


def test_lossy_warning_suppressed_by_env(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GRGA_QUIET_MIGRATION=1`` fully silences the v7→v8 warning."""
    monkeypatch.setenv("GRGA_QUIET_MIGRATION", "1")
    conn = connect(tmp_path / "graph.db")
    try:
        _populate_v7_fixture(conn)
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        capsys.readouterr()
        ensure_schema(conn)
        stderr = capsys.readouterr().err
        assert stderr == "", \
            f"GRGA_QUIET_MIGRATION should suppress all output; got: {stderr!r}"
    finally:
        conn.close()


def test_lossy_warning_not_emitted_on_fresh_db(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh / empty DB has nothing to prune, so the lossy warning must NOT fire.

    Regression guard: without this gate, ``grga index build --json`` on a
    first-run install would emit the scary "will permanently delete" banner
    before there is any data to delete — polluting the JSON contract with
    a false alarm.
    """
    monkeypatch.delenv("GRGA_QUIET_MIGRATION", raising=False)
    conn = connect(tmp_path / "fresh.db")
    try:
        capsys.readouterr()
        ensure_schema(conn)
        stderr = capsys.readouterr().err
        assert "WARNING" not in stderr, \
            f"fresh DB should not emit lossy warning; got: {stderr!r}"
        # No pairs table content either (sanity).
        assert conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_migration_rolls_back_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration failure inside the txn must leave the DB at its pre-v8 state.

    Monkey-patches the backfill helper to raise mid-migration, then verifies
    that the transaction rolls back and ``user_version`` stays at 7. Fault
    injection at the Python layer (rather than corrupting SQL) so the test
    exercises the exact rollback code path without depending on which query
    happens to error first.
    """
    from grga.graph import schema as schema_mod

    conn = connect(tmp_path / "graph.db")
    try:
        _populate_v7_fixture(conn)
        conn.execute("PRAGMA user_version = 7")
        conn.commit()

        def _boom(_conn):  # noqa: ARG001
            raise RuntimeError("simulated migration failure")

        monkeypatch.setattr(schema_mod, "_migrate_v7_to_v8_inplace", _boom)
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            ensure_schema(conn)
        # Version must NOT have advanced.
        assert read_schema_version(conn) == 7
        # keyword_stats stays empty (rollback undid any partial inserts).
        # NB: if _create_current_tables ran before the raise, the table
        # exists but must have zero rows.
        if _table_exists(conn, "keyword_stats"):
            assert conn.execute(
                "SELECT COUNT(*) FROM keyword_stats"
            ).fetchone()[0] == 0
    finally:
        conn.close()
