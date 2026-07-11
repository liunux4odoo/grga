"""Tests for :mod:`grga.graph.build` (chunk-level migration, schema v4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.build import (
    build_graph_index,
    extract_chunk_keywords,
    split_chunks,
)
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


def test_extract_chunk_keywords_basic():
    counts = extract_chunk_keywords("grga grga ripgrep pandoc")
    assert counts["grga"] == 2
    assert counts["ripgrep"] == 1
    assert counts["pandoc"] == 1


def test_extract_chunk_keywords_stores_folded_form():
    counts = extract_chunk_keywords("Foo Bar Bar Baz")
    # Every stored key is casefolded.
    for key in counts:
        assert key == key.casefold()
    assert counts["bar"] == 2


def test_extract_chunk_keywords_drops_stopwords():
    counts = extract_chunk_keywords("the and for grga")
    assert "the" not in counts
    assert "grga" in counts


def test_extract_chunk_keywords_strips_trailing_punctuation():
    counts = extract_chunk_keywords(
        "word. foo-bar, get_config. a.b.c... end- v1.2.3 c++ c#"
    )
    # Trailing '.' / '-' are no longer glued onto tokens.
    assert counts["word"] == 1
    assert counts["foo-bar"] == 1
    assert counts["get_config"] == 1
    assert counts["a.b.c"] == 1
    assert counts["end"] == 1
    # Legit '+' / '#' endings still preserved; internal '.' still allowed.
    assert counts["v1.2.3"] == 1
    assert counts["c++"] == 1
    assert counts["c#"] == 1
    # No polluted variants leak through.
    assert "word." not in counts
    assert "end-" not in counts
    assert "a.b.c..." not in counts


def test_split_chunks_line_mode():
    text = "line one here\nline two here\n\nline four here"
    chunks = split_chunks(text, mode="line")
    assert len(chunks) == 4  # incl. the blank line
    assert chunks[0].start_line == 1 and chunks[0].end_line == 1
    assert chunks[2].char_count == 0  # blank line


def test_split_chunks_chars_mode_respects_min_max():
    # 10 lines of ~40 chars each => several multi-line chunks.
    lines = [f"sentence number {i} with some filler words here" for i in range(20)]
    text = "\n".join(lines)
    chunks = split_chunks(text, mode="chars", min_chars=100, max_chars=200)
    assert len(chunks) > 1
    for ch in chunks:
        # No chunk wildly exceeds max (allow overshoot by one line).
        assert ch.char_count <= 260


def test_split_chunks_chars_mode_never_splits_single_long_line():
    long_line = "x" * 5000
    chunks = split_chunks(long_line, mode="chars", min_chars=100, max_chars=200)
    assert len(chunks) == 1
    assert chunks[0].char_count == 5000  # oversized chunk allowed


def test_split_chunks_chars_mode_heading_boundary():
    text = (
        "# Title\n"
        "some intro text that is quite short\n"
        "## Section\n"
        "more content in the section here\n"
    )
    chunks = split_chunks(text, mode="chars", is_markdown=True, min_chars=500, max_chars=1000)
    # Headings force a cut even below min_chars: at least 2 chunks.
    assert len(chunks) >= 2
    # Each heading starts a fresh chunk.
    starts = [c.text.splitlines()[0] if c.text else "" for c in chunks]
    assert any(s.startswith("# Title") for s in starts)
    assert any(s.startswith("## Section") for s in starts)


def test_build_populates_chunk_tables(sidecar_env, tmp_path):
    scope = tmp_path / "corpus"
    _write_scope(scope)
    db = tmp_path / "graph.db"

    stats = build_graph_index(scope, db_path=db, min_pair_count=1)
    assert stats.files_scanned >= 3
    assert stats.files_indexed >= 3
    assert stats.chunks_written > 0
    assert stats.keyword_chunks_written > 0
    assert stats.pairs_written > 0
    assert stats.heading_docs_written > 0
    assert stats.md_files_indexed == 2

    conn = connect(db)
    try:
        kw_rows = conn.execute(
            "SELECT keyword, doc_path, occurrence_count FROM keyword_chunks "
            "ORDER BY doc_path, keyword"
        ).fetchall()
        assert kw_rows
        docs = {row["doc_path"] for row in kw_rows}
        assert "install.md" in docs
        assert "usage.md" in docs

        # chunk_cooccur pairs carry npmi.
        pair_rows = conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs "
            "WHERE source='chunk_cooccur' AND npmi IS NOT NULL"
        ).fetchone()[0]
        assert pair_rows > 0

        head_rows = conn.execute(
            "SELECT heading_path, doc_path FROM heading_docs"
        ).fetchall()
        paths = {row["heading_path"] for row in head_rows}
        assert any("Install" in p for p in paths)
        macos_docs = {
            row["doc_path"]
            for row in head_rows
            if row["heading_path"].endswith("macOS")
        }
        assert macos_docs == {"install.md", "usage.md"}
    finally:
        conn.close()


def test_replace_flag_clears_previous_chunk_rows(sidecar_env, tmp_path):
    scope = tmp_path / "corpus"
    _write_scope(scope)
    db = tmp_path / "graph.db"

    build_graph_index(scope, db_path=db, min_pair_count=1)
    (scope / "install.md").unlink()

    stats = build_graph_index(scope, db_path=db, min_pair_count=1)
    assert stats.files_indexed == 2  # usage.md + code.py

    conn = connect(db)
    try:
        remaining = [
            row["doc_path"]
            for row in conn.execute("SELECT DISTINCT doc_path FROM chunks").fetchall()
        ]
        assert "install.md" not in remaining
        # chunk_cooccur pairs for the removed doc's exclusive keywords cleared.
        kw_remaining = [
            row["doc_path"]
            for row in conn.execute("SELECT DISTINCT doc_path FROM keyword_chunks").fetchall()
        ]
        assert "install.md" not in kw_remaining
    finally:
        conn.close()


def test_build_preserves_llm_promoted_pairs(sidecar_env, tmp_path):
    """``build`` clears only ``chunk_cooccur`` rows; ``llm_promoted`` survives."""
    scope = tmp_path / "corpus"
    _write_scope(scope)
    db = tmp_path / "graph.db"

    from grga.graph.schema import LLM_PROMOTED_SOURCE, ensure_schema

    build_graph_index(scope, db_path=db, min_pair_count=1)

    # Insert an llm_promoted pair for this scope.
    conn = connect(db)
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO keyword_pairs (scope, directory, keyword_a, keyword_b, "
            "source, cooccur_count, npmi, updated_at) "
            "VALUES ('', ?, 'llmx', 'llmy', ?, 5, NULL, '2026-01-01')",
            (str(scope.resolve()), LLM_PROMOTED_SOURCE),
        )
        conn.commit()
    finally:
        conn.close()

    # Rebuild: chunk_cooccur is replaced, llm_promoted preserved.
    build_graph_index(scope, db_path=db, min_pair_count=1)

    conn = connect(db)
    try:
        survived = conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs WHERE source=? "
            "AND keyword_a='llmx' AND keyword_b='llmy'",
            (LLM_PROMOTED_SOURCE,),
        ).fetchone()[0]
        assert survived == 1
    finally:
        conn.close()


def test_v1_counts_not_touched_by_build(sidecar_env, tmp_path):
    scope = tmp_path / "corpus"
    _write_scope(scope)
    db = tmp_path / "graph.db"

    from grga.deep_search import KeywordLibrary

    lib = KeywordLibrary(db)
    lib.add_keywords("global", str(scope), ["alpha", "beta"])
    lib.commit()
    lib.close()

    build_graph_index(scope, db_path=db, min_pair_count=1)

    conn = connect(db)
    try:
        counts = conn.execute(
            "SELECT COUNT(*) FROM keyword_counts WHERE term IN ('alpha', 'beta')"
        ).fetchone()[0]
        assert counts == 2
    finally:
        conn.close()
