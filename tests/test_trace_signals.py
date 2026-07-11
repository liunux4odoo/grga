"""trace-signals v1 contract tests.

Enforces the boundary table (`docs/design/trace-signals.md` §6),
verifies which verbs emit ``trace_signals_version`` + ``graph_actions``
(and which MUST NOT), and validates every wire payload against
``schemas/trace-signals-v1.json``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga.cli import app
from grga.graph.build import build_graph_index
from grga.trace import (
    FUTURE_OPS,
    NODE_PREFIXES,
    OPS,
    TRACE_SIGNALS_VERSION,
    encode_file,
    encode_heading,
    encode_keyword,
    envelope,
    llm_propose,
    node_prefix,
    score_node,
    traverse_edge,
)

runner = CliRunner()

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "trace-signals-v1.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _validate(payload: dict, schema: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(payload, schema)


# --------------------------------------------------------------------------- #
# Unit tests: node id encoding & action factories                             #
# --------------------------------------------------------------------------- #


def test_node_prefix_frozen():
    # v1 vocabulary MUST contain exactly kw / file / head.
    assert set(NODE_PREFIXES) == {"kw:", "file:", "head:"}
    assert set(OPS) == {"traverse_edge", "score_node", "llm_propose"}
    # Future words must not be accidentally in the live vocabulary.
    assert not set(FUTURE_OPS).intersection(OPS)


def test_encode_keyword_and_file_and_heading():
    assert encode_keyword("chunking") == "kw:chunking"
    assert encode_keyword("multi word").startswith("kw:")
    # POSIX normalisation for file ids.
    assert encode_file("docs\\rag\\embed.md") == "file:docs/rag/embed.md"
    assert encode_heading("docs/rag/embed.md", "macOS") == "head:docs/rag/embed.md#macOS"
    # Whitespace is URL-encoded so ids never contain raw spaces.
    assert " " not in encode_keyword("a b")


def test_node_prefix_helper():
    assert node_prefix("kw:foo") == "kw:"
    assert node_prefix("file:x") == "file:"
    assert node_prefix("head:x#y") == "head:"
    assert node_prefix("scope:unknown") == ""  # future-vocab, never emitted in v1


def test_action_factories_shape():
    e = traverse_edge("kw:a", "kw:b", edge_kind="co_occurrence", weight=0.5, hop=1)
    assert e.op == "traverse_edge"
    assert e.nodes == ["kw:a", "kw:b"]
    assert e.meta == {"edge_kind": "co_occurrence", "weight": 0.5, "hop": 1}

    s = score_node("kw:a", algo="ppr", score=0.3, hops_from_seed=1)
    assert s.op == "score_node"
    assert s.nodes == ["kw:a"]
    assert s.meta["algo"] == "ppr"
    assert s.meta["hops_from_seed"] == 1

    p = llm_propose(["kw:foo"], origin_query="q", rationale="")
    assert p.op == "llm_propose"
    assert p.meta == {"origin_query": "q", "rationale": ""}


def test_envelope_stamps_version_and_actions():
    out = envelope({"payload": 1}, [traverse_edge("kw:a", "kw:b", edge_kind="co_occurrence", weight=0.5, hop=1)])
    assert out["trace_signals_version"] == TRACE_SIGNALS_VERSION
    assert out["graph_actions"][0]["op"] == "traverse_edge"


# --------------------------------------------------------------------------- #
# Suggest boundary rules (§6)                                                 #
# --------------------------------------------------------------------------- #


def _stub_suggest_module(monkeypatch, **overrides):
    """Install a fake ``grga.suggest.suggest`` that returns a shaped result.

    The CLI imports ``suggest as run_suggest`` from ``grga.suggest``, so we
    monkeypatch the attribute the CLI actually resolves through.
    """
    from grga.suggest import Suggestion

    def fake(*args, **kwargs):
        s = Suggestion(
            intent=kwargs.get("intent", "expand"),
            keywords=overrides.get("keywords", []),
            queries=overrides.get("queries", []),
            keyword_pairs=[],
            reason=overrides.get("reason", "stub"),
            tokens_used=0,
            model="fake-llm",
            votes={"pairs_seen": 0, "pairs_recorded": 0, "votes_added": 0.0},
        )
        # Emit graph_actions exactly like the real code path does (§5.2:
        # ≥1 llm_propose when keywords proposed; empty on the empty branch).
        from grga.trace import encode_keyword, llm_propose

        if s.keywords:
            s.graph_actions = [
                llm_propose([encode_keyword(kw)], origin_query=" ".join(args[0:1]) or "q", rationale=s.reason)
                for kw in s.keywords
            ]
        return s

    monkeypatch.setattr("grga.suggest.suggest", fake)


def test_suggest_emits_llm_propose(monkeypatch, tmp_path, schema):
    _stub_suggest_module(monkeypatch, keywords=["chunking", "rerank"], reason="expand seeds")
    result = runner.invoke(
        app,
        ["suggest", "chunking", "--db", str(tmp_path / "v.db"), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["trace_signals_version"] == "1.0"
    assert payload["graph_actions"], "must emit at least one llm_propose when keywords proposed"
    for act in payload["graph_actions"]:
        assert act["op"] == "llm_propose"
        assert all(n.startswith("kw:") for n in act["nodes"])
        assert "origin_query" in act["meta"]
        assert "rationale" in act["meta"]  # allowed empty, but the key must exist
    _validate(payload, schema)


def test_suggest_empty_boundary(monkeypatch, tmp_path, schema):
    """§6: empty LLM suggestion yields ok:true + graph_actions:[]."""
    _stub_suggest_module(monkeypatch, keywords=[], reason="")
    result = runner.invoke(
        app,
        ["suggest", "chunking", "--db", str(tmp_path / "v.db"), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["trace_signals_version"] == "1.0"
    assert payload["graph_actions"] == []
    _validate(payload, schema)


# --------------------------------------------------------------------------- #
# Related boundary + shape tests                                              #
# --------------------------------------------------------------------------- #


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


def test_related_graph_emits_expected_ops(graph_scope, schema):
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "graph",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    _validate(payload, schema)
    ops = {a["op"] for a in payload["graph_actions"]}
    assert "score_node" in ops
    # ``brew`` co-occurs with other keywords → PPR walks at least one edge.
    assert "traverse_edge" in ops
    # Every node id uses a v1 prefix (no leakage from the future vocabulary).
    for act in payload["graph_actions"]:
        for node in act["nodes"]:
            assert node_prefix(node) in NODE_PREFIXES, f"unknown prefix in {node!r}"
    # llm_propose is forbidden outside suggest.
    assert "llm_propose" not in ops


def test_related_dir_has_empty_graph_actions(graph_scope, schema):
    """§4: only related graph populates graph_actions; dir/heading stay empty."""
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "dir",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["trace_signals_version"] == "1.0"
    assert payload["graph_actions"] == []
    _validate(payload, schema)


def test_related_heading_has_empty_graph_actions(graph_scope, schema):
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "heading",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["graph_actions"] == []


def test_related_graph_isolated_seed_is_empty(graph_scope, schema):
    """§6 boundary: isolated seed → graph_actions = [] (empty, not missing)."""
    result = runner.invoke(
        app,
        ["related", "no_such_keyword_xyz", "--mode", "graph",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["graph_actions"] == []
    assert payload["results"] == []
    _validate(payload, schema)


def test_related_graph_idempotent(graph_scope):
    """§6: same params twice → graph_actions serialise byte-identically."""
    args = [
        "related", "brew", "--mode", "graph",
        "--db", str(graph_scope["db"]),
        "-d", str(graph_scope["scope"]), "--json",
    ]
    a = runner.invoke(app, args).output
    b = runner.invoke(app, args).output
    payload_a = json.loads(a)
    payload_b = json.loads(b)
    assert payload_a["graph_actions"] == payload_b["graph_actions"]


def test_related_graph_no_negative_hops_and_edges_reach_scored(graph_scope):
    """Regression: every scored keyword must be reachable via traverse_edges.

    Prior implementation restricted BFS to keywords in ``kw_score``, which
    silently orphaned any keyword PPR reached through an unranked
    intermediate (missing incoming ``traverse_edge`` + ``hops_from_seed=-1``).
    The fix BFS-es the full graph and only gates emission.
    """
    result = runner.invoke(
        app,
        ["related", "brew", "--mode", "graph",
         "--db", str(graph_scope["db"]),
         "-d", str(graph_scope["scope"]), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    actions = payload["graph_actions"]

    for a in actions:
        if a["op"] == "score_node":
            assert a["meta"]["hops_from_seed"] >= 0, (
                f"negative hops_from_seed on {a['nodes']!r}: {a}"
            )

    # Every scored keyword (kw:*) must have an incoming traverse_edge (either
    # from a seed at hop 1 or from a predecessor on the shortest path).
    scored_kws = {
        a["nodes"][0] for a in actions
        if a["op"] == "score_node" and a["nodes"][0].startswith("kw:")
    }
    incoming_targets = {
        a["nodes"][1] for a in actions
        if a["op"] == "traverse_edge" and a["meta"]["edge_kind"] == "co_occurrence"
    }
    orphans = scored_kws - incoming_targets
    assert not orphans, f"scored keywords without incoming edge: {orphans}"


# --------------------------------------------------------------------------- #
# "Do NOT emit" invariants (§5.3)                                             #
# --------------------------------------------------------------------------- #


def test_search_json_has_no_trace_signals(docs_fixture, tmp_path, monkeypatch):
    """Hot-path verbs MUST NOT carry trace_signals_version / graph_actions."""
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    result = runner.invoke(
        app, ["search", "安装", "-d", str(docs_fixture), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "trace_signals_version" not in payload
    assert "graph_actions" not in payload


def test_expand_json_has_no_trace_signals(docs_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    result = runner.invoke(
        app,
        ["expand", "install.md:5", "--level", "chapter",
         "-d", str(docs_fixture), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "trace_signals_version" not in payload
    assert "graph_actions" not in payload


def test_probe_json_has_no_trace_signals(docs_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    result = runner.invoke(
        app, ["probe", "安装", "-d", str(docs_fixture), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "trace_signals_version" not in payload
    assert "graph_actions" not in payload
