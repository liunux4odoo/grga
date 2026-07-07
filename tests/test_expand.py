"""Tests for :mod:`grga.expand` (Phase 3.1 + 3.3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.expand import expand
from grga.models import Hit


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))


def _hit(path: str, line: int, text: str = "") -> Hit:
    return Hit(path=path, line_number=line, text=text)


# --- lines -------------------------------------------------------------------


def test_expand_lines_default_size(docs_fixture: Path):
    hit = _hit("install.md", 5)  # "## macOS"
    ctx = expand(hit, level="lines", size=2, base_dir=docs_fixture)
    assert ctx.level == "lines"
    assert ctx.range[0] == 3
    assert ctx.range[1] == 7
    assert "macOS" in ctx.text
    assert ctx.truncated is False


def test_expand_lines_size_zero(docs_fixture: Path):
    hit = _hit("install.md", 5)
    ctx = expand(hit, level="lines", size=0, base_dir=docs_fixture)
    assert ctx.range == (5, 5)
    assert ctx.text.strip() == "## macOS"


def test_expand_lines_clamps_at_boundaries(docs_fixture: Path):
    hit = _hit("install.md", 1)
    ctx = expand(hit, level="lines", size=100, base_dir=docs_fixture)
    assert ctx.range[0] == 1
    assert ctx.range[1] <= 1000


def test_expand_missing_file_returns_hit_text():
    hit = _hit("/nowhere/x.md", 5, text="fallback")
    ctx = expand(hit, level="lines", size=3)
    assert ctx.level == "lines"
    assert ctx.text == "fallback"


# --- paragraph ---------------------------------------------------------------


def test_expand_paragraph_returns_block(docs_fixture: Path):
    hit = _hit("paragraphs.md", 6)  # middle line of the 3-line paragraph
    ctx = expand(hit, level="paragraph", base_dir=docs_fixture)
    assert ctx.level == "paragraph"
    assert ctx.range == (5, 7)
    assert "第一句话" in ctx.text
    assert "第三句话" in ctx.text


def test_expand_paragraph_single_line_degrades_to_lines(docs_fixture: Path):
    hit = _hit("install.md", 7)  # single-line paragraph
    ctx = expand(hit, level="paragraph", base_dir=docs_fixture)
    assert ctx.level == "lines"
    assert ctx.range[0] <= 7 <= ctx.range[1]


# --- chapter -----------------------------------------------------------------


def test_expand_chapter_returns_full_section(docs_fixture: Path, sidecar_env):
    # line 5 is "## macOS" heading in install.md
    hit = _hit("install.md", 5)
    ctx = expand(hit, level="chapter", base_dir=docs_fixture)
    assert ctx.level == "chapter"
    assert "brew install grga" in ctx.text
    assert "Linux" not in ctx.text  # chapter must not bleed into next section


def test_expand_chapter_hit_inside_body(docs_fixture: Path, sidecar_env):
    # line 7: 'macOS ... brew' should belong to '## macOS' chapter
    hit = _hit("install.md", 7)
    ctx = expand(hit, level="chapter", base_dir=docs_fixture)
    assert ctx.level == "chapter"
    assert "## macOS" in ctx.text
    assert "brew install" in ctx.text


def test_expand_chapter_degrades_on_non_markdown(docs_fixture: Path, sidecar_env):
    hit = _hit("code_sample.py", 4)  # def hello_world():...
    ctx = expand(hit, level="chapter", base_dir=docs_fixture)
    assert ctx.level in ("paragraph", "lines")
    # Original hit line must be inside the range
    assert ctx.range[0] <= 4 <= ctx.range[1]


def test_expand_chapter_on_md_without_headings(md_eval_fixture: Path, sidecar_env):
    hit = _hit("d_no_heading.md", 1)
    ctx = expand(hit, level="chapter", base_dir=md_eval_fixture)
    # No chapters -> should degrade to paragraph or lines
    assert ctx.level in ("paragraph", "lines")


# --- document ----------------------------------------------------------------


def test_expand_document_returns_full_file(docs_fixture: Path):
    hit = _hit("install.md", 1)
    ctx = expand(hit, level="document", base_dir=docs_fixture)
    assert ctx.level == "document"
    assert ctx.range[0] == 1
    assert ctx.range[1] >= 20
    assert "Windows" in ctx.text


def test_expand_document_truncates_large_file(tmp_path: Path):
    big = tmp_path / "big.txt"
    huge_line = "x" * 2000
    big.write_text("\n".join([huge_line] * 800), encoding="utf-8")
    hit = _hit(str(big), 400, text=huge_line)
    ctx = expand(hit, level="document")
    assert ctx.level == "document"
    assert ctx.truncated is True
    assert ctx.range[0] <= 400 <= ctx.range[1]


# --- validation --------------------------------------------------------------


def test_unknown_level_raises():
    with pytest.raises(ValueError):
        expand(_hit("a.md", 1), level="entire_universe")
