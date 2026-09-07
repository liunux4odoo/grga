"""Integration tests for the index-build glob file filter (weak semantics).

Core contract (docs/design/plan-2026-09-07-build-glob-filter.md §2.3): a file
excluded by ``--include-glob`` / ``--exclude-glob`` only stops participating
in the current build — its existing index rows (keyword_docs / heading_docs /
docs) are preserved, never deleted. The mechanism: glob-excluded files count
as "seen in this scan", so the incremental removed-detection pass must not
misjudge them as disappeared.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.build import build_graph_index
from grga.graph.schema import connect


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    return tmp_path


def _write_corpus(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text(
        "# 甲\n\n甲 乙 丙 甲 乙 丙\n" * 5, encoding="utf-8"
    )
    (root / "b.md").write_text(
        "# 乙\n\n乙 丙 丁 乙 丙 丁\n" * 5, encoding="utf-8"
    )
    (root / "c.md").write_text(
        "# 丙\n\n丙 丁 戊 丙 丁 戊\n" * 5, encoding="utf-8"
    )


def _docs_in_db(db: Path) -> set:
    conn = connect(db)
    try:
        return {str(r[0]) for r in conn.execute("SELECT doc_path FROM docs")}
    finally:
        conn.close()


def _kw_docs_for(db: Path, doc_path: str) -> int:
    conn = connect(db)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM keyword_docs kd "
            "JOIN docs d ON d.id = kd.doc_id WHERE d.doc_path = ?",
            (doc_path,),
        ).fetchone()[0])
    finally:
        conn.close()


def test_excluded_file_rows_preserved_in_incremental_build(sidecar_env, tmp_path):
    """**Most critical acceptance case.** Exclude b.md from an incremental
    build: its existing rows must survive (weak semantics) and it must not
    be re-read.
    """
    scope = tmp_path / "corpus"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    build_graph_index(
        "s", directory=scope, db_path=db, mode="rebuild", refresh_ngram="append",
        min_pair_count=1, min_npmi=0.0,
    )
    assert "b.md" in _docs_in_db(db)
    b_rows_before = _kw_docs_for(db, "b.md")
    assert b_rows_before > 0

    s = build_graph_index(
        "s", directory=scope, db_path=db, mode="build", refresh_ngram="append",
        min_pair_count=1, min_npmi=0.0,
        exclude_globs=["b.md"],
    )
    assert s.docs_removed == 0, "glob-excluded file must not be treated as removed"
    assert s.docs_unchanged == 2  # a.md + c.md unchanged; b.md not read
    assert "b.md" in _docs_in_db(db)
    assert _kw_docs_for(db, "b.md") == b_rows_before


def test_recoverability_after_removing_glob(sidecar_env, tmp_path):
    """After an exclude-glob build, dropping the glob lets the file back in."""
    scope = tmp_path / "corpus"
    _write_corpus(scope)
    db = tmp_path / "graph.db"

    build_graph_index(
        "s", directory=scope, db_path=db, mode="rebuild", refresh_ngram="append",
        min_pair_count=1, min_npmi=0.0,
        exclude_globs=["b.md"],
    )
    assert "b.md" not in _docs_in_db(db)  # rebuild only rebuilt a.md + c.md

    s = build_graph_index(
        "s", directory=scope, db_path=db, mode="build", refresh_ngram="append",
        min_pair_count=1, min_npmi=0.0,
    )
    assert "b.md" in _docs_in_db(db)
    assert s.docs_added == 1


def test_include_glob_filters_build(sidecar_env, tmp_path):
    scope = tmp_path / "corpus"
    _write_corpus(scope)
    db = tmp_path / "graph.db"
    build_graph_index(
        "s", directory=scope, db_path=db, mode="rebuild", refresh_ngram="append",
        min_pair_count=1, min_npmi=0.0,
        include_globs=["a.md"],
    )
    assert _docs_in_db(db) == {"a.md"}


def test_build_batch_glob_preserves_excluded_rows(sidecar_env, tmp_path):
    """build + batch + glob combo: excluded file rows survive (the batched
    removed path — noted in the plan as "not currently exercised" — is now
    exercised by glob filtering).
    """
    scope = tmp_path / "corpus"
    _write_corpus(scope)
    db = tmp_path / "graph.db"
    build_graph_index(
        "s", directory=scope, db_path=db, mode="rebuild", refresh_ngram="replace",
        min_pair_count=1, min_npmi=0.0, batch_chunks=50,
    )
    assert "b.md" in _docs_in_db(db)
    b_before = _kw_docs_for(db, "b.md")
    assert b_before > 0

    s = build_graph_index(
        "s", directory=scope, db_path=db, mode="build", refresh_ngram="append",
        min_pair_count=1, min_npmi=0.0, batch_chunks=50,
        exclude_globs=["b.md"],
    )
    assert s.docs_removed == 0
    assert "b.md" in _docs_in_db(db)
    assert _kw_docs_for(db, "b.md") == b_before


def test_rebuild_with_glob_only_rebuilds_matches(sidecar_env, tmp_path):
    """rebuild + glob: the scope is cleared (rebuild's own semantics) and only
    matching files are re-indexed.
    """
    scope = tmp_path / "corpus"
    _write_corpus(scope)
    db = tmp_path / "graph.db"
    build_graph_index(
        "s", directory=scope, db_path=db, mode="rebuild", refresh_ngram="replace",
        min_pair_count=1, min_npmi=0.0,
        include_globs=["a.md", "c.md"],
    )
    assert _docs_in_db(db) == {"a.md", "c.md"}


def test_exclude_glob_case_insensitive_integration(sidecar_env, tmp_path):
    """``--exclude-glob '*.MD'`` excludes lowercase ``b.md``."""
    scope = tmp_path / "corpus"
    _write_corpus(scope)
    db = tmp_path / "graph.db"
    build_graph_index(
        "s", directory=scope, db_path=db, mode="rebuild", refresh_ngram="append",
        min_pair_count=1, min_npmi=0.0,
        exclude_globs=["*.MD"],
    )
    assert "b.md" not in _docs_in_db(db)


def test_all_files_excluded_build_is_noop(sidecar_env, tmp_path):
    """Empty-set boundary: every file excluded → build succeeds, nothing new
    is indexed, and no existing rows are deleted.
    """
    scope = tmp_path / "corpus"
    _write_corpus(scope)
    db = tmp_path / "graph.db"
    build_graph_index(
        "s", directory=scope, db_path=db, mode="rebuild", refresh_ngram="append",
        min_pair_count=1, min_npmi=0.0,
    )
    before = _docs_in_db(db)
    assert before == {"a.md", "b.md", "c.md"}

    s = build_graph_index(
        "s", directory=scope, db_path=db, mode="build", refresh_ngram="append",
        min_pair_count=1, min_npmi=0.0,
        exclude_globs=["*"],
    )
    assert s.docs_removed == 0
    assert s.docs_added == 0
    assert _docs_in_db(db) == before
