"""Tests for :mod:`grga.graph.ppr` (Phase 4.3).

A tiny 6-node graph is enough to verify the ranking against a hand-run
power iteration. We only assert relative ordering, not exact scores, so
the tests remain stable across floating-point revisions.
"""
from __future__ import annotations

import pytest

from grga.graph.ppr import (
    Edge,
    build_adjacency,
    build_indexed_graph,
    indexed_graph_from_adjacency,
    personalized_pagerank,
    personalized_pagerank_indexed,
)


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


def test_ppr_scores_match_reference_implementation():
    """Regression pin against a reference implementation.

    P0-3 hoists ``sum(neighbours.values())`` into an ``out_degree`` map
    computed once instead of on every power-iteration sweep, and swaps
    ``seed_in_graph`` from a list to a set for O(1) exclusion. Both are
    algebraically no-ops but silent PPR score drift would corrupt every
    downstream ``related --mode graph`` result. This test embeds an inline
    reference implementation (the pre-P0-3 code) and asserts byte-for-byte
    score equivalence on a fixed 40-node / ~120-edge random graph.
    """
    import random

    def _reference(seeds, adjacency, top_k=20, alpha=0.15, iters=20):
        def _fold(t):
            return t.casefold().strip()

        seed_set = [s for s in (_fold(x) for x in seeds) if s]
        if not seed_set or not adjacency:
            return []
        seed_in_graph = [s for s in seed_set if s in adjacency]
        if not seed_in_graph:
            return []
        p = 1.0 / len(seed_in_graph)
        personal = {s: p for s in seed_in_graph}
        scores = dict(personal)
        for _ in range(max(1, iters)):
            ns = {node: alpha * personal.get(node, 0.0) for node in adjacency}
            for node, mass in scores.items():
                nb = adjacency.get(node)
                if not nb:
                    for t, w in personal.items():
                        ns[t] = ns.get(t, 0.0) + (1.0 - alpha) * mass * w
                    continue
                total = sum(nb.values())
                if total <= 0:
                    continue
                share = (1.0 - alpha) * mass
                for n, w in nb.items():
                    ns[n] = ns.get(n, 0.0) + share * (w / total)
            scores = ns
        ranked = [
            (n, s) for n, s in scores.items()
            if n not in seed_in_graph and s > 0
        ]
        ranked.sort(key=lambda i: (-i[1], i[0]))
        return ranked[:top_k]

    random.seed(7)
    nodes = [f"n{i}" for i in range(40)]
    ed = {}
    for _ in range(120):
        a, b = random.sample(nodes, 2)
        k = tuple(sorted((a, b)))
        ed[k] = ed.get(k, 0.0) + random.random() * 3.0
    adjacency = build_adjacency([Edge(a=k[0], b=k[1], weight=w) for k, w in ed.items()])

    for seeds in (["n0"], ["n1", "n5"], ["n10", "n20", "n30"]):
        ref = _reference(seeds, adjacency)
        got = personalized_pagerank(seeds, adjacency)
        assert [k for k, _ in ref] == [k for k, _ in got], (
            f"ranking order drift for seeds={seeds}"
        )
        for (k_ref, s_ref), (k_got, s_got) in zip(ref, got):
            assert abs(s_ref - s_got) < 1e-9, (
                f"score drift for seeds={seeds}, node={k_ref}: "
                f"ref={s_ref} got={s_got}"
            )


def test_indexed_ppr_matches_dict_ppr():
    """P1-1 対拍：IndexedGraph 版 PPR 必须与 dict 版逐位一致。

    生成一个 10 节点、~20 边的确定图，同时经 dict adjacency 和
    IndexedGraph 两条路径跑 PPR，断言 top-k 排名一致、分数误差 < 1e-9。
    这是把 PPR 从 dict-of-dicts 迁移到 index-of-lists 的语义护栏。
    """
    import random

    random.seed(1337)
    edges: list = []
    nodes = [f"k{i}" for i in range(10)]
    seen = set()
    for _ in range(20):
        a, b = random.sample(nodes, 2)
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        edges.append(Edge(a=key[0], b=key[1], weight=1.0 + random.random() * 3.0))

    dict_adj = build_adjacency(edges)
    indexed_from_edges = build_indexed_graph(edges)
    indexed_from_adj = indexed_graph_from_adjacency(dict_adj)

    # Both index constructions must be structurally equivalent to the dict
    # (same nodes, same total out-degree per node).
    for name in dict_adj:
        assert name in indexed_from_edges.node_index
        assert name in indexed_from_adj.node_index
        idx1 = indexed_from_edges.node_index[name]
        idx2 = indexed_from_adj.node_index[name]
        assert abs(indexed_from_edges.out_degree[idx1] - sum(dict_adj[name].values())) < 1e-12
        assert abs(indexed_from_adj.out_degree[idx2] - sum(dict_adj[name].values())) < 1e-12

    for seeds in (["k0"], ["k3", "k7"], ["k1", "k4", "k9"]):
        dict_result = personalized_pagerank(seeds, dict_adj, top_k=15)
        idx_from_edges = personalized_pagerank_indexed(
            seeds, indexed_from_edges, top_k=15
        )
        idx_from_adj = personalized_pagerank_indexed(
            seeds, indexed_from_adj, top_k=15
        )

        # Same nodes in the same tie-broken order.
        assert [k for k, _ in dict_result] == [k for k, _ in idx_from_edges], (
            f"order drift (edges) for {seeds}"
        )
        assert [k for k, _ in dict_result] == [k for k, _ in idx_from_adj], (
            f"order drift (adjacency) for {seeds}"
        )
        # Scores within float noise.
        for (k1, s1), (_, s2) in zip(dict_result, idx_from_edges):
            assert abs(s1 - s2) < 1e-9, f"{seeds}: {k1} dict={s1} idx-edges={s2}"
        for (k1, s1), (_, s2) in zip(dict_result, idx_from_adj):
            assert abs(s1 - s2) < 1e-9, f"{seeds}: {k1} dict={s1} idx-adj={s2}"


def test_indexed_ppr_missing_seed_returns_empty():
    edges = [Edge("install", "macos", 1.0), Edge("macos", "brew", 1.0)]
    graph = build_indexed_graph(edges)
    assert personalized_pagerank_indexed(["nonexistent-token"], graph) == []


def test_indexed_ppr_empty_graph_returns_empty():
    graph = build_indexed_graph([])
    assert personalized_pagerank_indexed(["install"], graph) == []
