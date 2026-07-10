"""Tests for the implicit-AND query semantics and the global --max-total cap.

Covers three regressions we want to fence off:

1. Adjacent terms parse as AND by default (breaking change vs v1 OR default).
2. ``search_directory`` truncates streaming rga output once ``max_total`` real
   hits are collected and sets ``truncated`` in the returned JSON.
3. Layer 2 metadata (``occurrence_index`` / ``total_occurrences``) is computed
   *after* truncation, so the counts reflect the returned slice rather than
   the untrimmed rga output.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from grga.meta import annotate_result_inplace
from grga.search import (
    _compile_search_query,
    _find_rga_command,
    search_directory,
)


def _rga_or_skip():
    if _find_rga_command() is None:
        pytest.skip("rga not available")


def test_implicit_adjacent_terms_are_and_by_default():
    query = _compile_search_query("韩立 师傅", "auto")
    assert query["implicit_and"] is True
    assert query["pcre2"] is True
    assert query["patterns"] == ["(?=.*(?:韩立))(?=.*(?:师傅))"]
    assert query["implicit_notice"]
    assert "AND" in query["implicit_notice"]


def test_explicit_or_disables_implicit_notice():
    query = _compile_search_query("韩立 || 师傅", "auto")
    assert query["implicit_and"] is False
    assert query["pcre2"] is False
    assert query["patterns"] == ["(?:韩立)|(?:师傅)"]
    assert query["implicit_notice"] is None


def test_implicit_operator_or_restores_v1_or_semantics():
    query = _compile_search_query("韩立 师傅", "auto", implicit_operator="or")
    assert query["implicit_and"] is False
    assert query["pcre2"] is False
    assert query["patterns"] == ["(?:韩立)|(?:师傅)"]


def test_single_term_never_triggers_implicit_notice():
    query = _compile_search_query("墨大夫", "auto")
    assert query["implicit_and"] is False
    assert query["implicit_notice"] is None
    assert query["patterns"] == ["墨大夫"]


def test_max_total_truncates_streaming_rga_output(tmp_path: Path):
    _rga_or_skip()
    corpus = tmp_path / "many.txt"
    corpus.write_text("\n".join(f"line {i}: hit token" for i in range(2000)), encoding="utf-8")

    raw = search_directory(
        str(tmp_path),
        "token",
        pattern_mode="auto",
        before_context=0,
        after_context=0,
        max_total=25,
        timeout=30,
    )
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["truncated"] is True
    real = [m for m in payload["matches"] if not m.get("is_context")]
    assert len(real) == 25


def test_max_total_metadata_computed_after_truncation(tmp_path: Path):
    _rga_or_skip()
    corpus = tmp_path / "many.txt"
    corpus.write_text("\n".join(f"line {i}: hit token" for i in range(500)), encoding="utf-8")

    raw = search_directory(
        str(tmp_path),
        "token",
        pattern_mode="auto",
        before_context=0,
        after_context=0,
        max_total=10,
        timeout=30,
    )
    payload = json.loads(raw)
    assert payload["truncated"] is True
    annotate_result_inplace(payload, base_dir=str(tmp_path))
    real = [m for m in payload["matches"] if not m.get("is_context")]
    assert len(real) == 10
    for idx, match in enumerate(real, start=1):
        # occurrence_index counts among the *returned* slice, so the total
        # equals the truncated length (10), not the full 500 hits in the file.
        assert match["occurrence_index"] == idx
        assert match["total_occurrences"] == 10


def test_max_total_zero_or_none_disables_cap(tmp_path: Path):
    _rga_or_skip()
    corpus = tmp_path / "few.txt"
    corpus.write_text("\n".join(f"line {i}: token" for i in range(120)), encoding="utf-8")
    raw = search_directory(
        str(tmp_path),
        "token",
        pattern_mode="auto",
        before_context=0,
        after_context=0,
        max_total=None,
        timeout=30,
    )
    payload = json.loads(raw)
    real = [m for m in payload["matches"] if not m.get("is_context")]
    assert payload["truncated"] is False
    assert len(real) == 120


def test_strict_cap_returns_error_when_over_threshold(tmp_path: Path):
    _rga_or_skip()
    corpus = tmp_path / "flood.txt"
    corpus.write_text("\n".join(f"line {i}: needle" for i in range(300)), encoding="utf-8")
    raw = search_directory(
        str(tmp_path),
        "needle",
        pattern_mode="auto",
        before_context=0,
        after_context=0,
        max_total=50,
        strict_cap=True,
        timeout=30,
    )
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"] == "too_many_matches"
    assert payload["total_hits"] >= 300
    assert payload["matches"] == []


def test_total_files_matched_reflects_distinct_paths(tmp_path: Path):
    """P2.2 shape field: caller can tell "cap on one file" from "cap across many".

    Under ``truncated: true`` the response shape used to hide whether the
    cap fell on one giant file or spread across many small ones. The new
    ``total_files_matched`` top-level integer is that missing shape signal
    — a pure descriptive count of distinct file paths that produced at
    least one visible match. See design §7.3 / plan-2026-07-11 P2.2 for
    the rationale.
    """
    _rga_or_skip()
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    c = tmp_path / "c.txt"
    a.write_text("\n".join(f"line {i}: token" for i in range(50)), encoding="utf-8")
    b.write_text("\n".join(f"line {i}: token" for i in range(50)), encoding="utf-8")
    c.write_text("\n".join(f"line {i}: token" for i in range(50)), encoding="utf-8")

    # Uncapped: every file surfaces at least once.
    payload_full = json.loads(
        search_directory(
            str(tmp_path),
            "token",
            pattern_mode="auto",
            before_context=0,
            after_context=0,
            max_total=None,
            timeout=30,
        )
    )
    assert payload_full["truncated"] is False
    assert payload_full["total_files_matched"] == 3

    # Capped at 5: could truncate before every file is reached — the exact
    # value depends on rga's visit order, but must never exceed the number
    # of distinct paths present in ``matches``.
    payload_capped = json.loads(
        search_directory(
            str(tmp_path),
            "token",
            pattern_mode="auto",
            before_context=0,
            after_context=0,
            max_total=5,
            timeout=30,
        )
    )
    assert payload_capped["truncated"] is True
    distinct_in_slice = len({m["path"] for m in payload_capped["matches"] if m.get("path")})
    assert payload_capped["total_files_matched"] == distinct_in_slice
    assert 1 <= payload_capped["total_files_matched"] <= 3


def test_total_files_matched_zero_on_empty_matches(tmp_path: Path):
    """Boundary: no matches ⇒ ``total_files_matched == 0`` (not missing)."""
    _rga_or_skip()
    (tmp_path / "empty.txt").write_text("nothing relevant here\n", encoding="utf-8")
    payload = json.loads(
        search_directory(
            str(tmp_path),
            "no-such-token",
            pattern_mode="auto",
            before_context=0,
            after_context=0,
            max_total=None,
            timeout=30,
        )
    )
    assert payload["ok"] is True
    assert payload["truncated"] is False
    # Missing vs 0 matters — callers should not have to guess.
    assert payload["total_files_matched"] == 0
