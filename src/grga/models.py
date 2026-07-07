"""v2 data models: Hit and Context.

These dataclasses are additive: they do not replace any v1 dict-based
structures. `Hit.from_raw_match` bridges the raw `search_directory` /
`deep_search_directory` output dict to a strongly-typed record so callers
(agents, `expand`, `related`) can rely on a stable schema.

See ``docs/design/v2-design.md`` sections 6.1 and 7.2 for the contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, List, Mapping, Optional, Tuple


@dataclass
class Hit:
    """A single ripgrep-all hit enriched with v2 metadata.

    Static positional fields come from Layer 0 (ripgrep-all) directly.
    Layer 2 metadata (occurrence_index, line_ratio, ...) is populated by
    :mod:`grga.meta`. Structure metadata (heading_path, chapter_*) is
    populated best-effort from the Markdown sidecar and stays ``None``
    for files without a structure index.
    """

    path: str
    line_number: int
    text: str
    char_span: Tuple[int, int] = (0, 0)

    # Layer 2 - position metadata
    occurrence_index: int = 0
    total_occurrences: int = 0
    line_ratio: float = 0.0
    nearby_hits: int = 0

    # Layer 1 - structure metadata (best-effort, may be None)
    heading_path: Optional[List[str]] = None
    chapter_id: Optional[str] = None
    chapter_range: Optional[Tuple[int, int]] = None

    # Provenance
    matched_terms: List[str] = field(default_factory=list)
    is_llm_generated: bool = False

    # Bookkeeping fields carried through from v1 raw matches for
    # backward-compat rendering. Kept optional to avoid coupling.
    is_context: bool = False
    absolute_offset: Optional[int] = None
    submatches: List[Mapping[str, Any]] = field(default_factory=list)
    from_query: Optional[str] = None
    from_tier: Optional[int] = None

    @classmethod
    def from_raw_match(
        cls,
        raw: Mapping[str, Any],
        *,
        matched_terms: Optional[Iterable[str]] = None,
    ) -> "Hit":
        """Build a Hit from a raw v1 match dict (as produced by ``search_directory``)."""
        submatches = list(raw.get("submatches") or [])
        first = submatches[0] if submatches else None
        if first and isinstance(first.get("start"), int) and isinstance(first.get("end"), int):
            char_span: Tuple[int, int] = (int(first["start"]), int(first["end"]))
        else:
            char_span = (0, 0)
        return cls(
            path=str(raw.get("path", "")),
            line_number=int(raw.get("line_number") or 0),
            text=str(raw.get("text", "")),
            char_span=char_span,
            is_context=bool(raw.get("is_context", False)),
            absolute_offset=raw.get("absolute_offset"),
            submatches=submatches,
            matched_terms=list(matched_terms or []),
            from_query=raw.get("from_query"),
            from_tier=raw.get("from_tier"),
        )

    def to_dict(self) -> dict:
        """Serialize to a plain dict (JSON friendly)."""
        d = asdict(self)
        d["char_span"] = list(self.char_span)
        if self.chapter_range is not None:
            d["chapter_range"] = list(self.chapter_range)
        return d


@dataclass
class Context:
    """The expanded context surrounding a Hit.

    ``level`` is the level actually achieved after any degrade path (e.g.
    a ``chapter`` request on a non-Markdown file returns ``level="paragraph"``
    or ``"lines"``). Callers should always read ``level`` back and not
    assume the requested one was honoured.
    """

    hit: Hit
    text: str
    level: str
    range: Tuple[int, int]
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "hit": self.hit.to_dict(),
            "text": self.text,
            "level": self.level,
            "range": list(self.range),
            "truncated": self.truncated,
        }
