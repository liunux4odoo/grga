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
    build_graph_index(nlp, db_path=db, min_pair_count=1)
    build_graph_index(psych, db_path=db, min_pair_count=1)
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
