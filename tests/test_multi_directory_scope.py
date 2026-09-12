"""Tests for multi-directory scope identity and --keep-* options.

Design: docs/design/plan-2026-09-12-scope-directory-identity.md
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.build import (
    build_graph_index,
    forget_scope_phrases,
)
from grga.graph.schema import connect, read_schema_version, SCHEMA_VERSION


SCOPE_NAME = "multi-dir-test"


def _write_corpus(root: Path, file_name: str = "notes.md") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / file_name).write_text(
        "# Notes\n\n"
        "Quantum computing uses quantum bits (qubits) for computation.\n"
        "Quantum entanglement is a key concept in quantum physics.\n"
        "Superposition and entanglement are core principles.\n",
        encoding="utf-8",
    )


def _write_cjk_corpus(root: Path, file_name: str = "notes.md") -> None:
    """Write Chinese content that triggers n-gram phrase detection."""
    root.mkdir(parents=True, exist_ok=True)
    (root / file_name).write_text(
        "# 三体笔记\n\n"
        "面壁者罗辑提出了黑暗森林法则。\n"
        "章北海是太空军的政治委员。\n"
        "丁仪是物理学家，研究球状闪电。\n"
        "破壁人识破了面壁者的战略。\n"
        "思想钢印是太空军的秘密武器。\n",
        encoding="utf-8",
    )


def _count_docs(conn, scope_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM docs WHERE scope_id = ?", (scope_id,),
    ).fetchone()[0])


def _count_pairs(conn, scope_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source='chunk_cooccur'",
        (scope_id,),
    ).fetchone()[0])


def _count_dict(conn, scope_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM auto_ngram_dict WHERE scope_id = ?",
        (scope_id,),
    ).fetchone()[0])


def _get_scope_id(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM scopes WHERE name = ?", (name,)).fetchone()
    assert row is not None, f"scope {name!r} not found"
    return int(row["id"])


class TestMultiDirectoryScope:
    """Docs identity includes directory, so same-scope multi-directory works."""

    def test_two_dirs_same_scope_same_relpath_no_conflict(self, tmp_path):
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        _write_corpus(dir_a, "notes.md")
        _write_corpus(dir_b, "notes.md")

        db = tmp_path / "graph.db"

        build_graph_index(
            SCOPE_NAME, directory=dir_a, db_path=db,
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
            refresh_ngram="never",
        )
        build_graph_index(
            SCOPE_NAME, directory=dir_b, db_path=db,
            mode="build",  # incremental — append dir_b
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
            refresh_ngram="never",
        )

        conn = connect(db)
        try:
            scope_id = _get_scope_id(conn, SCOPE_NAME)
            assert _count_docs(conn, scope_id) == 2

            rows = conn.execute(
                "SELECT directory, doc_path FROM docs WHERE scope_id = ? "
                "ORDER BY directory",
                (scope_id,),
            ).fetchall()
            dirs = {row["directory"] for row in rows}
            assert str(dir_a.resolve()) in dirs
            assert str(dir_b.resolve()) in dirs
            assert all(row["doc_path"] == "notes.md" for row in rows)
        finally:
            conn.close()

    def test_build_incremental_scoped_to_directory(self, tmp_path):
        """Incremental build's unchanged-detection filters by directory."""
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        _write_corpus(dir_a, "notes.md")
        _write_corpus(dir_b, "notes.md")

        db = tmp_path / "graph.db"

        stats1 = build_graph_index(
            SCOPE_NAME, directory=dir_a, db_path=db,
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
            refresh_ngram="never",
        )
        assert stats1.files_indexed >= 1

        stats2 = build_graph_index(
            SCOPE_NAME, directory=dir_b, db_path=db,
            mode="build",
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
            refresh_ngram="never",
        )
        # dir_b is new — all files should be added, not unchanged.
        assert stats2.docs_added >= 1
        assert stats2.docs_unchanged == 0

        # Second build on dir_a should see all files as unchanged.
        stats3 = build_graph_index(
            SCOPE_NAME, directory=dir_a, db_path=db,
            mode="build",
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
            refresh_ngram="never",
        )
        assert stats3.docs_unchanged >= 1


class TestSchemaV13Migration:
    """v12 → v13: docs table gets directory column + new UNIQUE constraint."""

    def test_fresh_db_is_v13(self, tmp_path):
        db = tmp_path / "fresh.db"
        conn = connect(db)
        try:
            from grga.graph.schema import ensure_schema
            ver = ensure_schema(conn)
            assert ver == SCHEMA_VERSION
            assert SCHEMA_VERSION >= 13

            cols = [row[1] for row in conn.execute("PRAGMA table_info(docs)").fetchall()]
            assert "directory" in cols

            indexes = [row[1] for row in conn.execute("PRAGMA index_list(docs)").fetchall()]
            # sqlite_autoindex_ prefix covers the UNIQUE constraint index.
            # Verify the UNIQUE is (scope_id, directory, doc_path) by checking
            # the index_info output of the unique index.
            unique_cols = set()
            for idx in indexes:
                info = conn.execute(f"PRAGMA index_info({idx})").fetchall()
                col_names = {row[2] for row in info}
                if {"scope_id", "doc_path"} <= col_names:
                    unique_cols = col_names
            assert "directory" in unique_cols
        finally:
            conn.close()

    def test_legacy_rows_get_empty_directory(self, tmp_path):
        """Simulate a pre-v13 DB: create docs without directory, then upgrade."""
        db = tmp_path / "legacy.db"
        conn = connect(db)
        try:
            # Stamp v12 and create a docs table without the directory column.
            conn.execute("PRAGMA user_version = 12")
            conn.execute(
                """
                CREATE TABLE scopes (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE docs (
                    id INTEGER PRIMARY KEY,
                    scope_id INTEGER NOT NULL REFERENCES scopes(id),
                    doc_path TEXT NOT NULL,
                    size INTEGER,
                    mtime REAL,
                    content_hash TEXT,
                    n_chunks_with_kw INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(scope_id, doc_path)
                )
                """
            )
            conn.execute("INSERT INTO scopes (name) VALUES ('old-scope')")
            conn.execute(
                "INSERT INTO docs (scope_id, doc_path) VALUES (1, 'foo.md')"
            )
            conn.commit()

            from grga.graph.schema import ensure_schema
            ver = ensure_schema(conn)
            assert ver == SCHEMA_VERSION

            row = conn.execute(
                "SELECT directory, doc_path FROM docs WHERE id = 1"
            ).fetchone()
            assert row is not None
            assert row["directory"] == ""
            assert row["doc_path"] == "foo.md"
        finally:
            conn.close()


class TestKeepOptions:
    """forget --keep-* and rebuild --keep-* preserve knowledge assets."""

    def test_forget_keep_dict_preserves_auto_ngram_dict(self, tmp_path):
        scope_dir = tmp_path / "corpus"
        _write_cjk_corpus(scope_dir)
        db = tmp_path / "graph.db"

        build_graph_index(
            SCOPE_NAME, directory=scope_dir, db_path=db,
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
        )

        conn = connect(db)
        try:
            sid = _get_scope_id(conn, SCOPE_NAME)
            n_dict_before = _count_dict(conn, sid)
            n_pairs_before = _count_pairs(conn, sid)
        finally:
            conn.close()

        stats = forget_scope_phrases(
            SCOPE_NAME, db_path=db, all_phrases=True,
            keep_dict=True,
        )
        assert stats.keep_dict is True
        assert stats.ngram_dict_removed == 0

        conn = connect(db)
        try:
            sid = _get_scope_id(conn, SCOPE_NAME)
            assert _count_docs(conn, sid) == 0
            assert _count_dict(conn, sid) == n_dict_before
        finally:
            conn.close()

    def test_forget_keep_pairs_preserves_pairs_and_stats(self, tmp_path):
        scope_dir = tmp_path / "corpus"
        _write_corpus(scope_dir)
        db = tmp_path / "graph.db"

        build_graph_index(
            SCOPE_NAME, directory=scope_dir, db_path=db,
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
        )

        conn = connect(db)
        try:
            sid = _get_scope_id(conn, SCOPE_NAME)
            n_pairs_before = _count_pairs(conn, sid)
            assert n_pairs_before > 0
        finally:
            conn.close()

        stats = forget_scope_phrases(
            SCOPE_NAME, db_path=db, all_phrases=True,
            keep_pairs=True,
        )
        assert stats.keep_pairs is True

        conn = connect(db)
        try:
            sid = _get_scope_id(conn, SCOPE_NAME)
            assert _count_docs(conn, sid) == 0
            assert _count_pairs(conn, sid) == n_pairs_before
        finally:
            conn.close()

    def test_forget_keep_assets_preserves_everything(self, tmp_path):
        scope_dir = tmp_path / "corpus"
        _write_cjk_corpus(scope_dir)
        db = tmp_path / "graph.db"

        build_graph_index(
            SCOPE_NAME, directory=scope_dir, db_path=db,
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
        )

        conn = connect(db)
        try:
            sid = _get_scope_id(conn, SCOPE_NAME)
            n_pairs_before = _count_pairs(conn, sid)
            n_dict_before = _count_dict(conn, sid)
        finally:
            conn.close()

        stats = forget_scope_phrases(
            SCOPE_NAME, db_path=db, all_phrases=True,
            keep_dict=True, keep_pairs=True,
        )
        assert stats.keep_dict is True
        assert stats.keep_pairs is True

        conn = connect(db)
        try:
            sid = _get_scope_id(conn, SCOPE_NAME)
            assert _count_docs(conn, sid) == 0
            assert _count_pairs(conn, sid) == n_pairs_before
            assert _count_dict(conn, sid) == n_dict_before
        finally:
            conn.close()

    def test_forget_default_wipes_everything(self, tmp_path):
        scope_dir = tmp_path / "corpus"
        _write_corpus(scope_dir)
        db = tmp_path / "graph.db"

        build_graph_index(
            SCOPE_NAME, directory=scope_dir, db_path=db,
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
        )

        conn = connect(db)
        try:
            sid = _get_scope_id(conn, SCOPE_NAME)
            assert _count_pairs(conn, sid) > 0
        finally:
            conn.close()

        stats = forget_scope_phrases(
            SCOPE_NAME, db_path=db, all_phrases=True,
        )
        assert stats.all_wiped is True
        assert stats.keep_dict is False
        assert stats.keep_pairs is False

        conn = connect(db)
        try:
            sid = _get_scope_id(conn, SCOPE_NAME)
            assert _count_docs(conn, sid) == 0
            assert _count_pairs(conn, sid) == 0
            assert _count_dict(conn, sid) == 0
        finally:
            conn.close()

    def test_rebuild_keep_pairs_merges_new_pairs(self, tmp_path):
        scope_dir = tmp_path / "corpus"
        _write_corpus(scope_dir, "a.md")
        db = tmp_path / "graph.db"

        build_graph_index(
            SCOPE_NAME, directory=scope_dir, db_path=db,
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
        )

        conn = connect(db)
        try:
            sid = _get_scope_id(conn, SCOPE_NAME)
            pairs_before = _count_pairs(conn, sid)
        finally:
            conn.close()

        (scope_dir / "b.md").write_text(
            "# More\n\n"
            "Quantum cryptography uses quantum key distribution.\n"
            "Quantum algorithms include Shor's and Grover's.\n",
            encoding="utf-8",
        )

        build_graph_index(
            SCOPE_NAME, directory=scope_dir, db_path=db,
            mode="rebuild",
            keep_pairs=True,
            min_pair_count=1, min_npmi=0.0, batch_chunks=0,
        )

        conn = connect(db)
        try:
            sid = _get_scope_id(conn, SCOPE_NAME)
            pairs_after = _count_pairs(conn, sid)
            assert pairs_after >= pairs_before
        finally:
            conn.close()
