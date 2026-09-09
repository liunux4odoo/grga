"""Unit tests for Office-suffix routing in file_read.

Design: docs/design/plan-2026-09-09-office-adapters.md
The pandoc_office custom adapter (registered by
toolchain.ensure_rga_office_config) makes rga able to extract xlsx/pptx/
doc/xls/ppt. The graph index build enumerates files through
``indexable_suffixes()`` and routes reads through ``read_file_lines_ex``,
so those suffixes must be visible to both gates — otherwise the adapter
helps search but not ``grga index build``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.file_read import (
    RGA_BINARY_ONLY_SUFFIXES,
    RGA_SUPPORTED_SUFFIXES,
    ReadResult,
    ReadSource,
    indexable_suffixes,
    is_rga_binary_only_suffix,
    is_rga_supported_suffix,
    read_file_lines_ex,
)

OFFICE_SUFFIXES = (".xlsx", ".pptx", ".doc", ".xls", ".ppt")


# ---------------------------------------------------------------------------
# suffix sets
# ---------------------------------------------------------------------------


def test_office_suffixes_in_rga_supported():
    for ext in OFFICE_SUFFIXES:
        assert ext in RGA_SUPPORTED_SUFFIXES, ext


def test_office_suffixes_in_binary_only():
    for ext in OFFICE_SUFFIXES:
        assert ext in RGA_BINARY_ONLY_SUFFIXES, ext


def test_office_suffixes_indexable():
    indexable = indexable_suffixes()
    for ext in OFFICE_SUFFIXES:
        assert ext in indexable, ext


def test_office_suffix_routing_predicates(tmp_path: Path):
    for ext in OFFICE_SUFFIXES:
        p = tmp_path / f"file{ext}"
        assert is_rga_supported_suffix(p), ext
        assert is_rga_binary_only_suffix(p), ext


# ---------------------------------------------------------------------------
# read_file_lines_ex routing (no rga → FAILED, never native garbage)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", OFFICE_SUFFIXES)
def test_office_read_fails_cleanly_without_rga(monkeypatch, tmp_path: Path, ext):
    p = tmp_path / f"file{ext}"
    p.write_bytes(b"\x00\x01\x02")  # binary garbage: must not be tokenised
    monkeypatch.setattr("grga.file_read._find_rga", lambda: None)
    result = read_file_lines_ex(p)
    assert result.source == ReadSource.FAILED
    assert result.lines == []


def test_office_read_falls_back_after_rga_error(monkeypatch, tmp_path: Path):
    # rga exists but hard-fails → binary-only suffix must report FAILED,
    # not fall back to a native UTF-8 read of the binary bytes.
    p = tmp_path / "file.xlsx"
    p.write_bytes(b"PK\x03\x04binary-garbage")
    monkeypatch.setattr(
        "grga.file_read._find_rga",
        lambda: str(tmp_path / "rga"),
    )
    monkeypatch.setattr(
        "grga.file_read.subprocess.run",
        lambda *a, **k: type("C", (), {"returncode": 2, "stdout": "", "stderr": "boom"})(),
    )
    result = read_file_lines_ex(p)
    assert result.source == ReadSource.FAILED
    assert result.lines == []


def test_docx_remains_binary_only_after_office_addition():
    # .docx was already routed through rga; adding the legacy office
    # suffixes must not disturb the existing docx behaviour.
    assert ".docx" in RGA_SUPPORTED_SUFFIXES
    assert ".docx" in RGA_BINARY_ONLY_SUFFIXES


# ---------------------------------------------------------------------------
# ReadResult shape used by graph build (_classify_doc)
# ---------------------------------------------------------------------------


def test_failed_read_result_is_skipable_by_build():
    # _classify_doc treats ReadSource.FAILED as DOC_READ_ERROR and skips.
    result = ReadResult(lines=[], source=ReadSource.FAILED)
    assert result.source == ReadSource.FAILED
    assert result.lines == []
