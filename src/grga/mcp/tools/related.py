"""MCP tool ``grga_related`` — L2 keyword expansion via co-occurrence graph (plan §4.4).

Tool name stays ``grga_related`` (not ``grga_related_keywords``) because the
caller-facing verb is a graph-expansion action; ``_keywords`` is an internal
Python namespace distinction that should not leak into the contract.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from grga.mcp.utils import resolve_db_path
from grga.related import related_keywords

VALID_TAG_LOGIC = ("or", "and")


async def grga_related(
    seeds: List[str],
    limit_per_seed: int = 30,
    min_npmi: Optional[float] = None,
    scopes: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    tag_logic: str = "or",
    db_path: Optional[str] = None,
) -> dict:
    """Return candidate keywords co-occurring with the seed keywords.

    For each seed, loads its top ``limit_per_seed`` neighbours from the
    co-occurrence graph (ordered by stored npmi) tagged with which seed
    they came from. ``min_npmi`` can only tighten the build-time floor.
    ``scopes`` / ``tags`` filter by logical scope name / scope tag
    (``tag_logic`` = ``or`` / ``and``); both empty = cross-scope union.
    """
    if tag_logic not in VALID_TAG_LOGIC:
        raise ValueError(f"tag_logic must be one of: {', '.join(VALID_TAG_LOGIC)}")
    if min_npmi is not None and min_npmi < 0:
        raise ValueError("min_npmi cannot be negative")

    cleaned_seeds = [s for s in seeds if isinstance(s, str) and s.strip()]
    if not cleaned_seeds:
        raise ValueError("seeds cannot be empty")

    result = await asyncio.to_thread(
        related_keywords,
        cleaned_seeds,
        limit_per_seed=limit_per_seed,
        min_npmi=min_npmi,
        scopes=[s for s in (scopes or []) if s and s.strip()] or None,
        tags=[t for t in (tags or []) if t and t.strip()] or None,
        tag_logic=tag_logic,
        db_path=resolve_db_path(db_path),
    )
    return result.to_dict()
