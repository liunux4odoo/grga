"""P3 numpy backend equivalence — byte-identical top-K to pure-Python PPR.

Contract: :func:`grga.graph.ppr._ppr_numpy` and :func:`_ppr_python` must
produce the same top-K keywords under the same seeds and graph, with the
same tie-break rule ``(-score, node_id_string)``.

Guarded by ``importorskip('numpy')`` — grga's numpy dependency is
optional (``pip install grga[perf]``); if numpy is unavailable the
comparison test is skipped and only the pure-Python path is exercised.

See ``docs/design/plan-2026-07-12-related-perf.md`` §2.3 and
``experiments/related_perf_2026_07_12/FINDINGS.md`` §F2.
"""
from __future__ import annotations

import math
import random
from typing import List

import pytest

from grga.graph.ppr import (
    DEFAULT_ALPHA,
    DEFAULT_ITERS,
    Edge,
    _select_backend,
    build_indexed_graph,
    personalized_pagerank_indexed,
)


def _synth_graph(n_nodes: int, n_edges: int, seed: int = 42) -> List[Edge]:
    """Synthesise a reproducible undirected weighted graph."""
    rnd = random.Random(seed)
    nodes = [f"n{idx:05d}" for idx in range(n_nodes)]
    seen: set = set()
    edges: List[Edge] = []
    while len(edges) < n_edges:
        a, b = rnd.sample(nodes, 2)
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        w = rnd.uniform(0.05, 1.0)
        edges.append(Edge(a=key[0], b=key[1], weight=w))
    return edges


def test_backend_selection_defaults():
    """auto backend: py below NUMPY_MIN_EDGES, numpy above (if importable)."""
    from grga.graph.ppr import _HAS_NUMPY, NUMPY_MIN_EDGES

    small = _select_backend(NUMPY_MIN_EDGES // 2)
    large = _select_backend(NUMPY_MIN_EDGES + 1)
    assert small == "py"
    if _HAS_NUMPY:
        assert large == "numpy"
    else:
        assert large == "py"


def test_backend_env_override_py(monkeypatch):
    monkeypatch.setenv("GRGA_PPR_BACKEND", "py")
    assert _select_backend(10_000_000) == "py"


def test_backend_env_override_numpy_missing_numpy_raises(monkeypatch):
    """Requesting numpy explicitly while it is unavailable is a hard error."""
    monkeypatch.setenv("GRGA_PPR_BACKEND", "numpy")
    monkeypatch.setattr("grga.graph.ppr._HAS_NUMPY", False)
    with pytest.raises(RuntimeError, match="numpy is not importable"):
        _select_backend(1000)


def test_numpy_and_python_topk_equivalence(monkeypatch):
    """numpy vs py backend: identical top-K on a 200-node / 2k-edge graph.

    Skipped when numpy is unavailable in the test environment.
    """
    pytest.importorskip("numpy")
    edges = _synth_graph(n_nodes=200, n_edges=2000, seed=42)
    graph = build_indexed_graph(edges)
    # Pick 3 nodes that are actually in the graph (endpoints of edges) as
    # seeds. n00000 always appears via _synth's first-node bias but be
    # defensive.
    seeds = [graph.node_ids[0], graph.node_ids[7], graph.node_ids[33]]

    monkeypatch.setenv("GRGA_PPR_BACKEND", "py")
    from grga.graph.ppr import _select_backend as _sel

    assert _sel(len(edges)) == "py"
    py_result = personalized_pagerank_indexed(
        seeds, graph, top_k=25, alpha=DEFAULT_ALPHA, iters=DEFAULT_ITERS,
    )
    monkeypatch.setenv("GRGA_PPR_BACKEND", "numpy")
    assert _sel(len(edges)) == "numpy"
    np_result = personalized_pagerank_indexed(
        seeds, graph, top_k=25, alpha=DEFAULT_ALPHA, iters=DEFAULT_ITERS,
    )

    py_keys = [k for k, _ in py_result]
    np_keys = [k for k, _ in np_result]
    assert py_keys == np_keys, (
        f"top-25 keyword sequence differs between backends:\n"
        f"  py: {py_keys}\n  np: {np_keys}"
    )
    # Score alignment — small tolerance for float ordering.
    for (_, s_py), (_, s_np) in zip(py_result, np_result):
        assert math.isclose(s_py, s_np, rel_tol=1e-9, abs_tol=1e-12), (
            f"score divergence beyond float tolerance: py={s_py} np={s_np}"
        )


def test_numpy_backend_handles_dangling_nodes(monkeypatch):
    """A graph with isolated (dangling) nodes still respects seed leak."""
    pytest.importorskip("numpy")
    # Two components: one connected pair, one isolated node.
    edges = [Edge(a="alpha", b="beta", weight=1.0)]
    graph = build_indexed_graph(edges)
    # Manually inject an isolated node — build_indexed_graph doesn't
    # produce dangling nodes, so we mutate the returned structure with
    # one that has zero out_degree.
    graph.node_ids.append("orphan")
    graph.node_index["orphan"] = len(graph.node_ids) - 1
    graph.neighbors.append([])
    graph.out_degree.append(0.0)

    monkeypatch.setenv("GRGA_PPR_BACKEND", "numpy")
    result = personalized_pagerank_indexed(
        ["alpha"], graph, top_k=5, alpha=0.2, iters=20,
    )
    # orphan should not appear in top-K (no edges, no mass reaches it).
    kws = [k for k, _ in result]
    assert "orphan" not in kws
    # beta should dominate because alpha is the seed.
    assert kws and kws[0] == "beta"


def test_empty_graph_returns_empty(monkeypatch):
    """Empty graph short-circuits regardless of backend."""
    pytest.importorskip("numpy")
    from grga.graph.ppr import IndexedGraph

    empty = IndexedGraph(node_ids=[], node_index={}, neighbors=[], out_degree=[])
    monkeypatch.setenv("GRGA_PPR_BACKEND", "numpy")
    assert personalized_pagerank_indexed(["x"], empty, top_k=5) == []
    monkeypatch.setenv("GRGA_PPR_BACKEND", "py")
    assert personalized_pagerank_indexed(["x"], empty, top_k=5) == []
