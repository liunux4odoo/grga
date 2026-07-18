"""CLI tests for `grga expand` (Phase 3.4)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga.cli import app

runner = CliRunner()


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))


def test_expand_chapter_json(docs_fixture: Path, sidecar_env):
    result = runner.invoke(
        app,
        ["expand", "install.md:5", "--level", "chapter", "-d", str(docs_fixture), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["level"] == "chapter"
    assert "brew install grga" in payload["text"]
    assert payload["range"][0] <= 5 <= payload["range"][1]


def test_expand_chapter_plain_output(docs_fixture: Path, sidecar_env):
    result = runner.invoke(
        app,
        ["expand", "install.md:5", "--level", "chapter", "-d", str(docs_fixture)],
    )
    assert result.exit_code == 0, result.output
    assert "brew install grga" in result.output


def test_expand_degrades_on_non_markdown(docs_fixture: Path, sidecar_env):
    result = runner.invoke(
        app,
        ["expand", "code_sample.py:4", "--level", "chapter", "-d", str(docs_fixture), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["level"] in ("paragraph", "lines")


def test_expand_lines_size(docs_fixture: Path):
    result = runner.invoke(
        app,
        ["expand", "install.md:5", "--level", "lines", "--size", "1", "-d", str(docs_fixture), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["level"] == "lines"
    assert payload["range"] == [4, 6]


def test_expand_bad_target_format(docs_fixture: Path):
    result = runner.invoke(app, ["expand", "install.md", "-d", str(docs_fixture)])
    assert result.exit_code == 2


def test_expand_bad_line_number(docs_fixture: Path):
    result = runner.invoke(app, ["expand", "install.md:abc", "-d", str(docs_fixture)])
    assert result.exit_code == 2


def test_expand_invalid_level(docs_fixture: Path):
    result = runner.invoke(app, ["expand", "install.md:5", "--level", "galaxy", "-d", str(docs_fixture)])
    assert result.exit_code == 2


def test_expand_toc_json(docs_fixture: Path, sidecar_env):
    result = runner.invoke(
        app,
        ["expand", "install.md:7", "--level", "toc", "-d", str(docs_fixture), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["level"] == "toc"
    assert payload["truncated"] is False
    # Every heading in install.md should appear in the outline text.
    for title in ("安装指南", "macOS", "Linux", "Ubuntu", "Fedora", "Windows"):
        assert title in payload["text"], f"missing heading {title!r} in TOC output"
    # The hit chapter should be marked with the '> ' prefix.
    assert "> " in payload["text"]


def test_expand_toc_plain_output(docs_fixture: Path, sidecar_env):
    result = runner.invoke(
        app,
        ["expand", "install.md:7", "--level", "toc", "-d", str(docs_fixture)],
    )
    assert result.exit_code == 0, result.output
    assert "macOS" in result.output
    assert "level=toc" in result.output


def test_expand_toc_degrades_on_non_markdown(docs_fixture: Path, sidecar_env):
    result = runner.invoke(
        app,
        ["expand", "code_sample.py:4", "--level", "toc", "-d", str(docs_fixture), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # No heading tree available -> falls through the chapter degrade chain.
    assert payload["level"] in ("chapter", "paragraph", "lines")
