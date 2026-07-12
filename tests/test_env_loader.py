"""Tests for :mod:`grga.env_loader` (.env autoload + example seeding)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from grga import env_loader


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("XDG_CONFIG_HOME", "GRGA_OFFLINE", "GRGA_OPENAI_API_KEY", "FOO_BAR"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_config_dir_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert env_loader.config_dir() == tmp_path / "grga"
    assert env_loader.env_path() == tmp_path / "grga" / ".env"


def test_config_dir_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert env_loader.config_dir() == tmp_path / ".config" / "grga"


def test_parse_env_text_basic():
    parsed = env_loader.parse_env_text(
        "\n".join(
            [
                "# comment",
                "",
                "FOO=bar",
                "QUOTED=\"has spaces\"",
                "SINGLE='single'",
                "export EXPORTED=yes",
                "no_equals_line",
                "=missingkey",
                "1BAD=nope",
            ]
        )
    )
    assert parsed == {
        "FOO": "bar",
        "QUOTED": "has spaces",
        "SINGLE": "single",
        "EXPORTED": "yes",
    }


def test_ensure_env_seeds_example_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = env_loader.ensure_env()
    assert path == tmp_path / "grga" / ".env"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    for name in (
        "GRGA_OPENAI_API_KEY",
        "GRGA_TOOLS_DIR",
        "GRGA_OFFLINE",
        "GRGA_SIDECAR_ROOT",
        "GRGA_DISPLAY_SCALE",
        "GRGA_PPR_MIN_NPMI",
    ):
        assert name in content
    # Everything is commented by default.
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            pytest.fail(f"uncommented line in example: {line!r}")


def test_ensure_env_loads_existing(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    env_file = tmp_path / "grga" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("FOO_BAR=hello\n", encoding="utf-8")

    env_loader.ensure_env()
    assert os.environ["FOO_BAR"] == "hello"


def test_load_does_not_override_real_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("FOO_BAR", "from-shell")
    env_file = tmp_path / "grga" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("FOO_BAR=from-file\n", encoding="utf-8")

    loaded, skipped = env_loader.load_env_file(env_file)
    assert os.environ["FOO_BAR"] == "from-shell"
    assert skipped == 1
    assert loaded == 0


def test_ensure_env_does_not_clobber_existing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    env_file = tmp_path / "grga" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("FOO_BAR=keep\n", encoding="utf-8")

    env_loader.ensure_env()
    assert env_file.read_text(encoding="utf-8") == "FOO_BAR=keep\n"


@pytest.mark.skipif(os.name != "posix", reason="mode bits only meaningful on POSIX")
def test_ensure_env_writes_0600_on_posix(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = env_loader.ensure_env()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
