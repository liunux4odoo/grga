"""Tests for :mod:`grga.graph.promote` (v10 scope-aware).

Covers the threshold gate, idempotent reruns (via the ``promoted_at``
marker), scope filtering, dry-run, and the non-destructive guarantee on
``llm_extension_votes``.

v10 semantics: ``scope`` is a logical name; ``scope=None`` means "promote
across every scope in the votes table" (unchanged from pre-v10 for the
read side). There is no ``""`` sentinel — every vote row belongs to a
named scope by construction (see :mod:`grga.graph.votes`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.promote import PROMOTE_SCOPE, promote_votes
from grga.graph.schema import connect, ensure_schema
from grga.graph.votes import record_vote


def _seed_votes(db: Path, scope: str = "s1") -> None:
    # alpha<->beta reaches 3 votes; gamma<->delta only 2; epsilon<->zeta 4.
    for _ in range(3):
        record_vote("alpha", "beta", db_path=db, scope=scope)
    for _ in range(2):
        record_vote("gamma", "delta", db_path=db, scope=scope)
    for _ in range(4):
        record_vote("epsilon", "zeta", db_path=db, scope=scope)


def _pairs(db: Path) -> list[dict]:
    conn = connect(db)
    try:
        ensure_schema(conn)
        return [
            dict(row)
            for row in conn.execute(
                "SELECT p.source AS source, "
                "       ka.kw AS keyword_a, "
                "       kb.kw AS keyword_b, "
                "       p.cooccur_count AS count "
                "FROM keyword_pairs p "
                "JOIN keywords ka ON ka.id = p.kw_a_id "
                "JOIN keywords kb ON kb.id = p.kw_b_id "
                "ORDER BY ka.kw, kb.kw"
            ).fetchall()
        ]
    finally:
        conn.close()


def _vote_rows(db: Path) -> list[dict]:
    conn = connect(db)
    try:
        ensure_schema(conn)
        return [
            dict(row)
            for row in conn.execute(
                "SELECT ka.kw AS keyword_a, kb.kw AS keyword_b, "
                "       v.vote_count AS vote_count "
                "FROM llm_extension_votes v "
                "JOIN keywords ka ON ka.id = v.kw_a_id "
                "JOIN keywords kb ON kb.id = v.kw_b_id "
                "ORDER BY ka.kw, kb.kw"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_threshold_gate_only_promotes_qualifying_pairs(tmp_path):
    db = tmp_path / "graph.db"
    _seed_votes(db)
    stats = promote_votes(threshold=3, db_path=db)
    assert stats.candidates == 2
    assert stats.promoted == 2
    promoted_keys = {(p.keyword_a, p.keyword_b) for p in stats.pairs}
    assert ("alpha", "beta") in promoted_keys
    assert ("epsilon", "zeta") in promoted_keys
    assert ("gamma", "delta") not in promoted_keys

    pairs = _pairs(db)
    assert all(p["source"] == PROMOTE_SCOPE for p in pairs)
    keys = {(p["keyword_a"], p["keyword_b"]) for p in pairs}
    assert keys == {("alpha", "beta"), ("epsilon", "zeta")}


def test_dry_run_writes_nothing(tmp_path):
    db = tmp_path / "graph.db"
    _seed_votes(db)
    stats = promote_votes(threshold=3, db_path=db, dry_run=True)
    assert stats.dry_run is True
    assert stats.promoted == 2
    assert _pairs(db) == []


def test_promotion_is_idempotent(tmp_path):
    db = tmp_path / "graph.db"
    _seed_votes(db)
    first = promote_votes(threshold=3, db_path=db)
    assert first.promoted == 2

    second = promote_votes(threshold=3, db_path=db)
    assert second.promoted == 0
    assert second.skipped_already_promoted == 2

    pairs = {(p["keyword_a"], p["keyword_b"]): p["count"] for p in _pairs(db)}
    assert pairs[("alpha", "beta")] == pytest.approx(3.0)
    assert pairs[("epsilon", "zeta")] == pytest.approx(4.0)


def test_repromote_flag_forces_second_merge(tmp_path):
    db = tmp_path / "graph.db"
    _seed_votes(db)
    promote_votes(threshold=3, db_path=db)
    promote_votes(threshold=3, db_path=db, repromote=True)
    pairs = {(p["keyword_a"], p["keyword_b"]): p["count"] for p in _pairs(db)}
    assert pairs[("alpha", "beta")] == pytest.approx(6.0)


def test_votes_table_is_never_cleared(tmp_path):
    db = tmp_path / "graph.db"
    _seed_votes(db)
    promote_votes(threshold=3, db_path=db)
    votes = {(r["keyword_a"], r["keyword_b"]): r["vote_count"] for r in _vote_rows(db)}
    assert votes[("alpha", "beta")] == 3.0
    # Canonical order sorts delta before gamma (folded lexicographic).
    assert votes[("delta", "gamma")] == 2.0
    assert votes[("epsilon", "zeta")] == 4.0


def test_scope_filter_isolates_promotion(tmp_path):
    db = tmp_path / "graph.db"
    for _ in range(3):
        record_vote("alpha", "beta", db_path=db, scope="corpus-a")
    for _ in range(3):
        record_vote("gamma", "delta", db_path=db, scope="corpus-b")

    stats = promote_votes(threshold=3, db_path=db, scope="corpus-a")
    assert stats.promoted == 1
    keys = {(p["keyword_a"], p["keyword_b"]) for p in _pairs(db)}
    assert keys == {("alpha", "beta")}


def test_scope_none_promotes_across_every_scope(tmp_path):
    """``scope=None`` (default) promotes qualifying pairs everywhere."""
    db = tmp_path / "graph.db"
    for _ in range(3):
        record_vote("alpha", "beta", db_path=db, scope="s1")
    for _ in range(3):
        record_vote("gamma", "delta", db_path=db, scope="s2")

    stats = promote_votes(threshold=3, db_path=db)
    assert stats.promoted == 2


def test_promote_unknown_scope_returns_empty(tmp_path):
    """Filtering on a scope that has never been seen is a no-op."""
    db = tmp_path / "graph.db"
    _seed_votes(db, scope="known")
    stats = promote_votes(threshold=3, db_path=db, scope="nonexistent")
    assert stats.candidates == 0
    assert stats.promoted == 0
    assert _pairs(db) == []


def test_promote_survives_missing_keyword_pairs_table(tmp_path):
    """A DB that only ran build/vote (no KeywordLibrary) still promotes."""
    db = tmp_path / "graph.db"
    _seed_votes(db)
    stats = promote_votes(threshold=3, db_path=db)
    assert stats.promoted == 2
    assert len(_pairs(db)) == 2
