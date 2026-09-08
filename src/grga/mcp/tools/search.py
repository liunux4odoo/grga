"""MCP tool ``grga_search`` — L1 structured ripgrep-all search (plan §4.2)."""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

import grga.search as search_mod
from grga.mcp.utils import resolve_default_directory

# ``search_directory`` only supports these modes; ``literal`` / ``pcre2``
# are not implemented by the engine and would raise at query-compile time.
VALID_PATTERN_MODES = ("auto", "regex", "glob")

# Default cap on total real matches, matching the CLI's ``--max-total``
# default. ``max_total <= 0`` (or ``None``) opts into unlimited results.
DEFAULT_MAX_TOTAL = 100


async def grga_search(
    query: List[str],
    directory: Optional[str] = None,
    pattern_mode: str = "auto",
    case_sensitive: bool = False,
    max_total: Optional[int] = DEFAULT_MAX_TOTAL,
    before_context: int = 3,
    after_context: int = 3,
    multiline: bool = False,
    glob_patterns: Optional[List[str]] = None,
    glob_exclude: bool = False,
    path_only: bool = False,
    implicit_operator: str = "and",
) -> dict:
    """Structured ripgrep-all search returning typed hits with context.

    Returns ``ok`` / ``matches[]`` (path, line_number, text, submatches,
    before/after context) plus ``truncated`` / ``total_files_matched``.
    ``max_total`` caps the total real matches (default 100; pass ``0`` or
    ``null`` for unlimited); the result is ``truncated: true`` when the
    cap is exceeded. ``directory`` falls back to the server default set by
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

    # ``search_directory`` stops streaming only when ``max_total`` is set;
    # ``0`` / ``None`` mean unlimited (mirrors the CLI's ``--max-total 0``).
    effective_max_total = None if (max_total is None or max_total <= 0) else max_total

    raw = await asyncio.to_thread(
        search_mod.search_directory,
        effective_directory,
        [k for k in (query or []) if isinstance(k, str) and k.strip()],
        pattern_mode=pattern_mode,
        case_sensitive=case_sensitive,
        before_context=before_context,
        after_context=after_context,
        multiline=multiline,
        glob_patterns=glob_patterns,
        glob_exclude=glob_exclude,
        path_only=path_only,
        max_total=effective_max_total,
        implicit_operator=implicit_operator,
    )
    return json.loads(raw)
