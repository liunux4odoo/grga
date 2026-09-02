"""MCP tool ``grga_scope_list`` — list logical scopes (plan §4.6).

Returns ``{"scopes": [...]}`` (not a bare list): FastMCP serialises an
empty list to zero content blocks, which a caller cannot distinguish from
a broken response. The dict envelope keeps the empty case explicit.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from grga.graph.scope import list_scopes
from grga.mcp.utils import resolve_db_path


async def grga_scope_list(db_path: Optional[str] = None) -> dict:
    """List every logical scope with its directory bindings and sizes.

    Each entry carries ``name`` / ``tags`` / ``directories[]`` (local path
    bindings, evidence layer) and sizing signals (``n_pairs`` /
    ``n_keywords`` / ``n_docs``). Empty on a fresh DB. The returned
    ``directories`` let a caller map a scope name to a physical search
    directory for ``grga_search`` / ``grga_probe``.
    """
    scopes = await asyncio.to_thread(list_scopes, db_path=resolve_db_path(db_path))
    return {"scopes": [s.to_dict() for s in scopes]}
