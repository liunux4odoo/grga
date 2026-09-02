"""Lightweight integration tests for the grga MCP server.

Runs the real tool adapters (real grga internals, no mocks) against the
test fixtures via the in-process ``FastMCP.call_tool`` API, and asserts the
return-value structure matches the documented contracts (plan §4, §8.2).
Only ``suggest`` is excluded: it needs a live LLM, so it is covered by the
unit tests (mocked) instead.

``search`` / ``probe`` need the ``rga`` binary; if it is unavailable the
tools return a structured ``ok: false`` response rather than raising, so
the tests assert the *shape* of that response too.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from grga.mcp.server import build_server

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"
DOCS = FIXTURES / "docs"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def server():
    return build_server()


def _call(server, name, args=None):
    return _run(server.call_tool(name, args or {}))


def _text(result) -> str:
    """Extract the JSON text payload from a FastMCP ContentBlock list."""
    blocks = list(result)
    assert blocks, "expected at least one content block"
    text = "".join(b.text for b in blocks if getattr(b, "type", None) == "text")
    return text


def _json(server, name, args=None):
    return json.loads(_text(_call(server, name, args)))


# ---------------------------------------------------------------------------
# expand — pure Python, no external tools
# ---------------------------------------------------------------------------


def test_expand_chapter_structure(server):
    result = _json(server, "grga_expand", {
        "file_path": "paragraphs.md",
        "anchor_line": 3,
        "level": "chapter",
        "base_dir": str(DOCS),
    })
    assert result["hit"]["path"] == "paragraphs.md"
    assert result["level"] in ("chapter", "paragraph", "lines")
    assert "text" in result and "range" in result and "truncated" in result


def test_expand_bad_level_returns_error(server):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="level must be one of"):
        _call(server, "grga_expand", {
            "file_path": "paragraphs.md",
            "anchor_line": 3,
            "level": "bogus",
        })


# ---------------------------------------------------------------------------
# scope_list — reads the shared DB (may be fresh → empty envelope)
# ---------------------------------------------------------------------------


def test_scope_list_shape(server):
    result = _json(server, "grga_scope_list")
    assert isinstance(result, dict)
    assert isinstance(result.get("scopes"), list)
    for scope in result["scopes"]:
        assert scope["name"]
        assert isinstance(scope["directories"], list)
        assert "n_pairs" in scope and "n_keywords" in scope and "n_docs" in scope


# ---------------------------------------------------------------------------
# search / probe — need rga; assert shape either way
# ---------------------------------------------------------------------------


def test_search_shape(server, tmp_path):
    src = tmp_path / "corpus"
    src.mkdir()
    (src / "doc.txt").write_text("alpha beta line one\nbeta only line\n", encoding="utf-8")
    result = _json(server, "grga_search", {
        "query": ["alpha"],
        "directory": str(src),
        "max_total": 100,
    })
    assert "ok" in result
    if result["ok"]:
        assert "matches" in result
        assert "truncated" in result and "total_files_matched" in result


def test_probe_shape(server, tmp_path):
    src = tmp_path / "corpus"
    src.mkdir()
    (src / "doc.txt").write_text("alpha beta\n", encoding="utf-8")
    result = _json(server, "grga_probe", {
        "query": ["alpha"],
        "directory": str(src),
    })
    assert "ok" in result
    if result["ok"]:
        assert "total_lines" in result and "file_count" in result


# ---------------------------------------------------------------------------
# related — reads graph DB; fresh DB → empty results
# ---------------------------------------------------------------------------


def test_related_shape(server, tmp_path):
    db = tmp_path / "graph.db"
    result = _json(server, "grga_related", {
        "seeds": ["alpha"],
        "db_path": str(db),
    })
    assert isinstance(result, dict)
    assert "results" in result and "resolved_seeds" in result
    assert isinstance(result["results"], list)
