"""Unit tests for the grga MCP tool adapters.

Each tool gets a normal-return and an error-path case. grga internals are
mocked (``unittest.mock``) so these tests never touch a real corpus, rga,
or an LLM. The ``grga_tool`` wrapper is applied in :mod:`grga.mcp.server`,
so here we call the raw async functions directly; the tests assert both the
happy path and that bad input raises (the wrapper translates to ToolError).
"""
from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest

from grga.mcp.tools import expand as expand_tool
from grga.mcp.tools import probe as probe_tool
from grga.mcp.tools import related as related_tool
from grga.mcp.tools import scope as scope_tool
from grga.mcp.tools import search as search_tool
from grga.mcp.tools import suggest as suggest_tool


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_passes_through_json_response():
    payload = {"ok": True, "total_lines": 42, "file_count": 3}
    with mock.patch.object(probe_tool.probe_mod, "probe_directory", return_value=json.dumps(payload)):
        result = _run(probe_tool.grga_probe(["苍坤"], "/data", per_file=True))
    assert result == payload


def test_probe_rejects_empty_directory():
    with pytest.raises(ValueError):
        _run(probe_tool.grga_probe(["x"], "   "))


def test_probe_rejects_bad_pattern_mode():
    with pytest.raises(ValueError):
        _run(probe_tool.grga_probe(["x"], "/data", pattern_mode="nope"))


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_passes_through_json_response():
    payload = {"ok": True, "matches": [{"path": "a.txt", "line_number": 1, "text": "hit"}]}
    with mock.patch.object(search_tool.search_mod, "search_directory", return_value=json.dumps(payload)) as m:
        result = _run(search_tool.grga_search(["关键词"], "/data"))
    assert result == payload
    # default cap is bounded (not None) so large corpora don't stream unbounded
    assert m.call_args.kwargs["max_total"] == 1000


def test_search_zero_max_total_means_unlimited():
    payload = {"ok": True, "matches": []}
    with mock.patch.object(search_tool.search_mod, "search_directory", return_value=json.dumps(payload)) as m:
        _run(search_tool.grga_search(["x"], "/data", max_total=0))
    assert m.call_args.kwargs["max_total"] is None


def test_search_rejects_bad_implicit_operator():
    with pytest.raises(ValueError):
        _run(search_tool.grga_search(["x"], "/data", implicit_operator="xor"))


def test_search_rejects_unsupported_pattern_mode():
    # ``literal``/``pcre2`` are not implemented by search_directory; the
    # tool must reject them instead of advertising a value that would crash.
    with pytest.raises(ValueError, match="pattern_mode must be one of: auto, regex, glob"):
        _run(search_tool.grga_search(["x"], "/data", pattern_mode="literal"))


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------


def test_expand_returns_context_dict():
    ctx_dict = {
        "hit": {"path": "f.md", "line_number": 5, "text": ""},
        "text": "body",
        "level": "lines",
        "range": [4, 6],
        "truncated": False,
    }
    fake_ctx = mock.Mock()
    fake_ctx.to_dict.return_value = ctx_dict
    with mock.patch.object(expand_tool, "expand_hit", return_value=fake_ctx) as m:
        result = _run(expand_tool.grga_expand("f.md", 5, "lines", size=1, base_dir="/base"))
    assert result == ctx_dict
    hit = m.call_args.args[0]
    assert hit.path == "f.md" and hit.line_number == 5


def test_expand_rejects_unknown_level():
    with pytest.raises(ValueError):
        _run(expand_tool.grga_expand("f.md", 5, "bogus"))


def test_expand_rejects_bad_anchor_line():
    with pytest.raises(ValueError):
        _run(expand_tool.grga_expand("f.md", 0, "lines"))


# ---------------------------------------------------------------------------
# related
# ---------------------------------------------------------------------------


def test_related_returns_result_dict():
    result_dict = {
        "results": [{"kw": "遗书", "npmi": 0.87, "via_seed": "苍坤上人", "scopes": ["fanren"]}],
        "resolved_seeds": [{"seed": "苍坤上人", "in_index": True}],
    }
    fake = mock.Mock()
    fake.to_dict.return_value = result_dict
    with mock.patch.object(related_tool, "related_keywords", return_value=fake) as m:
        result = _run(related_tool.grga_related(["苍坤上人"], scopes=["fanren"]))
    assert result == result_dict
    _, kwargs = m.call_args
    assert kwargs["scopes"] == ["fanren"]
    assert kwargs["tag_logic"] == "or"


def test_related_rejects_empty_seeds():
    with pytest.raises(ValueError):
        _run(related_tool.grga_related([]))


def test_related_rejects_whitespace_only_seeds():
    with pytest.raises(ValueError, match="seeds cannot be empty"):
        _run(related_tool.grga_related(["   ", ""]))


def test_related_rejects_bad_tag_logic():
    with pytest.raises(ValueError):
        _run(related_tool.grga_related(["x"], tag_logic="nand"))


# ---------------------------------------------------------------------------
# suggest
# ---------------------------------------------------------------------------


def test_suggest_read_only_wraps_real_api():
    sug_dict = {"intent": "expand", "keywords": ["新词"], "queries": [], "ranking": []}
    fake = mock.Mock()
    fake.to_dict.return_value = sug_dict
    with mock.patch.object(suggest_tool, "suggest", return_value=fake) as m:
        result = _run(suggest_tool.grga_suggest(
            "原查询", hits=[{"path": "a", "line_number": 1, "text": "t"}]
        ))
    assert result == sug_dict
    _, kwargs = m.call_args
    assert kwargs["record_votes_enabled"] is False
    assert kwargs["intent"] == "expand"


def test_suggest_rejects_empty_query():
    with pytest.raises(ValueError):
        _run(suggest_tool.grga_suggest("   "))


def test_suggest_rejects_bad_intent():
    with pytest.raises(ValueError):
        _run(suggest_tool.grga_suggest("q", intent="teleport"))


# ---------------------------------------------------------------------------
# scope_list
# ---------------------------------------------------------------------------


def test_scope_list_returns_envelope():
    fake_scope = mock.Mock()
    fake_scope.to_dict.return_value = {"name": "fanren", "tags": [], "n_pairs": 3}
    with mock.patch.object(scope_tool, "list_scopes", return_value=[fake_scope]) as m:
        result = _run(scope_tool.grga_scope_list())
    assert result == {"scopes": [{"name": "fanren", "tags": [], "n_pairs": 3}]}
    assert m.call_args.kwargs["db_path"]  # resolve_db_path always returns a path


def test_scope_list_empty_returns_empty_envelope():
    with mock.patch.object(scope_tool, "list_scopes", return_value=[]):
        result = _run(scope_tool.grga_scope_list())
    assert result == {"scopes": []}
