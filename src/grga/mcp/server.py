"""FastMCP server exposing grga's five retrieval verbs + scope list.

Entry point: ``grga mcp start`` (stdlib stdio transport). Tools are
registered programmatically from ``grga.mcp.tools`` so each tool stays a
thin adapter (plan §3). Server logs go to stderr via
:func:`grga.mcp.utils.configure_logging`.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from grga.mcp import utils
from grga.mcp.tools import expand as expand_tool
from grga.mcp.tools import probe as probe_tool
from grga.mcp.tools import related as related_tool
from grga.mcp.tools import scope as scope_tool
from grga.mcp.tools import search as search_tool
from grga.mcp.tools import suggest as suggest_tool

_SERVER_NAME = "grga"

_SERVER_INSTRUCTIONS = (
    "grga retrieval server over a local corpus and offline co-occurrence "
    "graph. Use the reflection loop probe → search → expand/related → "
    "suggest: probe to size a query cheaply, search for typed hits, expand "
    "a hit into wider context, related to find co-occurring keywords, and "
    "suggest only as the LLM fallback. probe / search / expand / related / "
    "scope_list are offline and read-only; suggest calls the configured "
    "OpenAI-compatible LLM (GRGA_OPENAI_* env)."
)

_TOOLS = (
    (
        probe_tool.grga_probe,
        "grga_probe",
        "L0 cheap probe: count how a query scales on disk without reading "
        "match bodies. Returns total_lines / file_count / optional per-file "
        "counts so a caller can tighten or loosen a query before a full search.",
    ),
    (
        search_tool.grga_search,
        "grga_search",
        "L1 structured ripgrep-all search. Returns ok / matches[] (path, "
        "line_number, text, submatches, before/after context) plus "
        "truncated / total_files_matched. max_total caps real matches.",
    ),
    (
        expand_tool.grga_expand,
        "grga_expand",
        "L1 context expansion: turn a hit (file_path + anchor_line) into a "
        "lines / paragraph / chapter / document / toc block. The returned "
        "level reflects the level actually achieved after degrade.",
    ),
    (
        related_tool.grga_related,
        "grga_related",
        "L2 keyword expansion: for each seed, return top co-occurring "
        "candidate keywords from the offline graph (npmi / cooccur_count / "
        "chunk_df / via_seed / scopes) + per-seed vocabulary probe.",
    ),
    (
        suggest_tool.grga_suggest,
        "grga_suggest",
        "L3 LLM fallback: expand keywords, rerank hits, or reformulate a "
        "query. intent = expand | rerank | reformulate. Read-only (pairs "
        "are not voted into the graph). LLM endpoint/key/model come from "
        "the GRGA_OPENAI_* env (not overridable per call).",
    ),
    (
        scope_tool.grga_scope_list,
        "grga_scope_list",
        "List every logical scope with its local directory bindings and "
        "sizing signals (n_pairs / n_keywords / n_docs). Empty on a fresh DB.",
    ),
)


def build_server() -> FastMCP:
    """Construct and return a configured ``FastMCP`` instance."""
    utils.configure_logging()
    server = FastMCP(_SERVER_NAME, instructions=_SERVER_INSTRUCTIONS)
    for fn, name, description in _TOOLS:
        server.tool(name=name, description=description)(utils.grga_tool(fn))
    return server


def run_server() -> None:
    """Run the MCP server in stdio mode (blocking)."""
    build_server().run(transport="stdio")
