"""Regression gate for the Markdown parser bake-off (Phase 2.1)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "eval_md_parser.py"


@pytest.fixture(scope="module")
def eval_report():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "--only", "markdown-it-py", "marko", "mistune"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"eval script failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    return json.loads(result.stdout)


def test_primary_parser_markdown_it_py_all_pass(eval_report):
    primary = next((r for r in eval_report if r.get("parser") == "markdown-it-py"), None)
    assert primary is not None
    assert "unavailable" not in primary, primary
    assert primary["ok_files"] == primary["total_files"], primary


def test_marko_fallback_all_pass(eval_report):
    marko = next((r for r in eval_report if r.get("parser") == "marko"), None)
    assert marko is not None
    if "unavailable" in marko:
        pytest.skip("marko unavailable")
    assert marko["ok_files"] == marko["total_files"], marko


def test_code_block_disguise_case_is_zero_headings_from_code_block(eval_report):
    for report in eval_report:
        if "unavailable" in report:
            continue
        case = report["files"].get("a_code_block_disguise.md")
        assert case is not None
        assert case["matches"], f"{report['parser']} failed disguise case: {case}"
        assert len(case["got"]) == 3, f"{report['parser']} produced {case['got']}"
