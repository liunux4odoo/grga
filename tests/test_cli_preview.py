"""CLI ``related --preview`` output behaviour.

Covers P1-3 from ``MIGRATION_user_scoped_db_review.md``: ``--preview``
without ``--json`` used to be rejected with a usage error, so the caller
lost the preview signal on the human-readable path. The current behaviour:

* ``--preview --json``  -> preview embedded in JSON envelope (already
  covered by ``tests/test_cli_json.py``).
* ``--preview`` alone   -> results printed to stdout as usual, followed by
  a compact preview summary printed to stderr (would_add + scope count +
  top bridge keywords with a ``(polysemy?)`` tag when suspected).
* ``--preview --mode dir|heading``  -> still rejected (preview is a
  ``mode=graph`` observability signal only).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga.cli import app
from grga.graph.build import build_graph_index


runner = CliRunner()


def _err(result) -> str:
    """stderr text, tolerating click versions that fold it into output."""
    try:
        return result.stderr or result.output
    except ValueError:
        return result.output


@pytest.fixture
def two_scope_db(monkeypatch, tmp_path):
    """Two scopes bridged by a shared keyword so the preview has content."""
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    a = tmp_path / "scope_a"
    b = tmp_path / "scope_b"
    a.mkdir()
    b.mkdir()

    (a / "a1.md").write_text(
        "# Alpha doc\n\nalpha alpha bridgeword bridgeword talk.\n",
        encoding="utf-8",
    )
    (b / "b1.md").write_text(
        "# Beta doc\n\nbridgeword bridgeword betaword betaword talk.\n",
        encoding="utf-8",
    )

    db = tmp_path / "shared.db"
    build_graph_index(a, db_path=db, min_pair_count=1)
    build_graph_index(b, db_path=db, min_pair_count=1)
    return {"a": a, "b": b, "db": db}


def test_preview_without_json_prints_summary(two_scope_db):
    """``--preview`` without ``--json`` prints a stderr summary line."""
    result = runner.invoke(
        app,
        [
            "related", "alpha",
            "--mode", "graph",
            "--db", str(two_scope_db["db"]),
            "-d", str(two_scope_db["a"]),
            "--preview",
            # Synthetic mini-corpus: pin off the P1.2 default so the shared
            # bridgeword survives as a PPR bridge (test targets preview
            # output shape, not threshold policy).
            "--min-npmi", "0",
        ],
    )
    assert result.exit_code == 0, _err(result)
    assert "# preview:" in _err(result)
    assert "--cross-scope would add" in _err(result)
    assert "bridge keywords" in _err(result)


def test_preview_without_json_still_prints_results(two_scope_db):
    """Preview summary must not swallow the normal results output."""
    result = runner.invoke(
        app,
        [
            "related", "alpha",
            "--mode", "graph",
            "--db", str(two_scope_db["db"]),
            "-d", str(two_scope_db["a"]),
            "--preview",
            "--min-npmi", "0",
        ],
    )
    assert result.exit_code == 0
    # Isolated results (default) still surface a1.md on stdout.
    assert "a1.md" in result.output


def test_preview_no_scope_prints_note(two_scope_db):
    """No ``-d`` -> preview is None; render a short note, not a blank line."""
    result = runner.invoke(
        app,
        [
            "related", "alpha",
            "--mode", "graph",
            "--db", str(two_scope_db["db"]),
            "--preview",
            "--min-npmi", "0",
        ],
    )
    assert result.exit_code == 0
    assert "# preview:" in _err(result)
    assert "no --directory scope active" in _err(result)


def test_preview_rejected_for_non_graph_mode(two_scope_db):
    """``--preview`` remains a mode=graph observability signal."""
    result = runner.invoke(
        app,
        [
            "related", "alpha",
            "--mode", "dir",
            "--db", str(two_scope_db["db"]),
            "-d", str(two_scope_db["a"]),
            "--preview",
        ],
    )
    assert result.exit_code != 0
    assert "--preview only applies to --mode graph" in _err(result)
