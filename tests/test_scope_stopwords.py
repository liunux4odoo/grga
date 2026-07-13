"""Scope-local stopword derivation + pair-time containment filter (3a/3b).

Design origin: 2026-07-13 conversation on 者/性/化 leakage into keyword_pairs.
Global static stopword tables cannot express "same char, different corpus,
different semantics" (氯 in a chemistry scope vs 化 in generic docs vs 化劲 in
a 太极拳 tutorial). So we:

* 3a — derive a per-scope *candidate* set from single-CJK-char chunk-df.
* 3b — at pair enumeration, only drop a candidate when *this chunk* also
  contains a longer kw that has it as substring (structural evidence that
  the standalone char is a tokenisation artefact).
* keyword_stats keeps raw counts — filtering only affects keyword_pairs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.build import (
    STOPWORD_DF_THRESHOLD,
    STOPWORD_MIN_CHUNKS,
    _derive_scope_stopword_candidates,
    _filter_chunk_kws_for_pairs,
    _is_single_cjk,
    build_graph_index,
)
from grga.graph.schema import connect


# ---------------------------------------------------------------------------
# _is_single_cjk
# ---------------------------------------------------------------------------

def test_is_single_cjk_accepts_single_cjk_char():
    assert _is_single_cjk("者") is True
    assert _is_single_cjk("氯") is True
    assert _is_single_cjk("化") is True


def test_is_single_cjk_rejects_multi_char_cjk():
    assert _is_single_cjk("面壁者") is False
    assert _is_single_cjk("氯化钠") is False


def test_is_single_cjk_rejects_ascii():
    assert _is_single_cjk("a") is False
    assert _is_single_cjk("cli") is False
    assert _is_single_cjk("") is False


def test_is_single_cjk_rejects_non_cjk_unicode():
    assert _is_single_cjk("あ") is False  # Hiragana
    assert _is_single_cjk("한") is False  # Hangul


# ---------------------------------------------------------------------------
# _derive_scope_stopword_candidates
# ---------------------------------------------------------------------------

def test_derive_empty_chunk_tokens_returns_empty_set():
    assert _derive_scope_stopword_candidates({}) == frozenset()


def test_derive_small_scope_short_circuits_to_empty_set():
    # Below STOPWORD_MIN_CHUNKS, chunk-df ratio has no statistical power.
    # 者 appears in every chunk but the small-scope guard bails out first.
    n = STOPWORD_MIN_CHUNKS - 1
    chunk_tokens = {(1, i): ["者", "计划"] for i in range(n)}
    result = _derive_scope_stopword_candidates(chunk_tokens)
    assert result == frozenset()


def test_derive_small_scope_can_be_overridden_via_min_chunks():
    # The unit-test variants below deliberately relax min_chunks to focus
    # on the df logic. Guard behaviour is covered by the test above.
    chunk_tokens = {(1, i): ["者"] for i in range(5)}
    result = _derive_scope_stopword_candidates(chunk_tokens, min_chunks=1)
    assert "者" in result


def test_derive_flags_high_df_single_cjk_char():
    # "者" appears in 4 of 5 chunks (df=0.8, > 0.15 threshold).
    chunk_tokens = {
        (1, 1): ["面壁者", "罗辑", "者"],
        (1, 2): ["者", "计划"],
        (1, 3): ["者", "战略"],
        (1, 4): ["者", "破壁人"],
        (1, 5): ["无关词", "另一词"],
    }
    result = _derive_scope_stopword_candidates(chunk_tokens, min_chunks=1)
    assert "者" in result


def test_derive_ignores_low_df_single_cjk_char():
    # "氯" appears in 1 of 10 chunks (df=0.10, ≤ 0.15 threshold).
    chunk_tokens = {(1, i): ["填充词"] for i in range(1, 10)}
    chunk_tokens[(1, 10)] = ["氯", "钠"]
    result = _derive_scope_stopword_candidates(chunk_tokens, min_chunks=1)
    assert "氯" not in result


def test_derive_ignores_multi_char_tokens_even_if_ubiquitous():
    # "面壁者" appears in every chunk but is multi-char → never in set.
    chunk_tokens = {
        (1, i): ["面壁者", "内容"] for i in range(1, 20)
    }
    result = _derive_scope_stopword_candidates(chunk_tokens, min_chunks=1)
    assert "面壁者" not in result


def test_derive_ignores_ascii_tokens_even_if_ubiquitous():
    chunk_tokens = {(1, i): ["a", "the"] for i in range(1, 20)}
    result = _derive_scope_stopword_candidates(chunk_tokens, min_chunks=1)
    assert "a" not in result
    assert "the" not in result


def test_derive_respects_custom_threshold():
    # df=0.5 for "者" — filter should skip it if threshold=0.6.
    chunk_tokens = {
        (1, 1): ["者"],
        (1, 2): ["者"],
        (1, 3): ["其他"],
        (1, 4): ["其他"],
    }
    aggressive = _derive_scope_stopword_candidates(
        chunk_tokens, df_threshold=0.4, min_chunks=1,
    )
    conservative = _derive_scope_stopword_candidates(
        chunk_tokens, df_threshold=0.6, min_chunks=1,
    )
    assert "者" in aggressive
    assert "者" not in conservative


def test_derive_uses_casefold():
    # ASCII case wouldn't matter (rejected anyway), but the code path folds
    # tokens before counting. Sanity: two casings of a CJK char (there are
    # none in practice) — just verify the fold call doesn't blow up.
    chunk_tokens = {(1, i): ["者"] for i in range(1, 10)}
    result = _derive_scope_stopword_candidates(chunk_tokens, min_chunks=1)
    assert "者" in result


# ---------------------------------------------------------------------------
# _filter_chunk_kws_for_pairs
# ---------------------------------------------------------------------------

def test_filter_drops_candidate_contained_by_longer_kw():
    kws = ["面壁者", "罗辑", "者"]
    result = _filter_chunk_kws_for_pairs(kws, stopword_candidates=frozenset({"者"}))
    assert "面壁者" in result
    assert "罗辑" in result
    assert "者" not in result


def test_filter_keeps_candidate_when_no_containing_kw_present():
    # 者 in the chunk but nothing longer contains it → keep it. Absent
    # structural evidence, we don't know if it's an artefact or a concept.
    kws = ["计划", "战略", "者"]
    result = _filter_chunk_kws_for_pairs(kws, stopword_candidates=frozenset({"者"}))
    assert "者" in result


def test_filter_keeps_non_candidate_always():
    # 氯 not in the candidate set — never filtered, even when 氯化钠 is
    # in the same chunk.
    kws = ["氯化钠", "氯", "钠"]
    result = _filter_chunk_kws_for_pairs(kws, stopword_candidates=frozenset())
    assert set(result) == {"氯化钠", "氯", "钠"}


def test_filter_no_candidates_returns_input_verbatim():
    kws = ["a", "b", "c"]
    result = _filter_chunk_kws_for_pairs(kws, stopword_candidates=frozenset())
    assert result == kws
    # And it's a copy, not the same list identity (safe for the caller).
    assert result is not kws


def test_filter_handles_multiple_candidates_and_containers():
    # Both 者 and 人 are candidates, both have containing kws in this chunk.
    kws = ["面壁者", "破壁人", "者", "人", "罗辑"]
    result = _filter_chunk_kws_for_pairs(
        kws, stopword_candidates=frozenset({"者", "人"}),
    )
    assert "者" not in result
    assert "人" not in result
    assert "面壁者" in result
    assert "破壁人" in result
    assert "罗辑" in result


def test_filter_candidate_survives_when_only_self_contains():
    # A candidate's own string is not counted as a "containing" kw.
    kws = ["者"]
    result = _filter_chunk_kws_for_pairs(kws, stopword_candidates=frozenset({"者"}))
    assert result == ["者"]


# ---------------------------------------------------------------------------
# Integration: end-to-end build with 3a+3b applied
# ---------------------------------------------------------------------------

@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    return tmp_path


def _read_pair_pairs(db_path: Path) -> set:
    """Return {(kw_a_text, kw_b_text)} for all chunk_cooccur pairs."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT ka.kw, kb.kw
            FROM keyword_pairs p
            JOIN keywords ka ON ka.id = p.kw_a_id
            JOIN keywords kb ON kb.id = p.kw_b_id
            WHERE p.source = 'chunk_cooccur'
            """
        ).fetchall()
        return {(r[0], r[1]) for r in rows}
    finally:
        conn.close()


def test_build_filters_high_df_char_when_contained(sidecar_env, tmp_path):
    """者 is high-df and 面壁者 is a discovered n-gram → 者 pairs vanish."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # Repeat body enough times for auto-ngram to discover 面壁者 (needs
    # min_count=3 and diverse left/right contexts). 者 also appears in
    # standalone form to test the leak.
    body = (
        "面壁者罗辑开始计划战略行动\n"
        "指挥官面壁者进入舰桥继续任务\n"
        "第二天面壁者看到地球外面情况\n"
        "船员面壁者登上舰桥执行巡逻\n"
        "士兵面壁者训练新兵作战方式\n"
        "者也参与其中执行任务安排\n"
        "者继续等待时机行动方案\n"
    )
    (scope / "santi.txt").write_text(body * 3, encoding="utf-8")

    db = tmp_path / "graph.db"
    stats = build_graph_index(
        scope, db_path=db, min_pair_count=1, min_npmi=0.0, ngram_iters=2,
    )

    # 面壁者 should be a discovered phrase.
    conn = connect(db)
    try:
        phrases = {r[0] for r in conn.execute("SELECT phrase FROM auto_ngram_dict")}
        # keyword_stats should still have 者 (not filtered).
        kw_rows = conn.execute(
            "SELECT k.kw FROM keyword_stats ks "
            "JOIN keywords k ON k.id = ks.kw_id"
        ).fetchall()
        kw_texts = {r[0] for r in kw_rows}
    finally:
        conn.close()

    assert "面壁者" in phrases
    assert "者" in kw_texts, "keyword_stats must keep raw counts (no filtering)"

    # But no keyword_pairs row should have 者 as an endpoint in chunks
    # where 面壁者 also appears (3b's containment rule).
    pairs = _read_pair_pairs(db)
    # A pair having 者 on either endpoint is only tolerated if it came from
    # a 者-only chunk. Given the body above, every 面壁者 chunk also has
    # 者 (if tokenisation kept 者 free) — those must be filtered out. 者-
    # only chunks may still contribute e.g. (者, 参与)/其他 — but those
    # pairs' *other* endpoint must not be 面壁者 (since 面壁者 not in
    # those chunks).
    for a, b in pairs:
        if a == "者" or b == "者":
            # This 者-pair must have come from a 者-only chunk, so its
            # partner must NOT be 面壁者 (which lived in the other chunks).
            other = b if a == "者" else a
            assert other != "面壁者", (
                f"expected no (者, 面壁者) pair after 3b filter, got {(a, b)}"
            )
    _ = stats  # touched to silence linters


def test_build_keeps_low_df_char_pairs(sidecar_env, tmp_path):
    """氯 is low-df in a small text — 3a shouldn't flag it, pairs stay.

    Note: 氯 must appear in at least two chunks (one alongside partners,
    one solo) so the independence gate (co < max(df_a, df_b)) does not
    drop its pairs as artefacts. A single glued co-occurrence would fail
    that gate regardless of the stopword filter under test.
    """
    scope = tmp_path / "corpus"
    scope.mkdir()
    # 氯 appears standalone plus alongside 氯化钠 / atomic-number context.
    # Filler chunks push its chunk-df ratio below the 3a stopword threshold.
    filler = "\n".join(f"填充内容{i} 段落信息" for i in range(20))
    body = (
        f"{filler}\n"
        "氯化钠是一种常见盐类化合物\n"
        "氯的原子序数是十七号元素\n"
        "氯 独立段落 提供独立性证据\n"
    )
    (scope / "chem.txt").write_text(body, encoding="utf-8")

    db = tmp_path / "graph.db"
    build_graph_index(scope, db_path=db, min_pair_count=1, min_npmi=0.0)

    pairs = _read_pair_pairs(db)
    # 氯 not high-df → not a candidate → its pairs (with 钠, 化合物, ...)
    # must survive. Assertion: at least one pair endpoint is 氯.
    kws_in_pairs = {kw for pair in pairs for kw in pair}
    assert "氯" in kws_in_pairs, (
        f"氯 pairs must survive when its chunk-df is below threshold; "
        f"pairs seen: {pairs}"
    )


def test_stopword_derived_emit_fires(sidecar_env, tmp_path):
    """Progress trace should include the derived candidate set."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    body = (
        "面壁者罗辑开始计划战略行动\n"
        "指挥官面壁者进入舰桥继续任务\n"
        "者也参与其中执行安排\n"
    )
    (scope / "santi.txt").write_text(body * 4, encoding="utf-8")

    events: list = []

    def on_event(payload):
        events.append(payload)

    db = tmp_path / "graph.db"
    build_graph_index(
        scope, db_path=db, min_pair_count=1, min_npmi=0.0,
        on_event=on_event,
    )

    stopword_events = [e for e in events if e.get("phase") == "stopword_derived"]
    assert len(stopword_events) == 1
    ev = stopword_events[0]
    assert ev["threshold"] == STOPWORD_DF_THRESHOLD
    assert "count" in ev
    assert "chars" in ev
    assert isinstance(ev["chars"], list)
