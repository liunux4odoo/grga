"""LRU cache for the ``IndexedGraph`` used by ``related --mode graph``.

``load_edges_indexed`` (the id-native loader, see plan-2026-07-12-related-perf
§2.1) accounts for the bulk of a cold ``related --mode graph`` call, and a
caller agent typically issues several related queries against the same DB /
scope in quick succession. Caching cuts that per-call cost to a hash lookup.

Invalidation is by ``(db_path, db_mtime, scope_key, min_npmi)`` tuple: any
write to the DB (from ``index build`` / ``suggest`` / ``promote``) bumps the
mtime and the next call rebuilds; a different ``min_npmi`` occupies its own
cache slot to prevent thresholds from cross-contaminating. LRU cap =
``CACHE_MAXSIZE`` (8) so a caller who juggles a handful of scopes still gets
warm hits without unbounded memory growth.

The cache opens its own short-lived SQLite connection on miss; the caller's
connection is unaffected. The returned tuple is ``(edges, adj, indexed)``
for back-compat with existing 3-tuple unpacking, but ``adj`` is always
``None`` (the dict-of-dicts adjacency that :func:`build_adjacency` used to
produce was dead weight on the hot path — no consumer read it; see
plan §2.5 / P2.1) and ``edges`` is always ``()`` (no consumer needed the
raw ``Edge`` list either).  ``indexed`` is the sole live payload — callers
should treat it as read-only shared state; mutation is not part of the
contract.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple, Union

from grga.graph.ppr import IndexedGraph, load_edges_indexed
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
    min_npmi: float,
) -> IndexedGraph:
    conn = connect(db_path_str)
    try:
        return load_edges_indexed(conn, directory=scope_key, min_npmi=min_npmi)
    finally:
        conn.close()


def get_adjacency(
    db_path: Union[str, Path],
    scope_key: Optional[str],
    *,
    min_npmi: float = 0.0,
) -> Tuple[tuple, None, IndexedGraph]:
    """Return cached ``(edges=(), adj=None, indexed_graph)`` for the given key.

    ``scope_key`` is passed through unchanged: ``None`` means "whole graph".
    ``min_npmi`` (default 0.0) is added to the cache key so different
    thresholds do not share entries. The
    ``(db_path, mtime, scope_key, min_npmi)`` quadruple is the full key,
    so an ``index build`` that rewrites the DB naturally invalidates every
    scoped and unscoped entry for that file.

    The 3-tuple return shape is preserved for backward compatibility with
    callers that unpack ``_, _, indexed = get_adjacency(...)`` — but only
    the third slot carries live data. See module docstring for why the
    ``edges`` list and dict ``adjacency`` were dropped.
    """
    resolved = Path(db_path).expanduser().resolve()
    key = (str(resolved), _mtime_or_zero(resolved), scope_key, float(min_npmi))
    indexed = _cached_adjacency(*key)
    return (), None, indexed


def clear_adjacency_cache() -> None:
    """Drop every cached entry. Used by tests and by long-running callers
    that want to force a rebuild without touching the DB mtime."""
    _cached_adjacency.cache_clear()


def cache_info():
    """Expose the underlying ``lru_cache`` stats for tests / diagnostics."""
    return _cached_adjacency.cache_info()
