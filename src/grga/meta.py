"""Layer 2 hit metadata computation (Phase 1.2).

``annotate_hits`` turns raw v1 match dicts into :class:`grga.models.Hit`
records with position metadata filled in:

* ``occurrence_index`` / ``total_occurrences`` - the 1-based rank of this hit
  among all real (non-context) hits in the same file, and the total count.
* ``line_ratio`` - ``line_number / total_lines`` clamped to ``[0, 1]``.
* ``nearby_hits`` - number of *other* real hits within +/- ``window`` lines
  in the same file.
* ``matched_terms`` - best-effort from the raw submatches.

This module is pure (no I/O beyond the caller-supplied line counts) so it is
cheap to unit-test. Structure metadata is filled separately by
``annotate_structure`` (Phase 2.4).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

from grga.models import Hit

DEFAULT_NEARBY_WINDOW = 5


def _matched_terms_from_raw(raw: Mapping[str, Any]) -> List[str]:
    terms: List[str] = []
    seen = set()
    for sub in raw.get("submatches") or []:
        text = sub.get("match")
        if isinstance(text, str) and text and text not in seen:
            seen.add(text)
            terms.append(text)
    return terms


def annotate_hits(
    raw_hits: Iterable[Mapping[str, Any]],
    file_line_counts: Optional[Mapping[str, int]] = None,
    *,
    window: int = DEFAULT_NEARBY_WINDOW,
) -> List[Hit]:
    """Annotate raw matches with Layer 2 position metadata.

    Args:
        raw_hits: iterable of raw match dicts (``path``/``line_number``/...).
            Both real matches and context lines may be present; context lines
            are excluded from the emitted list but do not affect counting.
        file_line_counts: optional mapping ``path -> total line count`` used
            for ``line_ratio``. Missing entries yield ``line_ratio == 0.0``.
        window: +/- line window for ``nearby_hits``.

    Returns:
        A list of :class:`Hit` (real matches only) in input order.
    """
    file_line_counts = file_line_counts or {}

    raw_list = list(raw_hits)
    # Real hits only, grouped per file, tracking line numbers.
    per_file_lines: dict[str, List[int]] = {}
    for raw in raw_list:
        if raw.get("is_context"):
            continue
        path = str(raw.get("path", ""))
        line_number = int(raw.get("line_number") or 0)
        per_file_lines.setdefault(path, []).append(line_number)

    per_file_index: dict[str, int] = {}
    hits: List[Hit] = []
    for raw in raw_list:
        if raw.get("is_context"):
            continue
        path = str(raw.get("path", ""))
        line_number = int(raw.get("line_number") or 0)

        lines_in_file = per_file_lines.get(path, [])
        total_occurrences = len(lines_in_file)
        per_file_index[path] = per_file_index.get(path, 0) + 1
        occurrence_index = per_file_index[path]

        total_lines = int(file_line_counts.get(path, 0) or 0)
        if total_lines > 0:
            line_ratio = max(0.0, min(1.0, line_number / total_lines))
        else:
            line_ratio = 0.0

        nearby_hits = _count_nearby(lines_in_file, line_number, occurrence_index, window)

        hit = Hit.from_raw_match(raw, matched_terms=_matched_terms_from_raw(raw))
        hit.occurrence_index = occurrence_index
        hit.total_occurrences = total_occurrences
        hit.line_ratio = round(line_ratio, 4)
        hit.nearby_hits = nearby_hits
        hits.append(hit)

    return hits


def _count_nearby(lines_in_file: List[int], line_number: int, occurrence_index: int, window: int) -> int:
    """Count other real hits within +/- window lines, excluding this occurrence.

    ``occurrence_index`` is 1-based; the current hit occupies position
    ``occurrence_index - 1`` in ``lines_in_file`` (input order preserved).
    """
    count = 0
    self_pos = occurrence_index - 1
    for idx, other_line in enumerate(lines_in_file):
        if idx == self_pos:
            continue
        if abs(other_line - line_number) <= window:
            count += 1
    return count


def count_file_lines(paths: Iterable[str], base_dir: Optional[str] = None) -> dict:
    """Count total lines for each path (best-effort).

    Paths that fail to read are simply omitted, so ``line_ratio`` will fall
    back to ``0.0`` for them. This keeps annotation non-fatal for binary or
    preprocessed sources.
    """
    base = Path(base_dir) if base_dir else None
    counts: dict[str, int] = {}
    for path in set(paths):
        if not path:
            continue
        candidate = Path(path)
        if not candidate.is_absolute() and base is not None:
            candidate = base / path
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                counts[path] = sum(1 for _ in fh)
        except (OSError, ValueError):
            continue
    return counts


def annotate_result_inplace(
    result: Mapping[str, Any],
    *,
    base_dir: Optional[str] = None,
    window: int = DEFAULT_NEARBY_WINDOW,
) -> Mapping[str, Any]:
    """Enrich a v1 search result dict's ``matches`` with Layer 2 metadata.

    This mutates the real (non-context) match dicts in place by adding the
    keys ``occurrence_index / total_occurrences / line_ratio / nearby_hits /
    matched_terms``. Context lines are left untouched. The result dict is
    returned for convenience. Safe to call on failed results (no matches).
    """
    if not result.get("ok"):
        return result
    matches = result.get("matches") or []
    real_paths = [str(m.get("path", "")) for m in matches if not m.get("is_context")]
    base = base_dir or result.get("directory")
    line_counts = count_file_lines(real_paths, base_dir=base)

    hits = annotate_hits(matches, file_line_counts=line_counts, window=window)
    hit_iter = iter(hits)
    for match in matches:
        if match.get("is_context"):
            continue
        hit = next(hit_iter, None)
        if hit is None:
            break
        match["occurrence_index"] = hit.occurrence_index
        match["total_occurrences"] = hit.total_occurrences
        match["line_ratio"] = hit.line_ratio
        match["nearby_hits"] = hit.nearby_hits
        match["matched_terms"] = hit.matched_terms
    return result


_MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}


def annotate_structure_inplace(
    result: Mapping[str, Any],
    *,
    base_dir: Optional[str] = None,
) -> Mapping[str, Any]:
    """Back-fill Layer 1 structure metadata onto Markdown hits (Phase 2.4).

    For each real hit whose file is Markdown, look up the enclosing chapter via
    the structure sidecar and set ``heading_path / chapter_id / chapter_range``.
    Non-Markdown hits (and Markdown files that fail to parse) are left with the
    fields absent, so callers keep the ``None`` default. Best-effort and never
    fatal.
    """
    if not result.get("ok"):
        return result
    matches = result.get("matches") or []
    base = base_dir or result.get("directory")
    base_path = Path(base) if base else None

    # Cache structure docs per file to avoid re-parsing for multiple hits.
    doc_cache: dict[str, Any] = {}
    for match in matches:
        if match.get("is_context"):
            continue
        path = str(match.get("path", ""))
        if not path:
            continue
        suffix = Path(path).suffix.lower()
        if suffix not in _MARKDOWN_SUFFIXES:
            continue
        resolved = Path(path)
        if not resolved.is_absolute() and base_path is not None:
            resolved = base_path / path
        key = str(resolved)
        if key not in doc_cache:
            doc_cache[key] = _load_structure_doc(resolved)
        doc = doc_cache[key]
        if doc is None:
            continue
        line_number = int(match.get("line_number") or 0)
        chapter = doc.chapter_for_line(line_number)
        if chapter is None:
            continue
        match["heading_path"] = list(chapter.heading_path)
        match["chapter_id"] = chapter.chapter_id
        match["chapter_range"] = [chapter.start_line, chapter.end_line]
    return result


def _load_structure_doc(path: Path):
    try:
        from grga.structure.sidecar import load_or_build

        doc, _ = load_or_build(path)
        if doc.error is not None:
            return None
        return doc
    except Exception:
        return None
