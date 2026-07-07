"""CLI ``--json`` contract tests (Phase 6.2).

Every grga v2 subcommand must emit a single parseable JSON document under
``--json`` with the fields declared in ``docs/api.md``. These tests use the
Typer ``CliRunner`` so no real subprocess or network is involved; the LLM
call in ``suggest`` is stubbed via ``grga.suggest.suggest``'s ``llm_callable``
hook exercised through a monkeypatched module function.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga.cli import app
from grga.graph.build import build_graph_index

runner = CliRunner()


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))


@pytest.fixture
def graph_db(cross_heading_fixture: Path, tmp_path, sidecar_env) -> Path:
    db = tmp_path / "graph.db"
    build_graph_index(cross_heading_fixture, db_path=db, min_term_count=1)
    return db


def _parse(output: str):
    return json.loads(output)


def test_search_json_contract(docs_fixture: Path, sidecar_env):
    result = runner.invoke(
        app, ["search", "安装", "-d", str(docs_fixture), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = _parse(result.output)
    assert payload["ok"] is True
    assert isinstance(payload["patterns"], list)
    assert isinstance(payload["matches"], list)
    real = [m for m in payload["matches"] if not m.get("is_context")]
    assert real, "expected at least one real match"
    first = real[0]
    for key in ("path", "line_number", "text", "occurrence_index",
                "total_occurrences", "line_ratio", "nearby_hits",
                "matched_terms"):
        assert key in first, f"missing {key}"


def test_expand_json_contract(docs_fixture: Path, sidecar_env):
    result = runner.invoke(
        app,
        ["expand", "install.md:5", "--level", "chapter",
         "-d", str(docs_fixture), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse(result.output)
    assert set(("hit", "text", "level", "range", "truncated")) <= payload.keys()
    assert payload["level"] in ("lines", "paragraph", "chapter", "document")
    assert isinstance(payload["range"], list) and len(payload["range"]) == 2


def test_related_json_contract(graph_db: Path, cross_heading_fixture: Path):
    result = runner.invoke(
        app,
        ["related", "macOS", "--mode", "heading",
         "--db", str(graph_db), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse(result.output)
    assert isinstance(payload, list)
    if payload:
        row = payload[0]
        for key in ("doc_path", "score", "mode", "reason", "keywords"):
            assert key in row, f"missing {key}"
        assert row["mode"] == "heading"


def test_related_graph_json_contract(graph_db: Path):
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "graph",
         "--db", str(graph_db), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse(result.output)
    assert isinstance(payload, list)


def test_index_build_json_contract(cross_heading_fixture: Path, tmp_path, sidecar_env):
    db = tmp_path / "build.db"
    result = runner.invoke(
        app,
        ["index", "build", "-s", str(cross_heading_fixture),
         "--db", str(db), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse(result.output)
    for key in ("scope", "files_scanned", "files_indexed",
                "keyword_docs_written", "heading_docs_written",
                "md_files_indexed", "elapsed_seconds", "reasons"):
        assert key in payload, f"missing {key}"


def test_index_promote_json_contract(graph_db: Path):
    result = runner.invoke(
        app,
        ["index", "promote", "--db", str(graph_db), "--dry-run", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse(result.output)
    for key in ("threshold", "dry_run", "candidates", "promoted",
                "skipped_already_promoted", "pairs"):
        assert key in payload, f"missing {key}"
    assert payload["dry_run"] is True


def test_suggest_json_contract(monkeypatch, tmp_path, docs_fixture: Path):
    db = tmp_path / "votes.db"

    def fake_suggest(*args, **kwargs):
        from grga.suggest import Suggestion

        return Suggestion(
            intent=kwargs.get("intent", "expand"),
            keywords=["brew", "apt"],
            queries=["brew || apt"],
            keyword_pairs=[("brew", "apt")],
            reason="stub",
            tokens_used=0,
            model="fake-llm",
            votes={"pairs_seen": 1, "pairs_recorded": 1, "votes_added": 1.0},
        )

    monkeypatch.setattr("grga.suggest.suggest", fake_suggest)

    result = runner.invoke(
        app,
        ["suggest", "how to install", "--intent", "expand",
         "--db", str(db), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse(result.output)
    for key in ("intent", "keywords", "queries", "keyword_pairs",
                "ranking", "scores", "reason", "llm_generated",
                "tokens_used", "model"):
        assert key in payload, f"missing {key}"
    assert payload["llm_generated"] is True
