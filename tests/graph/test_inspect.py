"""``grga graph inspect`` / ``inspect_keyword`` coverage.

Exercises the three shapes from ``MIGRATION_user_scoped_db.md`` §4 (修正 4):

* single-scope keyword -> no polysemy,
* cross-scope keyword whose neighbours agree -> no polysemy,
* cross-scope keyword whose neighbours diverge -> suspected polysemy.
"""
from __future__ import annotations

import pytest

from grga.graph.build import build_graph_index
from grga.graph.inspect import inspect_keyword


@pytest.fixture
def polysemy_db(monkeypatch, tmp_path):
    """One shared DB with a clean keyword and a polysemous keyword.

    ``transformer`` (clean, single meaning) always co-occurs with ML terms.
    ``attention`` is polysemous: in scope ``nlp`` it co-occurs with ML terms,
    in scope ``psych`` it co-occurs with an entirely different vocabulary.
    ``shared`` co-occurs with the *same* neighbour vocabulary in both scopes
    (cross-scope but not polysemous).
    """
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    nlp = tmp_path / "nlp"
    psych = tmp_path / "psych"
    nlp.mkdir()
    psych.mkdir()

    (nlp / "model.md").write_text(
        "# NLP\n\nattention transformer encoder decoder softmax.\n"
        "transformer encoder decoder embedding.\n",
        encoding="utf-8",
    )
    (nlp / "shared.md").write_text(
        "# shared nlp\n\nshared commonword commonterm anchorword.\n",
        encoding="utf-8",
    )
    (psych / "mind.md").write_text(
        "# Psychology\n\nattention cognition perception memory arousal.\n"
        "cognition perception memory salience.\n",
        encoding="utf-8",
    )
    (psych / "shared.md").write_text(
        "# shared psych\n\nshared commonword commonterm anchorword.\n",
        encoding="utf-8",
    )

    db = tmp_path / "shared.db"
    build_graph_index(nlp, db_path=db, min_pair_count=1, min_npmi=0.0)
    build_graph_index(psych, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"nlp": nlp, "psych": psych, "db": db}


def test_single_scope_keyword_not_polysemous(polysemy_db):
    report = inspect_keyword("transformer", db_path=polysemy_db["db"])
    assert report["keyword"] == "transformer"
    assert report["total_docs"] >= 1
    # transformer only appears in the nlp scope.
    assert len(report["docs_by_scope"]) == 1
    assert report["suspected_polysemy"] is False


def test_cross_scope_agreeing_neighbours_not_polysemous(polysemy_db):
    report = inspect_keyword("shared", db_path=polysemy_db["db"])
    # shared appears in both scopes ...
    assert len(report["docs_by_scope"]) == 2
    # ... but with identical neighbour vocabulary, so no polysemy flag.
    assert report["suspected_polysemy"] is False


def test_cross_scope_diverging_neighbours_flagged(polysemy_db):
    report = inspect_keyword("attention", db_path=polysemy_db["db"])
    assert len(report["docs_by_scope"]) == 2
    assert report["suspected_polysemy"] is True
    assert report["polysemy_hint"]


def test_top_cooccurring_carry_shared_scopes(polysemy_db):
    report = inspect_keyword("attention", db_path=polysemy_db["db"])
    kws = {e["keyword"]: e for e in report["top_cooccurring_keywords"]}
    assert "transformer" in kws or "cognition" in kws
    for entry in report["top_cooccurring_keywords"]:
        assert isinstance(entry["shared_scopes"], list)
        assert entry["shared_scopes"]


def test_unknown_keyword_returns_empty_report(polysemy_db):
    report = inspect_keyword("totally-unknown-token", db_path=polysemy_db["db"])
    assert report["total_docs"] == 0
    assert report["docs_by_scope"] == {}
    assert report["top_cooccurring_keywords"] == []
    # The not-indexed branch omits ``suspected_polysemy`` / ``polysemy_hint``
    # entirely (a term that never entered the graph cannot be polysemous;
    # naming ironclad §1.5.1). The polysemy verdict must be inferred from
    # field *absence*, not from a ``False`` sentinel.
    assert "suspected_polysemy" not in report
    assert "polysemy_hint" not in report
    # ``similar_keywords`` is always present on the not-indexed branch —
    # empty list when no vocab entry matches (this is the case here: the
    # unknown token shares no substring with any indexed keyword).
    assert report["similar_keywords"] == []


def test_similar_keywords_surfaces_superstring_vocab(polysemy_db):
    """Missing seed contained in an indexed keyword must be surfaced.

    Analogue of the ``苍坤`` → ``苍坤上人`` case: the graph does not know
    ``transform`` but it knows ``transformer``. The vocab-similarity probe
    must return the superstring so the caller can retry with a corrected
    seed.
    """
    report = inspect_keyword("transform", db_path=polysemy_db["db"])
    assert report["total_docs"] == 0
    kws = {entry["kw"]: entry for entry in report["similar_keywords"]}
    assert "transformer" in kws
    assert kws["transformer"]["kind"] == "superstring"
    assert kws["transformer"]["chunk_df"] >= 1


def test_similar_keywords_surfaces_substring_vocab(polysemy_db):
    """Missing seed containing an indexed keyword must be surfaced too.

    The graph knows ``attention``; a caller asking for the longer
    ``attention-mechanism`` gets the shorter indexed vocab back with
    ``kind='substring'``.
    """
    report = inspect_keyword("attention-mechanism", db_path=polysemy_db["db"])
    assert report["total_docs"] == 0
    kws = {entry["kw"]: entry for entry in report["similar_keywords"]}
    assert "attention" in kws
    assert kws["attention"]["kind"] == "substring"


def test_similar_keywords_skips_short_seeds(polysemy_db):
    """Single-char seeds must not fire a vocab firehose."""
    report = inspect_keyword("a", db_path=polysemy_db["db"])
    assert report["total_docs"] == 0
    assert report["similar_keywords"] == []


def test_similar_keywords_absent_on_indexed_branch(polysemy_db):
    """Indexed keywords use ``top_cooccurring_keywords``, not similarity."""
    report = inspect_keyword("transformer", db_path=polysemy_db["db"])
    assert report["total_docs"] >= 1
    # similar_keywords is a not-indexed-branch construct; the indexed
    # branch has real neighbours to show and does not emit the vocab probe.
    assert "similar_keywords" not in report


def test_similar_keywords_escapes_like_wildcards(tmp_path, monkeypatch):
    """Underscore/percent in the seed must not over-match via LIKE wildcards.

    SQL LIKE treats ``_`` as "any single char" and ``%`` as "any run of
    chars". A seed like ``a_b`` must match ``a_b_extra`` (literal
    substring) but must **not** falsely match ``axb_extra``. Regression
    test for the escape logic in ``_similar_keywords``.
    """
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "notes.md").write_text(
        "# Underscore corpus\n\n"
        "a_b_extra a_b_more.\n"
        "axb_extra axb_more.\n",
        encoding="utf-8",
    )
    db = tmp_path / "esc.db"
    build_graph_index(scope, db_path=db, min_pair_count=1, min_npmi=0.0)

    # Seed ``a_b`` is not itself an indexed keyword. Only literal
    # substrings/superstrings of ``a_b`` may appear; ``axb_*`` must not.
    report = inspect_keyword("a_b", db_path=db)
    assert report["total_docs"] == 0
    matched = {entry["kw"] for entry in report["similar_keywords"]}
    for kw in matched:
        assert ("a_b" in kw) or (kw in "a_b"), (
            f"unexpected LIKE-wildcard match: {kw!r} contains no literal 'a_b'"
        )
    # None of the ``axb_*`` tokens should slip in.
    assert not any(kw.startswith("axb") for kw in matched)


def test_inspect_missing_keyword_gives_hint(polysemy_db):
    """A keyword not in the graph must not read like 'the corpus lacks it'.

    total_docs=0 means the term never entered keyword_docs (graph indexes
    heading / structured terms), not that the corpus text lacks it. The
    hint must be non-empty and point the caller at full-text search /
    (re)indexing.

    Under the P2.1 naming split, the hint lives on ``index_hint`` (a
    factual name for an indexing-layer condition) rather than
    ``polysemy_hint`` (a meaning-collision signal). ``polysemy_hint``
    must not appear on the not-indexed response at all.
    """
    report = inspect_keyword("attention-not-indexed-xyz", db_path=polysemy_db["db"])
    assert report["total_docs"] == 0
    assert "polysemy_hint" not in report, (
        "not-indexed responses must not carry a polysemy_hint field"
    )
    hint = report["index_hint"]
    assert hint, "empty-result hint must be actionable, not blank"
    assert "grga search" in hint
    assert "attention-not-indexed-xyz" in hint
