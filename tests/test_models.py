"""Tests for :mod:`grga.models` (Phase 1.1)."""
from __future__ import annotations

import json

import pytest

from grga.models import Context, Hit


def test_hit_defaults_are_stable():
    hit = Hit(path="a.md", line_number=1, text="hello")
    assert hit.char_span == (0, 0)
    assert hit.occurrence_index == 0
    assert hit.total_occurrences == 0
    assert hit.line_ratio == 0.0
    assert hit.nearby_hits == 0
    assert hit.heading_path is None
    assert hit.chapter_id is None
    assert hit.chapter_range is None
    assert hit.matched_terms == []
    assert hit.is_llm_generated is False
    assert hit.is_context is False


def test_hit_from_raw_match_populates_span_and_provenance():
    raw = {
        "path": "docs/install.md",
        "line_number": 3,
        "absolute_offset": 42,
        "text": "在 macOS 上使用 brew 安装",
        "is_context": False,
        "submatches": [{"match": "macOS", "start": 2, "end": 7}],
        "from_query": "macOS",
        "from_tier": 1,
    }
    hit = Hit.from_raw_match(raw, matched_terms=["macOS"])
    assert hit.path == "docs/install.md"
    assert hit.line_number == 3
    assert hit.text.startswith("在 macOS")
    assert hit.char_span == (2, 7)
    assert hit.matched_terms == ["macOS"]
    assert hit.from_query == "macOS"
    assert hit.from_tier == 1
    assert hit.absolute_offset == 42
    assert hit.is_context is False


def test_hit_from_raw_match_handles_context_lines_without_submatches():
    raw = {"path": "a.md", "line_number": 1, "text": "# Title", "is_context": True, "submatches": []}
    hit = Hit.from_raw_match(raw)
    assert hit.is_context is True
    assert hit.char_span == (0, 0)
    assert hit.matched_terms == []


def test_hit_from_raw_match_tolerates_missing_fields():
    hit = Hit.from_raw_match({})
    assert hit.path == ""
    assert hit.line_number == 0
    assert hit.text == ""


def test_hit_to_dict_is_json_serializable():
    hit = Hit(
        path="a.md",
        line_number=2,
        text="hi",
        char_span=(0, 2),
        heading_path=["Chapter 1"],
        chapter_id="chapter-1",
        chapter_range=(1, 5),
    )
    payload = hit.to_dict()
    assert payload["char_span"] == [0, 2]
    assert payload["chapter_range"] == [1, 5]
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "chapter-1" in encoded


def test_context_to_dict_round_trip():
    hit = Hit(path="a.md", line_number=3, text="hello")
    ctx = Context(hit=hit, text="hello world", level="lines", range=(1, 5), truncated=False)
    payload = ctx.to_dict()
    assert payload["level"] == "lines"
    assert payload["range"] == [1, 5]
    assert payload["hit"]["path"] == "a.md"
    assert payload["truncated"] is False
    json.dumps(payload)  # must not raise
