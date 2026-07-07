"""CLI smoke tests (Phase 0.4).

These tests exercise the top-level `grga` command against the fixtures corpus
and verify that both the plain and `--json` output paths return non-empty
results with the expected fields. They act as a regression fence around the
v1 self-use capability while v2 modules are added on top.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga.cli import app


runner = CliRunner()


@pytest.fixture(scope="module")
def rga_available() -> bool:
    from grga.search import _find_rga_command  # type: ignore[attr-defined]

    return _find_rga_command() is not None


def test_cli_search_json_exit_ok(docs_fixture: Path, rga_available: bool):
    if not rga_available:
        pytest.skip("rga not available")
    result = runner.invoke(
        app,
        ["search", "-d", str(docs_fixture), "--json", "macOS"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload.get("ok") is True
    matches = payload.get("matches", [])
    hits = [m for m in matches if not m.get("is_context")]
    assert len(hits) > 0
    first = hits[0]
    for key in ("path", "line_number", "text", "is_context"):
        assert key in first


def test_cli_search_plain_output(docs_fixture: Path, rga_available: bool):
    if not rga_available:
        pytest.skip("rga not available")
    result = runner.invoke(
        app,
        ["search", "-d", str(docs_fixture), "hello"],
    )
    assert result.exit_code == 0, result.output
    assert "hello" in result.output.lower()
    assert "matches in" in result.stderr or "matches in" in result.output


def test_cli_search_missing_directory_exits_nonzero():
    result = runner.invoke(
        app,
        ["search", "-d", "/tmp/grga_definitely_not_here_xyz", "foo"],
    )
    assert result.exit_code != 0


def test_cli_search_empty_query_exits_nonzero(docs_fixture: Path):
    result = runner.invoke(app, ["search", "-d", str(docs_fixture), ""])
    assert result.exit_code != 0


def test_cli_preview_prints_rga_command(docs_fixture: Path):
    result = runner.invoke(app, ["preview", "hello world"])
    assert result.exit_code == 0
    out = result.output.strip()
    assert out
    assert "hello" in out


def test_cli_search_metadata_present_by_default(docs_fixture: Path, rga_available: bool):
    if not rga_available:
        pytest.skip("rga not available")
    result = runner.invoke(app, ["search", "-d", str(docs_fixture), "--json", "macOS"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    hits = [m for m in payload["matches"] if not m.get("is_context")]
    assert hits
    for h in hits:
        assert "occurrence_index" in h
        assert "total_occurrences" in h
        assert "line_ratio" in h
        assert "nearby_hits" in h
        assert "matched_terms" in h


def test_cli_search_no_metadata_flag_keeps_legacy_shape(docs_fixture: Path, rga_available: bool):
    if not rga_available:
        pytest.skip("rga not available")
    result = runner.invoke(app, ["search", "-d", str(docs_fixture), "--json", "--no-metadata", "macOS"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    hits = [m for m in payload["matches"] if not m.get("is_context")]
    assert hits
    for h in hits:
        assert "occurrence_index" not in h
        assert "line_ratio" not in h

