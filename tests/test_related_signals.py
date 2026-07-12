"""related-signals v2 contract tests.

Replaces the pre-2026-07-12 ``tests/test_trace_signals.py`` which enforced
the (now-removed) trace-signals v1 ``graph_actions`` action-log contract.
The new contract focuses on **caller relevance judgement**, not walk
visualisation, and is documented in ``docs/design/related-signals-v2.md``:

* ``related --json --mode graph`` returns ``results[]`` +
  ``crossed_scopes_preview`` + ``expansion`` (query-level seeds + PPR
  neighbours with ``ppr_score`` / ``hops_from_seed``).
* Each result in graph mode carries ``keyword_contributions[]`` — per-doc
  per-keyword occurrence evidence, sorted by ``(-occurrences, kw)``.
* Hot-path verbs (``search`` / ``probe`` / ``expand``) MUST NOT carry any
  ``expansion`` / ``keyword_contributions`` / ``graph_signals`` /
  ``trace_signals_version`` / ``graph_actions`` field.
* ``dir`` / ``heading`` modes and ``suggest`` never emit ``expansion`` or
  ``keyword_contributions`` (graph-only signals).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga.cli import app
from grga.graph.build import build_graph_index

runner = CliRunner()

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "related-signals-v2.json"
)


@pytest.fixture(scope="module")
def schema() -> dict:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _validate(payload: dict, schema: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(payload, schema)


@pytest.fixture
def graph_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    scope = tmp_path / "corpus"
    (scope / "install").mkdir(parents=True)
    (scope / "usage").mkdir(parents=True)
    (scope / "install" / "macos.md").write_text(
        "# Install\n\n## macOS\n\ngrga install with brew.\n"
        "brew install ripgrep pandoc.\n",
        encoding="utf-8",
    )
    (scope / "install" / "linux.md").write_text(
        "# Install\n\n## Linux\n\napt install grga.\n"
        "apt install ripgrep pandoc.\n",
        encoding="utf-8",
    )
    (scope / "usage" / "cli.md").write_text(
        "# Usage\n\n## macOS\n\nRun grga on macOS.\n"
        "grga ripgrep command.\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    build_graph_index(scope, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"scope": scope, "db": db}


# --------------------------------------------------------------------------- #
# heading mode: no graph-only signals                                         #
# (mode=dir removed in v8 Phase 3b-2; heading covers the "plain results,     #
#  no expansion/contributions" invariant.)                                    #
# --------------------------------------------------------------------------- #


def test_related_heading_has_no_expansion_or_contributions(graph_scope):
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "heading",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "expansion" not in payload
    assert "graph_actions" not in payload
    for r in payload["results"]:
        assert "keyword_contributions" not in r


# --------------------------------------------------------------------------- #
# suggest: no trace fields                                                    #
# --------------------------------------------------------------------------- #


def _stub_suggest_module(monkeypatch, **overrides):
    """Install a fake ``grga.suggest.suggest`` returning a shaped Suggestion."""
    from grga.suggest import Suggestion

    def fake(*args, **kwargs):
        return Suggestion(
            intent=kwargs.get("intent", "expand"),
            keywords=overrides.get("keywords", []),
            queries=overrides.get("queries", []),
            keyword_pairs=[],
            reason=overrides.get("reason", "stub"),
            tokens_used=0,
            model="fake-llm",
            votes={"pairs_seen": 0, "pairs_recorded": 0, "votes_added": 0.0},
        )

    monkeypatch.setattr("grga.suggest.suggest", fake)


def test_suggest_json_has_no_trace_fields(monkeypatch, tmp_path):
    _stub_suggest_module(monkeypatch, keywords=["chunking", "rerank"])
    result = runner.invoke(
        app,
        ["suggest", "chunking", "--db", str(tmp_path / "v.db"), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # v2 contract: no envelope, no graph_actions, no trace_signals_version.
    assert "trace_signals_version" not in payload
    assert "graph_actions" not in payload
    # Core Suggestion fields still there.
    for key in ("intent", "keywords", "queries", "keyword_pairs",
                "ranking", "scores", "reason", "llm_generated",
                "tokens_used", "model"):
        assert key in payload, f"missing {key}"


def test_suggest_empty_keywords_still_no_trace(monkeypatch, tmp_path):
    _stub_suggest_module(monkeypatch, keywords=[])
    result = runner.invoke(
        app,
        ["suggest", "chunking", "--db", str(tmp_path / "v.db"), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "graph_actions" not in payload
    assert payload["keywords"] == []


# --------------------------------------------------------------------------- #
# Hot-path verbs unchanged                                                    #
# --------------------------------------------------------------------------- #


def test_search_json_has_no_graph_signals(docs_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    result = runner.invoke(
        app, ["search", "安装", "-d", str(docs_fixture), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for key in ("trace_signals_version", "graph_actions",
                "expansion", "keyword_contributions"):
        assert key not in payload, f"hot-path verb must not carry {key}"


def test_expand_json_has_no_graph_signals(docs_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    result = runner.invoke(
        app,
        ["expand", "install.md:5", "--level", "chapter",
         "-d", str(docs_fixture), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for key in ("trace_signals_version", "graph_actions",
                "expansion", "keyword_contributions"):
        assert key not in payload, f"hot-path verb must not carry {key}"


def test_probe_json_has_no_graph_signals(docs_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    result = runner.invoke(
        app, ["probe", "安装", "-d", str(docs_fixture), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for key in ("trace_signals_version", "graph_actions",
                "expansion", "keyword_contributions"):
        assert key not in payload, f"hot-path verb must not carry {key}"
