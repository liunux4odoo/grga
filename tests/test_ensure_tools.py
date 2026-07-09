"""ensure_tools() decision-matrix tests (Step 3.2).

Unit-level coverage of the branch matrix: present / missing × interactive
/ json / offline × prompt yes/no × partial install. CLI exit-code mapping
is covered separately in the CLI tests.
"""

from __future__ import annotations

import pytest

from grga import toolchain
from grga.toolchain import (
    MissingToolsError,
    OfflineError,
    PartialInstallError,
    UserAbortError,
    ensure_tools,
)


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setattr(toolchain, "add_tools_to_path", lambda: None)
    monkeypatch.setattr(toolchain, "maybe_migrate_legacy_tools", lambda progress=None: None)


def _present(*names):
    present = set(names)
    return lambda command: f"/opt/{command}" if command in present else None


def test_all_present_is_silent(monkeypatch):
    monkeypatch.setattr(toolchain, "find_tool", _present("rg", "rga"))
    assert ensure_tools(["rga"], interactive=False, json_mode=False, offline=False) == []


def test_empty_required_noop(monkeypatch):
    monkeypatch.setattr(toolchain, "find_tool", lambda command: None)
    assert ensure_tools([], interactive=True, json_mode=False, offline=False) == []


def test_missing_json_mode_raises(monkeypatch):
    monkeypatch.setattr(toolchain, "find_tool", lambda command: None)
    with pytest.raises(MissingToolsError):
        ensure_tools(["rga"], interactive=False, json_mode=True, offline=False)


def test_missing_noninteractive_raises(monkeypatch):
    monkeypatch.setattr(toolchain, "find_tool", lambda command: None)
    with pytest.raises(MissingToolsError):
        ensure_tools(["rga"], interactive=False, json_mode=False, offline=False)


def test_missing_offline_raises(monkeypatch):
    monkeypatch.setattr(toolchain, "find_tool", lambda command: None)
    with pytest.raises(OfflineError):
        ensure_tools(["rga"], interactive=True, json_mode=False, offline=True)


def test_interactive_prompt_yes_installs(monkeypatch):
    calls = {"present": False}

    def find_tool(command):
        return f"/opt/{command}" if calls["present"] else None

    def fake_install(names=None, base_url=None, progress=None):
        calls["present"] = True
        return [{"name": n, "status": "installed"} for n in (names or [])]

    monkeypatch.setattr(toolchain, "find_tool", find_tool)
    monkeypatch.setattr(toolchain, "install_tools", fake_install)
    result = ensure_tools(["rga"], interactive=True, json_mode=False, offline=False, prompt=lambda names: True)
    assert result == []


def test_interactive_prompt_no_aborts(monkeypatch):
    monkeypatch.setattr(toolchain, "find_tool", lambda command: None)
    with pytest.raises(UserAbortError):
        ensure_tools(["rga"], interactive=True, json_mode=False, offline=False, prompt=lambda names: False)


def test_partial_install_raises(monkeypatch):
    def fake_install(names=None, base_url=None, progress=None):
        # never makes tools resolvable → still missing afterwards
        return []

    monkeypatch.setattr(toolchain, "find_tool", lambda command: None)
    monkeypatch.setattr(toolchain, "install_tools", fake_install)
    with pytest.raises(PartialInstallError):
        ensure_tools(["rga"], interactive=True, json_mode=False, offline=False, prompt=lambda names: True)
