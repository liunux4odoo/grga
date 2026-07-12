"""related-signals v3 contract tests.

v3 (2026-07-13, v9 keyword-primary) replaces the v2 contract:

* ``related --json --mode graph`` returns ``{"results": [RelatedKeyword]}``
  — candidate keywords + association evidence (kw / npmi / cooccur_count /
  via_seed / chunk_df / source). The pre-v3 ``expansion`` block,
  ``crossed_scopes_preview``, ``keyword_contributions``, and ``graph_signals``
  are all gone (PPR + doc aggregation removed in Phase 3b-2).
* ``related --json --mode heading`` returns ``{"results": [Related]}`` with
  no ``keyword_contributions`` (that field was deleted from the dataclass).
* ``--mode dir`` is removed (CLI rejects it).
* Hot-path verbs (``search`` / ``probe`` / ``expand``) and ``suggest`` MUST
  NOT carry any ``expansion`` / ``keyword_contributions`` / ``graph_signals``
  / ``trace_signals_version`` / ``graph_actions`` field.

Contract: ``schemas/related-signals-v3.json``; narrative:
``docs/design/related-signals-v3.md``.
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
    Path(__file__).resolve().parent.parent / "schemas" / "related-signals-v3.json"
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
# graph mode: candidate keywords + evidence (v3 contract)                     #
# --------------------------------------------------------------------------- #


def test_related_graph_v3_envelope_shape(graph_scope, schema):
    """graph mode returns ``{results: [RelatedKeyword]}`` — no envelope
    metadata, no expansion, no crossed_scopes_preview."""
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "graph",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    _validate(payload, schema)

    # v3 envelope MUST be exactly {results}. Everything from v2 is gone.
    assert set(payload.keys()) == {"results"}
    for banned in (
        "expansion", "crossed_scopes_preview", "trace_signals_version",
        "graph_actions",
    ):
        assert banned not in payload, f"v2 field {banned!r} leaked into v3"


def test_related_graph_v3_related_keyword_fields(graph_scope, schema):
    """Each row is a RelatedKeyword with all required evidence fields."""
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "graph",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json",
         # min_npmi=0 so tiny corpus produces at least one candidate
         "--min-npmi", "0.0"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    _validate(payload, schema)
    if not payload["results"]:
        pytest.skip("fixture corpus produced no candidates; shape check only")

    for r in payload["results"]:
        assert set(r.keys()) == {
            "kw", "npmi", "cooccur_count", "via_seed", "chunk_df", "source",
        }, f"unexpected RelatedKeyword shape: {sorted(r.keys())}"
        assert isinstance(r["kw"], str) and r["kw"]
        assert isinstance(r["npmi"], (int, float))
        assert r["cooccur_count"] >= 0
        assert r["via_seed"] == "brew"
        assert r["chunk_df"] >= 0
        assert r["source"] in ("chunk_cooccur", "llm_promoted")
        # v3: no doc-oriented fields on RelatedKeyword
        for banned in ("doc_path", "score", "mode", "reason", "keywords",
                       "directory", "qualified_path", "keyword_contributions",
                       "ppr_score", "hops_from_seed"):
            assert banned not in r, f"v2 doc field {banned!r} on v3 RelatedKeyword"


def test_related_graph_v3_multi_seed_no_fusion(graph_scope, schema):
    """Multi-seed queries return per-seed rows; a candidate reachable from
    both seeds appears twice with distinct ``via_seed`` values (design D4)."""
    result = runner.invoke(
        app,
        ["related", "brew", "ripgrep", "--mode", "graph",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json",
         "--min-npmi", "0.0"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    _validate(payload, schema)
    seeds_seen = {r["via_seed"] for r in payload["results"]}
    # At least one seed should have produced rows on this fixture; the
    # invariant we care about is that every via_seed is one of the input
    # seeds, no synthetic "fused" value ever appears.
    assert seeds_seen <= {"brew", "ripgrep"}, (
        f"unexpected via_seed values in multi-seed query: {seeds_seen}"
    )


def test_related_graph_v3_unknown_seed_empty(graph_scope, schema):
    """Seed absent from the vocab (hub or misspelled) returns empty results,
    still under the v3 envelope shape."""
    result = runner.invoke(
        app,
        ["related", "totally_absent_xyz", "--mode", "graph",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    _validate(payload, schema)
    assert payload["results"] == []


def test_related_dir_mode_removed(graph_scope):
    """``--mode dir`` was removed in Phase 3b-2. CLI must reject it."""
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "dir",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code != 0
    # Typer's usage error path lists the valid choices in stderr.
    combined = result.output + (result.stderr or "")
    assert "graph" in combined and "heading" in combined


# --------------------------------------------------------------------------- #
# heading mode: no graph-only signals                                         #
# (mode=dir removed in v8 Phase 3b-2; heading covers the "plain results,     #
#  no expansion/contributions" invariant.)                                    #
# --------------------------------------------------------------------------- #


def test_related_heading_has_no_expansion_or_contributions(graph_scope, schema):
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "heading",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    _validate(payload, schema)
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
