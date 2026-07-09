"""Regression tests for the P2 dependency-chain bug (Task 2).

``ensure_tools(["rga"])`` must implicitly include ``rg`` because ``rga``
shells out to it. The exact bug was: managed ``rga`` present, ``rg``
missing → ``find_tool("rga")`` returned successfully → ``rga`` crashed
at spawn time.
"""

from __future__ import annotations

import pytest

from grga import toolchain
from grga.toolchain import (
    MissingToolsError,
    OfflineError,
    TOOL_DEPENDENCIES,
    _expand_dependencies,
    ensure_tools,
)


def test_expand_rga_adds_rg():
    assert _expand_dependencies(["rga"]) == ["rg", "rga"]


def test_expand_dedup_preserves_order():
    assert _expand_dependencies(["rg", "rga"]) == ["rg", "rga"]


def test_expand_leaf_tool_unchanged():
    assert _expand_dependencies(["pandoc"]) == ["pandoc"]


def test_expand_empty():
    assert _expand_dependencies([]) == []


def test_rga_preproc_also_depends_on_rg():
    assert "rg" in TOOL_DEPENDENCIES["rga-preproc"]
    assert _expand_dependencies(["rga-preproc"]) == ["rg", "rga-preproc"]


def test_ensure_tools_reports_rg_when_only_rga_present(monkeypatch):
    """rga installed but rg missing → MissingToolsError.missing includes rg."""

    def fake_find_tool(command):
        return "/opt/grga/rga" if command == "rga" else None

    monkeypatch.setattr(toolchain, "find_tool", fake_find_tool)
    monkeypatch.setattr(toolchain, "add_tools_to_path", lambda: None)
    monkeypatch.setattr(toolchain, "maybe_migrate_legacy_tools", lambda progress=None: None)

    with pytest.raises(MissingToolsError) as exc:
        ensure_tools(["rga"], interactive=False, json_mode=True, offline=False)
    assert "rg" in exc.value.missing


def test_ensure_tools_offline_lists_both_missing(monkeypatch):
    monkeypatch.setattr(toolchain, "find_tool", lambda command: None)
    monkeypatch.setattr(toolchain, "add_tools_to_path", lambda: None)
    monkeypatch.setattr(toolchain, "maybe_migrate_legacy_tools", lambda progress=None: None)

    with pytest.raises(OfflineError) as exc:
        ensure_tools(["rga"], interactive=False, json_mode=False, offline=True)
    assert set(exc.value.missing) >= {"rg", "rga"}
