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
    build_graph_index(a, db_path=db, min_pair_count=1, min_npmi=0.0)
    build_graph_index(b, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"a": a, "b": b, "db": db}


def test_preview_without_json_prints_summary(two_scope_db):
    """``--preview`` used to print a stderr summary line (pre-v8 PPR mode).

    v8 keyword-primary (Phase 3b-1): ``--preview`` no longer applies to
    ``--mode graph`` — graph now returns candidate keywords, not documents,
    so ``crossed_scopes_preview`` has no meaning. The flag is accepted for
    CLI backward-compat and emits a deprecation note; the summary is gone.
    """
    result = runner.invoke(
        app,
        [
            "related", "alpha",
            "--mode", "graph",
            "--db", str(two_scope_db["db"]),
            "-d", str(two_scope_db["a"]),
            "--preview",
            # Synthetic mini-corpus: pin off the P1.2 default so the shared
            # bridgeword survives (test targets flag semantics, not policy).
            "--min-npmi", "0",
        ],
    )
    assert result.exit_code == 0, _err(result)
    # Deprecation note surfaces the flag becoming a no-op.
    assert "--preview" in _err(result)
    assert "ignored" in _err(result)
    # Legacy summary is gone.
    assert "# preview:" not in _err(result)


def test_preview_without_json_still_prints_results(two_scope_db):
    """``--preview`` no-op still lets the normal results flow through.

    Note: v8 graph results are keyword candidates (RelatedKeyword), not
    document rows — assertions on ``a1.md`` would target the pre-v8
    contract.
    """
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
    # bridgeword is alpha's strongest partner in this fixture — it should
    # show up as a candidate keyword in stdout.
    assert "bridgeword" in result.output


def test_preview_no_scope_prints_note(two_scope_db):
    """``--preview`` without ``-d`` used to print a summary note (pre-v8).

    v8: same as above — flag is a no-op, only the deprecation note fires.
    """
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
    assert "--preview" in _err(result)
    assert "# preview:" not in _err(result)


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
