"""Tests for :mod:`grga.graph.votes` (Phase 5.2).

Covers accumulation, deduplication, self-pair filtering, scope isolation,
and idempotent reruns.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.schema import connect, ensure_schema
from grga.graph.votes import (
    VoteResult,
    get_votes,
    record_vote,
    record_votes,
    record_votes_from_keywords,
)


def _rows(db: Path) -> list[dict]:
    conn = connect(db)
    try:
        ensure_schema(conn)
        return [
            dict(row)
            for row in conn.execute(
                "SELECT directory, keyword_a, keyword_b, vote_count FROM llm_extension_votes"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_record_vote_creates_row_with_folded_canonical_order(tmp_path):
    db = tmp_path / "votes.db"
    result = record_vote("Neural", "Network", db_path=db)
    assert result.pairs_seen == 1
    assert result.pairs_recorded == 1
    rows = _rows(db)
    assert len(rows) == 1
    # Canonical order (folded + sorted).
    assert rows[0]["keyword_a"] == "network"
    assert rows[0]["keyword_b"] == "neural"
    assert rows[0]["vote_count"] == 1.0
    assert rows[0]["directory"] == ""


def test_record_vote_reverse_order_merges_into_one_row(tmp_path):
    db = tmp_path / "votes.db"
    record_vote("alpha", "beta", db_path=db)
    record_vote("beta", "alpha", db_path=db)
    record_vote("BETA", "ALPHA", db_path=db)
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["vote_count"] == 3.0


def test_record_votes_deduplicates_within_call(tmp_path):
    db = tmp_path / "votes.db"
    result = record_votes(
        [("a", "b"), ("b", "a"), ("A", "B"), ("a", "b")],
        db_path=db,
        weight=1.0,
    )
    assert result.pairs_seen == 4
    assert result.pairs_recorded == 1  # collapse within one call
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["vote_count"] == 1.0


def test_record_vote_skips_self_pairs_and_empties(tmp_path):
    db = tmp_path / "votes.db"
    result = record_votes(
        [("solo", "solo"), ("", "beta"), ("gamma", "  "), ("Alpha", "alpha")],
        db_path=db,
    )
    assert result.pairs_seen == 4
    assert result.pairs_recorded == 0
    assert _rows(db) == []


def test_record_vote_scope_isolates_directories(tmp_path):
    db = tmp_path / "votes.db"
    record_vote("neural", "network", directory=tmp_path / "corpus_a", db_path=db)
    record_vote("neural", "network", directory=tmp_path / "corpus_b", db_path=db)
    rows = _rows(db)
    assert len(rows) == 2
    dirs = {row["directory"] for row in rows}
    assert len(dirs) == 2


def test_record_votes_from_keywords_generates_full_clique(tmp_path):
    db = tmp_path / "votes.db"
    result = record_votes_from_keywords(["alpha", "beta", "gamma"], db_path=db)
    # C(3,2) = 3 pairs.
    assert result.pairs_recorded == 3
    rows = _rows(db)
    assert {(row["keyword_a"], row["keyword_b"]) for row in rows} == {
        ("alpha", "beta"),
        ("alpha", "gamma"),
        ("beta", "gamma"),
    }


def test_record_votes_from_keywords_needs_two_terms(tmp_path):
    db = tmp_path / "votes.db"
    result = record_votes_from_keywords(["alpha"], db_path=db)
    assert result == VoteResult()
    assert _rows(db) == []


def test_weight_parameter_accumulates(tmp_path):
    db = tmp_path / "votes.db"
    record_vote("alpha", "beta", db_path=db, weight=1.5)
    record_vote("alpha", "beta", db_path=db, weight=0.5)
    rows = _rows(db)
    assert rows[0]["vote_count"] == pytest.approx(2.0)


def test_get_votes_filters_by_min_and_scope(tmp_path):
    db = tmp_path / "votes.db"
    record_vote("alpha", "beta", db_path=db)
    record_vote("alpha", "gamma", db_path=db, weight=3.0)
    record_vote("delta", "epsilon", db_path=db, directory=tmp_path / "other")
    all_votes = get_votes(db_path=db)
    assert len(all_votes) == 3
    hot = get_votes(db_path=db, min_votes=2)
    assert len(hot) == 1
    assert (hot[0]["keyword_a"], hot[0]["keyword_b"]) == ("alpha", "gamma")
    scoped = get_votes(db_path=db, directory=tmp_path / "other")
    assert len(scoped) == 1


def test_get_votes_empty_directory_selects_only_unscoped(tmp_path):
    """`directory=""` must restrict to unscoped rows, not collapse to all.

    Regression for the scope-sentinel unification: unscoped votes (stored as
    ``""``) and scoped votes coexist; asking for ``""`` returns only the
    unscoped ones, while ``None`` returns everything.
    """
    db = tmp_path / "votes.db"
    record_vote("alpha", "beta", db_path=db)  # unscoped -> stored as ""
    record_vote("gamma", "delta", db_path=db, directory=tmp_path / "corpus")
    unscoped = get_votes(db_path=db, directory="")
    assert len(unscoped) == 1
    assert (unscoped[0]["keyword_a"], unscoped[0]["keyword_b"]) == ("alpha", "beta")
    assert get_votes(db_path=db, directory="") != get_votes(db_path=db)  # None = all
    assert len(get_votes(db_path=db)) == 2


def test_shared_connection_stays_open_for_caller(tmp_path):
    db = tmp_path / "votes.db"
    conn = connect(db)
    try:
        record_vote("alpha", "beta", conn=conn)
        record_vote("beta", "alpha", conn=conn)
        rows = [
            dict(row)
            for row in conn.execute("SELECT * FROM llm_extension_votes").fetchall()
        ]
        assert len(rows) == 1
        assert rows[0]["vote_count"] == 2.0
    finally:
        conn.close()
