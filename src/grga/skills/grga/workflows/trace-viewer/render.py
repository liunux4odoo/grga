#!/usr/bin/env python3
"""grga trace-viewer renderer.

Reads a JSONL trace (see collect.md) and writes a single self-contained
HTML file that visualises how the keyword co-occurrence graph expanded
across a grga session.

Contract lives in IMPLEMENTATION.md; this module is stdlib-only.
"""

from __future__ import annotations

import argparse
import html
import json
import string
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


VALID_VERBS = frozenset({"related", "suggest"})
# v1 frozen set of graph_action ops. Anything outside this set is treated
# as a "future op" and skipped per IMPLEMENTATION.md boundary table.
VALID_OPS = frozenset({"traverse_edge", "score_node", "llm_propose"})
REQUIRED_RECORD_FIELDS = ("step", "verb", "graph_actions")

EXIT_OK = 0
EXIT_IO = 1
EXIT_EMPTY = 2
EXIT_ARGS = 3

VENDOR_JS_REL = Path("vendor") / "cytoscape.min.js"
TEMPLATE_REL = Path("template.html")

# Node-count threshold above which the viewer falls back from `cose`
# (spring-embedder, quadratic in practice) to `grid`. `cose` freezes the
# tab at multi-thousand-node scale; `grid` at least remains navigable.
LAYOUT_GRID_THRESHOLD = 1500

# Cap per-node score-observation lists. Long sessions can re-score a hub
# node hundreds of times; without a cap the JSON payload bloats and the
# detail panel renders one row per observation. We keep the top-K by
# score (max is what `bestScore` actually reads) plus a `dropped` count
# so the UI can indicate truncation.
SCORES_TOP_K = 20


def _js_escape_for_script(payload: str) -> str:
    """Neutralise HTML-significant sequences inside a JSON string so it
    can be safely embedded between literal ``<script>...</script>`` tags.

    The pairing with the HTML parser matters: ``</script>`` inside a
    ``<script>`` element terminates the element at parse time, before
    any JS runs, so runtime escaping in JS offers no protection. We
    rewrite ``<`` / ``>`` / ``&`` (plus the JSON-legal line separators
    ``U+2028`` / ``U+2029`` that break some JS parsers) to their
    ``\\uXXXX`` forms. The result is still valid JSON, and none of the
    input strings contained literal backslashes that could interact.
    """
    return (
        payload
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _sanitise_for_html_comment(text: str) -> str:
    """Make a string safe to place inside an ``<!-- ... -->`` comment.

    HTML comments end at ``-->``; a filename containing that substring
    (or embedded ``<``/``>``) can close the comment and inject markup.
    We collapse ``--`` and drop angle brackets — the header is
    diagnostic only, so lossy sanitisation is fine.
    """
    return (
        text.replace("<", "")
        .replace(">", "")
        .replace("--", "__")
    )


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

class _Reporter:
    """Tiny stderr reporter that honours --quiet.

    Errors always print; warnings are suppressed when quiet."""

    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet

    def warn(self, msg: str) -> None:
        if not self.quiet:
            print(f"[warn] {msg}", file=sys.stderr)

    def error(self, msg: str) -> None:
        print(f"[error] {msg}", file=sys.stderr)


def _preview(line: str, n: int = 80) -> str:
    """Return up to n chars of a line, single-lined, for diagnostics."""
    trimmed = line.strip().replace("\n", " ").replace("\r", " ")
    return trimmed[:n]


def _iter_valid_records(
    path: Path, reporter: _Reporter
) -> Iterable[Dict[str, Any]]:
    """Yield structurally-valid records from a JSONL file.

    Skips (with warnings) blank lines, unparseable JSON, records missing
    required fields, and records with unknown verbs.
    """
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if not raw.strip():
                # blank / whitespace-only line: silently skipped per contract
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                reporter.warn(
                    f"line {lineno}: JSON decode failed; "
                    f"first 80 chars: {_preview(raw)!r}"
                )
                continue
            if not isinstance(rec, dict):
                reporter.warn(
                    f"line {lineno}: expected JSON object, got "
                    f"{type(rec).__name__}"
                )
                continue
            missing = [k for k in REQUIRED_RECORD_FIELDS if k not in rec]
            if missing:
                reporter.warn(
                    f"line {lineno}: missing required fields {missing}"
                )
                continue
            if rec.get("verb") not in VALID_VERBS:
                reporter.warn(
                    f"line {lineno}: unsupported verb "
                    f"{rec.get('verb')!r}; expected one of "
                    f"{sorted(VALID_VERBS)}"
                )
                continue
            if not isinstance(rec.get("graph_actions"), list):
                reporter.warn(
                    f"line {lineno}: graph_actions must be a list"
                )
                continue
            if not isinstance(rec.get("step"), int):
                reporter.warn(
                    f"line {lineno}: step must be an integer"
                )
                continue
            yield rec


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _infer_node_kind(node_id: str) -> str:
    """Classify a node by its id prefix.

    grga uses `kw:` / `file:` / `head:` (see README). Unknown prefixes
    fall back to `other` so the viewer can still render them.
    """
    if node_id.startswith("kw:"):
        return "kw"
    if node_id.startswith("file:"):
        return "file"
    if node_id.startswith("head:"):
        return "head"
    return "other"


def _touch_node(
    nodes: Dict[str, Dict[str, Any]], node_id: str, step: int
) -> Dict[str, Any]:
    node = nodes.get(node_id)
    if node is None:
        node = {
            "id": node_id,
            "kind": _infer_node_kind(node_id),
            "first_step": step,
        }
        nodes[node_id] = node
    else:
        if step < node["first_step"]:
            node["first_step"] = step
    return node


def _edge_key(source: str, target: str, edge_kind: str) -> Tuple[str, str, str]:
    """Undirected edge identity for dedup.

    Edges are treated as undirected because grga's keyword_pairs graph is
    undirected; we normalise by sorting endpoints. edge_kind participates
    in the key so `co_occurrence` and `keyword_doc` links between the same
    pair remain distinct.
    """
    a, b = sorted([source, target])
    return (a, b, edge_kind)


def _merge_weight(existing: Optional[float], incoming: Optional[float]) -> Optional[float]:
    """Weight merge strategy for repeated edges: keep the MAX seen weight.

    Rationale: score-like weights (PPR mass, edge affinity) are bounded in
    [0, 1] and later observations tend to be as-or-more-informed than
    earlier ones; taking the max avoids under-weighting an edge that was
    strongly reinforced later in the session while still being determinstic.
    """
    if incoming is None:
        return existing
    if existing is None:
        return incoming
    try:
        return max(existing, incoming)
    except TypeError:
        # incomparable types: prefer the newer value
        return incoming


def aggregate(records: List[Dict[str, Any]], reporter: _Reporter) -> Dict[str, Any]:
    """Turn validated JSONL records into the viewer's data payload.

    Returns a dict with `steps`, `nodes`, `edges`, and `trace_signals_version`.
    """
    steps_out: List[Dict[str, Any]] = []
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    trace_signals_version: Optional[str] = None

    # Deterministic ordering: process records by ascending step. Ties keep
    # input order (stable sort).
    sorted_records = sorted(
        enumerate(records), key=lambda x: (x[1].get("step", 0), x[0])
    )

    for _orig_idx, rec in sorted_records:
        step = rec["step"]
        verb = rec["verb"]
        raw_actions = rec.get("graph_actions", [])
        args = rec.get("args", {}) if isinstance(rec.get("args"), dict) else {}
        ts = rec.get("ts", "")
        version = rec.get("trace_signals_version")
        if trace_signals_version is None and isinstance(version, str):
            trace_signals_version = version

        kept_actions: List[Dict[str, Any]] = []
        for a_idx, action in enumerate(raw_actions):
            if not isinstance(action, dict):
                reporter.warn(
                    f"step {step} action #{a_idx}: not an object, skipped"
                )
                continue
            op = action.get("op")
            if op not in VALID_OPS:
                reporter.warn(
                    f"step {step} action #{a_idx}: unknown op {op!r} "
                    f"(future or malformed); skipped"
                )
                continue
            action_nodes = action.get("nodes")
            if not isinstance(action_nodes, list) or not action_nodes:
                reporter.warn(
                    f"step {step} action #{a_idx}: missing/empty 'nodes'; "
                    f"skipped"
                )
                continue
            meta = action.get("meta") if isinstance(action.get("meta"), dict) else {}

            # Register every mentioned node.
            for nid in action_nodes:
                if not isinstance(nid, str) or not nid:
                    continue
                _touch_node(nodes, nid, step)

            if op == "traverse_edge":
                # traverse_edge nodes are a chain [src, mid1, ..., dst]
                # (v1 typically has 2). Materialise consecutive pairs.
                edge_kind = str(meta.get("edge_kind", "unknown"))
                weight = meta.get("weight")
                pairs = list(zip(action_nodes, action_nodes[1:]))
                if not pairs:
                    reporter.warn(
                        f"step {step} action #{a_idx}: traverse_edge needs "
                        f">= 2 nodes; skipped"
                    )
                    continue
                for src, dst in pairs:
                    if not isinstance(src, str) or not isinstance(dst, str):
                        continue
                    if src == dst:
                        continue
                    key = _edge_key(src, dst, edge_kind)
                    edge = edges.get(key)
                    if edge is None:
                        # Preserve the first-seen orientation for display.
                        edges[key] = {
                            "source": src,
                            "target": dst,
                            "edge_kind": edge_kind,
                            "op": "traverse_edge",
                            "style": "solid",
                            "first_step": step,
                            "weight": weight,
                        }
                    else:
                        # Repeated edge: keep earliest step, merge weight.
                        if step < edge["first_step"]:
                            edge["first_step"] = step
                        edge["weight"] = _merge_weight(edge.get("weight"), weight)
            elif op == "score_node":
                # Attach score to nodes; not an edge.
                algo = meta.get("algo")
                score = meta.get("score")
                hops = meta.get("hops_from_seed")
                for nid in action_nodes:
                    if not isinstance(nid, str):
                        continue
                    node = _touch_node(nodes, nid, step)
                    scores = node.setdefault("scores", [])
                    scores.append(
                        {"step": step, "algo": algo, "score": score, "hops_from_seed": hops}
                    )
            elif op == "llm_propose":
                # Synthesize a dashed edge from origin_query (as kw:) to
                # each proposed node. When origin_query is missing we still
                # register the target nodes but do not fabricate an edge.
                #
                # llm_propose edges use a DIRECTED key so
                # `chunking -> reranker` at step 1 and
                # `reranker -> chunking` at step 5 remain distinct — each
                # carries a source-specific `rationale` we must not
                # silently collapse. `traverse_edge` remains undirected
                # because grga's keyword_pairs graph is undirected.
                origin_query = meta.get("origin_query")
                if isinstance(origin_query, str) and origin_query:
                    origin_id = (
                        origin_query
                        if origin_query.startswith(("kw:", "file:", "head:"))
                        else f"kw:{origin_query}"
                    )
                    _touch_node(nodes, origin_id, step)
                    for nid in action_nodes:
                        if not isinstance(nid, str) or not nid or nid == origin_id:
                            continue
                        key = ("llm_propose", origin_id, nid)
                        edge = edges.get(key)
                        if edge is None:
                            edges[key] = {
                                "source": origin_id,
                                "target": nid,
                                "edge_kind": "llm_propose",
                                "op": "llm_propose",
                                "style": "dashed",
                                "first_step": step,
                                "weight": None,
                                "rationale": meta.get("rationale"),
                            }
                        else:
                            if step < edge["first_step"]:
                                edge["first_step"] = step

            kept_actions.append(
                {
                    "op": op,
                    "nodes": [n for n in action_nodes if isinstance(n, str)],
                    "meta": meta,
                }
            )

        steps_out.append(
            {
                "step": step,
                "ts": ts,
                "verb": verb,
                "args": args,
                "actions": kept_actions,
            }
        )

    # Deterministic ordering for nodes / edges (sorted by first_step then id).
    # Cap each node's score list to the top-K by score to bound the JSON
    # payload and the detail-panel row count on long re-scoring sessions.
    for node in nodes.values():
        scores = node.get("scores")
        if scores and len(scores) > SCORES_TOP_K:
            def _score_val(s: Dict[str, Any]) -> float:
                v = s.get("score")
                return v if isinstance(v, (int, float)) else float("-inf")

            top = sorted(scores, key=_score_val, reverse=True)[:SCORES_TOP_K]
            # Re-sort the kept ones by step for stable, readable display.
            node["scores"] = sorted(top, key=lambda s: s.get("step", 0))
            node["scores_dropped"] = len(scores) - SCORES_TOP_K

    nodes_list = sorted(nodes.values(), key=lambda n: (n["first_step"], n["id"]))
    edges_list = sorted(
        edges.values(),
        key=lambda e: (e["first_step"], e["source"], e["target"], e["edge_kind"]),
    )
    max_step = max((s["step"] for s in steps_out), default=0)

    return {
        "steps": steps_out,
        "nodes": nodes_list,
        "edges": edges_list,
        "trace_signals_version": trace_signals_version or "unknown",
        "max_step": max_step,
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _derive_title(input_path: Path, override: Optional[str]) -> str:
    if override:
        return override
    # session-id may contain path separators -> use basename minus suffix.
    stem = Path(input_path.name).stem or "trace"
    return f"grga trace: {stem}"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _build_generation_header(
    input_path: Path, trace_signals_version: str
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # HTML comment; the marker `generated at` is what the acceptance
    # command greps out to compare determinism. Filenames and version
    # strings are sanitised so a filename like ``x--><script>`` cannot
    # close the comment.
    safe_name = _sanitise_for_html_comment(input_path.name)
    safe_version = _sanitise_for_html_comment(trace_signals_version)
    return (
        f"<!-- grga trace-viewer\n"
        f"     generated at {now}\n"
        f"     input: {safe_name}\n"
        f"     trace_signals_version: {safe_version}\n"
        f"-->"
    )


def render_html(
    data: Dict[str, Any],
    template_text: str,
    cytoscape_js: str,
    title: str,
    generation_header: str,
) -> str:
    """Fill the template with the four required placeholders.

    We use ``string.Template.safe_substitute`` so the massive cytoscape
    payload can contain `$` characters without breaking substitution.
    The title is HTML-escaped (it lands in element text) and the JSON
    payload is escaped so it cannot break out of its ``<script>`` host.
    """
    template = string.Template(template_text)
    trace_json = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    trace_json = _js_escape_for_script(trace_json)
    return template.safe_substitute(
        title=html.escape(title, quote=True),
        cytoscape_js=cytoscape_js,
        trace_data_json=trace_json,
        generation_header=generation_header,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="render.py",
        description=(
            "Render a grga trace JSONL file into a self-contained HTML "
            "viewer."
        ),
    )
    p.add_argument("input", help="Path to the JSONL trace file")
    p.add_argument(
        "-o", "--output", required=True, help="Path to write the HTML output"
    )
    p.add_argument(
        "--title",
        default=None,
        help="Optional page title (defaults to derived from input filename)",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress/warning messages on stderr",
    )
    return p


def _run(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    # argparse exits with code 2 for parse errors, but our contract wants 3.
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # `parser.parse_args` calls sys.exit; translate its code to ours.
        if exc.code == 0:
            return EXIT_OK
        return EXIT_ARGS

    reporter = _Reporter(quiet=args.quiet)
    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists() or not input_path.is_file():
        reporter.error(f"input file not found or not a regular file: {input_path}")
        return EXIT_IO
    try:
        # readability probe (open + close is enough)
        with input_path.open("r", encoding="utf-8"):
            pass
    except OSError as exc:
        reporter.error(f"cannot read input: {exc}")
        return EXIT_IO

    records = list(_iter_valid_records(input_path, reporter))
    if not records:
        reporter.error("no valid records in input (empty file or all lines invalid)")
        return EXIT_EMPTY

    data = aggregate(records, reporter)

    here = Path(__file__).resolve().parent
    template_path = here / TEMPLATE_REL
    vendor_path = here / VENDOR_JS_REL
    if not template_path.exists():
        reporter.error(f"template.html missing next to render.py: {template_path}")
        return EXIT_IO
    if not vendor_path.exists():
        reporter.error(f"vendor/cytoscape.min.js missing: {vendor_path}")
        return EXIT_IO

    template_text = _read_text(template_path)
    cytoscape_js = _read_text(vendor_path)

    title = _derive_title(input_path, args.title)
    header = _build_generation_header(input_path, data["trace_signals_version"])
    html = render_html(
        data=data,
        template_text=template_text,
        cytoscape_js=cytoscape_js,
        title=title,
        generation_header=header,
    )

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html, encoding="utf-8")
    except OSError as exc:
        reporter.error(f"cannot write output: {exc}")
        return EXIT_IO

    if not args.quiet:
        print(
            f"[ok] wrote {output_path} "
            f"({len(data['steps'])} steps, {len(data['nodes'])} nodes, "
            f"{len(data['edges'])} edges)",
            file=sys.stderr,
        )
    return EXIT_OK


def main() -> None:
    sys.exit(_run())


if __name__ == "__main__":
    main()
