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
    build_graph_index(scope, db_path=db, min_pair_count=1)
    return {"scope": scope, "db": db}


# --------------------------------------------------------------------------- #
# expansion block                                                             #
# --------------------------------------------------------------------------- #


def test_related_graph_json_has_expansion_block(graph_scope, schema):
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "graph",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    _validate(payload, schema)

    # Envelope no longer carries the trace-signals fields.
    assert "trace_signals_version" not in payload
    assert "graph_actions" not in payload

    # Query-level expansion block: seeds carry per-kw chunk_df, expanded
    # entries fold ppr_score + hops + chunk_df + edges_to_seeds into a
    # single object per keyword (contract v2.1).
    exp = payload["expansion"]
    assert [s["kw"] for s in exp["seeds"]] == ["brew"]
    seed_kws = {s["kw"] for s in exp["seeds"]}
    for seed in exp["seeds"]:
        # chunk_df is optional (rare keywords may have no keyword_chunks row);
        # when present it must be a non-negative int.
        if "chunk_df" in seed:
            assert isinstance(seed["chunk_df"], int) and seed["chunk_df"] >= 0
    assert isinstance(exp["total_chunks_in_scope"], int)
    assert exp["total_chunks_in_scope"] >= 0
    assert isinstance(exp["expanded_keywords"], list)

    if exp["expanded_keywords"]:
        scores = [kw["ppr_score"] for kw in exp["expanded_keywords"]]
        assert scores == sorted(scores, reverse=True)
        for kw in exp["expanded_keywords"]:
            assert kw["kw"] not in seed_kws, "seeds must not repeat in expanded"
            assert isinstance(kw["ppr_score"], (int, float))
            assert "edges_to_seeds" in kw, (
                "every expanded keyword must carry edges_to_seeds (may be empty)"
            )
            assert isinstance(kw["edges_to_seeds"], list)
            for edge in kw["edges_to_seeds"]:
                assert edge["seed"] in seed_kws, (
                    f"edge.seed {edge['seed']!r} not in query seeds {seed_kws}"
                )
                # npmi may be null (llm_promoted edges); cooccur_count is int
                assert isinstance(edge["cooccur_count"], int)
                assert edge["cooccur_count"] >= 0
                assert isinstance(edge["source"], str)
            if "hops_from_seed" in kw:
                assert kw["hops_from_seed"] >= 0
            if "chunk_df" in kw:
                assert kw["chunk_df"] >= 0


def test_related_graph_isolated_seed_emits_empty_expansion(graph_scope, schema):
    """Isolated seed → PPR yields nothing → expansion.expanded_keywords is []."""
    result = runner.invoke(
        app,
        ["related", "no_such_keyword_xyz", "--mode", "graph",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    _validate(payload, schema)
    assert payload["results"] == []
    exp = payload["expansion"]
    assert [s["kw"] for s in exp["seeds"]] == ["no_such_keyword_xyz"]
    assert exp["expanded_keywords"] == []
    # total_chunks_in_scope reflects the scope's chunk count regardless of
    # whether the seed itself is indexed.
    assert isinstance(exp["total_chunks_in_scope"], int)
    assert exp["total_chunks_in_scope"] >= 0


# --------------------------------------------------------------------------- #
# keyword_contributions on each result                                        #
# --------------------------------------------------------------------------- #


def test_related_graph_results_carry_keyword_contributions(graph_scope, schema):
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "graph",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    _validate(payload, schema)
    assert payload["results"], "expected at least one result on this corpus"

    for r in payload["results"]:
        contribs = r["keyword_contributions"]
        assert isinstance(contribs, list) and contribs, (
            f"graph-mode result must carry non-empty keyword_contributions: {r}"
        )
        # Sorted by (-occurrences, kw): occurrences monotone non-increasing.
        occ_seq = [c["occurrences"] for c in contribs]
        assert occ_seq == sorted(occ_seq, reverse=True)
        for c in contribs:
            assert isinstance(c["kw"], str) and c["kw"], "kw must be a non-empty string"
            assert isinstance(c["occurrences"], int) and c["occurrences"] > 0

        # keywords field mirrors the contributions' kw set (sorted).
        assert set(r["keywords"]) == {c["kw"] for c in contribs}


# --------------------------------------------------------------------------- #
# dir / heading modes: no graph-only signals                                  #
# --------------------------------------------------------------------------- #


def test_related_dir_has_no_expansion_or_contributions(graph_scope):
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "dir",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "expansion" not in payload
    assert "graph_actions" not in payload
    assert "trace_signals_version" not in payload
    for r in payload["results"]:
        assert "keyword_contributions" not in r
        assert "graph_signals" not in r


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
