"""Boundary tests for :mod:`grga.paths` (Step 2.1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from grga import paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("GRGA_TOOLS_DIR", "GRGA_STATE_DIR", "XDG_STATE_HOME", "XDG_DATA_HOME", "LOCALAPPDATA", "APPDATA"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _force_posix(monkeypatch):
    monkeypatch.setattr(paths, "_is_windows", lambda: False)


def _force_windows(monkeypatch):
    monkeypatch.setattr(paths, "_is_windows", lambda: True)


def test_resolve_tools_root_platform_default_posix(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/home/user")))
    assert paths.resolve_tools_root() == Path("/home/user/.local/share/grga/tools")


def test_resolve_tools_root_platform_default_windows(monkeypatch):
    _force_windows(monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\me\\AppData\\Local")
    assert paths.resolve_tools_root() == Path("C:\\Users\\me\\AppData\\Local") / "grga" / "tools"


def test_resolve_tools_root_windows_no_localappdata_falls_back(monkeypatch):
    _force_windows(monkeypatch)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("C:\\Users\\me")))
    assert paths.resolve_tools_root() == Path("C:\\Users\\me/AppData/Local/grga/tools")


def test_grga_tools_dir_override(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setenv("GRGA_TOOLS_DIR", "/tmp/x")
    assert paths.resolve_tools_root() == Path("/tmp/x")


def test_grga_tools_dir_empty_string_ignored(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setenv("GRGA_TOOLS_DIR", "")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/home/user")))
    assert paths.resolve_tools_root() == Path("/home/user/.local/share/grga/tools")


def test_grga_tools_dir_whitespace_only_ignored(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setenv("GRGA_TOOLS_DIR", "   ")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/home/user")))
    assert paths.resolve_tools_root() == Path("/home/user/.local/share/grga/tools")


def test_xdg_data_home_used_when_no_override(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg")
    assert paths.resolve_tools_root() == Path("/tmp/xdg/grga/tools")


def test_grga_tools_dir_beats_xdg(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setenv("GRGA_TOOLS_DIR", "/tmp/override")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg")
    assert paths.resolve_tools_root() == Path("/tmp/override")


def test_resolve_tools_dir_appends_platform(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setenv("GRGA_TOOLS_DIR", "/tmp/x")
    assert paths.resolve_tools_dir("linux-aarch64") == Path("/tmp/x/linux-aarch64")


def test_resolve_tools_dir_does_not_create_directory(monkeypatch, tmp_path):
    _force_posix(monkeypatch)
    root = tmp_path / "cache"
    monkeypatch.setenv("GRGA_TOOLS_DIR", str(root))
    result = paths.resolve_tools_dir("linux-x86_64")
    assert not result.exists()


def test_state_dir_priority_grga_state_dir(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setenv("GRGA_STATE_DIR", "/tmp/state")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xstate")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdata")
    assert paths.resolve_state_dir() == Path("/tmp/state")


def test_state_dir_priority_xdg_state_home(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xstate")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdata")
    assert paths.resolve_state_dir() == Path("/tmp/xstate/grga")


def test_state_dir_priority_xdg_data_home(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdata")
    assert paths.resolve_state_dir() == Path("/tmp/xdata/grga")


def test_state_dir_platform_default_posix(monkeypatch):
    _force_posix(monkeypatch)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/home/user")))
    assert paths.resolve_state_dir() == Path("/home/user/.local/share/grga")


def test_state_dir_platform_default_windows(monkeypatch):
    _force_windows(monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Local")
    assert paths.resolve_state_dir() == Path("C:\\Local") / "grga"
