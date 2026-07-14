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
    """Post-v9 (Phase 3b-3): build populates keyword_docs (doc-level) +
    keyword_pairs + heading_docs. ``keyword_chunks_written`` on
    :class:`BuildStats` is retained as the doc-level row count for JSON
    contract stability. ``chunks_written`` stays populated by the pre-v9
    accounting but the underlying ``chunks`` table is gone; the count now
    reflects "chunks processed in RAM during pass 2" — a stat, not a table
    row count.
    """
    scope = tmp_path / "corpus"
    _write_scope(scope)
    db = tmp_path / "graph.db"

    stats = build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)
    assert stats.files_scanned >= 3
    assert stats.files_indexed >= 3
    # v9: chunks_written may be 0 (no chunks table in v9); the meaningful
    # signal is keyword_docs population.
    assert stats.keyword_chunks_written > 0
    assert stats.pairs_written > 0
    assert stats.heading_docs_written > 0
    assert stats.md_files_indexed == 2

    conn = connect(db)
    try:
        kw_rows = conn.execute(
            "SELECT k.kw AS keyword, d.doc_path AS doc_path, "
            "       kd.occurrence_count AS occurrence_count "
            "FROM keyword_docs kd "
            "JOIN keywords k ON k.id = kd.kw_id "
            "JOIN docs d ON d.id = kd.doc_id "
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

    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)
    (scope / "install.md").unlink()

    stats = build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)
    assert stats.files_indexed == 2  # usage.md + code.py

    conn = connect(db)
    try:
        remaining = [
            row["doc_path"]
            for row in conn.execute(
                "SELECT DISTINCT d.doc_path AS doc_path "
                "FROM keyword_docs kd JOIN docs d ON d.id = kd.doc_id"
            ).fetchall()
        ]
        assert "install.md" not in remaining
        # keyword_docs rows for the removed doc's keywords cleared.
        kw_remaining = [
            row["doc_path"]
            for row in conn.execute(
                "SELECT DISTINCT d.doc_path AS doc_path "
                "FROM keyword_docs kd JOIN docs d ON d.id = kd.doc_id"
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

    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)

    # Insert an llm_promoted pair for this scope.
    conn = connect(db)
    try:
        ensure_schema(conn)
        interner = IdInterner(conn)
        scope_id = interner.scope_id("test-scope")
        kw_a = interner.kw_id("llmx")
        kw_b = interner.kw_id("llmy")
        conn.execute(
            "INSERT INTO keyword_pairs (scope_id, kw_a_id, kw_b_id, "
            "source, cooccur_count, npmi, updated_at) "
            "VALUES (?, ?, ?, ?, 5, NULL, '2026-01-01')",
            (scope_id, kw_a, kw_b, LLM_PROMOTED_SOURCE),
        )
        conn.commit()
    finally:
        conn.close()

    # Rebuild: chunk_cooccur is replaced, llm_promoted preserved.
    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)

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
    lib.add_keywords("global", "test-scope", ["alpha", "beta"])
    lib.commit()
    lib.close()

    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)

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


def test_detect_ngram_phrases_salvages_substring():
    """苍坤上人 kept by all three gates; 苍坤 fails right-entropy but is
    salvaged because it is a substring of 苍坤上人 and has enough count.
    """
    from grga.graph.build import detect_ngram_phrases
    # Char-level tokens (as jieba would emit for unknown compounds). 苍坤
    # is followed by 上 in every 3-gram occurrence bar one, so its right
    # entropy is far below 1.0 nat and must be rescued by the substring
    # salvage, not by the entropy gate. Left contexts around 苍坤上人 are
    # varied so the 4-gram itself passes branch entropy.
    def line(prefix: str, tail: str) -> list:
        return list(prefix) + list("苍坤上人") + list(tail)
    lines = [
        line("师父", "开示"),
        line("拜访", "讲经"),
        line("寺中", "闭关"),
        line("徒弟", "接引"),
        line("山门", "亲迎"),
        # One line where 苍坤 stands alone (short-form reference) — feeds
        # 苍坤's independent count without touching 上人.
        list("苍坤") + list("早年游学"),
    ]
    out = detect_ngram_phrases(lines, max_n=4, min_count=3)
    phrases = {e["phrase"] for e in out}
    assert "苍坤上人" in phrases, f"long form should pass all gates: {phrases}"
    assert "苍坤" in phrases, f"substring salvage missed 苍坤: {phrases}"
    # Salvaged entry keeps its real (sub-threshold) branch_ent so caller
    # can distinguish it from a natively-passing phrase.
    salvaged = next(e for e in out if e["phrase"] == "苍坤")
    assert salvaged["branch_ent"] < 1.0, salvaged


def test_detect_ngram_phrases_salvages_infix_not_only_edges():
    """Salvage rule must accept infix substrings, not just prefix/suffix."""
    from grga.graph.build import detect_ngram_phrases
    # Long form 甲乙丙丁 with varied left AND right contexts.
    lines = [
        list("张甲乙丙丁一"),
        list("李甲乙丙丁二"),
        list("王甲乙丙丁三"),
        list("赵甲乙丙丁四"),
        list("钱甲乙丙丁五"),
        # A few lines that expose 乙丙 without 甲 on the left / 丁 on the
        # right, so 乙丙 still fails entropy (dominated by the compound
        # occurrences) but has real standalone count for salvage.
        list("独乙丙出"),
        list("孤乙丙立"),
    ]
    out = detect_ngram_phrases(
        lines, max_n=4, min_count=3, ent_threshold=0.5,
    )
    phrases = {e["phrase"] for e in out}
    assert "甲乙丙丁" in phrases, phrases
    # 乙丙 is an INFIX of 甲乙丙丁 — must be salvaged by substring rule.
    assert "乙丙" in phrases, f"infix salvage failed: {phrases}"


def test_detect_ngram_phrases_drops_zero_margin_boundary_fragments():
    """Salvage tightened: boundary fragments with margin=0 must be dropped.

    苍坤上 and 坤上人 are 3-gram slices of 苍坤上人 that share its exact
    count — every occurrence of either fragment is *inside* a 苍坤上人
    hit, so the independence margin ``count(sub) - max(count(container))``
    is zero. They carry no independent co-occurrence evidence and only
    bloat the vocab / pair tables if admitted. The tightened salvage rule
    must let 苍坤上人 through (all three gates), let 苍坤 through (real
    standalone occurrence in one line → margin ≥ 1), and reject both
    boundary fragments.
    """
    from grga.graph.build import detect_ngram_phrases

    def line(prefix: str, tail: str) -> list:
        return list(prefix) + list("苍坤上人") + list(tail)

    lines = [
        line("师父", "开示"),
        line("拜访", "讲经"),
        line("寺中", "闭关"),
        line("徒弟", "接引"),
        line("山门", "亲迎"),
        # One line where 苍坤 stands alone — gives 苍坤 an independent count
        # of 1. 苍坤上 and 坤上人 never appear without the full 4-gram.
        list("苍坤") + list("早年游学"),
    ]
    out = detect_ngram_phrases(lines, max_n=4, min_count=3)
    phrases = {e["phrase"] for e in out}
    assert "苍坤上人" in phrases, phrases
    assert "苍坤" in phrases, f"legit substring dropped: {phrases}"
    # These must NOT survive — no independence margin.
    assert "苍坤上" not in phrases, (
        f"boundary fragment 苍坤上 salvaged despite margin=0: {phrases}"
    )
    assert "坤上人" not in phrases, (
        f"boundary fragment 坤上人 salvaged despite margin=0: {phrases}"
    )


def test_detect_ngram_phrases_salvages_via_multiple_containers():
    """Independence via *different* containers must still count.

    A substring reached through two distinct containers (苍坤 in both
    苍坤上人 and 苍坤真人) clears the margin even with zero fully-standalone
    hits: combining with several neighbours is itself independence
    evidence. This distinguishes "hub of a small compound family" from
    "pure edge fragment glued to exactly one neighbour".
    """
    from grga.graph.build import detect_ngram_phrases

    def with_container(prefix: str, tail: str, container: str) -> list:
        return list(prefix) + list(container) + list(tail)

    lines = [
        # 3 occurrences of 苍坤上人 with varied L/R context.
        with_container("师父", "开示", "苍坤上人"),
        with_container("拜访", "讲经", "苍坤上人"),
        with_container("寺中", "闭关", "苍坤上人"),
        # 3 occurrences of 苍坤真人 with varied L/R context.
        with_container("门徒", "参礼", "苍坤真人"),
        with_container("客卿", "拜见", "苍坤真人"),
        with_container("弟子", "叩首", "苍坤真人"),
    ]
    out = detect_ngram_phrases(lines, max_n=4, min_count=3)
    phrases = {e["phrase"] for e in out}
    # Both long forms should pass all three gates.
    assert "苍坤上人" in phrases, phrases
    assert "苍坤真人" in phrases, phrases
    # 苍坤 appears 6× total, 3× inside each container. Max container
    # count is 3, so margin = 6 - 3 = 3 ≥ 1 → salvaged.
    assert "苍坤" in phrases, (
        f"substring across multiple containers should salvage: {phrases}"
    )


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
    stats = build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0, ngram_iters=2)
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
    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)

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
    stats = build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0, ngram_iters=3)
    assert stats.ngram_iters_run == 3


# ---------------------------------------------------------------------------
# v8 keyword-primary asset population (Phase 2, plan-2026-07-12-keyword-primary.md).
# ---------------------------------------------------------------------------


def test_build_populates_keyword_stats(sidecar_env, tmp_path):
    """Every build must materialise keyword_stats + scope_stats for the scope.

    v9 (Phase 3b-3): keyword_stats is now derived from ``keyword_docs``
    (doc-level inverted index) — the pre-v9 chunk-level ``keyword_chunks``
    is gone. The correctness invariant becomes ``keyword_stats.chunk_df ==
    SUM(keyword_docs.chunk_count)`` per (dir, kw), and
    ``scope_stats.n_chunks == SUM(docs.n_chunks_with_kw)`` per dir.
    """
    scope = tmp_path / "corpus"
    _write_scope(scope)
    db = tmp_path / "graph.db"

    stats = build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)
    assert stats.keyword_stats_written > 0, \
        "build must populate keyword_stats"

    conn = connect(db)
    try:
        # keyword_stats derived aggregate should equal the hand-computed
        # aggregate from keyword_docs (scope_id, kw_id):
        #   chunk_df = SUM(chunk_count)
        #   doc_df   = COUNT(*)
        #   total_occurrences = SUM(occurrence_count)
        expected = {
            (int(row[0]), int(row[1])): (int(row[2]), int(row[3]), int(row[4]))
            for row in conn.execute(
                "SELECT scope_id, kw_id, "
                "       COALESCE(SUM(chunk_count), 0), COUNT(*), "
                "       COALESCE(SUM(occurrence_count), 0) "
                "FROM keyword_docs "
                "GROUP BY scope_id, kw_id"
            )
        }
        got = {
            (int(row[0]), int(row[1])): (int(row[2]), int(row[3]), int(row[4]))
            for row in conn.execute(
                "SELECT scope_id, kw_id, chunk_df, doc_df, total_occurrences "
                "FROM keyword_stats"
            )
        }
        assert got == expected, "keyword_stats drifted from keyword_docs"

        # scope_stats.n_chunks = SUM(docs.n_chunks_with_kw) per scope.
        for row in conn.execute(
            "SELECT s.scope_id, s.n_chunks, "
            "       (SELECT COALESCE(SUM(d.n_chunks_with_kw), 0) FROM docs d "
            "        WHERE d.scope_id = s.scope_id) "
            "FROM scope_stats s"
        ):
            assert row[1] == row[2], \
                f"scope_stats.n_chunks {row[1]} != SUM(docs.n_chunks_with_kw) {row[2]}"
    finally:
        conn.close()


def test_build_write_time_gate_default_filters_low_score_pairs(sidecar_env, tmp_path):
    """CLI-equivalent build defaults (min_pair_count=5, min_npmi=0.2) must
    admit only pairs clearing BOTH gates."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # 10 identical lines pairing alpha+beta strongly (co=10, npmi high).
    # A 'noise' pair (alpha+gamma) appears once — should be excluded.
    lines = ["alpha beta stayed together" for _ in range(10)]
    lines.append("alpha gamma noise once")
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"

    # Explicit production-default settings.
    from grga.graph.schema import BUILD_MIN_NPMI, BUILD_MIN_PAIR_COUNT
    stats = build_graph_index("test-scope", directory=scope, db_path=db,
        min_pair_count=BUILD_MIN_PAIR_COUNT,
        min_npmi=BUILD_MIN_NPMI,
    )

    conn = connect(db)
    try:
        surviving = list(conn.execute(
            "SELECT ka.kw, kb.kw, p.cooccur_count, p.npmi "
            "FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id "
            "WHERE p.source='chunk_cooccur'"
        ))
        kws_involved = {row[0] for row in surviving} | {row[1] for row in surviving}
        assert "gamma" not in kws_involved, \
            "gamma pair (co=1) must be excluded by min_pair_count>=5"
        for row in surviving:
            assert row[2] >= BUILD_MIN_PAIR_COUNT
            assert row[3] is not None and row[3] >= BUILD_MIN_NPMI
    finally:
        conn.close()


def test_build_min_npmi_opt_out(sidecar_env, tmp_path):
    """min_npmi=0.0 disables the read-side floor — pairs with any real npmi survive.

    Note: chunk_cooccur rows with mathematically undefined npmi (p_ab=1,
    i.e. a pair covering every chunk) are dropped regardless of the floor
    — that's a build-time invariant. Mix a solo chunk in to keep p_ab<1.
    """
    scope = tmp_path / "corpus"
    scope.mkdir()
    lines = ["alpha beta seen"] * 6 + ["alpha solo"]
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    stats = build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=5, min_npmi=0.0,
    )
    assert stats.pairs_written > 0, "min_npmi=0.0 must admit pairs with defined npmi"


def test_build_min_pair_count_opt_out(sidecar_env, tmp_path):
    """min_pair_count=1 admits pairs from tiny corpora (test-mode use).

    Fixtures add a solo chunk per word so the pairs are *independent*
    (co < max(df_a, df_b)) rather than glued — glued pairs (co == df_a ==
    df_b) are dropped by the parameter-free independence gate regardless
    of min_pair_count. See resolve_min_pair_count / _finalize_pairs_from_temp.
    """
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "doc.txt").write_text(
        "alpha beta\nalpha solo\ngamma delta\ngamma solo\n", encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    stats = build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0,
    )
    # (alpha,beta) co=1 df_a=2 df_b=1 → independent; (gamma,delta) likewise.
    # (alpha,solo)/(gamma,solo) also independent. Assert the two concept
    # pairs survived without over-constraining incidental solo pairs.
    conn = connect(db)
    try:
        surviving = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT ka.kw, kb.kw FROM keyword_pairs p "
                "JOIN keywords ka ON ka.id = p.kw_a_id "
                "JOIN keywords kb ON kb.id = p.kw_b_id "
                "WHERE p.source='chunk_cooccur'"
            )
        }
    finally:
        conn.close()
    flat = {frozenset(pair) for pair in surviving}
    assert frozenset({"alpha", "beta"}) in flat
    assert frozenset({"gamma", "delta"}) in flat


# --------------------------------------------------------------------------- #
# v3.2 adaptive write-time gates (independence + scope-adaptive floor).       #
# See docs/design/plan-2026-07-13-adaptive-build-gates.md (design)            #
# + src/grga/graph/build.py::resolve_min_pair_count.                          #
# --------------------------------------------------------------------------- #


def test_resolve_min_pair_count_ladder_boundaries():
    """The step ladder is defined at explicit chunk-count boundaries."""
    from grga.graph.build import resolve_min_pair_count

    assert resolve_min_pair_count(0) == 2       # empty/degenerate
    assert resolve_min_pair_count(1) == 2
    assert resolve_min_pair_count(200) == 2     # inclusive upper bound
    assert resolve_min_pair_count(201) == 3     # step
    assert resolve_min_pair_count(2000) == 3
    assert resolve_min_pair_count(2001) == 4    # step
    assert resolve_min_pair_count(20000) == 4
    assert resolve_min_pair_count(20001) == 5   # step (pre-v3.2 fixed)
    assert resolve_min_pair_count(10**7) == 5


def test_independence_gate_drops_fully_glued_pairs(sidecar_env, tmp_path):
    """Two words that always co-occur (co == df_a == df_b) score npmi=1.0
    as an artefact, not signal. The parameter-free independence gate must
    drop them regardless of min_pair_count."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # 5 chunks, each containing exactly "alpha beta". No solo chunk for
    # either — they are fully glued (df_alpha = df_beta = co = 5).
    (scope / "doc.txt").write_text(
        "\n".join(["alpha beta"] * 5), encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)

    conn = connect(db)
    try:
        pairs = list(conn.execute(
            "SELECT ka.kw, kb.kw FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id "
            "WHERE p.source='chunk_cooccur'"
        ))
    finally:
        conn.close()
    assert pairs == [], (
        f"fully-glued (alpha,beta) must be dropped by independence gate; "
        f"got {pairs}"
    )


def test_independence_gate_keeps_one_way_attachment(sidecar_env, tmp_path):
    """A rare word that always appears alongside a common one — as long as
    the common one has any independent chunk — is a *real* strong link and
    must survive. Gate 1 uses max(df_a, df_b), not min, precisely to keep
    this case."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # beta only appears alongside alpha (df_beta = 3, co = 3), but alpha
    # also appears in a solo chunk (df_alpha = 4). max(df_a, df_b) = 4 > co=3
    # → not glued → survives.
    (scope / "doc.txt").write_text(
        "\n".join(["alpha beta"] * 3 + ["alpha solo"]),
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)

    conn = connect(db)
    try:
        pairs = {
            frozenset({row[0], row[1]})
            for row in conn.execute(
                "SELECT ka.kw, kb.kw FROM keyword_pairs p "
                "JOIN keywords ka ON ka.id = p.kw_a_id "
                "JOIN keywords kb ON kb.id = p.kw_b_id "
                "WHERE p.source='chunk_cooccur'"
            )
        }
    finally:
        conn.close()
    assert frozenset({"alpha", "beta"}) in pairs, (
        f"one-way attachment (beta only with alpha, alpha has solo) is "
        f"genuine signal and must survive independence gate; got {pairs}"
    )


def test_adaptive_min_pair_count_none_resolves_from_n_chunks(sidecar_env, tmp_path):
    """Passing ``min_pair_count=None`` (or omitting it) triggers the
    scope-adaptive ladder — small scopes get a lower floor than fixed 5.

    A ~10-chunk corpus resolves to floor=2 via
    :func:`resolve_min_pair_count`; a pair with co=2 must survive the
    adaptive default even though the pre-v3.2 fixed floor (5) would drop it.
    """
    scope = tmp_path / "corpus"
    scope.mkdir()
    # Two "alpha beta" chunks + solo chunks for independence + filler so
    # the scope stays small (<=200 chunks → floor=2).
    lines = ["alpha beta", "alpha beta", "alpha solo", "beta solo"]
    lines += [f"filler word {i}" for i in range(4)]
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    # Explicit None to exercise the sentinel path.
    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=None, min_npmi=0.0)

    conn = connect(db)
    try:
        pairs = list(conn.execute(
            "SELECT ka.kw, kb.kw, p.cooccur_count "
            "FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id "
            "WHERE p.source='chunk_cooccur'"
        ))
    finally:
        conn.close()
    ab = [(a, b, co) for (a, b, co) in pairs if {a, b} == {"alpha", "beta"}]
    assert ab, (
        f"adaptive floor (n_chunks<=200 → floor=2) must admit co=2 pair; "
        f"got {pairs}"
    )
    assert ab[0][2] == 2


def test_independence_gate_survives_incremental_grow_only_accumulation(
    sidecar_env, tmp_path
):
    """Strict three-way-equality Gate 1 tolerates incremental grow-only skew.

    In ``mode=build`` the ``cooccur_count`` is grow-only (design D6, upsert
    with ``+= excluded.cooccur_count``) while ``chunk_df`` is recomputed
    fresh every build. After a doc update this can produce
    ``co > min(df_a, df_b)`` for a *legitimate* one-way attachment pair.
    A ``co >= max(df_a, df_b)`` Gate 1 would misfire and delete such a
    pair; strict ``df_a == df_b == co`` does not.

    Fixture designed so that after the incremental rebuild:

    * ``co`` accumulates to a value equal to ``max(df_a, df_b)`` — the
      exact bug trigger for a ``co >= max`` Gate 1 (which would drop).
    * ``co`` still stays below ``n_chunks`` so ``_npmi`` remains defined
      (does not hit ``p_ab >= 1.0`` degenerate).
    * The three-way equality condition (``df_a == df_b == co``) is *not*
      met, so strict Gate 1 keeps the pair.

    Numbers: doc a has 4 "alpha beta" + 1 "alpha only" chunks. Doc b has
    3 "alpha only" + 3 "gamma delta" filler. Total n_chunks=11. Build 1:
    co(alpha,beta)=4, df_alpha=8, df_beta=4. Build 2 rewrites a with same
    shape, co accumulates to 4+4=8. df_alpha=8, df_beta=4, n_chunks=11.
    Old ``co >= max``: 8 >= 8 → dropped (bug). Strict ``df_a == df_b ==
    co``: 8 == 4? no → survives.
    """
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "a.md").write_text(
        "\n".join(["alpha beta"] * 4 + ["alpha only"] * 1),
        encoding="utf-8",
    )
    (scope / "b.md").write_text(
        "\n".join(["alpha only"] * 3 + ["gamma delta"] * 3),
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    build_graph_index("test-scope", directory=scope, db_path=db, mode="rebuild")

    # Sanity: build 1 admits the pair.
    def _pair_row():
        c = connect(db)
        try:
            row = c.execute(
                "SELECT p.cooccur_count "
                "FROM keyword_pairs p "
                "JOIN keywords ka ON ka.id = p.kw_a_id "
                "JOIN keywords kb ON kb.id = p.kw_b_id "
                "WHERE p.source='chunk_cooccur' "
                "AND ((ka.kw='alpha' AND kb.kw='beta') "
                "  OR (ka.kw='beta' AND kb.kw='alpha'))"
            ).fetchone()
            return row["cooccur_count"] if row else None
        finally:
            c.close()

    co_after_build1 = _pair_row()
    assert co_after_build1 == 4, f"build 1 co: {co_after_build1}"

    # Rewrite doc a with different content (new hash → re-indexed) but
    # same chunk shape. Sleep to make sure mtime differs.
    import time
    time.sleep(0.05)
    (scope / "a.md").write_text(
        "\n".join(["alpha beta v2"] * 4 + ["alpha only v2"] * 1),
        encoding="utf-8",
    )
    build_graph_index("test-scope", directory=scope, db_path=db, mode="build", refresh_ngram="never")

    co_after_build2 = _pair_row()
    # Under strict Gate 1 the pair survives despite the grow-only skew
    # (co=8 == max(df)=8 but df_a != df_b, so not 3-way equal → not glued).
    # Under the pre-fix ``co >= max`` rule the pair would have been dropped.
    assert co_after_build2 is not None, (
        "Gate 1 must not misfire on one-way attachment after incremental "
        "accumulation; the (alpha,beta) pair should survive."
    )
    # Sanity: cooccur grew (grow-only invariant).
    assert co_after_build2 > co_after_build1, (
        f"co should grow across incremental builds; "
        f"build1={co_after_build1} build2={co_after_build2}"
    )
