"""Tests for :mod:`grga.structure.sidecar` (Phase 2.3)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from grga.structure.sidecar import default_sidecar_root, load_or_build, sidecar_path_for


@pytest.fixture
def sidecar_root(monkeypatch, tmp_path):
    root = tmp_path / "sidecars"
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(root))
    return root


def _write_md(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_default_sidecar_root_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "x"))
    assert default_sidecar_root() == tmp_path / "x"


def test_default_sidecar_root_falls_back_to_cache_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("GRGA_SIDECAR_ROOT", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert default_sidecar_root() == tmp_path / "cache" / "grga" / "structure"


def test_default_sidecar_root_sidecar_env_beats_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecar"))
    monkeypatch.setenv("GRGA_CACHE_DIR", str(tmp_path / "cache"))
    assert default_sidecar_root() == tmp_path / "sidecar"


def test_first_build_writes_sidecar(sidecar_root: Path, tmp_path: Path):
    md = tmp_path / "a.md"
    _write_md(md, "# T\n\nhi\n")
    doc, from_cache = load_or_build(md)
    assert from_cache is False
    assert doc.chapters and doc.chapters[0].heading_path == ["T"]
    sidecar = sidecar_path_for(md)
    assert sidecar.exists()


def test_second_call_uses_cache(sidecar_root: Path, tmp_path: Path):
    md = tmp_path / "a.md"
    _write_md(md, "# T\n\nhi\n")
    load_or_build(md)
    doc2, from_cache = load_or_build(md)
    assert from_cache is True
    assert doc2.chapters


def test_mtime_change_triggers_rebuild(sidecar_root: Path, tmp_path: Path):
    md = tmp_path / "a.md"
    _write_md(md, "# T\n")
    load_or_build(md)
    time.sleep(0.02)
    _write_md(md, "# T\n\n## Sub\n")
    now = time.time() + 5
    os.utime(md, (now, now))
    doc, from_cache = load_or_build(md)
    assert from_cache is False
    assert any(c.heading_path == ["T", "Sub"] for c in doc.chapters)


def test_second_call_is_fast(sidecar_root: Path, tmp_path: Path):
    md = tmp_path / "big.md"
    body = "# Title\n\n" + "\n".join(f"paragraph {i}" for i in range(200))
    _write_md(md, body)
    load_or_build(md)
    start = time.perf_counter()
    _, from_cache = load_or_build(md)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert from_cache is True
    assert elapsed_ms < 20, f"cache read took {elapsed_ms:.2f}ms"


def test_missing_file_returns_error_doc_and_no_sidecar(sidecar_root: Path, tmp_path: Path):
    missing = tmp_path / "gone.md"
    doc, from_cache = load_or_build(missing)
    assert from_cache is False
    assert doc.error
    assert not sidecar_path_for(missing).exists()


def test_corrupted_sidecar_is_recovered(sidecar_root: Path, tmp_path: Path):
    md = tmp_path / "a.md"
    _write_md(md, "# T\n")
    doc, _ = load_or_build(md)
    sidecar = sidecar_path_for(md)
    sidecar.write_text("<<not json>>", encoding="utf-8")
    doc2, from_cache = load_or_build(md)
    assert from_cache is False
    assert doc2.chapters == doc.chapters


def test_sidecar_paths_are_hashed(sidecar_root: Path, tmp_path: Path):
    md_a = tmp_path / "sub_a" / "very_long_folder_name" / "file.md"
    md_b = tmp_path / "sub_b" / "very_long_folder_name" / "file.md"
    md_a.parent.mkdir(parents=True)
    md_b.parent.mkdir(parents=True)
    md_a.write_text("# A\n", encoding="utf-8")
    md_b.write_text("# B\n", encoding="utf-8")
    load_or_build(md_a)
    load_or_build(md_b)
    assert sidecar_path_for(md_a) != sidecar_path_for(md_b)
