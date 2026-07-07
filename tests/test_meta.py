"""Tests for :mod:`grga.meta` (Phase 1.2 + Phase 2.4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.meta import annotate_hits, annotate_result_inplace, annotate_structure_inplace


def _raw(path: str, line: int, *, context: bool = False, submatches=None) -> dict:
    return {
        "path": path,
        "line_number": line,
        "text": f"line {line}",
        "is_context": context,
        "submatches": submatches or [],
    }


def test_context_lines_are_dropped():
    raw = [_raw("a.md", 1, context=True), _raw("a.md", 2), _raw("a.md", 3, context=True)]
    hits = annotate_hits(raw)
    assert len(hits) == 1
    assert hits[0].line_number == 2


def test_occurrence_index_and_total_per_file():
    raw = [
        _raw("a.md", 5),
        _raw("a.md", 12),
        _raw("b.md", 3),
        _raw("a.md", 40),
    ]
    hits = annotate_hits(raw, file_line_counts={"a.md": 100, "b.md": 10})
    a_hits = [h for h in hits if h.path == "a.md"]
    b_hits = [h for h in hits if h.path == "b.md"]
    assert [h.occurrence_index for h in a_hits] == [1, 2, 3]
    assert all(h.total_occurrences == 3 for h in a_hits)
    assert b_hits[0].occurrence_index == 1
    assert b_hits[0].total_occurrences == 1


def test_line_ratio_uses_file_line_counts():
    raw = [_raw("a.md", 25), _raw("a.md", 100)]
    hits = annotate_hits(raw, file_line_counts={"a.md": 100})
    assert hits[0].line_ratio == 0.25
    assert hits[1].line_ratio == 1.0


def test_line_ratio_defaults_to_zero_when_unknown():
    raw = [_raw("a.md", 5)]
    hits = annotate_hits(raw)
    assert hits[0].line_ratio == 0.0


def test_nearby_hits_counts_other_hits_within_window():
    raw = [_raw("a.md", 10), _raw("a.md", 12), _raw("a.md", 20), _raw("a.md", 100)]
    hits = annotate_hits(raw, window=5)
    by_line = {h.line_number: h for h in hits}
    assert by_line[10].nearby_hits == 1  # 12 is within +/-5
    assert by_line[12].nearby_hits == 1  # 10
    assert by_line[20].nearby_hits == 0
    assert by_line[100].nearby_hits == 0


def test_nearby_hits_ignores_other_files():
    raw = [_raw("a.md", 10), _raw("b.md", 11), _raw("b.md", 12)]
    hits = annotate_hits(raw, window=5)
    by_key = {(h.path, h.line_number): h for h in hits}
    assert by_key[("a.md", 10)].nearby_hits == 0
    assert by_key[("b.md", 11)].nearby_hits == 1
    assert by_key[("b.md", 12)].nearby_hits == 1


def test_matched_terms_extracted_from_submatches():
    raw = [
        _raw(
            "a.md",
            5,
            submatches=[{"match": "macOS", "start": 0, "end": 5}, {"match": "macOS", "start": 20, "end": 25}],
        )
    ]
    hits = annotate_hits(raw)
    assert hits[0].matched_terms == ["macOS"]


def test_empty_input_returns_empty_list():
    assert annotate_hits([]) == []


def test_matched_terms_dedup_multi_word_submatch():
    raw = [
        _raw(
            "a.md",
            1,
            submatches=[
                {"match": "foo", "start": 0, "end": 3},
                {"match": "bar", "start": 4, "end": 7},
                {"match": "foo", "start": 10, "end": 13},
            ],
        )
    ]
    hits = annotate_hits(raw)
    assert hits[0].matched_terms == ["foo", "bar"]


def test_hits_preserve_provenance_from_raw():
    raw = [
        {
            "path": "a.md",
            "line_number": 3,
            "text": "hello",
            "is_context": False,
            "submatches": [{"match": "hello", "start": 0, "end": 5}],
            "from_query": "q1",
            "from_tier": 2,
            "absolute_offset": 42,
        }
    ]
    hits = annotate_hits(raw)
    h = hits[0]
    assert h.from_query == "q1"
    assert h.from_tier == 2
    assert h.absolute_offset == 42
    assert h.char_span == (0, 5)


# --- Phase 2.4: structure back-fill ------------------------------------------


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))


def test_annotate_structure_fills_heading_path(docs_fixture: Path, sidecar_env):
    # 'macOS' is on line 5 of install.md, under heading '## macOS'
    result = {
        "ok": True,
        "directory": str(docs_fixture),
        "matches": [
            {
                "path": "install.md",
                "line_number": 5,
                "text": "## macOS",
                "is_context": False,
                "submatches": [{"match": "macOS", "start": 3, "end": 8}],
            }
        ],
    }
    annotate_structure_inplace(result, base_dir=str(docs_fixture))
    match = result["matches"][0]
    assert match["heading_path"] == ["安装指南", "macOS"]
    assert match["chapter_id"].endswith("/macos") or match["chapter_id"].endswith("macos")
    assert isinstance(match["chapter_range"], list) and len(match["chapter_range"]) == 2


def test_annotate_structure_skips_non_markdown(docs_fixture: Path, sidecar_env):
    result = {
        "ok": True,
        "directory": str(docs_fixture),
        "matches": [
            {
                "path": "code_sample.py",
                "line_number": 3,
                "text": "def hello_world():",
                "is_context": False,
                "submatches": [],
            }
        ],
    }
    annotate_structure_inplace(result, base_dir=str(docs_fixture))
    match = result["matches"][0]
    assert "heading_path" not in match
    assert "chapter_id" not in match


def test_annotate_structure_no_op_on_failed_result(sidecar_env):
    result = {"ok": False, "error": "boom", "matches": []}
    annotate_structure_inplace(result)
    assert result == {"ok": False, "error": "boom", "matches": []}


def test_annotate_result_inplace_adds_layer2_fields(docs_fixture: Path, sidecar_env):
    result = {
        "ok": True,
        "directory": str(docs_fixture),
        "matches": [
            {
                "path": "install.md",
                "line_number": 5,
                "text": "## macOS",
                "is_context": False,
                "submatches": [{"match": "macOS", "start": 3, "end": 8}],
            },
            {
                "path": "install.md",
                "line_number": 7,
                "text": "在 macOS 上使用 brew 安装：",
                "is_context": False,
                "submatches": [{"match": "macOS", "start": 2, "end": 7}],
            },
        ],
    }
    annotate_result_inplace(result, base_dir=str(docs_fixture))
    first = result["matches"][0]
    assert first["occurrence_index"] == 1
    assert first["total_occurrences"] == 2
    assert 0 <= first["line_ratio"] <= 1
    assert first["matched_terms"] == ["macOS"]
