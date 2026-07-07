"""Markdown structure extractor (Phase 2.2).

``parse_markdown(path) -> StructureDoc`` returns a serialisable chapter tree
using ``markdown-it-py`` (see ``docs/adr/0001-md-parser.md``). Failures are
tolerated: unreadable / non-markdown files return an empty StructureDoc so
callers can degrade gracefully (design §4.1).

The extracted schema is intentionally close to the sidecar JSON layout
described in ``docs/design/v2-design.md`` §6.3.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SLUG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


@dataclass
class Chapter:
    heading_path: List[str]
    level: int
    start_line: int
    end_line: int
    chapter_id: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Paragraph:
    start_line: int
    end_line: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StructureDoc:
    path: str
    mtime: float
    total_lines: int
    chapters: List[Chapter] = field(default_factory=list)
    paragraphs: List[Paragraph] = field(default_factory=list)
    parser: str = "markdown-it-py"
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "mtime": self.mtime,
            "total_lines": self.total_lines,
            "parser": self.parser,
            "chapters": [c.to_dict() for c in self.chapters],
            "paragraphs": [p.to_dict() for p in self.paragraphs],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "StructureDoc":
        return cls(
            path=str(payload.get("path", "")),
            mtime=float(payload.get("mtime", 0.0)),
            total_lines=int(payload.get("total_lines", 0)),
            parser=str(payload.get("parser", "markdown-it-py")),
            error=payload.get("error"),
            chapters=[Chapter(**c) for c in payload.get("chapters", [])],
            paragraphs=[Paragraph(**p) for p in payload.get("paragraphs", [])],
        )

    def chapter_for_line(self, line_number: int) -> Optional[Chapter]:
        """Return the innermost (highest level number) chapter covering ``line_number``."""
        best: Optional[Chapter] = None
        for chapter in self.chapters:
            if chapter.start_line <= line_number <= chapter.end_line:
                if best is None or chapter.level > best.level:
                    best = chapter
        return best

    def paragraph_for_line(self, line_number: int) -> Optional[Paragraph]:
        for p in self.paragraphs:
            if p.start_line <= line_number <= p.end_line:
                return p
        return None


def _slugify(text: str) -> str:
    cleaned = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return cleaned or "section"


def _extract_headings(md_text: str) -> List[Tuple[int, str, int]]:
    """Return [(level, text, line_number), ...] using markdown-it-py.

    ``line_number`` is 1-based. Falls back to a naive regex parse if
    ``markdown-it-py`` is unavailable, which still correctly ignores fenced
    code blocks (leading indent lines and lines between ``` fences).
    """
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return _extract_headings_fallback(md_text)

    md = MarkdownIt("commonmark")
    tokens = md.parse(md_text)
    headings: List[Tuple[int, str, int]] = []
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open" or not tok.map:
            continue
        inline = tokens[i + 1] if i + 1 < len(tokens) else None
        text_parts: List[str] = []
        if inline and inline.type == "inline":
            for child in inline.children or []:
                if child.type in ("text", "code_inline"):
                    text_parts.append(child.content)
        text = " ".join("".join(text_parts).split()).strip()
        level = int(tok.tag[1:])
        line = tok.map[0] + 1
        headings.append((level, text, line))
    return headings


def _extract_headings_fallback(md_text: str) -> List[Tuple[int, str, int]]:
    """Regex fallback: ignore ATX-style headings inside fenced/indented code."""
    headings: List[Tuple[int, str, int]] = []
    in_fence = False
    fence_marker = ""
    for idx, raw_line in enumerate(md_text.splitlines(), start=1):
        stripped = raw_line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        if raw_line.startswith("    ") or raw_line.startswith("\t"):
            continue
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", raw_line)
        if not m:
            continue
        headings.append((len(m.group(1)), m.group(2).strip(), idx))
    return headings


def _build_chapters(
    headings: List[Tuple[int, str, int]], total_lines: int
) -> List[Chapter]:
    """Turn a flat heading list into chapters with heading_path + end_line."""
    if not headings:
        return []
    chapters: List[Chapter] = []
    stack: List[Tuple[int, str]] = []  # (level, text)
    id_counts: Dict[str, int] = {}
    for level, text, line in headings:
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))
        heading_path = [t for _, t in stack]
        slug = "/".join(_slugify(t) for t in heading_path)
        id_counts[slug] = id_counts.get(slug, 0) + 1
        chapter_id = slug if id_counts[slug] == 1 else f"{slug}-{id_counts[slug]}"
        chapters.append(
            Chapter(
                heading_path=heading_path,
                level=level,
                start_line=line,
                end_line=total_lines,
                chapter_id=chapter_id,
            )
        )

    # Compute end_line: each chapter ends before the next chapter at the same
    # or shallower level. Deeper chapters are children and don't close a parent.
    for i, chapter in enumerate(chapters):
        end_line = total_lines
        for j in range(i + 1, len(chapters)):
            candidate = chapters[j]
            if candidate.level <= chapter.level:
                end_line = candidate.start_line - 1
                break
        chapter.end_line = max(chapter.start_line, end_line)
    return chapters


def _build_paragraphs(md_text: str) -> List[Paragraph]:
    """Split by blank lines. Fenced code blocks are treated as single paragraphs."""
    paragraphs: List[Paragraph] = []
    in_fence = False
    fence_marker = ""
    start: Optional[int] = None
    for idx, raw_line in enumerate(md_text.splitlines(), start=1):
        stripped = raw_line.strip()
        head = raw_line.lstrip()[:3]
        if head in ("```", "~~~"):
            marker = head
            if not in_fence:
                if start is None:
                    start = idx
                in_fence = True
                fence_marker = marker
                continue
            if marker == fence_marker:
                in_fence = False
                fence_marker = ""
                continue
        if in_fence:
            if start is None:
                start = idx
            continue
        if not stripped:
            if start is not None:
                paragraphs.append(Paragraph(start_line=start, end_line=idx - 1))
                start = None
            continue
        if start is None:
            start = idx
    if start is not None:
        paragraphs.append(Paragraph(start_line=start, end_line=idx))
    return paragraphs


def parse_markdown(path: str | Path) -> StructureDoc:
    """Parse a Markdown file into a :class:`StructureDoc`.

    Non-existent, unreadable, or non-Markdown files return an empty
    StructureDoc with ``error`` set (degrade rule from design §4.1).
    """
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError as exc:
        return StructureDoc(path=str(path), mtime=0.0, total_lines=0, error=f"stat: {exc}")

    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return StructureDoc(path=str(path), mtime=mtime, total_lines=0, error=f"read: {exc}")

    total_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    if total_lines == 0 and text:
        total_lines = 1
    try:
        headings = _extract_headings(text)
        chapters = _build_chapters(headings, total_lines)
        paragraphs = _build_paragraphs(text)
    except Exception as exc:  # pragma: no cover - defensive
        return StructureDoc(path=str(path), mtime=mtime, total_lines=total_lines, error=f"parse: {exc}")

    return StructureDoc(
        path=str(path),
        mtime=mtime,
        total_lines=total_lines,
        chapters=chapters,
        paragraphs=paragraphs,
    )
