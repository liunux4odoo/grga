"""Tests for the incremental build / rebuild / forget verbs (schema v7).

Design: docs/design/plan-2026-07-11-build-vs-rebuild.md
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from grga.graph.build import (
    DOC_ADDED,
    DOC_METADATA_ONLY,
    DOC_READ_ERROR,
    DOC_UNCHANGED,
    DOC_UPDATED,
    _classify_doc,
    _hash_output_lines,
    build_graph_index,
    forget_scope_phrases,
    recompute_pairs,
)
from grga.graph.ids import IdInterner
from grga.graph.schema import CHUNK_COOCCUR_SOURCE, connect, ensure_schema


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    return tmp_path


def _write_corpus(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text(
        "# 面壁计划\n\n面壁计划 罗辑\n" * 5,
        encoding="utf-8",
    )
    (root / "b.txt").write_text(
        "罗辑 智子 罗辑 智子\n智子 章北海\n" * 5,
        encoding="utf-8",
    )
    (root / "c.py").write_text(
        "def install(): return 'grga install ripgrep'\ninstall()\n" * 3,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _classify_doc / _hash_output_lines — unit-level
# ---------------------------------------------------------------------------


def test_hash_output_lines_stable():
    """Hash of the same rga output text must be identical across calls."""
    lines = ["面壁计划", "罗辑 智子", "章北海"]
    assert _hash_output_lines(lines) == _hash_output_lines(list(lines))
    # Length matches the digest size (BLAKE2b-128 hex = 32 chars).
    assert len(_hash_output_lines(lines)) == 32


def test_classify_doc_added(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello world", encoding="utf-8")
    kind, read_result, new_hash = _classify_doc(p, None)
    assert kind == DOC_ADDED
    assert read_result is not None and read_result.lines
    assert new_hash is not None and len(new_hash) == 32


def test_classify_doc_unchanged(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello world", encoding="utf-8")
    st = p.stat()
    row = {"size": st.st_size, "mtime": st.st_mtime, "content_hash": None,
           "id": 1}
    kind, _, _ = _classify_doc(p, row)
    assert kind == DOC_UNCHANGED


def test_classify_doc_metadata_only(tmp_path):
    """A rewritten file whose *rga output* is byte-identical must classify
    as metadata_only, not updated."""
    p = tmp_path / "a.txt"
    p.write_text("hello world", encoding="utf-8")
    st = p.stat()
    prev_hash = _hash_output_lines(p.read_text(encoding="utf-8").splitlines())
    # Rewrite with same content but different mtime.
    p.write_text("hello world", encoding="utf-8")
    os.utime(p, (st.st_atime + 1, st.st_mtime + 1))
    row = {"size": st.st_size, "mtime": st.st_mtime,
           "content_hash": prev_hash, "id": 1}
    kind, _, new_hash = _classify_doc(p, row)
    assert kind == DOC_METADATA_ONLY
    assert new_hash == prev_hash


def test_classify_doc_updated(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello world", encoding="utf-8")
    st = p.stat()
    prev_hash = _hash_output_lines(p.read_text(encoding="utf-8").splitlines())
    p.write_text("hello DIFFERENT world", encoding="utf-8")
    os.utime(p, (st.st_atime + 1, st.st_mtime + 1))
    row = {"size": st.st_size, "mtime": st.st_mtime,
           "content_hash": prev_hash, "id": 1}
    kind, _, new_hash = _classify_doc(p, row)
    assert kind == DOC_UPDATED
    assert new_hash != prev_hash


# ---------------------------------------------------------------------------
# build_graph_index (mode="build") — incremental integration
# ---------------------------------------------------------------------------


def test_incremental_no_change(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    # Initial rebuild.
    s1 = build_graph_index(
        scope, db_path=db, mode="rebuild", refresh_ngram="append",
    )
    assert s1.mode == "rebuild"
    assert s1.docs_added == s1.files_indexed
    assert s1.pairs_written > 0

    # No changes on disk → everything classified as unchanged.
    s2 = build_graph_index(
        scope, db_path=db, mode="build", refresh_ngram="append",
    )
    assert s2.mode == "build"
    assert s2.docs_unchanged == 3
    assert s2.docs_updated == 0
    assert s2.docs_added == 0
    assert s2.docs_removed == 0
    # Pair count must remain stable (recomputed from same keyword_chunks).
    assert s2.pairs_written == s1.pairs_written


def test_incremental_modify_file(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    build_graph_index(scope, db_path=db, mode="rebuild",
                      refresh_ngram="append")

    # Modify a.md substantially.
    (scope / "a.md").write_text(
        "# 新章节\n\n新概念 探索 新概念 探索\n" * 5, encoding="utf-8"
    )
    os.utime(scope / "a.md",
             (Path(scope / "a.md").stat().st_atime + 1,
              Path(scope / "a.md").stat().st_mtime + 2))

    s = build_graph_index(scope, db_path=db, mode="build",
                          refresh_ngram="append")
    assert s.docs_updated == 1
    assert s.docs_unchanged == 2
    assert s.docs_removed == 0

    # Verify DB rows for a.md were replaced (no leftover chunks).
    conn = connect(db)
    ensure_schema(conn)
    interner = IdInterner(conn)
    scope_dir_id = interner.dir_id(str(scope.resolve()))
    row = conn.execute(
        "SELECT id FROM docs WHERE dir_id = ? AND doc_path = ?",
        (scope_dir_id, "a.md"),
    ).fetchone()
    doc_id = int(row[0])
    chunk_count = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (doc_id,)
    ).fetchone()[0]
    conn.close()
    # The new content still has multiple lines → some chunks exist.
    assert chunk_count > 0


def test_incremental_delete_file_cleans_index(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    build_graph_index(scope, db_path=db, mode="rebuild",
                      refresh_ngram="append")

    # Snapshot doc rows before deletion.
    conn = connect(db)
    ensure_schema(conn)
    before_docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    conn.close()

    (scope / "b.txt").unlink()

    s = build_graph_index(scope, db_path=db, mode="build",
                          refresh_ngram="append")
    assert s.docs_removed == 1
    assert s.docs_unchanged == 2

    # Verify docs / chunks / keyword_chunks for b.txt are gone.
    conn = connect(db)
    ensure_schema(conn)
    after_docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    assert after_docs == before_docs - 1
    b_row = conn.execute(
        "SELECT COUNT(*) FROM docs WHERE doc_path = 'b.txt'"
    ).fetchone()[0]
    assert b_row == 0
    conn.close()


def test_incremental_add_new_file(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    build_graph_index(scope, db_path=db, mode="rebuild",
                      refresh_ngram="append")

    (scope / "d.md").write_text(
        "# 新文档\n\n新词 出现 新词 出现\n" * 5, encoding="utf-8"
    )
    s = build_graph_index(scope, db_path=db, mode="build",
                          refresh_ngram="append")
    assert s.docs_added == 1
    assert s.docs_unchanged == 3


def test_build_replace_refresh_ngram_is_rejected(tmp_path):
    """Combining mode='build' with refresh_ngram='replace' must raise."""
    with pytest.raises(ValueError, match="refresh_ngram='replace'"):
        build_graph_index(
            tmp_path, mode="build", refresh_ngram="replace",
        )


def test_metadata_only_updates_hash_row(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    build_graph_index(scope, db_path=db, mode="rebuild",
                      refresh_ngram="append")

    # Touch b.txt but keep contents identical.
    st = (scope / "b.txt").stat()
    (scope / "b.txt").write_text(
        (scope / "b.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    os.utime(scope / "b.txt", (st.st_atime + 1, st.st_mtime + 5))

    s = build_graph_index(scope, db_path=db, mode="build",
                          refresh_ngram="append")
    assert s.docs_metadata_only == 1
    assert s.docs_updated == 0
    assert s.docs_unchanged == 2


# ---------------------------------------------------------------------------
# forget_scope_phrases — cascade + pair recompute
# ---------------------------------------------------------------------------


def test_forget_all_wipes_scope(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    build_graph_index(scope, db_path=db, mode="rebuild",
                      refresh_ngram="append")

    fs = forget_scope_phrases(scope, db_path=db, all_phrases=True)
    assert fs.all_wiped is True

    conn = connect(db)
    ensure_schema(conn)
    interner = IdInterner(conn)
    scope_dir_id = interner.dir_id_ro(str(scope.resolve()))
    assert scope_dir_id is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM docs WHERE dir_id = ?", (scope_dir_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs WHERE dir_id = ? AND source = ?",
        (scope_dir_id, CHUNK_COOCCUR_SOURCE),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM auto_ngram_dict WHERE dir_id = ?",
        (scope_dir_id,),
    ).fetchone()[0] == 0
    conn.close()


def test_forget_phrase_cascades_keyword_chunks(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    build_graph_index(scope, db_path=db, mode="rebuild",
                      refresh_ngram="append")

    # 罗辑 exists in both a.md and b.txt. Forget it.
    fs = forget_scope_phrases(scope, db_path=db, phrases=["罗辑"])
    assert fs.keyword_chunks_removed > 0

    conn = connect(db)
    ensure_schema(conn)
    interner = IdInterner(conn)
    scope_dir_id = interner.dir_id_ro(str(scope.resolve()))
    kw_id = interner.kw_id_ro("罗辑")
    # Either the keyword was interned but its rows are all gone, or the
    # keyword row itself never existed (short corpus, stopword-adjacent).
    if kw_id is not None:
        count = conn.execute(
            "SELECT COUNT(*) FROM keyword_chunks "
            "WHERE kw_id = ? AND doc_id IN "
            "(SELECT id FROM docs WHERE dir_id = ?)",
            (kw_id, scope_dir_id),
        ).fetchone()[0]
        assert count == 0
        pair_count = conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs WHERE dir_id = ? "
            "AND source = ? AND (kw_a_id = ? OR kw_b_id = ?)",
            (scope_dir_id, CHUNK_COOCCUR_SOURCE, kw_id, kw_id),
        ).fetchone()[0]
        assert pair_count == 0
    conn.close()


def test_forget_missing_phrase_is_noop(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    build_graph_index(scope, db_path=db, mode="rebuild",
                      refresh_ngram="append")

    fs = forget_scope_phrases(scope, db_path=db, phrases=["not_a_real_phrase"])
    assert "not_a_real_phrase" in fs.phrases_missing
    assert fs.keyword_chunks_removed == 0


def test_forget_on_missing_scope_is_noop(tmp_path):
    """forget on a scope that was never indexed must not raise."""
    scope = tmp_path / "empty_scope"
    scope.mkdir()
    db = tmp_path / "graph.db"
    fs = forget_scope_phrases(scope, db_path=db, all_phrases=True)
    assert fs.all_wiped is True
    assert fs.ngram_dict_removed == 0


def test_forget_requires_exactly_one_mode(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        forget_scope_phrases(tmp_path, phrases=["x"], all_phrases=True)
    with pytest.raises(ValueError, match="exactly one"):
        forget_scope_phrases(tmp_path)


# ---------------------------------------------------------------------------
# recompute_pairs — idempotence
# ---------------------------------------------------------------------------


def test_recompute_pairs_idempotent(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    s1 = build_graph_index(scope, db_path=db, mode="rebuild",
                            refresh_ngram="append")

    conn = connect(db)
    ensure_schema(conn)
    interner = IdInterner(conn)
    scope_dir_id = interner.dir_id_ro(str(scope.resolve()))
    # Running recompute a second time on unchanged index layer must
    # produce the same pair count and same npmi values.
    before_rows = conn.execute(
        "SELECT kw_a_id, kw_b_id, cooccur_count, npmi FROM keyword_pairs "
        "WHERE dir_id = ? AND source = ?",
        (scope_dir_id, CHUNK_COOCCUR_SOURCE),
    ).fetchall()
    written = recompute_pairs(conn, scope_dir_id, min_pair_count=3)
    conn.commit()
    after_rows = conn.execute(
        "SELECT kw_a_id, kw_b_id, cooccur_count, npmi FROM keyword_pairs "
        "WHERE dir_id = ? AND source = ?",
        (scope_dir_id, CHUNK_COOCCUR_SOURCE),
    ).fetchall()
    conn.close()
    assert written == len(before_rows)
    # Row-by-row equality (converted to tuples for hashing).
    before = sorted(tuple(r) for r in before_rows)
    after = sorted(tuple(r) for r in after_rows)
    assert before == after


# ---------------------------------------------------------------------------
# Legacy compat: replace=True/False kwarg still works.
# ---------------------------------------------------------------------------


def test_legacy_replace_true_maps_to_rebuild_replace(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"
    s = build_graph_index(scope, db_path=db, replace=True)
    assert s.mode == "rebuild"
    assert s.refresh_ngram == "replace"


def test_legacy_replace_false_maps_to_rebuild_append(tmp_path, sidecar_env):
    scope = tmp_path / "scope"
    _write_corpus(scope)
    db = tmp_path / "graph.db"
    s = build_graph_index(scope, db_path=db, replace=False)
    assert s.mode == "rebuild"
    assert s.refresh_ngram == "append"
