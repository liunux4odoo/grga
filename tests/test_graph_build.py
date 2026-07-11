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
            "SELECT k.kw AS keyword, d.doc_path AS doc_path, "
            "       kc.occurrence_count AS occurrence_count "
            "FROM keyword_chunks kc "
            "JOIN keywords k ON k.id = kc.kw_id "
            "JOIN docs d ON d.id = kc.doc_id "
            "ORDER BY d.doc_path, k.kw"
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
            "SELECT hd.heading_path AS heading_path, d.doc_path AS doc_path "
            "FROM heading_docs hd JOIN docs d ON d.id = hd.doc_id"
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
            for row in conn.execute(
                "SELECT DISTINCT d.doc_path AS doc_path "
                "FROM chunks c JOIN docs d ON d.id = c.doc_id"
            ).fetchall()
        ]
        assert "install.md" not in remaining
        # chunk_cooccur pairs for the removed doc's exclusive keywords cleared.
        kw_remaining = [
            row["doc_path"]
            for row in conn.execute(
                "SELECT DISTINCT d.doc_path AS doc_path "
                "FROM keyword_chunks kc JOIN docs d ON d.id = kc.doc_id"
            ).fetchall()
        ]
        assert "install.md" not in kw_remaining
    finally:
        conn.close()


def test_build_preserves_llm_promoted_pairs(sidecar_env, tmp_path):
    """``build`` clears only ``chunk_cooccur`` rows; ``llm_promoted`` survives."""
    scope = tmp_path / "corpus"
    _write_scope(scope)
    db = tmp_path / "graph.db"

    from grga.graph.ids import IdInterner
    from grga.graph.schema import LLM_PROMOTED_SOURCE, ensure_schema

    build_graph_index(scope, db_path=db, min_pair_count=1)

    # Insert an llm_promoted pair for this scope.
    conn = connect(db)
    try:
        ensure_schema(conn)
        interner = IdInterner(conn)
        dir_id = interner.dir_id(str(scope.resolve()))
        kw_a = interner.kw_id("llmx")
        kw_b = interner.kw_id("llmy")
        conn.execute(
            "INSERT INTO keyword_pairs (dir_id, kw_a_id, kw_b_id, "
            "source, cooccur_count, npmi, updated_at) "
            "VALUES (?, ?, ?, ?, 5, NULL, '2026-01-01')",
            (dir_id, kw_a, kw_b, LLM_PROMOTED_SOURCE),
        )
        conn.commit()
    finally:
        conn.close()

    # Rebuild: chunk_cooccur is replaced, llm_promoted preserved.
    build_graph_index(scope, db_path=db, min_pair_count=1)

    conn = connect(db)
    try:
        survived = conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id "
            "WHERE p.source = ? AND ka.kw = 'llmx' AND kb.kw = 'llmy'",
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
            "SELECT COUNT(*) FROM keyword_counts c "
            "JOIN keywords k ON k.id = c.kw_id "
            "WHERE k.kw IN ('alpha', 'beta')"
        ).fetchone()[0]
        assert counts == 2
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# Schema v6: hybrid tokeniser + auto n-gram detection tests
# ---------------------------------------------------------------------------

def test_tokenize_hybrid_preserves_ascii_code_idents():
    from grga.graph.build import _new_jieba_tokenizer, tokenize_hybrid
    tok = _new_jieba_tokenizer([])
    result = tokenize_hybrid(
        "word. foo-bar, get_config. a.b.c... end- v1.2.3 c++ c#", tok
    )
    # Every ASCII code identifier survives whole (same as pre-v6 regex).
    assert "foo-bar" in result
    assert "get_config" in result
    assert "a.b.c" in result
    assert "v1.2.3" in result
    assert "c++" in result
    assert "c#" in result
    # Trailing punctuation never sneaks in.
    assert not any(t.endswith(".") or t.endswith("-") for t in result)


def test_tokenize_hybrid_segments_cjk_via_jieba():
    from grga.graph.build import _new_jieba_tokenizer, tokenize_hybrid
    tok = _new_jieba_tokenizer([])
    result = tokenize_hybrid("章北海驾驶自然选择号", tok)
    # Without an auto dict jieba splits 章北海 -> ['章', '北海'].
    assert "章" in result
    assert "北海" in result
    # Function words are still emitted; kw filter drops them later.
    assert "驾驶" in result
    assert "自然选择" in result


def test_tokenize_hybrid_honours_seeded_phrases():
    from grga.graph.build import _new_jieba_tokenizer, tokenize_hybrid
    tok = _new_jieba_tokenizer(["章北海", "自然选择号"])
    result = tokenize_hybrid("章北海驾驶自然选择号", tok)
    assert "章北海" in result
    assert "自然选择号" in result
    assert "章" not in result  # jieba now folds it into 章北海
    assert "北海" not in result


def test_join_by_pair_rule_all_four_adjacencies():
    from grga.graph.build import _join_by_pair_rule
    # CJK + CJK -> concat
    assert _join_by_pair_rule(("章", "北海")) == "章北海"
    # CJK + ASCII -> concat (Chinese typography puts them flush)
    assert _join_by_pair_rule(("艾", "AA")) == "艾AA"
    # ASCII + CJK -> concat
    assert _join_by_pair_rule(("PDC", "主席")) == "PDC主席"
    # ASCII + ASCII -> single space
    assert _join_by_pair_rule(("nano", "center")) == "nano center"
    # Mixed 3-gram
    assert _join_by_pair_rule(("PDC", "主席", "说道")) == "PDC主席说道"
    assert _join_by_pair_rule(("水滴", "V", "型")) == "水滴V型"
    # 3 multi-char ASCII tokens -> two spaces (single-char ASCII never
    # occurs: the regex ASCII branch requires >= 2 chars).
    assert _join_by_pair_rule(("aa", "bb", "cc")) == "aa bb cc"


def test_merge_auto_phrases_requires_positional_adjacency():
    from grga.graph.build import _new_jieba_tokenizer, tokenize_hybrid_with_pos, merge_auto_phrases
    tok = _new_jieba_tokenizer([])
    phrases = {"艾AA"}
    # Adjacent in source: 艾AA (no gap) -> should merge
    pos = tokenize_hybrid_with_pos("维德对艾AA说", tok)
    merged = merge_auto_phrases(pos, phrases)
    assert "艾AA" in merged
    # Separated by punctuation in source: "艾, AA" -> must NOT merge
    pos2 = tokenize_hybrid_with_pos("艾, AA是谁", tok)
    merged2 = merge_auto_phrases(pos2, phrases)
    assert "艾AA" not in merged2
    assert "艾" in merged2 and "AA" in merged2


def test_detect_ngram_phrases_finds_repeated_compounds():
    from grga.graph.build import detect_ngram_phrases
    # Both left AND right contexts must vary so branch_entropy >= threshold.
    lines = [
        ["指挥官", "章", "北海", "驾驶", "飞船"],
        ["士兵", "章", "北海", "看到", "地球"],
        ["船员", "章", "北海", "登上", "舰桥"],
        ["船长", "章", "北海", "开始", "训练"],
        ["少校", "章", "北海", "命令", "开火"],
    ]
    out = detect_ngram_phrases(lines, max_n=3, ent_threshold=0.5)
    phrases = {e["phrase"] for e in out}
    assert "章北海" in phrases, f"expected phrase not detected: {phrases}"


def test_detect_ngram_phrases_bounded_npmi():
    from grga.graph.build import detect_ngram_phrases
    # Perfect co-occurrence corpus — NPMI must stay in [-1, 1] for ALL n.
    lines = [["甲", "乙", "丙"] for _ in range(5)]
    out = detect_ngram_phrases(
        lines, max_n=3, min_count=3,
        npmi_threshold=-1.0, ent_threshold=-1.0,
    )
    for entry in out:
        assert -1.0001 <= entry["npmi"] <= 1.0001, entry


def test_build_populates_auto_ngram_dict(sidecar_env, tmp_path):
    """End-to-end: enough repeated CJK compounds should land in auto_ngram_dict."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # Build lines with 5 different left contexts and 5 different right contexts
    # for "章北海" so both entropy sides clear the 1.0 nat gate.
    body = (
        "章北海驾驶飞船穿过战场\n"
        "指挥官章北海命令开火射击\n"
        "第二天章北海看到地球外面\n"
        "船员章北海登上舰桥继续巡逻\n"
        "士兵章北海开始训练新兵作战\n"
    )
    (scope / "santi.txt").write_text(body * 3, encoding="utf-8")
    db = tmp_path / "graph.db"
    stats = build_graph_index(scope, db_path=db, min_pair_count=1, ngram_iters=2)
    assert stats.ngram_iters_run == 2
    assert stats.ngram_phrases_written >= 1

    conn = connect(db)
    try:
        phrases = {r[0] for r in conn.execute(
            "SELECT phrase FROM auto_ngram_dict")}
    finally:
        conn.close()
    assert "章北海" in phrases, f"expected 章北海 in dict, got {sorted(phrases)}"


def test_build_hybrid_keeps_code_identifiers(sidecar_env, tmp_path):
    """Ensure the mainline switch to hybrid did not regress code search."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "code.py").write_text(
        "def foo_bar():\n"
        "    return v1.2.3\n\n"
        "def get_config():\n"
        "    self.x = 'utf-8'\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    build_graph_index(scope, db_path=db, min_pair_count=1)

    conn = connect(db)
    try:
        kws = {r[0] for r in conn.execute("SELECT kw FROM keywords")}
    finally:
        conn.close()
    for ident in ("foo_bar", "v1.2.3", "get_config", "utf-8", "self.x"):
        assert ident in kws, f"code identifier {ident!r} lost after hybrid switch"


def test_build_ngram_iters_respected(sidecar_env, tmp_path):
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "a.txt").write_text("hello world\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    stats = build_graph_index(scope, db_path=db, min_pair_count=1, ngram_iters=3)
    assert stats.ngram_iters_run == 3
