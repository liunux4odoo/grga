"""CLI-layer tests for ``grga skill install`` exit-code contract (Task 1).

Focus: the self-reflexive path (installing twice) must return exit code 3
when a target already exists without ``--force`` — this is the bug the
followup plan flagged. The unit-level behaviour of ``skill_install`` is
covered separately.
"""

from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from grga.cli import app

runner = CliRunner()


def test_first_install_exit_0(tmp_path):
    result = runner.invoke(app, ["skill", "install", "--to", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    reports = json.loads(result.output)
    assert reports and all(r["status"] == "installed" for r in reports)


def test_reinstall_without_force_exit_3(tmp_path):
    runner.invoke(app, ["skill", "install", "--to", str(tmp_path)])
    result = runner.invoke(app, ["skill", "install", "--to", str(tmp_path), "--json"])
    assert result.exit_code == 3, result.output
    reports = json.loads(result.output)
    assert any(r["status"] == "exists" for r in reports)


def test_reinstall_with_force_exit_0(tmp_path):
    runner.invoke(app, ["skill", "install", "--to", str(tmp_path)])
    result = runner.invoke(app, ["skill", "install", "--to", str(tmp_path), "--force", "--json"])
    assert result.exit_code == 0, result.output
    reports = json.loads(result.output)
    assert all(r["status"] == "installed" for r in reports)


def test_named_install_exit_0(tmp_path):
    result = runner.invoke(app, ["skill", "install", "--to", str(tmp_path), "--name", "grga", "--json"])
    assert result.exit_code == 0, result.output
    reports = json.loads(result.output)
    assert len(reports) == 1 and reports[0]["status"] == "installed"


def test_named_reinstall_without_force_exit_3(tmp_path):
    runner.invoke(app, ["skill", "install", "--to", str(tmp_path), "--name", "grga"])
    result = runner.invoke(app, ["skill", "install", "--to", str(tmp_path), "--name", "grga"])
    assert result.exit_code == 3, result.output


def test_unknown_skill_exit_9(tmp_path):
    result = runner.invoke(app, ["skill", "install", "--to", str(tmp_path), "--name", "nonexistent", "--json"])
    assert result.exit_code == 9, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "unknown_skill"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses filesystem permissions")
def test_readonly_target_exit_9(tmp_path):
    readonly = tmp_path / "ro"
    readonly.mkdir()
    os.chmod(readonly, 0o500)
    try:
        result = runner.invoke(app, ["skill", "install", "--to", str(readonly / "sub"), "--json"])
        assert result.exit_code == 9, result.output
        payload = json.loads(result.output)
        assert payload["error"] in ("io", "unknown_skill")
    finally:
        os.chmod(readonly, 0o700)
