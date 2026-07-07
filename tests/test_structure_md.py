"""Tests for :mod:`grga.structure.md` (Phase 2.2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.structure.md import (
    StructureDoc,
    _build_chapters,
    _extract_headings_fallback,
    parse_markdown,
)


def test_parse_install_fixture(docs_fixture: Path):
    doc = parse_markdown(docs_fixture / "install.md")
    assert doc.error is None
    heads = [c.heading_path for c in doc.chapters]
    assert ["安装指南"] in heads
    assert ["安装指南", "macOS"] in heads
    assert ["安装指南", "Linux", "Ubuntu"] in heads


def test_code_block_disguise_ignored(md_eval_fixture: Path):
    doc = parse_markdown(md_eval_fixture / "a_code_block_disguise.md")
    assert doc.error is None
    assert len(doc.chapters) == 3
    texts = [c.heading_path[-1] for c in doc.chapters]
    assert texts == ["Real Heading", "Second Real Heading", "Third Real Heading"]
    for c in doc.chapters:
        assert c.start_line in (1, 12, 19)


def test_deep_nesting_heading_paths(md_eval_fixture: Path):
    doc = parse_markdown(md_eval_fixture / "b_deep_nesting.md")
    paths = [c.heading_path for c in doc.chapters]
    assert paths[0] == ["Level 1"]
    assert paths[1] == ["Level 1", "Level 2"]
    assert paths[3] == ["Level 1", "Level 2", "Level 3", "Level 4"]
    assert paths[-1] == ["Level 1", "Back to Level 2"]


def test_inline_code_in_heading(md_eval_fixture: Path):
    doc = parse_markdown(md_eval_fixture / "c_inline_code_in_heading.md")
    assert doc.chapters[0].heading_path == ["Heading with inline code"]
    assert doc.chapters[1].heading_path == ["Heading with inline code", "Another code in heading"]


def test_no_heading_returns_empty_chapters(md_eval_fixture: Path):
    doc = parse_markdown(md_eval_fixture / "d_no_heading.md")
    assert doc.chapters == []
    assert doc.paragraphs, "should still produce paragraph splits"


def test_bom_crlf_handled(md_eval_fixture: Path):
    doc = parse_markdown(md_eval_fixture / "e_bom_crlf.md")
    paths = [c.heading_path for c in doc.chapters]
    assert paths[0] == ["BOM Heading"]
    assert paths[1] == ["BOM Heading", "Second"]


def test_missing_file_returns_error_doc(tmp_path: Path):
    doc = parse_markdown(tmp_path / "does_not_exist.md")
    assert doc.error and "stat" in doc.error
    assert doc.chapters == []


def test_chapter_end_line_stops_before_next_same_or_higher(md_eval_fixture: Path):
    doc = parse_markdown(md_eval_fixture / "a_code_block_disguise.md")
    ch0, ch1, ch2 = doc.chapters
    assert ch0.end_line >= ch0.start_line
    assert ch1.start_line > ch0.start_line
    # Chapter 1 (h1) covers everything up to end of file
    assert ch0.end_line == doc.total_lines
    # Chapter 2 (h2) ends where chapter 3 (h2) starts
    assert ch1.end_line == ch2.start_line - 1


def test_chapter_for_line_returns_innermost():
    doc = StructureDoc(
        path="x.md",
        mtime=0.0,
        total_lines=20,
        chapters=[],
    )
    from grga.structure.md import Chapter

    doc.chapters = [
        Chapter(heading_path=["Top"], level=1, start_line=1, end_line=20, chapter_id="top"),
        Chapter(heading_path=["Top", "Sub"], level=2, start_line=5, end_line=15, chapter_id="top/sub"),
        Chapter(heading_path=["Top", "Sub", "Deep"], level=3, start_line=8, end_line=12, chapter_id="top/sub/deep"),
    ]
    assert doc.chapter_for_line(2).chapter_id == "top"
    assert doc.chapter_for_line(6).chapter_id == "top/sub"
    assert doc.chapter_for_line(10).chapter_id == "top/sub/deep"
    assert doc.chapter_for_line(100) is None


def test_serialization_roundtrip(md_eval_fixture: Path):
    doc = parse_markdown(md_eval_fixture / "b_deep_nesting.md")
    payload = doc.to_dict()
    reloaded = StructureDoc.from_dict(payload)
    assert reloaded.total_lines == doc.total_lines
    assert [c.heading_path for c in reloaded.chapters] == [c.heading_path for c in doc.chapters]
    assert reloaded.chapters[0].chapter_id == doc.chapters[0].chapter_id


def test_regex_fallback_ignores_fenced_code():
    text = """# Real

```python
# not heading
```

## Real2
"""
    heads = _extract_headings_fallback(text)
    assert [(h[0], h[1]) for h in heads] == [(1, "Real"), (2, "Real2")]


def test_build_chapters_empty():
    assert _build_chapters([], total_lines=10) == []


def test_paragraphs_contain_hits(docs_fixture: Path):
    doc = parse_markdown(docs_fixture / "install.md")
    # heading 'macOS' should be in some paragraph range
    heading_lines = [c.start_line for c in doc.chapters]
    for line in heading_lines:
        assert doc.paragraph_for_line(line) is not None
