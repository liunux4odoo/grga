"""L1 rule-based expansion (Phase 3.1 + 3.3).

Implements ``expand(hit, level, size=3, base_dir=None) -> Context`` for the
five levels defined in ``docs/design/v2-design.md`` §4.1 (``lines``,
``paragraph``, ``chapter``, ``document``) plus ``toc`` (single-document
outline). Degrade chain for structured levels:
``chapter -> paragraph -> lines(+/-3) -> lines(+/-0)``; ``toc`` degrades
to ``chapter`` when the file has no parseable heading tree.

The module is pure Python (no external process, no LLM); it only reads the
target file and, for ``chapter`` / ``toc``, consults the structure sidecar
built in Phase 2.3.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from grga.file_read import read_file_lines
from grga.models import Context, Hit

DEFAULT_LINES_SIZE = 3
_MAX_DOCUMENT_BYTES = 1_000_000  # 1MB safety cap for level="document"
# Reuse the same order of magnitude for ``toc`` rendering. A 1MB outline
# is already absurd (~tens of thousands of headings); the cap only exists
# to keep pathological corpora from producing unbounded output. The
# truncation strategy mirrors ``document``: centre the window on the
# hit's chapter and shrink symmetrically until the rendered text fits.
_MAX_TOC_BYTES = _MAX_DOCUMENT_BYTES


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

    * ``toc`` on a file with no parseable heading tree -> ``chapter``
      (and onward through the chapter degrade chain).
    * ``chapter`` on a non-Markdown or unparsable file -> ``paragraph``.
    * ``paragraph`` with fewer than 2 non-empty lines -> ``lines`` (+/- size).
    * ``lines`` with ``size == 0`` -> the hit line only.

    ``toc`` returns the document outline (one line per heading) instead of
    body text. Like ``document`` it returns the *whole* outline by default
    and only truncates — centred on the hit's chapter — when the rendered
    text would exceed the safety cap; a non-truncated ``toc`` is the full
    document outline.
    """
    normalized = _normalize_level(level)
    resolved_path = _resolve_path(hit.path, base_dir)
    lines = _read_lines(resolved_path)
    if not lines:
        return Context(hit=hit, text=hit.text, level="lines", range=(hit.line_number, hit.line_number), truncated=False)

    if normalized == "document":
        return _expand_document(hit, lines)
    if normalized == "toc":
        ctx = _try_expand_toc(hit, resolved_path, lines)
        if ctx is not None:
            return ctx
        normalized = "chapter"  # degrade: no heading tree -> fall through
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
    if level not in ("lines", "paragraph", "chapter", "document", "toc"):
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
    chapters = _load_structure_chapters(resolved_path)
    if not chapters:
        return None
    # Innermost chapter covering the hit line — same rule as
    # :meth:`StructureDoc.chapter_for_line`, inlined so we don't reload
    # the doc after the shared loader already vetted the chapter list.
    covering = None
    for ch in chapters:
        if ch.start_line <= hit.line_number <= ch.end_line:
            if covering is None or ch.level > covering.level:
                covering = ch
    if covering is None:
        return None
    start = max(1, covering.start_line)
    end = min(len(lines), covering.end_line)
    text = "\n".join(lines[start - 1 : end])
    return Context(hit=hit, text=text, level="chapter", range=(start, end), truncated=False)


def _load_structure_chapters(resolved_path: Path):
    """Return the chapter list for a Markdown file, or ``None``.

    Shared by ``chapter`` and ``toc``. Uses the sidecar cache
    (``structure/sidecar.py``) so a file already indexed by ``search`` /
    ``expand --level chapter`` is essentially free to re-read; falls back
    to a direct parse when the sidecar cannot be built. Non-Markdown
    suffixes and parse errors return ``None`` so the caller can degrade.
    """
    from grga.structure.md import parse_markdown  # lazy: keep cold path light
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
    return doc.chapters


def _render_toc_lines(chapters, center_id: Optional[str]) -> List[str]:
    """Render chapters as an indented outline, marking the centre chapter.

    One physical line per heading:

        ``> ## macOS  [5-10]``

    * ``> `` prefix marks the chapter containing the hit (``center_id``);
      other rows are indented two spaces to keep columns aligned.
    * Indentation reflects nesting depth (``level - 1`` levels of two
      spaces), so the outline reads as a tree.
    * The trailing ``[start-end]`` is the chapter's inclusive line range,
      matching the ``range`` semantics of the other expand levels.
    """
    rendered: List[str] = []
    for ch in chapters:
        title = ch.heading_path[-1] if ch.heading_path else ""
        hashes = "#" * ch.level
        depth_pad = "  " * max(0, ch.level - 1)
        marker = "> " if ch.chapter_id == center_id else "  "
        rendered.append(
            f"{marker}{depth_pad}{hashes} {title}  [{ch.start_line}-{ch.end_line}]"
        )
    return rendered


def _try_expand_toc(hit: Hit, resolved_path: Path, lines: List[str]) -> Optional[Context]:
    """Expand into a single-document table of contents (outline).

    Returns ``None`` (so the caller degrades to ``chapter``) when the file
    has no parseable heading tree. Otherwise returns a ``toc`` context:

    * ``text`` is one rendered line per heading (see :func:`_render_toc_lines`).
    * The chapter containing the hit line is the **centre** and is marked
      with ``> ``. When the hit falls outside every chapter (e.g. a preamble
      before the first heading), the nearest chapter by line number is used.
    * Like ``document``, the full outline is returned by default. Only when
      the rendered text exceeds :data:`_MAX_TOC_BYTES` is it truncated —
      symmetrically around the centre chapter — and ``truncated`` set to
      ``True``. A non-truncated ``toc`` therefore equals the whole-document
      outline.
    * ``range`` spans the first-to-last returned chapter (``start_line`` of
      the first row to ``end_line`` of the last), i.e. the slice of the
      document the outline covers.
    """
    chapters = _load_structure_chapters(resolved_path)
    if not chapters:
        return None

    center = _center_chapter(chapters, hit.line_number)
    center_id = center.chapter_id if center is not None else None

    def _build(subset) -> Context:
        text = "\n".join(_render_toc_lines(subset, center_id))
        start = max(1, min(ch.start_line for ch in subset))
        end = min(len(lines), max(ch.end_line for ch in subset))
        truncated = len(subset) < len(chapters)
        return Context(hit=hit, text=text, level="toc", range=(start, end), truncated=truncated)

    full = _build(chapters)
    if len(full.text.encode("utf-8", errors="replace")) <= _MAX_TOC_BYTES:
        return full

    # Over the cap: shrink a window centred on the hit's chapter until the
    # rendered outline fits. Mirrors the document-level "centre on the hit,
    # then cap" behaviour rather than a caller-facing entry limit.
    center_idx = next(
        (i for i, ch in enumerate(chapters) if ch.chapter_id == center_id), 0
    )
    window = len(chapters)
    while window > 1:
        window -= 1
        half = window // 2
        lo = max(0, center_idx - half)
        hi = min(len(chapters), lo + window)
        lo = max(0, hi - window)
        subset = chapters[lo:hi]
        candidate = _build(subset)
        if len(candidate.text.encode("utf-8", errors="replace")) <= _MAX_TOC_BYTES:
            return candidate
    # Degenerate: even one heading exceeds the cap — return just the centre.
    single = _build([chapters[center_idx]])
    return single


def _center_chapter(chapters, line_number: int):
    """Pick the chapter to centre a ``toc`` window on.

    Prefers the innermost chapter covering ``line_number`` (same rule as
    :meth:`StructureDoc.chapter_for_line`). When the line falls outside
    every chapter (e.g. a preamble before the first heading, or past the
    last), returns the nearest chapter by line distance so the outline is
    still anchored somewhere sensible.
    """
    covering = None
    for ch in chapters:
        if ch.start_line <= line_number <= ch.end_line:
            if covering is None or ch.level > covering.level:
                covering = ch
    if covering is not None:
        return covering

    def _distance(ch) -> int:
        if line_number < ch.start_line:
            return ch.start_line - line_number
        if line_number > ch.end_line:
            return line_number - ch.end_line
        return 0

    return min(chapters, key=_distance)


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
