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


def test_expand_lines_out_of_range_line_clamps_not_inverts(docs_fixture: Path):
    # Regression: line number beyond EOF must clamp, not produce inverted range.
    hit = _hit("install.md", 9999)
    ctx = expand(hit, level="lines", size=3, base_dir=docs_fixture)
    assert ctx.level == "lines"
    assert ctx.range[0] <= ctx.range[1]
    assert ctx.text  # non-empty, tail of file
    assert ctx.range[1] <= 31


def test_expand_chapter_out_of_range_degrades_without_inversion(docs_fixture: Path, sidecar_env):
    hit = _hit("install.md", 9999)
    ctx = expand(hit, level="chapter", size=3, base_dir=docs_fixture)
    assert ctx.range[0] <= ctx.range[1]


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


# --- toc ---------------------------------------------------------------------


def test_expand_toc_returns_full_outline(docs_fixture: Path, sidecar_env):
    # install.md has H1/H2/H3 headings; a hit inside "## macOS" should be
    # the centre, but every heading must still appear when the outline fits.
    hit = _hit("install.md", 7)
    ctx = expand(hit, level="toc", base_dir=docs_fixture)
    assert ctx.level == "toc"
    assert ctx.truncated is False
    # One line per heading; install.md has 7 headings (1 H1 + 3 H2 + 2 H3
    # + trailing "## Windows").
    rendered = ctx.text.splitlines()
    heading_titles = ["安装指南", "macOS", "Linux", "Ubuntu", "Fedora", "Windows"]
    for title in heading_titles:
        assert any(title in row for row in rendered), f"missing heading {title!r} in TOC"
    # Range must cover from first heading's start_line to last heading's end_line.
    assert ctx.range[0] == 1
    assert ctx.range[1] >= 20


def test_expand_toc_marks_hit_chapter(docs_fixture: Path, sidecar_env):
    hit = _hit("install.md", 7)  # inside "## macOS"
    ctx = expand(hit, level="toc", base_dir=docs_fixture)
    rendered = ctx.text.splitlines()
    marked = [row for row in rendered if row.startswith("> ")]
    assert len(marked) == 1, f"exactly one heading should be marked, got: {marked}"
    assert "macOS" in marked[0]


def test_expand_toc_indentation_reflects_nesting(docs_fixture: Path, sidecar_env):
    hit = _hit("install.md", 1)
    ctx = expand(hit, level="toc", base_dir=docs_fixture)
    rendered = ctx.text.splitlines()
    # H1 line ("# 安装指南") should have no depth padding after the marker.
    h1_rows = [r for r in rendered if "安装指南" in r]
    assert h1_rows, "H1 heading missing from TOC"
    # After stripping the 2-char marker, an H1 row starts with '# ' directly.
    assert h1_rows[0][2:].startswith("# "), f"H1 unexpectedly indented: {h1_rows[0]!r}"
    # H3 line ("### Ubuntu") should have 4 spaces of depth padding before ###.
    h3_rows = [r for r in rendered if "Ubuntu" in r]
    assert h3_rows
    assert h3_rows[0][2:].startswith("    ### "), f"H3 wrong indent: {h3_rows[0]!r}"


def test_expand_toc_line_range_suffix(docs_fixture: Path, sidecar_env):
    hit = _hit("install.md", 1)
    ctx = expand(hit, level="toc", base_dir=docs_fixture)
    # Every heading row should end with a `[start-end]` line range suffix.
    for row in ctx.text.splitlines():
        assert row.rstrip().endswith("]"), f"row missing range suffix: {row!r}"
        assert "[" in row and "-" in row.rsplit("[", 1)[1]


def test_expand_toc_hit_before_first_heading(md_eval_fixture: Path, sidecar_env, tmp_path: Path):
    # Craft a file whose first heading is not on line 1 so we exercise the
    # "hit falls outside every chapter -> nearest chapter" branch.
    doc = tmp_path / "preamble.md"
    doc.write_text(
        "This is a preamble paragraph.\n\nAnother preamble line.\n\n# Chapter A\n\nA body.\n",
        encoding="utf-8",
    )
    hit = _hit(str(doc), 1)  # inside the preamble, before any heading
    ctx = expand(hit, level="toc")
    assert ctx.level == "toc"
    # The single "# Chapter A" heading must still be marked as the centre
    # (nearest by line distance).
    assert "> " in ctx.text
    assert "Chapter A" in ctx.text


def test_expand_toc_degrades_when_no_heading_tree(docs_fixture: Path, sidecar_env):
    hit = _hit("code_sample.py", 4)
    ctx = expand(hit, level="toc", base_dir=docs_fixture)
    # No parseable Markdown headings on a .py file -> fall through the chain.
    assert ctx.level in ("chapter", "paragraph", "lines")


def test_expand_toc_degrades_on_md_without_headings(md_eval_fixture: Path, sidecar_env):
    hit = _hit("d_no_heading.md", 1)
    ctx = expand(hit, level="toc", base_dir=md_eval_fixture)
    assert ctx.level in ("chapter", "paragraph", "lines")


def test_expand_toc_truncates_when_over_cap(monkeypatch, tmp_path: Path, sidecar_env):
    # Force a tiny cap so a modest outline exceeds it and the truncation
    # branch runs. Reuse the real chapter structure sidecar path.
    monkeypatch.setattr("grga.expand._MAX_TOC_BYTES", 80)
    doc = tmp_path / "many-headings.md"
    body = "\n".join(f"## Section {i}\n\nBody {i}.\n" for i in range(30))
    doc.write_text("# Root\n\n" + body, encoding="utf-8")
    hit = _hit(str(doc), 40)  # somewhere in the middle
    ctx = expand(hit, level="toc")
    assert ctx.level == "toc"
    assert ctx.truncated is True
    # Truncated outline must still contain the centre marker.
    assert "> " in ctx.text
