"""Tests for the trace-viewer renderer.

Every row of IMPLEMENTATION.md's boundary table has at least one test.
The tests import `render` as a module and, where needed, drive its
`_run` entry point end-to-end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import render  # noqa: E402  (path-hack above is deliberate)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            if isinstance(r, str):
                fh.write(r + "\n")
            else:
                fh.write(json.dumps(r) + "\n")


def _rec(step, verb="related", actions=None, ts="2026-07-09T00:00:00Z",
         mode="graph", intent=None, seeds=None, directory="."):
    return {
        "ts": ts,
        "step": step,
        "verb": verb,
        "args": {
            "seeds": seeds if seeds is not None else ["chunking"],
            "mode": mode,
            "intent": intent,
            "directory": directory,
            "extra": {},
        },
        "trace_signals_version": "1.0",
        "graph_actions": actions or [],
    }


def _traverse(a, b, kind="co_occurrence", weight=0.5):
    return {"op": "traverse_edge", "nodes": [a, b],
            "meta": {"edge_kind": kind, "weight": weight, "hop": 1}}


def _score(node, score=0.3, algo="ppr", hops=1):
    return {"op": "score_node", "nodes": [node],
            "meta": {"algo": algo, "score": score, "hops_from_seed": hops}}


def _propose(target, origin="chunking"):
    return {"op": "llm_propose", "nodes": [target],
            "meta": {"origin_query": origin, "rationale": "expand"}}


def _run_cli(tmp_path, in_records, extra_args=None) -> tuple[int, Path]:
    """Write records, run CLI, return (rc, output_path)."""
    ipath = tmp_path / "trace.jsonl"
    opath = tmp_path / "out.html"
    _write_jsonl(ipath, in_records)
    args = [str(ipath), "-o", str(opath), "--quiet"]
    if extra_args:
        args.extend(extra_args)
    rc = render._run(args)
    return rc, opath


# ---------------------------------------------------------------------------
# Boundary table: input-level cases
# ---------------------------------------------------------------------------

def test_input_missing_file_exits_1(tmp_path):
    opath = tmp_path / "out.html"
    rc = render._run([str(tmp_path / "no-such.jsonl"), "-o", str(opath), "--quiet"])
    assert rc == render.EXIT_IO
    assert not opath.exists()


def test_empty_file_exits_2(tmp_path):
    ipath = tmp_path / "empty.jsonl"
    ipath.write_text("", encoding="utf-8")
    opath = tmp_path / "out.html"
    rc = render._run([str(ipath), "-o", str(opath), "--quiet"])
    assert rc == render.EXIT_EMPTY


def test_all_blank_lines_exits_2(tmp_path):
    rc, opath = _run_cli(tmp_path, ["", "   ", "\t"])
    assert rc == render.EXIT_EMPTY


def test_all_bad_lines_exits_2(tmp_path):
    bad = [
        "not json",
        "{oops",
        '{"missing":"fields"}',
        '{"step":1,"verb":"unknown","graph_actions":[]}',
    ]
    rc, _ = _run_cli(tmp_path, bad)
    assert rc == render.EXIT_EMPTY


def test_mixed_valid_and_invalid_lines_exits_0(tmp_path):
    lines = [
        "",  # blank
        "not json",  # invalid
        '{"step":1,"verb":"related","graph_actions":[]}',  # ok (minimal)
        json.dumps(_rec(2, actions=[_traverse("kw:a", "kw:b")])),  # ok
        '{"step":3,"verb":"probe","graph_actions":[]}',  # bad verb
    ]
    rc, opath = _run_cli(tmp_path, lines)
    assert rc == render.EXIT_OK
    assert opath.exists()
    # verify only valid records made it through
    html = opath.read_text(encoding="utf-8")
    assert "kw:a" in html
    assert "kw:b" in html


def test_missing_required_field_is_skipped(tmp_path):
    lines = [
        '{"verb":"related","graph_actions":[]}',  # no step
        '{"step":1,"graph_actions":[]}',           # no verb
        '{"step":2,"verb":"related"}',              # no graph_actions
        json.dumps(_rec(3, actions=[_traverse("kw:a", "kw:b")])),
    ]
    rc, opath = _run_cli(tmp_path, lines)
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    assert [s["step"] for s in payload["steps"]] == [3]


# ---------------------------------------------------------------------------
# Boundary table: per-record semantics
# ---------------------------------------------------------------------------

def test_empty_graph_actions_step_kept_but_no_new_elements(tmp_path):
    """Single record with graph_actions=[] must appear in `steps` but not add nodes/edges."""
    rc, opath = _run_cli(tmp_path, [_rec(1, actions=[])])
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    assert len(payload["steps"]) == 1
    assert payload["steps"][0]["actions"] == []
    assert payload["nodes"] == []
    assert payload["edges"] == []


def test_all_records_empty_actions_still_renders(tmp_path):
    rc, opath = _run_cli(
        tmp_path,
        [_rec(1, actions=[]), _rec(2, actions=[]), _rec(3, actions=[])],
    )
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    assert len(payload["steps"]) == 3
    assert payload["nodes"] == []
    assert payload["edges"] == []


def test_future_op_is_skipped_other_actions_kept(tmp_path):
    """v1 must ignore unknown ops but keep sibling actions in the same record."""
    rec = _rec(
        1,
        actions=[
            {"op": "hit_file", "nodes": ["file:foo.md"], "meta": {}},  # future
            _traverse("kw:a", "kw:b"),
        ],
    )
    rc, opath = _run_cli(tmp_path, [rec])
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    ops_seen = {a["op"] for a in payload["steps"][0]["actions"]}
    assert ops_seen == {"traverse_edge"}
    assert any(n["id"] == "kw:a" for n in payload["nodes"])
    assert any(n["id"] == "kw:b" for n in payload["nodes"])
    # future op's node was never registered as a side-effect
    assert not any(n["id"] == "file:foo.md" for n in payload["nodes"])


def test_url_encoded_node_id_preserved(tmp_path):
    nid = "head:RAG%2FPipeline%2FReranking"
    rc, opath = _run_cli(
        tmp_path, [_rec(1, actions=[_traverse("kw:x", nid, kind="heading_doc")])]
    )
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    ids = {n["id"] for n in payload["nodes"]}
    assert nid in ids


def test_edge_dedup_earliest_step_and_weight_max(tmp_path):
    """Same undirected edge in multiple steps -> single entry, earliest first_step, max weight."""
    lines = [
        _rec(1, actions=[_traverse("kw:a", "kw:b", weight=0.3)]),
        _rec(2, actions=[_traverse("kw:b", "kw:a", weight=0.8)]),   # reversed + higher weight
        _rec(3, actions=[_traverse("kw:a", "kw:b", weight=0.4)]),
    ]
    rc, opath = _run_cli(tmp_path, lines)
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    same_kind = [e for e in payload["edges"] if e["edge_kind"] == "co_occurrence"]
    assert len(same_kind) == 1
    assert same_kind[0]["first_step"] == 1
    assert same_kind[0]["weight"] == pytest.approx(0.8)


def test_session_id_with_path_separators_uses_basename(tmp_path):
    """Input path containing directories should not crash title derivation."""
    subdir = tmp_path / "a" / "b"
    subdir.mkdir(parents=True)
    ipath = subdir / "weird/name.session.jsonl".replace("/", "_")  # safe filesystem name
    _write_jsonl(ipath, [_rec(1, actions=[_traverse("kw:a", "kw:b")])])
    opath = tmp_path / "out.html"
    rc = render._run([str(ipath), "-o", str(opath), "--quiet"])
    assert rc == render.EXIT_OK
    html = opath.read_text(encoding="utf-8")
    # basename minus suffix should appear in title
    assert "weird_name.session" in html


def test_large_trace_still_renders(tmp_path):
    """Large trace (>10k actions) must at least produce output."""
    actions = [
        _traverse(f"kw:a{i}", f"kw:b{i}", weight=(i % 100) / 100.0)
        for i in range(4000)
    ] + [
        _score(f"kw:a{i}", score=(i % 100) / 100.0) for i in range(4000)
    ] + [
        _propose(f"kw:llm{i}", origin=f"seed{i % 50}") for i in range(2500)
    ]
    rc, opath = _run_cli(tmp_path, [_rec(1, actions=actions)])
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    # 4000 traverse edges + 2500 llm_propose edges (dedup on identical key -> may be less)
    assert len(payload["edges"]) > 5000


def test_output_path_overwritten_without_prompt(tmp_path):
    ipath = tmp_path / "trace.jsonl"
    opath = tmp_path / "out.html"
    _write_jsonl(ipath, [_rec(1, actions=[_traverse("kw:a", "kw:b")])])
    opath.write_text("PRE-EXISTING CONTENT", encoding="utf-8")
    rc = render._run([str(ipath), "-o", str(opath), "--quiet"])
    assert rc == render.EXIT_OK
    assert "PRE-EXISTING CONTENT" not in opath.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Additional contract checks
# ---------------------------------------------------------------------------

def test_output_is_self_contained(tmp_path):
    """Contract: no *resource* references to external URLs.

    Matches IMPLEMENTATION.md §验收标准 item 2 (self-contained check),
    which greps for `src="http…"` / `href="http…"`. XML namespace URIs
    embedded inside vendored JS (e.g. `http://www.w3.org/2000/svg`) are
    strings, not fetches, and are allowed.
    """
    import re
    rc, opath = _run_cli(tmp_path, [_rec(1, actions=[_traverse("kw:a", "kw:b")])])
    assert rc == render.EXIT_OK
    html = opath.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html
    pat = re.compile(r'''(src|href)\s*=\s*["']https?://''')
    assert pat.search(html) is None, "output must not reference external URLs"


def test_template_has_four_required_placeholders_substituted(tmp_path):
    rc, opath = _run_cli(
        tmp_path,
        [_rec(1, actions=[_traverse("kw:a", "kw:b")], ts="2026-01-01T00:00:00Z")],
    )
    assert rc == render.EXIT_OK
    html = opath.read_text(encoding="utf-8")
    # placeholders should be gone
    for marker in ("${title}", "${cytoscape_js}", "${trace_data_json}", "${generation_header}"):
        assert marker not in html, f"{marker} still in output"
    # generation header content should be present
    assert "generated at" in html
    assert "trace_signals_version" in html


def test_cytoscape_inlined_not_scripted(tmp_path):
    rc, opath = _run_cli(tmp_path, [_rec(1, actions=[_traverse("kw:a", "kw:b")])])
    assert rc == render.EXIT_OK
    html = opath.read_text(encoding="utf-8")
    assert "<script src=" not in html
    # rough sanity: inlined cytoscape is much larger than any template noise
    assert len(html) > 200_000


def test_llm_propose_creates_dashed_edge(tmp_path):
    rc, opath = _run_cli(
        tmp_path,
        [_rec(1, verb="suggest", intent="expand",
              actions=[_propose("kw:reranker", origin="chunking")])],
    )
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    llm_edges = [e for e in payload["edges"] if e["op"] == "llm_propose"]
    assert len(llm_edges) == 1
    assert llm_edges[0]["style"] == "dashed"
    endpoints = {llm_edges[0]["source"], llm_edges[0]["target"]}
    assert endpoints == {"kw:chunking", "kw:reranker"}


def test_score_node_recorded_on_node_not_edge(tmp_path):
    rc, opath = _run_cli(
        tmp_path,
        [_rec(1, actions=[
            _traverse("kw:a", "kw:b"),
            _score("kw:b", score=0.9),
        ])],
    )
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    kw_b = [n for n in payload["nodes"] if n["id"] == "kw:b"][0]
    assert kw_b["scores"][0]["score"] == pytest.approx(0.9)
    assert len(payload["edges"]) == 1  # only the traverse edge


def test_records_out_of_order_are_normalised(tmp_path):
    rc, opath = _run_cli(
        tmp_path,
        [
            _rec(3, actions=[_traverse("kw:a", "kw:c")]),
            _rec(1, actions=[_traverse("kw:a", "kw:b")]),
            _rec(2, actions=[]),
        ],
    )
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    assert [s["step"] for s in payload["steps"]] == [1, 2, 3]
    edge_ab = [e for e in payload["edges"] if {e["source"], e["target"]} == {"kw:a", "kw:b"}][0]
    assert edge_ab["first_step"] == 1


def test_missing_output_arg_exits_3(tmp_path):
    ipath = tmp_path / "trace.jsonl"
    _write_jsonl(ipath, [_rec(1, actions=[])])
    rc = render._run([str(ipath), "--quiet"])
    assert rc == render.EXIT_ARGS


def test_determinism_bit_for_bit_modulo_header(tmp_path):
    lines = [
        _rec(1, actions=[_traverse("kw:a", "kw:b")]),
        _rec(2, actions=[_traverse("kw:b", "kw:c"), _score("kw:c", score=0.4)]),
    ]
    rc1, o1 = _run_cli(tmp_path, lines)
    tmp_path2 = tmp_path / "second"
    tmp_path2.mkdir()
    rc2, o2 = _run_cli(tmp_path2, lines)
    assert rc1 == rc2 == render.EXIT_OK
    a = [l for l in o1.read_text().splitlines() if "generated at" not in l]
    b = [l for l in o2.read_text().splitlines() if "generated at" not in l]
    assert a == b


# ---------------------------------------------------------------------------
# Internal-payload extractor
# ---------------------------------------------------------------------------

def _extract_trace(html_path: Path) -> dict:
    """Pull the window.__TRACE__ payload out of the rendered HTML.

    The renderer emits `window.__TRACE__ = <json>;` on its own line, so a
    simple locate-and-parse is sufficient.
    """
    text = html_path.read_text(encoding="utf-8")
    marker = "window.__TRACE__ = "
    idx = text.find(marker)
    assert idx != -1, "TRACE payload marker not found"
    start = idx + len(marker)
    end = text.find(";\n</script>", start)
    assert end != -1, "TRACE payload terminator not found"
    return json.loads(text[start:end])


# ---------------------------------------------------------------------------
# Security regressions (review findings 1-3)
# ---------------------------------------------------------------------------

def test_script_close_in_node_id_cannot_break_out(tmp_path):
    """Untrusted node id containing `</script>` must be neutralised so
    the tag does not close the embedding <script> element at parse time.
    """
    bad = "kw:</script><img src=x onerror=alert(1)>"
    rc, opath = _run_cli(tmp_path, [_rec(1, actions=[_traverse(bad, "kw:b")])])
    assert rc == render.EXIT_OK
    html_text = opath.read_text(encoding="utf-8")
    # Raw `</script>` from user data must not appear anywhere in the
    # output; the escaped form (\u003c/script>) is what should be there.
    assert "</script><img" not in html_text
    assert "\\u003c/script\\u003e" in html_text or "\\u003c/script>" in html_text
    # But the JSON payload must still be parseable and preserve the
    # original id (JSON.parse in the browser un-escapes \u003c).
    payload = _extract_trace(opath)
    ids = {n["id"] for n in payload["nodes"]}
    assert bad in ids


def test_script_close_in_rationale_cannot_break_out(tmp_path):
    rc, opath = _run_cli(
        tmp_path,
        [_rec(1, verb="suggest",
              actions=[{
                  "op": "llm_propose",
                  "nodes": ["kw:reranker"],
                  "meta": {
                      "origin_query": "chunking",
                      "rationale": "hello</script><script>alert('pwn')</script>",
                  },
              }])],
    )
    assert rc == render.EXIT_OK
    html_text = opath.read_text(encoding="utf-8")
    assert "</script><script>alert" not in html_text


def test_html_in_title_is_escaped(tmp_path):
    rc, opath = _run_cli(
        tmp_path,
        [_rec(1, actions=[_traverse("kw:a", "kw:b")])],
        extra_args=["--title", "<script>alert(1)</script>"],
    )
    assert rc == render.EXIT_OK
    html_text = opath.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text


def test_html_in_derived_title_from_filename_is_escaped(tmp_path):
    """A filename containing HTML markup must not inject into <h1>/<title>."""
    ipath = tmp_path / "<img src=x>_bad.jsonl"
    opath = tmp_path / "out.html"
    _write_jsonl(ipath, [_rec(1, actions=[_traverse("kw:a", "kw:b")])])
    rc = render._run([str(ipath), "-o", str(opath), "--quiet"])
    assert rc == render.EXIT_OK
    html_text = opath.read_text(encoding="utf-8")
    assert "<img src=x>" not in html_text.split("<script>", 1)[0]  # not in head/body markup
    assert "&lt;img" in html_text


def test_comment_breakout_via_filename_sanitised(tmp_path):
    """Filename with `-->` must not close the header HTML comment."""
    ipath = tmp_path / "x--_evil.jsonl"  # simulate suspicious chars we sanitise
    # actually reproduce the exact attack: filename with literal `-->`
    ipath = tmp_path / "boom-->x.jsonl"
    _write_jsonl(ipath, [_rec(1, actions=[_traverse("kw:a", "kw:b")])])
    opath = tmp_path / "out.html"
    rc = render._run([str(ipath), "-o", str(opath), "--quiet"])
    assert rc == render.EXIT_OK
    html_text = opath.read_text(encoding="utf-8")
    # Find the header comment block and ensure it terminates on its own line.
    head = html_text.split("<html", 1)[0]
    # Count `-->` occurrences in the comment region — should be exactly one
    # (the closing of the generation-header comment we emit).
    assert head.count("-->") == 1
    # And the raw dangerous substring must not appear inside the comment.
    assert "boom-->x" not in head


# ---------------------------------------------------------------------------
# Business-logic regressions (review findings 6-7)
# ---------------------------------------------------------------------------

def test_llm_propose_directed_key_preserves_both_rationales(tmp_path):
    """Two llm_propose edges between the same nodes in opposite directions
    are distinct proposals and must both be preserved with their own
    rationales.
    """
    rc, opath = _run_cli(
        tmp_path,
        [
            _rec(1, verb="suggest", actions=[{
                "op": "llm_propose",
                "nodes": ["kw:reranker"],
                "meta": {"origin_query": "chunking", "rationale": "A->B"},
            }]),
            _rec(2, verb="suggest", actions=[{
                "op": "llm_propose",
                "nodes": ["kw:chunking"],
                "meta": {"origin_query": "reranker", "rationale": "B->A"},
            }]),
        ],
    )
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    llm_edges = [e for e in payload["edges"] if e["op"] == "llm_propose"]
    assert len(llm_edges) == 2
    rationales = {e["rationale"] for e in llm_edges}
    assert rationales == {"A->B", "B->A"}


def test_llm_propose_repeated_same_direction_still_deduped(tmp_path):
    """Same directed llm_propose in two steps -> single entry, earliest step."""
    rc, opath = _run_cli(
        tmp_path,
        [
            _rec(1, verb="suggest", actions=[{
                "op": "llm_propose", "nodes": ["kw:x"],
                "meta": {"origin_query": "y", "rationale": "r1"},
            }]),
            _rec(3, verb="suggest", actions=[{
                "op": "llm_propose", "nodes": ["kw:x"],
                "meta": {"origin_query": "y", "rationale": "r2"},
            }]),
        ],
    )
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    llm_edges = [e for e in payload["edges"] if e["op"] == "llm_propose"]
    assert len(llm_edges) == 1
    assert llm_edges[0]["first_step"] == 1


def test_score_list_capped_and_dropped_reported(tmp_path):
    """Node with hundreds of score observations must be capped at
    SCORES_TOP_K; the delta is reported via `scores_dropped`.
    """
    n_obs = render.SCORES_TOP_K + 30
    actions = [_traverse("kw:hub", "kw:seed")]
    for i in range(n_obs):
        actions.append(_score("kw:hub", score=(i % 100) / 100.0))
    rc, opath = _run_cli(tmp_path, [_rec(1, actions=actions)])
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    hub = [n for n in payload["nodes"] if n["id"] == "kw:hub"][0]
    assert len(hub["scores"]) == render.SCORES_TOP_K
    assert hub["scores_dropped"] == 30


def test_score_list_below_cap_has_no_dropped_field(tmp_path):
    actions = [_traverse("kw:a", "kw:b"), _score("kw:b", score=0.5)]
    rc, opath = _run_cli(tmp_path, [_rec(1, actions=actions)])
    assert rc == render.EXIT_OK
    payload = _extract_trace(opath)
    kw_b = [n for n in payload["nodes"] if n["id"] == "kw:b"][0]
    assert "scores_dropped" not in kw_b
    assert len(kw_b["scores"]) == 1
