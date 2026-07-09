"""Tests for :mod:`grga.graph.build` (Phase 4.2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.build import build_graph_index, extract_keywords
from grga.graph.schema import connect


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    return tmp_path


def _write_scope(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "install.md").write_text(
        "# Install\n\n## macOS\n\nBrew install grga on macOS.\n"
        "grga works with ripgrep and pandoc.\n"
        "## Linux\n\nUse apt install grga on Linux.\n",
        encoding="utf-8",
    )
    (root / "usage.md").write_text(
        "# Usage\n\n## macOS\n\nRun grga on macOS with ripgrep.\n"
        "pandoc powers pdf extraction.\n",
        encoding="utf-8",
    )
    (root / "code.py").write_text(
        "def install():\n    return 'grga install ripgrep'\n\n"
        "install()\n"
        "install()\n",
        encoding="utf-8",
    )


def test_extract_keywords_basic():
    text = "grga grga ripgrep pandoc\ngrga usage"
    result = extract_keywords(text, min_count=2)
    terms = [item[0] for item in result]
    assert "grga" in [t.lower() for t in terms]
    grga_row = next(item for item in result if item[0].lower() == "grga")
    assert grga_row[1] == 3  # three occurrences
    assert grga_row[2] == 1  # first line


def test_extract_keywords_stores_folded_form_v3():
    """Schema v3: extract_keywords emits casefolded keywords (PERF plan §P1-2).

    Prior to v3 the tokeniser preserved the original casing of the first
    occurrence so ``keyword_docs.keyword`` held mixed-case strings. Read
    paths then had to wrap every lookup in ``LOWER(keyword)``, which is
    non-sargable and forced a full-table SCAN when the query lacked a
    ``directory`` predicate. v3 stores the folded form so lookups can use
    the ``(directory, keyword, ...)`` index directly.
    """
    text = "the and Foo\nBar Bar Bar\nBaz Baz Baz\n"
    terms = [term for term, _, _ in extract_keywords(text, min_count=2)]
    # Every term is its casefolded form; no mixed-case leakage.
    for term in terms:
        assert term == term.casefold(), (
            f"v3 must emit folded keywords, got {term!r}"
        )
    # And the actual folded contents are preserved.
    fold_set = set(terms)
    assert "bar" in fold_set
    assert "baz" in fold_set


def test_build_populates_both_tables(sidecar_env, tmp_path):
    scope = tmp_path / "corpus"
    _write_scope(scope)
    db = tmp_path / "graph.db"

    stats = build_graph_index(scope, db_path=db, min_term_count=1)
    assert stats.files_scanned >= 3
    assert stats.files_indexed >= 3
    assert stats.keyword_docs_written > 0
    assert stats.heading_docs_written > 0
    assert stats.md_files_indexed == 2

    conn = connect(db)
    try:
        kw_rows = conn.execute(
            "SELECT keyword, doc_path, occurrence_count FROM keyword_docs ORDER BY doc_path, keyword"
        ).fetchall()
        assert kw_rows
        # Every stored keyword should be reachable via its doc.
        docs = {row["doc_path"] for row in kw_rows}
        assert "install.md" in docs
        assert "usage.md" in docs

        head_rows = conn.execute(
            "SELECT heading_path, doc_path FROM heading_docs"
        ).fetchall()
        paths = {row["heading_path"] for row in head_rows}
        assert any("Install" in p for p in paths)
        # Shared heading across docs (Phase 4 heading mode relies on it):
        macos_docs = {
            row["doc_path"]
            for row in head_rows
            if row["heading_path"].endswith("macOS")
        }
        assert macos_docs == {"install.md", "usage.md"}
    finally:
        conn.close()


def test_replace_flag_clears_previous_rows(sidecar_env, tmp_path):
    scope = tmp_path / "corpus"
    _write_scope(scope)
    db = tmp_path / "graph.db"

    build_graph_index(scope, db_path=db, min_term_count=1)
    (scope / "install.md").unlink()

    stats = build_graph_index(scope, db_path=db, min_term_count=1)
    assert stats.files_indexed == 2  # usage.md + code.py

    conn = connect(db)
    try:
        remaining = [
            row["doc_path"]
            for row in conn.execute("SELECT DISTINCT doc_path FROM keyword_docs").fetchall()
        ]
        assert "install.md" not in remaining
    finally:
        conn.close()


def test_v1_tables_are_not_touched_by_build(sidecar_env, tmp_path):
    scope = tmp_path / "corpus"
    _write_scope(scope)
    db = tmp_path / "graph.db"

    # Seed v1 rows first.
    from grga.deep_search import KeywordLibrary

    lib = KeywordLibrary(db)
    lib.add_keywords("global", str(scope), ["alpha", "beta"])
    lib.commit()
    lib.close()

    build_graph_index(scope, db_path=db, min_term_count=1)

    conn = connect(db)
    try:
        pair = conn.execute(
            "SELECT count FROM keyword_pairs WHERE keyword_a = 'alpha' AND keyword_b = 'beta'"
        ).fetchone()
        assert pair is not None and pair["count"] == 1
        counts = conn.execute(
            "SELECT COUNT(*) FROM keyword_counts WHERE term IN ('alpha', 'beta')"
        ).fetchone()[0]
        assert counts == 2
    finally:
        conn.close()
