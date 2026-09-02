"""MCP tool ``grga_suggest`` — L3 LLM suggestion (plan §4.5).

The plan's original draft described a keyword *prefix-completion* tool, but
grga's real ``suggest()`` is the L3 LLM fallback (query + hits → expanded
keywords / reformulated queries / hit reranking). Per the design decision,
this tool wraps the real ``grga.suggest.suggest()`` verb with
``record_votes_enabled=False`` so the MCP server stays on the read-only /
retrieval side (plan §1.1).
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from grga.mcp.utils import resolve_db_path
from grga.suggest import VALID_INTENTS, suggest

DEFAULT_MAX_HITS = 20
DEFAULT_SNIPPET_CHARS = 400


async def grga_suggest(
    query: str,
    intent: str = "expand",
    hits: Optional[List[dict]] = None,
    scope: Optional[str] = None,
    max_hits: int = DEFAULT_MAX_HITS,
    snippet_chars: int = DEFAULT_SNIPPET_CHARS,
    db_path: Optional[str] = None,
) -> dict:
    """Ask the L3 LLM to expand keywords, rerank hits, or reformulate a query.

    ``intent`` is one of ``expand`` / ``rerank`` / ``reformulate``. ``hits``
    is the list of prior search matches (each a dict with ``path`` /
    ``line_number`` / ``text``). This is the only grga verb that calls an
    LLM. The LLM endpoint / key / model come exclusively from the
    ``GRGA_OPENAI_*`` environment (or the server's own config) — they are
    **not** overridable per call, so a caller cannot redirect the request
    to an arbitrary endpoint. Read-only: emitted keyword pairs are **not**
    voted into the graph.
    """
    if not query or not str(query).strip():
        raise ValueError("query cannot be empty")
    if intent not in VALID_INTENTS:
        raise ValueError(
            f"intent must be one of: {', '.join(VALID_INTENTS)}"
        )

    suggestion = await asyncio.to_thread(
        suggest,
        str(query).strip(),
        list(hits or []),
        intent=intent,
        scope=scope,
        max_hits=max_hits,
        snippet_chars=snippet_chars,
        record_votes_enabled=False,
        db_path=resolve_db_path(db_path),
    )
    return suggestion.to_dict()
