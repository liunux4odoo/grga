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
    build_graph_index("nlp", directory=nlp, db_path=db, min_pair_count=1, min_npmi=0.0)
    build_graph_index("psych", directory=psych, db_path=db, min_pair_count=1, min_npmi=0.0)
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
    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)

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


def test_top_docs_locates_files_for_indexed_keyword(polysemy_db):
    """Indexed branch surfaces the documents holding the keyword.

    ``top_docs`` is the locate-me shortcut: each row carries the scope,
    the doc_path, and the occurrence/chunk counts so a caller can jump
    straight to the file without a separate ``search`` round-trip.
    """
    report = inspect_keyword("attention", db_path=polysemy_db["db"])
    assert "top_docs" in report
    assert report["top_docs"], "indexed keyword must surface locating docs"
    for entry in report["top_docs"]:
        assert set(entry) == {"scope", "doc_path", "occurrence_count", "chunk_count"}
        assert entry["occurrence_count"] >= 1
    # attention lives in both nlp and psych — both scopes must be routable.
    scopes = {e["scope"] for e in report["top_docs"]}
    assert scopes == {"nlp", "psych"}
    # Sorted by occurrence density (descending) across scopes.
    occ = [e["occurrence_count"] for e in report["top_docs"]]
    assert occ == sorted(occ, reverse=True)


def test_top_docs_respects_limit_and_truncation_flag(polysemy_db):
    """``docs_limit`` caps the list and sets the truncation flag."""
    report = inspect_keyword("attention", db_path=polysemy_db["db"], docs_limit=1)
    assert len(report["top_docs"]) == 1
    assert report["top_docs_truncated"] is True


def test_top_docs_disabled_when_limit_zero(polysemy_db):
    """``docs_limit=0`` suppresses the field for JSON-size-sensitive callers."""
    report = inspect_keyword("attention", db_path=polysemy_db["db"], docs_limit=0)
    assert "top_docs" not in report
    assert "top_docs_truncated" not in report


def test_top_docs_absent_on_not_indexed_branch(polysemy_db):
    """The not-indexed branch has no local evidence, so no ``top_docs``."""
    report = inspect_keyword("totally-unknown-token", db_path=polysemy_db["db"])
    assert report["total_docs"] == 0
    assert "top_docs" not in report


# ---------------------------------------------------------------------------
# --scope / --tag / --tag-logic filter
# ---------------------------------------------------------------------------


def test_scope_filter_narrows_docs_by_scope(polysemy_db):
    """``scope_names`` restricts every scope-scoped field to the named scopes."""
    report = inspect_keyword(
        "attention", db_path=polysemy_db["db"], scope_names=["nlp"]
    )
    assert set(report["docs_by_scope"]) == {"nlp"}
    scopes = {e["scope"] for e in report["top_docs"]}
    assert scopes == {"nlp"}
    for entry in report["top_cooccurring_keywords"]:
        assert set(entry["shared_scopes"]) <= {"nlp"}
    # Only one scope in view — polysemy flag must never trigger.
    assert report["suspected_polysemy"] is False


def test_scope_filter_repeatable_intersects_union(polysemy_db):
    """Multiple ``--scope`` values union across the named scopes."""
    report = inspect_keyword(
        "attention", db_path=polysemy_db["db"],
        scope_names=["nlp", "psych"],
    )
    assert set(report["docs_by_scope"]) == {"nlp", "psych"}


def test_scope_filter_no_match_returns_hint(polysemy_db):
    """An explicit --scope that resolves to no scope IDs bails out."""
    report = inspect_keyword(
        "attention", db_path=polysemy_db["db"],
        scope_names=["no-such-scope"],
    )
    assert report["total_docs"] == 0
    assert report["docs_by_scope"] == {}
    assert "scope_filter_hint" in report
    # Distinct from index_hint — the keyword itself is indexed, only the
    # filter didn't match.
    assert "index_hint" not in report


def test_tag_filter_narrows_by_scope_tags(polysemy_db, tmp_path):
    """``tag_names`` uses scope_tags to resolve the scope filter."""
    from grga.graph.scope import add_tags
    add_tags("nlp", ["ml"], db_path=polysemy_db["db"])
    add_tags("psych", ["cog"], db_path=polysemy_db["db"])

    report = inspect_keyword(
        "attention", db_path=polysemy_db["db"], tag_names=["ml"]
    )
    assert set(report["docs_by_scope"]) == {"nlp"}


def test_scope_and_tag_intersect(polysemy_db):
    """--scope and --tag together must intersect."""
    from grga.graph.scope import add_tags
    add_tags("nlp", ["ml"], db_path=polysemy_db["db"])
    add_tags("psych", ["ml"], db_path=polysemy_db["db"])

    # --scope=nlp AND --tag=ml => only nlp
    report = inspect_keyword(
        "attention", db_path=polysemy_db["db"],
        scope_names=["nlp"], tag_names=["ml"],
    )
    assert set(report["docs_by_scope"]) == {"nlp"}

    # --scope=nlp AND --tag=non-existent => empty match
    report = inspect_keyword(
        "attention", db_path=polysemy_db["db"],
        scope_names=["nlp"], tag_names=["no-such-tag"],
    )
    assert "scope_filter_hint" in report


def test_tag_logic_and_requires_every_tag(polysemy_db):
    """``tag_logic='and'`` matches only scopes carrying every listed tag."""
    from grga.graph.scope import add_tags
    add_tags("nlp", ["ml", "papers"], db_path=polysemy_db["db"])
    add_tags("psych", ["papers"], db_path=polysemy_db["db"])

    # or = union (both scopes match at least one)
    r_or = inspect_keyword(
        "attention", db_path=polysemy_db["db"],
        tag_names=["ml", "papers"], tag_logic="or",
    )
    assert set(r_or["docs_by_scope"]) == {"nlp", "psych"}

    # and = intersection (only nlp carries both)
    r_and = inspect_keyword(
        "attention", db_path=polysemy_db["db"],
        tag_names=["ml", "papers"], tag_logic="and",
    )
    assert set(r_and["docs_by_scope"]) == {"nlp"}


def test_invalid_tag_logic_raises():
    """Unknown ``tag_logic`` value must be rejected early."""
    import pytest
    with pytest.raises(ValueError):
        inspect_keyword("x", db_path="/tmp/does-not-exist.db", tag_logic="xor")
