"""Adjacency LRU cache invariants (PERF_optimization_plan §P0-1).

Four contracts:

1. Two consecutive ``get_adjacency(db, scope)`` calls with unchanged inputs
   go to SQL exactly once (the second is a cache hit).
2. A DB write that bumps the file mtime invalidates the entry: the next
   ``get_adjacency`` reissues the load_edges query.
3. Different ``scope_key`` values occupy independent cache slots.
4. When the cache exceeds ``CACHE_MAXSIZE`` the oldest entry is evicted;
   requesting it again forces a rebuild.
"""
from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

from grga.graph.adjacency_cache import (
    CACHE_MAXSIZE,
    cache_info,
    clear_adjacency_cache,
    get_adjacency,
)
from grga.graph.build import build_graph_index


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_adjacency_cache()
    yield
    clear_adjacency_cache()


@pytest.fixture
def two_scope_db(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    a = tmp_path / "scope_a"
    b = tmp_path / "scope_b"
    a.mkdir()
    b.mkdir()
    (a / "a1.md").write_text(
        "# A\n\nalpha alpha bridgeword bridgeword.\n", encoding="utf-8"
    )
    (b / "b1.md").write_text(
        "# B\n\nbridgeword bridgeword beta beta.\n", encoding="utf-8"
    )
    db = tmp_path / "shared.db"
    build_graph_index(a, db_path=db, min_pair_count=1)
    build_graph_index(b, db_path=db, min_pair_count=1)
    return {"a": a, "b": b, "db": db}


def test_second_call_hits_cache(two_scope_db):
    """Same (db, scope) twice ⇒ load_edges runs exactly once."""
    with patch(
        "grga.graph.adjacency_cache.load_edges", wraps=__import__(
            "grga.graph.ppr", fromlist=["load_edges"]
        ).load_edges,
    ) as spy:
        _, adj1, idx1 = get_adjacency(two_scope_db["db"], str(two_scope_db["a"]))
        _, adj2, idx2 = get_adjacency(two_scope_db["db"], str(two_scope_db["a"]))
    assert spy.call_count == 1, (
        f"expected 1 SQL rebuild, got {spy.call_count}"
    )
    # Second return is the *same object* (returned by reference).
    assert adj1 is adj2
    assert idx1 is idx2


def test_mtime_bump_invalidates(two_scope_db):
    """A DB write that changes mtime forces a rebuild on next call."""
    from grga.graph import ppr as ppr_mod

    with patch(
        "grga.graph.adjacency_cache.load_edges", wraps=ppr_mod.load_edges
    ) as spy:
        get_adjacency(two_scope_db["db"], str(two_scope_db["a"]))
        # Force a resolvable mtime bump — the filesystem may collapse
        # within-second writes, so we advance and rewrite explicitly.
        new_mtime = time.time() + 5
        os.utime(two_scope_db["db"], (new_mtime, new_mtime))
        get_adjacency(two_scope_db["db"], str(two_scope_db["a"]))
    assert spy.call_count == 2, (
        f"mtime change should force a rebuild, got {spy.call_count} calls"
    )


def test_different_scopes_are_independent(two_scope_db):
    """(db, scope_a) and (db, scope_b) each pay one build; neither
    displaces the other on subsequent identical calls."""
    from grga.graph import ppr as ppr_mod

    with patch(
        "grga.graph.adjacency_cache.load_edges", wraps=ppr_mod.load_edges
    ) as spy:
        get_adjacency(two_scope_db["db"], str(two_scope_db["a"]))
        get_adjacency(two_scope_db["db"], str(two_scope_db["b"]))
        get_adjacency(two_scope_db["db"], None)  # global
        # Re-read all three: all hits.
        get_adjacency(two_scope_db["db"], str(two_scope_db["a"]))
        get_adjacency(two_scope_db["db"], str(two_scope_db["b"]))
        get_adjacency(two_scope_db["db"], None)
    assert spy.call_count == 3, (
        f"expected 3 initial builds and 3 hits, got {spy.call_count}"
    )
    stats = cache_info()
    assert stats.hits >= 3
    assert stats.currsize == 3


def test_lru_evicts_oldest_when_over_maxsize(two_scope_db, tmp_path):
    """Filling CACHE_MAXSIZE + 1 entries evicts the least-recent one."""
    from grga.graph import ppr as ppr_mod

    # Prime CACHE_MAXSIZE distinct entries by varying the scope_key string.
    # (Different scope strings all resolve to real cache keys regardless of
    # whether the SQL result is empty; the cache does not filter by hit
    # size.)
    priming_scopes = [f"/tmp/nonexistent_scope_{i}" for i in range(CACHE_MAXSIZE)]

    with patch(
        "grga.graph.adjacency_cache.load_edges", wraps=ppr_mod.load_edges
    ) as spy:
        # Fill cache with CACHE_MAXSIZE entries.
        for s in priming_scopes:
            get_adjacency(two_scope_db["db"], s)
        assert cache_info().currsize == CACHE_MAXSIZE

        # One more entry evicts the *first* one (LRU order).
        get_adjacency(two_scope_db["db"], "/tmp/overflow_scope")
        assert cache_info().currsize == CACHE_MAXSIZE

        # Re-fetching the first primed entry must miss (was evicted).
        pre_count = spy.call_count
        get_adjacency(two_scope_db["db"], priming_scopes[0])
        assert spy.call_count == pre_count + 1, (
            "oldest entry should have been evicted; expected a rebuild"
        )
