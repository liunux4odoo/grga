"""LRU cache for the (edges, adjacency, indexed_graph) tuple.

``load_edges`` + ``build_adjacency`` + ``build_indexed_graph`` together
account for ~30% of a ``related --mode graph`` call, and a caller agent
typically issues several related queries against the same DB / scope in
quick succession. Caching cuts that per-call cost to a hash lookup.

Invalidation is by ``(db_path, db_mtime, scope_key)`` tuple: any write to
the DB (from ``index build`` / ``suggest`` / ``promote``) bumps the mtime
and the next call rebuilds. LRU cap = ``CACHE_MAXSIZE`` (8) so a caller
who juggles a handful of scopes still gets warm hits without unbounded
memory growth.

The cache opens its own short-lived SQLite connection on miss; the
caller's connection is unaffected. ``edges``, ``adjacency``, and
``indexed_graph`` are returned *by reference* (no deep copy), so callers
MUST treat them as read-only shared state. Every consumer in
``related.py`` derives its own per-call structures on top of these —
mutation of the cached objects is not part of the contract.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from grga.graph.ppr import (
    Edge,
    IndexedGraph,
    build_adjacency,
    build_indexed_graph,
    load_edges,
)
from grga.graph.schema import connect

CACHE_MAXSIZE = 8


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@lru_cache(maxsize=CACHE_MAXSIZE)
def _cached_adjacency(
    db_path_str: str,
    mtime: float,
    scope_key: Optional[str],
) -> Tuple[List[Edge], Dict[str, Dict[str, float]], IndexedGraph]:
    conn = connect(db_path_str)
    try:
        edges = load_edges(conn, directory=scope_key)
        adjacency = build_adjacency(edges)
        indexed = build_indexed_graph(edges)
        return edges, adjacency, indexed
    finally:
        conn.close()


def get_adjacency(
    db_path: Union[str, Path],
    scope_key: Optional[str],
) -> Tuple[List[Edge], Dict[str, Dict[str, float]], IndexedGraph]:
    """Return cached ``(edges, adjacency, indexed_graph)`` for the given key.

    ``scope_key`` is passed through to :func:`load_edges` unchanged: ``None``
    means "whole graph". The ``(db_path, mtime, scope_key)`` triple is the
    cache key, so an ``index build`` that rewrites the DB naturally
    invalidates every scoped and unscoped entry for that file.

    The returned ``indexed_graph`` is the preferred structure for hot-path
    callers (see :func:`grga.graph.ppr.personalized_pagerank_indexed`);
    ``adjacency`` is retained for legacy dict callers.
    """
    resolved = Path(db_path).expanduser().resolve()
    key = (str(resolved), _mtime_or_zero(resolved), scope_key)
    return _cached_adjacency(*key)


def clear_adjacency_cache() -> None:
    """Drop every cached entry. Used by tests and by long-running callers
    that want to force a rebuild without touching the DB mtime."""
    _cached_adjacency.cache_clear()


def cache_info():
    """Expose the underlying ``lru_cache`` stats for tests / diagnostics."""
    return _cached_adjacency.cache_info()
