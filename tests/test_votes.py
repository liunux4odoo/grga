"""Tests for :mod:`grga.graph.votes` (v10 scope-aware).

v10 semantics (plan-2026-07-14):

* Every knowledge write requires a named ``scope`` — there is no
  "unscoped" sentinel. ``scope=None`` **silently skips recording**
  (returns an empty :class:`VoteResult`).
* ``scope`` is a logical name (portable across machines), not a
  filesystem path; ``record_vote(scope="physics-cn")``.
* Read-side ``get_votes(scope=None)`` still returns every row (no
  filter), while ``get_votes(scope="physics-cn")`` restricts to that
  scope.

Covers: accumulation, deduplication, self-pair filtering, scope
isolation, idempotent reruns, ``scope=None`` skip.
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
                "SELECT s.name AS scope, "
                "       ka.kw AS keyword_a, "
                "       kb.kw AS keyword_b, "
                "       v.vote_count AS vote_count "
                "FROM llm_extension_votes v "
                "JOIN scopes s ON s.id = v.scope_id "
                "JOIN keywords ka ON ka.id = v.kw_a_id "
                "JOIN keywords kb ON kb.id = v.kw_b_id "
                "ORDER BY s.name, ka.kw, kb.kw"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_record_vote_creates_row_with_folded_canonical_order(tmp_path):
    db = tmp_path / "votes.db"
    result = record_vote("Neural", "Network", scope="ml-en", db_path=db)
    assert result.pairs_seen == 1
    assert result.pairs_recorded == 1
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["keyword_a"] == "network"
    assert rows[0]["keyword_b"] == "neural"
    assert rows[0]["vote_count"] == 1.0
    assert rows[0]["scope"] == "ml-en"


def test_record_vote_reverse_order_merges_into_one_row(tmp_path):
    db = tmp_path / "votes.db"
    record_vote("alpha", "beta", scope="s1", db_path=db)
    record_vote("beta", "alpha", scope="s1", db_path=db)
    record_vote("BETA", "ALPHA", scope="s1", db_path=db)
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["vote_count"] == 3.0


def test_record_votes_deduplicates_within_call(tmp_path):
    db = tmp_path / "votes.db"
    result = record_votes(
        [("a", "b"), ("b", "a"), ("A", "B"), ("a", "b")],
        scope="s1",
        db_path=db,
        weight=1.0,
    )
    assert result.pairs_seen == 4
    assert result.pairs_recorded == 1
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["vote_count"] == 1.0


def test_record_vote_skips_self_pairs_and_empties(tmp_path):
    db = tmp_path / "votes.db"
    result = record_votes(
        [("solo", "solo"), ("", "beta"), ("gamma", "  "), ("Alpha", "alpha")],
        scope="s1",
        db_path=db,
    )
    assert result.pairs_seen == 4
    assert result.pairs_recorded == 0
    assert _rows(db) == []


def test_scope_isolates_votes(tmp_path):
    db = tmp_path / "votes.db"
    record_vote("neural", "network", scope="corpus-a", db_path=db)
    record_vote("neural", "network", scope="corpus-b", db_path=db)
    rows = _rows(db)
    assert len(rows) == 2
    scopes = {row["scope"] for row in rows}
    assert scopes == {"corpus-a", "corpus-b"}


def test_record_votes_from_keywords_generates_full_clique(tmp_path):
    db = tmp_path / "votes.db"
    result = record_votes_from_keywords(
        ["alpha", "beta", "gamma"], scope="s1", db_path=db
    )
    assert result.pairs_recorded == 3
    rows = _rows(db)
    assert {(row["keyword_a"], row["keyword_b"]) for row in rows} == {
        ("alpha", "beta"),
        ("alpha", "gamma"),
        ("beta", "gamma"),
    }


def test_record_votes_from_keywords_needs_two_terms(tmp_path):
    db = tmp_path / "votes.db"
    result = record_votes_from_keywords(["alpha"], scope="s1", db_path=db)
    assert result == VoteResult()
    assert _rows(db) == []


def test_weight_parameter_accumulates(tmp_path):
    db = tmp_path / "votes.db"
    record_vote("alpha", "beta", scope="s1", db_path=db, weight=1.5)
    record_vote("alpha", "beta", scope="s1", db_path=db, weight=0.5)
    rows = _rows(db)
    assert rows[0]["vote_count"] == pytest.approx(2.0)


def test_scope_none_silently_skips_recording(tmp_path):
    """v10: knowledge cannot be written without a named scope.

    ``record_vote(scope=None)`` returns an empty result and writes nothing
    — the caller had no scope to attribute the vote to. This lets
    ``suggest(scope=None)`` and similar upstream helpers pass ``scope``
    through unconditionally without a guard.
    """
    db = tmp_path / "votes.db"
    result = record_vote("alpha", "beta", scope=None, db_path=db)
    assert result == VoteResult()
    assert _rows(db) == []


def test_get_votes_filters_by_min_and_scope(tmp_path):
    db = tmp_path / "votes.db"
    record_vote("alpha", "beta", scope="s1", db_path=db)
    record_vote("alpha", "gamma", scope="s1", db_path=db, weight=3.0)
    record_vote("delta", "epsilon", scope="other", db_path=db)
    all_votes = get_votes(db_path=db)
    assert len(all_votes) == 3
    hot = get_votes(db_path=db, min_votes=2)
    assert len(hot) == 1
    assert (hot[0]["keyword_a"], hot[0]["keyword_b"]) == ("alpha", "gamma")
    scoped = get_votes(db_path=db, scope="other")
    assert len(scoped) == 1
    assert scoped[0]["scope"] == "other"


def test_get_votes_unknown_scope_returns_empty(tmp_path):
    """Read side treats an unknown scope name as "no matches" (short-circuit)."""
    db = tmp_path / "votes.db"
    record_vote("alpha", "beta", scope="known", db_path=db)
    assert get_votes(db_path=db, scope="nonexistent") == []


def test_shared_connection_stays_open_for_caller(tmp_path):
    db = tmp_path / "votes.db"
    conn = connect(db)
    try:
        record_vote("alpha", "beta", scope="s1", conn=conn)
        record_vote("beta", "alpha", scope="s1", conn=conn)
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM llm_extension_votes"
            ).fetchall()
        ]
        assert len(rows) == 1
        assert rows[0]["vote_count"] == 2.0
    finally:
        conn.close()
