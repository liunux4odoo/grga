#!/usr/bin/env python3
"""Markdown parser bake-off for grga v2 Phase 2.1.

Compares candidates on their ability to (a) extract *real* headings with
line numbers and (b) leave code-block-embedded ``#`` alone. Also captures
inline-code inside headings, code-block ranges, and BOM/CRLF handling.

Candidates:
* ``markdown-it-py`` (pure Python, CommonMark strict)
* ``marko``          (pure Python, CommonMark strict, AST driven)
* ``mistune``        (pure Python, fast)
* ``pulldown-cmark-cli`` (Rust CLI, optional; skipped if not installed)

Usage::

    python scripts/eval_md_parser.py [--fixtures DIR] [--json]

The golden expectations live in :data:`GOLDEN`. The script exits non-zero if
any candidate diverges from golden on any file; that keeps CI honest.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "md_eval"


# ---------------------------------------------------------------------------
# Golden expectations (heading count and line numbers per fixture)
# ---------------------------------------------------------------------------

GOLDEN: Dict[str, Dict[str, Any]] = {
    "a_code_block_disguise.md": {
        "headings": [
            {"level": 1, "text": "Real Heading", "line": 1},
            {"level": 2, "text": "Second Real Heading", "line": 12},
            {"level": 2, "text": "Third Real Heading", "line": 19},
        ],
        # code_block_disguise: fenced + indented; both must NOT produce headings
        "code_block_heading_count": 0,
    },
    "b_deep_nesting.md": {
        "headings": [
            {"level": 1, "text": "Level 1", "line": 1},
            {"level": 2, "text": "Level 2", "line": 3},
            {"level": 3, "text": "Level 3", "line": 5},
            {"level": 4, "text": "Level 4", "line": 7},
            {"level": 4, "text": "Another Level 4", "line": 11},
            {"level": 2, "text": "Back to Level 2", "line": 13},
        ],
    },
    "c_inline_code_in_heading.md": {
        "headings": [
            {"level": 1, "text": "Heading with inline code", "line": 1},
            {"level": 2, "text": "Another code in heading", "line": 5},
        ],
    },
    "d_no_heading.md": {"headings": []},
    "e_bom_crlf.md": {
        "headings": [
            {"level": 1, "text": "BOM Heading", "line": 1},
            {"level": 2, "text": "Second", "line": 5},
        ],
    },
}


# ---------------------------------------------------------------------------
# Parser adapters -> unified schema {headings: [{level, text, line}]}
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def parse_markdown_it(path: Path) -> Dict[str, Any]:
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark")
    text = path.read_text(encoding="utf-8-sig")
    tokens = md.parse(text)
    headings: List[Dict[str, Any]] = []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.map:
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            heading_text = ""
            if inline and inline.type == "inline":
                heading_text = "".join(
                    child.content
                    for child in (inline.children or [])
                    if child.type in ("text", "code_inline")
                )
            headings.append(
                {
                    "level": int(tok.tag[1:]),
                    "text": _normalize_text(heading_text),
                    "line": tok.map[0] + 1,
                }
            )
    return {"headings": headings}


def parse_marko(path: Path) -> Dict[str, Any]:
    import marko

    text = path.read_text(encoding="utf-8-sig")
    doc = marko.Markdown().parse(text)
    lines = text.splitlines()

    def _text_of(node) -> str:
        parts: List[str] = []
        for child in getattr(node, "children", []) or []:
            if hasattr(child, "children"):
                parts.append(_text_of(child))
            elif isinstance(getattr(child, "children", None), str):
                parts.append(child.children)
            else:
                parts.append(str(getattr(child, "children", "") or ""))
        if isinstance(getattr(node, "children", None), str):
            parts.append(node.children)
        return "".join(parts)

    def _find_line(text_needle: str, start: int = 0) -> int:
        needle = text_needle.strip()
        for i, line in enumerate(lines[start:], start=start):
            if needle and needle in line:
                return i + 1
        return start + 1

    headings: List[Dict[str, Any]] = []
    cursor = 0
    for node in doc.children:
        cls = type(node).__name__
        if cls == "Heading":
            heading_text = _normalize_text(_text_of(node))
            line = _find_line(heading_text.split(" `")[0].split()[0] if heading_text else "", cursor)
            headings.append({"level": int(node.level), "text": heading_text, "line": line})
            cursor = line
    return {"headings": headings}


def parse_mistune(path: Path) -> Dict[str, Any]:
    import mistune

    text = path.read_text(encoding="utf-8-sig")
    markdown = mistune.create_markdown(renderer=None)
    ast = markdown(text)
    lines = text.splitlines()

    def _text_of(node) -> str:
        if node.get("type") == "text":
            return node.get("raw", "")
        if node.get("type") == "codespan":
            return node.get("raw", "")
        parts = []
        for child in node.get("children") or []:
            parts.append(_text_of(child))
        return "".join(parts)

    def _find_line(text_needle: str, start: int = 0) -> int:
        needle = text_needle.strip()
        for i, line in enumerate(lines[start:], start=start):
            if needle and needle in line:
                return i + 1
        return start + 1

    headings: List[Dict[str, Any]] = []
    cursor = 0
    for node in ast:
        if node.get("type") == "heading":
            heading_text = _normalize_text(_text_of(node))
            line = _find_line(heading_text.split(" `")[0].split()[0] if heading_text else "", cursor)
            headings.append(
                {"level": int(node.get("attrs", {}).get("level", 1)), "text": heading_text, "line": line}
            )
            cursor = line
    return {"headings": headings}


def parse_pulldown_cmark_cli(path: Path) -> Dict[str, Any]:
    exe = shutil.which("pulldown-cmark") or shutil.which("pulldown-cmark-cli")
    if not exe:
        raise RuntimeError("pulldown-cmark-cli not installed")
    # This adapter is a stub: real integration would parse events with
    # ``pulldown-cmark`` as a library or a JSON emitter. Left for the ADR.
    raise RuntimeError("pulldown-cmark-cli adapter not yet implemented")


PARSERS: Dict[str, Callable[[Path], Dict[str, Any]]] = {
    "markdown-it-py": parse_markdown_it,
    "marko": parse_marko,
    "mistune": parse_mistune,
    "pulldown-cmark-cli": parse_pulldown_cmark_cli,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def evaluate_parser(name: str, parser: Callable[[Path], Dict[str, Any]], fixtures: Path) -> Dict[str, Any]:
    per_file: Dict[str, Any] = {}
    cold_start = None
    total_time = 0.0
    ok_files = 0
    total_files = 0
    for md_file in sorted(fixtures.glob("*.md")):
        total_files += 1
        golden = GOLDEN.get(md_file.name, {})
        start = time.perf_counter()
        try:
            result = parser(md_file)
        except Exception as exc:
            per_file[md_file.name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        elapsed = time.perf_counter() - start
        if cold_start is None:
            cold_start = elapsed
        total_time += elapsed
        expected = [
            (h["level"], _normalize_text(h["text"]), h["line"]) for h in golden.get("headings", [])
        ]
        got = [
            (h["level"], _normalize_text(h["text"]), h["line"])
            for h in result.get("headings", [])
        ]
        matches = expected == got
        per_file[md_file.name] = {
            "expected": expected,
            "got": got,
            "matches": matches,
            "elapsed_ms": round(elapsed * 1000, 2),
        }
        if matches:
            ok_files += 1
    return {
        "parser": name,
        "cold_start_ms": round((cold_start or 0.0) * 1000, 2),
        "total_ms": round(total_time * 1000, 2),
        "avg_ms": round((total_time / max(total_files, 1)) * 1000, 2),
        "ok_files": ok_files,
        "total_files": total_files,
        "files": per_file,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    ap.add_argument("--json", action="store_true", help="Emit raw JSON report")
    ap.add_argument("--only", nargs="+", choices=list(PARSERS), help="Restrict to specific parsers")
    args = ap.parse_args()

    fixtures = args.fixtures
    if not fixtures.is_dir():
        print(f"fixtures directory not found: {fixtures}", file=sys.stderr)
        return 2

    selected = args.only or list(PARSERS)
    reports: List[Dict[str, Any]] = []
    for name in selected:
        try:
            reports.append(evaluate_parser(name, PARSERS[name], fixtures))
        except Exception as exc:  # noqa: BLE001
            reports.append({"parser": name, "unavailable": f"{type(exc).__name__}: {exc}"})

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        # exit non-zero if any *available* parser has failing files
        any_failure = any(
            r.get("ok_files", 0) != r.get("total_files", 0)
            for r in reports
            if "unavailable" not in r
        )
        return 1 if any_failure else 0

    for report in reports:
        if "unavailable" in report:
            print(f"[{report['parser']}] UNAVAILABLE: {report['unavailable']}")
            continue
        header = f"[{report['parser']}] ok {report['ok_files']}/{report['total_files']}  cold={report['cold_start_ms']}ms  avg={report['avg_ms']}ms"
        print(header)
        for name, info in report["files"].items():
            if "error" in info:
                print(f"  {name}: ERROR {info['error']}")
                continue
            status = "PASS" if info["matches"] else "FAIL"
            print(f"  {name}: {status}  ({info['elapsed_ms']}ms)")
            if not info["matches"]:
                print(f"    expected: {info['expected']}")
                print(f"    got     : {info['got']}")
    any_failure = any(
        r.get("ok_files", 0) != r.get("total_files", 0)
        for r in reports
        if "unavailable" not in r
    )
    return 1 if any_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
