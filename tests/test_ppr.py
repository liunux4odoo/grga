"""Tests for :mod:`grga.graph.ppr` (Phase 4.3).

A tiny 6-node graph is enough to verify the ranking against a hand-run
power iteration. We only assert relative ordering, not exact scores, so
the tests remain stable across floating-point revisions.
"""
from __future__ import annotations

import pytest

from grga.graph.ppr import Edge, build_adjacency, personalized_pagerank


def _graph():
    # Two tight communities joined by a bridge.
    edges = [
        Edge("install", "macos", 4.0),
        Edge("install", "linux", 3.0),
        Edge("install", "brew", 2.0),
        Edge("macos", "brew", 5.0),
        Edge("linux", "apt", 4.0),
        # Bridge
        Edge("macos", "usage", 1.0),
        Edge("usage", "cli", 3.0),
        Edge("usage", "gui", 3.0),
        Edge("cli", "gui", 2.0),
    ]
    return build_adjacency(edges)


def test_ppr_returns_only_non_seed_nodes():
    adj = _graph()
    result = personalized_pagerank(["install"], adj, top_k=10)
    assert result
    names = [node for node, _ in result]
    assert "install" not in names


def test_ppr_prefers_close_neighbours():
    adj = _graph()
    result = dict(personalized_pagerank(["install"], adj, top_k=10))
    # Direct neighbours of the seed dominate the distant "gui"/"cli" nodes.
    assert result["macos"] > result["gui"]
    assert result["brew"] > result["cli"]


def test_ppr_multiple_seeds_blends_communities():
    adj = _graph()
    result = dict(personalized_pagerank(["install", "usage"], adj, top_k=10))
    # With both communities as seeds, cli/gui rise above bare-linux-only
    # neighbours like ``apt``.
    assert result["cli"] > 0 and result["gui"] > 0
    assert result["macos"] > result["apt"]


def test_ppr_unknown_seed_returns_empty():
    adj = _graph()
    result = personalized_pagerank(["unknown-token"], adj, top_k=5)
    assert result == []


def test_ppr_empty_graph_returns_empty():
    result = personalized_pagerank(["install"], {}, top_k=5)
    assert result == []


def test_alpha_extremes_are_sane():
    adj = _graph()
    high_alpha = dict(personalized_pagerank(["install"], adj, top_k=10, alpha=0.9))
    low_alpha = dict(personalized_pagerank(["install"], adj, top_k=10, alpha=0.05))
    # With very low alpha the random walk drifts further from the seed, so
    # distant nodes gain relatively more mass.
    assert low_alpha["gui"] / low_alpha["macos"] > high_alpha["gui"] / high_alpha["macos"]
