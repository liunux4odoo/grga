"""Shared helpers for the grga MCP server.

Kept dependency-light: only ``mcp.server.fastmcp.exceptions.ToolError``
is imported from the SDK here. Tools use these helpers for error wrapping,
DB-path resolution, and logging setup.
"""
from __future__ import annotations

import functools
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.server.fastmcp.exceptions import ToolError

_logger = logging.getLogger("grga.mcp")

_GRGA_DB_PATH_ENV = "GRGA_DB_PATH"
_GRGA_MCP_LOG_LEVEL_ENV = "GRGA_MCP_LOG_LEVEL"
_GRGA_MCP_DEFAULT_DIRECTORY_ENV = "GRGA_MCP_DEFAULT_DIRECTORY"


def resolve_db_path(db_path) -> str:
    """Resolve the graph DB path for a tool call.

    Priority: ``$GRGA_DB_PATH`` > explicit ``db_path`` argument > the
    shared default deep-search DB. Returns a string (or the default).
    """
    env_db = os.environ.get(_GRGA_DB_PATH_ENV)
    if env_db:
        return str(Path(env_db).expanduser())
    if db_path:
        return str(Path(db_path).expanduser())
    from grga.deep_search import DEFAULT_DB_PATH

    return str(DEFAULT_DB_PATH)


def resolve_default_directory() -> str:
    """Return the server-wide default search directory.

    Set by ``grga mcp start --directory <path>`` (via
    ``$GRGA_MCP_DEFAULT_DIRECTORY``). Tools fall back to it when the caller
    omits ``directory`` / ``base_dir``. Returns ``""`` when unset so callers
    can distinguish "no default" cheaply.
    """
    env_dir = os.environ.get(_GRGA_MCP_DEFAULT_DIRECTORY_ENV)
    if env_dir:
        return str(Path(env_dir).expanduser())
    return ""


def configure_logging() -> None:
    """Configure the ``grga.mcp`` logger from ``$GRGA_MCP_LOG_LEVEL``.

    Level is one of DEBUG / INFO / WARNING / ERROR (default WARNING).
    Logs go to stderr so they never pollute the stdio MCP protocol on
    stdout.
    """
    level = os.environ.get(_GRGA_MCP_LOG_LEVEL_ENV, "WARNING").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        level = "WARNING"
    if not _logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        _logger.addHandler(handler)
    _logger.setLevel(level)
    _logger.propagate = False


def grga_tool(func: Callable[..., Awaitable[Any]]):
    """Wrap an async tool function so unexpected exceptions become MCP ``ToolError``.

    grga internal functions signal recoverable failures two ways: raised
    exceptions and structured ``ok: false`` payloads. The latter pass
    through untouched (caller reads ``ok``); the former are translated to
    a human-readable :class:`ToolError` (never a Python traceback), per
    plan §6.2.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        _logger.info("tool %s called", func.__name__)
        try:
            return await func(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - boundary wrapper
            message = str(exc).strip() or type(exc).__name__
            _logger.warning("tool %s failed: %s", func.__name__, message)
            raise ToolError(message) from exc

    return wrapper
