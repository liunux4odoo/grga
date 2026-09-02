"""CLI tests for ``grga mcp start`` argument handling.

``run_server`` is patched out so the tests never block on a real stdio
server; they only assert argument → env-var plumbing.
"""
from __future__ import annotations

import os
from unittest import mock

import pytest
from typer.testing import CliRunner

from grga.cli import app

runner = CliRunner()

_DIR_ENV = "GRGA_MCP_DEFAULT_DIRECTORY"
_DB_ENV = "GRGA_DB_PATH"


@pytest.fixture
def clear_mcp_env(monkeypatch):
    monkeypatch.delenv(_DIR_ENV, raising=False)
    monkeypatch.delenv(_DB_ENV, raising=False)


def test_mcp_start_defaults_to_no_directory(clear_mcp_env):
    with mock.patch("grga.mcp.server.run_server") as run_server:
        result = runner.invoke(app, ["mcp", "start"])
    assert result.exit_code == 0, result.output
    run_server.assert_called_once_with()
    assert os.environ.get(_DIR_ENV) is None


def test_mcp_start_directory_sets_default_env(clear_mcp_env, tmp_path):
    target = tmp_path / "corpus"
    target.mkdir()
    with mock.patch("grga.mcp.server.run_server") as run_server:
        result = runner.invoke(app, ["mcp", "start", "--directory", str(target)])
    assert result.exit_code == 0, result.output
    run_server.assert_called_once_with()
    assert os.environ.get(_DIR_ENV) == str(target)


def test_mcp_start_directory_requires_existing_dir(tmp_path):
    missing = tmp_path / "does-not-exist"
    with mock.patch("grga.mcp.server.run_server") as run_server:
        result = runner.invoke(app, ["mcp", "start", "--directory", str(missing)])
    assert result.exit_code != 0
    run_server.assert_not_called()


def test_mcp_start_db_path_sets_db_env(clear_mcp_env, tmp_path):
    with mock.patch("grga.mcp.server.run_server") as run_server:
        result = runner.invoke(app, ["mcp", "start", "--db-path", str(tmp_path / "graph.db")])
    assert result.exit_code == 0, result.output
    run_server.assert_called_once_with()
    assert os.environ.get(_DB_ENV) == str((tmp_path / "graph.db").expanduser())
