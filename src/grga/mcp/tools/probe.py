"""MCP tool ``grga_probe`` — L0 cheap hit-count probe (plan §4.1)."""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

import grga.probe as probe_mod

VALID_PATTERN_MODES = ("auto", "regex", "glob")


async def grga_probe(
    query: List[str],
    directory: str,
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
    """
    if pattern_mode not in VALID_PATTERN_MODES:
        raise ValueError(
            f"pattern_mode must be one of: {', '.join(VALID_PATTERN_MODES)}"
        )
    if not directory or not str(directory).strip():
        raise ValueError("directory cannot be empty")
    if implicit_operator not in ("and", "or"):
        raise ValueError("implicit_operator must be 'and' or 'or'")

    raw = await asyncio.to_thread(
        probe_mod.probe_directory,
        directory,
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
