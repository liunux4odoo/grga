"""MCP tool ``grga_probe`` — L0 cheap hit-count probe (plan §4.1)."""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from grga.mcp.utils import resolve_default_directory
import grga.probe as probe_mod

VALID_PATTERN_MODES = ("auto", "regex", "glob")


async def grga_probe(
    query: List[str],
    directory: Optional[str] = None,
    pattern_mode: str = "auto",
    case_sensitive: bool = False,
    timeout: int = 60,
    multiline: bool = False,
    glob_patterns: Optional[List[str]] = None,
    path_only: bool = False,
    per_file: bool = False,
    implicit_operator: str = "and",
) -> dict:
    """Probe how a query scales on disk: count matches without reading bodies.

    Runs ``rga --count-matches`` (or ``rg --files`` for ``path_only``) and
    returns structured metadata (total_lines / file_count / per-file counts)
    so a caller can decide whether to tighten or loosen a query before a
    full ``grga_search``. Never fetches match bodies or context.

    ``directory`` falls back to the server default set by
    ``grga mcp start --directory`` when omitted.
    """
    effective_directory = directory or resolve_default_directory()
    if not effective_directory or not str(effective_directory).strip():
        raise ValueError(
            "directory cannot be empty (pass directory, or start the server "
            "with `grga mcp start --directory <path>`)"
        )
    if pattern_mode not in VALID_PATTERN_MODES:
        raise ValueError(
            f"pattern_mode must be one of: {', '.join(VALID_PATTERN_MODES)}"
        )
    if implicit_operator not in ("and", "or"):
        raise ValueError("implicit_operator must be 'and' or 'or'")

    raw = await asyncio.to_thread(
        probe_mod.probe_directory,
        effective_directory,
        [k for k in (query or []) if isinstance(k, str) and k.strip()],
        pattern_mode=pattern_mode,
        case_sensitive=case_sensitive,
        timeout=timeout,
        multiline=multiline,
        glob_patterns=glob_patterns,
        path_only=path_only,
        per_file=per_file,
        implicit_operator=implicit_operator,
    )
    return json.loads(raw)
