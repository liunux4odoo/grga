"""L1 rule-based expansion (Phase 3.1 + 3.3).

Implements ``expand(hit, level, size=3, base_dir=None) -> Context`` for the
four levels defined in ``docs/design/v2-design.md`` §4.1 (``lines``,
``paragraph``, ``chapter``, ``document``) plus the degrade chain
``chapter -> paragraph -> lines(+/-3) -> lines(+/-0)``.

The module is pure Python (no external process, no LLM); it only reads the
target file and, for ``chapter``, consults the structure sidecar built in
Phase 2.3.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from grga.file_read import read_file_lines
from grga.models import Context, Hit

DEFAULT_LINES_SIZE = 3
_MAX_DOCUMENT_BYTES = 1_000_000  # 1MB safety cap for level="document"


def expand(
    hit: Hit,
    level: str = "lines",
    size: int = DEFAULT_LINES_SIZE,
    *,
    base_dir: Optional[str | Path] = None,
) -> Context:
    """Expand a hit into a :class:`Context` at the requested level.

    The returned ``Context.level`` may differ from ``level`` when the degrade
    chain kicks in (design §4.1). The mapping is:

    * ``chapter`` on a non-Markdown or unparsable file -> ``paragraph``.
    * ``paragraph`` with fewer than 2 non-empty lines -> ``lines`` (+/- size).
    * ``lines`` with ``size == 0`` -> the hit line only.
    """
    normalized = _normalize_level(level)
    resolved_path = _resolve_path(hit.path, base_dir)
    lines = _read_lines(resolved_path)
    if not lines:
        return Context(hit=hit, text=hit.text, level="lines", range=(hit.line_number, hit.line_number), truncated=False)

    if normalized == "document":
        return _expand_document(hit, lines)
    if normalized == "chapter":
        ctx = _try_expand_chapter(hit, resolved_path, lines)
        if ctx is not None:
            return ctx
        normalized = "paragraph"  # degrade
    if normalized == "paragraph":
        ctx = _try_expand_paragraph(hit, lines)
        if ctx is not None:
            return ctx
        normalized = "lines"  # degrade
    return _expand_lines(hit, lines, size)


def _normalize_level(level: str) -> str:
    if level not in ("lines", "paragraph", "chapter", "document"):
        raise ValueError(f"unknown expand level: {level!r}")
    return level


def _resolve_path(path: str, base_dir: Optional[str | Path]) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() and base_dir is not None:
        return Path(base_dir) / path
    return candidate


def _read_lines(path: Path) -> List[str]:
    return read_file_lines(path)


def _expand_lines(hit: Hit, lines: List[str], size: int) -> Context:
    total = len(lines)
    line_idx = max(1, min(hit.line_number, total)) if total else max(1, hit.line_number)
    if size <= 0:
        start = end = min(line_idx, total) if total else line_idx
        text = lines[start - 1] if 1 <= start <= total else hit.text
        return Context(hit=hit, text=text, level="lines", range=(start, end), truncated=False)
    start = max(1, line_idx - size)
    end = min(total, line_idx + size)
    text = "\n".join(lines[start - 1 : end])
    return Context(hit=hit, text=text, level="lines", range=(start, end), truncated=False)


def _try_expand_paragraph(hit: Hit, lines: List[str]) -> Optional[Context]:
    line_idx = hit.line_number
    if line_idx < 1 or line_idx > len(lines):
        return None
    # Walk backwards to previous blank
    start = line_idx
    while start > 1 and lines[start - 2].strip():
        start -= 1
    # Walk forwards to next blank
    end = line_idx
    while end < len(lines) and lines[end].strip():
        end += 1
    # Fewer than 2 non-empty lines: not really a paragraph, degrade
    non_empty = sum(1 for i in range(start - 1, end) if lines[i].strip())
    if non_empty < 2:
        return None
    text = "\n".join(lines[start - 1 : end])
    return Context(hit=hit, text=text, level="paragraph", range=(start, end), truncated=False)


def _try_expand_chapter(hit: Hit, resolved_path: Path, lines: List[str]) -> Optional[Context]:
    from grga.structure.md import parse_markdown  # imported lazily to keep cold path light
    from grga.structure.sidecar import load_or_build

    suffix = resolved_path.suffix.lower()
    if suffix not in {".md", ".markdown", ".mdown", ".mkd"}:
        return None
    try:
        doc, _ = load_or_build(resolved_path)
    except Exception:
        doc = parse_markdown(resolved_path)
    if doc.error is not None or not doc.chapters:
        return None
    chapter = doc.chapter_for_line(hit.line_number)
    if chapter is None:
        return None
    start = max(1, chapter.start_line)
    end = min(len(lines), chapter.end_line)
    text = "\n".join(lines[start - 1 : end])
    return Context(hit=hit, text=text, level="chapter", range=(start, end), truncated=False)


def _expand_document(hit: Hit, lines: List[str]) -> Context:
    text = "\n".join(lines)
    truncated = False
    if len(text.encode("utf-8", errors="replace")) > _MAX_DOCUMENT_BYTES:
        # Truncate around the hit line to stay within cap while keeping context.
        line_idx = max(1, min(hit.line_number, len(lines)))
        window = 500
        start = max(1, line_idx - window)
        end = min(len(lines), line_idx + window)
        text = "\n".join(lines[start - 1 : end])
        truncated = True
        return Context(hit=hit, text=text, level="document", range=(start, end), truncated=True)
    return Context(hit=hit, text=text, level="document", range=(1, len(lines)), truncated=truncated)
