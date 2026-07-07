"""Tests for :mod:`grga.graph.promote` (Phase 5.3).

Covers the threshold gate, idempotent reruns (via the ``promoted_at``
marker), scope filtering, dry-run, and the non-destructive guarantee on
``llm_extension_votes``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.promote import PROMOTE_SCOPE, promote_votes
from grga.graph.schema import connect, ensure_schema
from grga.graph.votes import record_vote


def _seed_votes(db: Path, directory=None) -> None:
    # alpha<->beta reaches 3 votes; gamma<->delta only 2; epsilon<->zeta 4.
    for _ in range(3):
        record_vote("alpha", "beta", db_path=db, directory=directory)
    for _ in range(2):
        record_vote("gamma", "delta", db_path=db, directory=directory)
    for _ in range(4):
        record_vote("epsilon", "zeta", db_path=db, directory=directory)


def _pairs(db: Path) -> list[dict]:
    conn = connect(db)
    try:
        ensure_schema(conn)
        return [
            dict(row)
            for row in conn.execute(
                "SELECT scope, keyword_a, keyword_b, count FROM keyword_pairs"
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
                "SELECT keyword_a, keyword_b, vote_count FROM llm_extension_votes"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_threshold_gate_only_promotes_qualifying_pairs(tmp_path):
    db = tmp_path / "graph.db"
    _seed_votes(db)
    stats = promote_votes(threshold=3, db_path=db)
    assert stats.candidates == 2  # alpha/beta (3) and epsilon/zeta (4)
    assert stats.promoted == 2
    promoted_keys = {(p.keyword_a, p.keyword_b) for p in stats.pairs}
    assert ("alpha", "beta") in promoted_keys
    assert ("epsilon", "zeta") in promoted_keys
    assert ("gamma", "delta") not in promoted_keys

    pairs = _pairs(db)
    assert all(p["scope"] == PROMOTE_SCOPE for p in pairs)
    keys = {(p["keyword_a"], p["keyword_b"]) for p in pairs}
    assert keys == {("alpha", "beta"), ("epsilon", "zeta")}


def test_dry_run_writes_nothing(tmp_path):
    db = tmp_path / "graph.db"
    _seed_votes(db)
    stats = promote_votes(threshold=3, db_path=db, dry_run=True)
    assert stats.dry_run is True
    assert stats.promoted == 2  # would-promote count
    assert _pairs(db) == []


def test_promotion_is_idempotent(tmp_path):
    db = tmp_path / "graph.db"
    _seed_votes(db)
    first = promote_votes(threshold=3, db_path=db)
    assert first.promoted == 2

    # Second run: already-promoted pairs are skipped, no double counting.
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
    # Additive merge means the count doubles on the forced re-promote.
    assert pairs[("alpha", "beta")] == pytest.approx(6.0)


def test_votes_table_is_never_cleared(tmp_path):
    db = tmp_path / "graph.db"
    _seed_votes(db)
    promote_votes(threshold=3, db_path=db)
    # All original vote rows survive (auditable, reversible).
    votes = {(r["keyword_a"], r["keyword_b"]): r["vote_count"] for r in _vote_rows(db)}
    assert votes[("alpha", "beta")] == 3.0
    assert votes[("delta", "gamma")] == 2.0
    assert votes[("epsilon", "zeta")] == 4.0


def test_scope_filter_isolates_promotion(tmp_path):
    db = tmp_path / "graph.db"
    scope_a = tmp_path / "corpus_a"
    scope_b = tmp_path / "corpus_b"
    for _ in range(3):
        record_vote("alpha", "beta", db_path=db, directory=scope_a)
    for _ in range(3):
        record_vote("gamma", "delta", db_path=db, directory=scope_b)

    stats = promote_votes(threshold=3, db_path=db, directory=scope_a)
    assert stats.promoted == 1
    keys = {(p["keyword_a"], p["keyword_b"]) for p in _pairs(db)}
    assert keys == {("alpha", "beta")}


def test_scope_filter_empty_string_promotes_only_unscoped(tmp_path):
    """`directory=""` must promote only unscoped votes, not every scope.

    Regression for the scope-sentinel unification: previously ``""`` collapsed
    to ``None`` (meaning "match all") which made "promote only unscoped
    votes" inexpressible and inconsistent with :func:`grga.graph.votes.get_votes`.
    """
    db = tmp_path / "graph.db"
    for _ in range(3):
        record_vote("alpha", "beta", db_path=db)  # unscoped
    for _ in range(3):
        record_vote("gamma", "delta", db_path=db, directory=tmp_path / "corpus")

    stats = promote_votes(threshold=3, db_path=db, directory="")
    assert stats.promoted == 1
    keys = {(p["keyword_a"], p["keyword_b"]) for p in _pairs(db)}
    assert keys == {("alpha", "beta")}


def test_scope_filter_none_promotes_all_scopes(tmp_path):
    """`directory=None` (default) still promotes across every scope."""
    db = tmp_path / "graph.db"
    for _ in range(3):
        record_vote("alpha", "beta", db_path=db)
    for _ in range(3):
        record_vote("gamma", "delta", db_path=db, directory=tmp_path / "corpus")

    stats = promote_votes(threshold=3, db_path=db)
    assert stats.promoted == 2


def test_promote_survives_missing_keyword_pairs_table(tmp_path):
    """A DB that only ran build/vote (no KeywordLibrary) still promotes."""
    db = tmp_path / "graph.db"
    _seed_votes(db)
    # No KeywordLibrary was ever instantiated here, so keyword_pairs may not
    # exist yet; promote must create it lazily.
    stats = promote_votes(threshold=3, db_path=db)
    assert stats.promoted == 2
    assert len(_pairs(db)) == 2
