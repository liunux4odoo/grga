"""MCP tool ``grga_expand`` — L1 context expansion (plan §4.3).

Note: the plan's draft listed ``base_dir`` / ``scope`` as alternatives, but
the real :func:`grga.expand.expand` only accepts ``base_dir`` and plan §6.1
binds the retrieval tools to ``directory``-style paths only (no scope →
directory bridging). ``scope`` is therefore intentionally omitted.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from grga.expand import expand as expand_hit
from grga.models import Hit

VALID_LEVELS = ("lines", "paragraph", "chapter", "document", "toc")


async def grga_expand(
    file_path: str,
    anchor_line: int,
    level: str,
    size: int = 3,
    base_dir: Optional[str] = None,
) -> dict:
    """Expand a hit into a wider context block.

    ``level`` degrades predictably (chapter → paragraph → lines → single
    line); the returned ``level`` field reflects the level actually
    achieved. ``document`` / ``toc`` carry a built-in 1 MB safety cap and
    set ``truncated: true`` when they exceed it.
    """
    if not file_path or not str(file_path).strip():
        raise ValueError("file_path cannot be empty")
    if level not in VALID_LEVELS:
        raise ValueError(f"level must be one of: {', '.join(VALID_LEVELS)}")
    if anchor_line < 1:
        raise ValueError("anchor_line must be a positive line number")
    if size < 0:
        raise ValueError("size cannot be negative")

    hit = Hit(path=file_path, line_number=anchor_line, text="")
    context = await asyncio.to_thread(
        expand_hit, hit, level=level, size=size, base_dir=base_dir
    )
    return context.to_dict()
