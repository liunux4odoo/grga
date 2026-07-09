"""Offline graph index builder (Phase 4.2).

``build_graph_index`` walks a directory scope, extracts keyword occurrences
from every text file, and fills the v2 ``keyword_docs`` / ``heading_docs``
tables (see :mod:`grga.graph.schema`).

Design constraints (from ``docs/design/v2-plan.md`` Phase 4.2):

* Additive with v1: this module never writes to ``keyword_counts`` or
  ``keyword_pairs`` (those tables belong to the LLM-driven deep-search flow
  and get updated at query time).
* Pure Python keyword extraction: an offline-only path cannot depend on an
  LLM. We tokenise on Unicode word boundaries, keep alphanumeric / CJK
  chunks with length ``2..40``, and drop a small English/Chinese stopword
  set. Callers that want the LLM-derived keywords still get them through
  ``deep_search.KeywordLibrary``.
* Structure-aware ``heading_docs`` rows come from the Phase 2 sidecar
  parser and only cover Markdown files. Non-Markdown files simply don't
  populate ``heading_docs``.

The walker respects a size cap (``max_file_bytes``) so huge binaries and
minified bundles don't blow up the index.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

from grga.graph.schema import connect, ensure_schema

# The tokeniser deliberately allows CJK code points alongside ASCII words.
# ``re`` in Py3.12 supports Unicode categories by default via \w; we widen
# the char class explicitly so the intent is obvious.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]{1,39}|[\u4e00-\u9fff]{2,20}")

_STOPWORDS = frozenset(
    {
        # English
        "the", "and", "for", "with", "that", "this", "from", "have", "has",
        "not", "was", "are", "you", "your", "but", "any", "all", "can",
        "should", "would", "could", "into", "onto", "over", "under", "than",
        "then", "when", "where", "which", "while", "will", "using", "used",
        "use", "one", "two", "may", "also", "such", "only", "each", "some",
        "true", "false", "none", "null", "self", "type", "types", "value",
        "values", "return", "returns", "class", "def", "function", "code",
        "test", "tests", "line", "lines", "file", "files", "path", "paths",
        # Chinese fillers
        "\u7684", "\u4e86", "\u548c", "\u6216", "\u4e0e", "\u4ee5\u53ca",
        "\u4f46\u662f", "\u56e0\u4e3a", "\u6240\u4ee5", "\u8fd9\u4e2a",
        "\u90a3\u4e2a", "\u4e00\u4e2a", "\u4e00\u4e9b", "\u6211\u4eec",
    }
)

_MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}
_TEXT_SUFFIXES = {
    ".md", ".markdown", ".mdown", ".mkd", ".rst", ".txt", ".text",
    ".py", ".pyi", ".rb", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".java", ".kt", ".swift", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".vue", ".css", ".scss", ".less", ".html", ".htm", ".xml", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".json", ".sh", ".bash",
    ".zsh", ".fish", ".ps1", ".sql", ".r", ".m",
}


@dataclass
class BuildStats:
    """Summary produced by :func:`build_graph_index`."""

    scope: str
    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    keyword_docs_written: int = 0
    heading_docs_written: int = 0
    md_files_indexed: int = 0
    elapsed_seconds: float = 0.0
    reasons: Dict[str, int] = field(default_factory=dict)

    def bump_reason(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "files_scanned": self.files_scanned,
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "keyword_docs_written": self.keyword_docs_written,
            "heading_docs_written": self.heading_docs_written,
            "md_files_indexed": self.md_files_indexed,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "reasons": dict(self.reasons),
        }


def iter_text_files(
    scope: Path,
    *,
    max_file_bytes: int,
    ignore_dirs: Iterable[str],
) -> Iterable[Path]:
    """Yield candidate text files under ``scope``.

    Directories matching ``ignore_dirs`` (name-only, matched at any depth)
    are pruned. Files larger than ``max_file_bytes`` or with unrecognised
    suffixes are silently dropped so binary blobs never reach the tokeniser.
    """
    ignore_set = {name for name in ignore_dirs}
    if scope.is_file():
        yield scope
        return
    stack: List[Path] = [scope]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            if entry.is_dir():
                if name in ignore_set:
                    continue
                stack.append(entry)
                continue
            if not entry.is_file():
                continue
            suffix = entry.suffix.lower()
            if suffix not in _TEXT_SUFFIXES:
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size > max_file_bytes:
                continue
            yield entry


def extract_keywords(
    text: str,
    *,
    max_terms: int = 200,
    min_count: int = 2,
) -> List[Tuple[str, int, int]]:
    """Tokenise ``text`` and return ``[(term, count, first_line), ...]``.

    Terms occurring fewer than ``min_count`` times are dropped except when
    they are the very first token of the file (this keeps titles and rare
    proper nouns). Results are truncated to ``max_terms`` entries sorted by
    count desc, then first_line asc for stability.

    Since schema v3 the emitted ``term`` is the **casefolded** form (not the
    original casing): ``keyword_docs.keyword`` stores folded keywords so
    read queries can use a plain ``keyword IN (...)`` index lookup instead
    of the non-sargable ``LOWER(keyword)`` that forced a full-table scan
    (PERF plan §P1-2).
    """
    counts: Dict[str, int] = {}
    first_lines: Dict[str, int] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in _TOKEN_RE.finditer(line):
            term = match.group(0)
            key = term.casefold()
            if key in _STOPWORDS:
                continue
            counts[key] = counts.get(key, 0) + 1
            if key not in first_lines:
                first_lines[key] = line_no
    if not counts:
        return []
    result = []
    for key, count in counts.items():
        if count < min_count and first_lines[key] > 1:
            continue
        # Store the folded key directly (v3): no original-casing lookup.
        result.append((key, count, first_lines[key]))
    result.sort(key=lambda item: (-item[1], item[2], item[0]))
    return result[:max_terms]


def _load_structure(path: Path):
    """Return a StructureDoc or None (Markdown only, best-effort)."""
    suffix = path.suffix.lower()
    if suffix not in _MARKDOWN_SUFFIXES:
        return None
    try:
        from grga.structure.sidecar import load_or_build

        doc, _ = load_or_build(path)
        if doc.error is not None:
            return None
        return doc
    except Exception:
        return None


def build_graph_index(
    scope: Union[str, Path],
    *,
    db_path: Union[str, Path, None] = None,
    max_terms_per_file: int = 200,
    min_term_count: int = 2,
    max_file_bytes: int = 512 * 1024,
    ignore_dirs: Iterable[str] = (".git", ".grga", ".venv", "node_modules",
                                   "__pycache__", "dist", "build", "target",
                                   ".mypy_cache", ".pytest_cache"),
    replace: bool = True,
    progress: Optional[Callable[[str], None]] = None,
) -> BuildStats:
    """Build ``keyword_docs`` and ``heading_docs`` rows for ``scope``.

    Args:
        scope: root directory (or single file) to index.
        db_path: SQLite path; defaults to
            ``grga.deep_search.DEFAULT_DB_PATH`` so v1 and v2 share the
            same artefact.
        max_terms_per_file / min_term_count / max_file_bytes: extraction
            knobs, see :func:`extract_keywords` and :func:`iter_text_files`.
        ignore_dirs: directory names pruned during the walk.
        replace: when ``True``, all pre-existing rows for this ``directory``
            scope in ``keyword_docs`` / ``heading_docs`` are cleared first so
            renamed or deleted files disappear. Set ``False`` to accumulate.
        progress: optional callable receiving a short status message every
            few files (used by the CLI).

    Returns:
        A :class:`BuildStats` describing the run.
    """
    from grga.deep_search import DEFAULT_DB_PATH

    started = time.perf_counter()
    scope_path = Path(scope).expanduser().resolve()
    scope_key = str(scope_path)
    stats = BuildStats(scope=scope_key)

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        if replace:
            conn.execute("DELETE FROM keyword_docs WHERE directory = ?", (scope_key,))
            conn.execute("DELETE FROM heading_docs WHERE directory = ?", (scope_key,))
            conn.commit()

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for path in iter_text_files(
            scope_path,
            max_file_bytes=max_file_bytes,
            ignore_dirs=ignore_dirs,
        ):
            stats.files_scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stats.files_skipped += 1
                stats.bump_reason("read_error")
                continue

            keywords = extract_keywords(
                text,
                max_terms=max_terms_per_file,
                min_count=min_term_count,
            )
            if not keywords:
                stats.files_skipped += 1
                stats.bump_reason("no_keywords")
                continue

            try:
                rel_path = str(path.resolve().relative_to(scope_path))
            except ValueError:
                rel_path = str(path.resolve())

            rows = [
                (scope_key, term, rel_path, float(count), int(first_line), now)
                for term, count, first_line in keywords
            ]
            conn.executemany(
                """
                INSERT INTO keyword_docs
                    (directory, keyword, doc_path, occurrence_count, first_line, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(directory, keyword, doc_path) DO UPDATE SET
                    occurrence_count = excluded.occurrence_count,
                    first_line = excluded.first_line,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            stats.keyword_docs_written += len(rows)
            stats.files_indexed += 1

            structure = _load_structure(path)
            if structure is not None and structure.chapters:
                head_rows = []
                for chapter in structure.chapters:
                    heading_path_key = " > ".join(chapter.heading_path)
                    head_rows.append(
                        (
                            scope_key,
                            heading_path_key,
                            rel_path,
                            chapter.chapter_id,
                            int(chapter.start_line),
                            int(chapter.end_line),
                            now,
                        )
                    )
                if head_rows:
                    conn.executemany(
                        """
                        INSERT INTO heading_docs
                            (directory, heading_path, doc_path, chapter_id,
                             start_line, end_line, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(directory, heading_path, doc_path, chapter_id)
                        DO UPDATE SET
                            start_line = excluded.start_line,
                            end_line = excluded.end_line,
                            updated_at = excluded.updated_at
                        """,
                        head_rows,
                    )
                    stats.heading_docs_written += len(head_rows)
                    stats.md_files_indexed += 1

            if progress is not None and stats.files_scanned % 25 == 0:
                progress(
                    f"indexed {stats.files_indexed}/{stats.files_scanned} files "
                    f"({stats.keyword_docs_written} keyword rows)"
                )

        conn.commit()
    finally:
        conn.close()

    stats.elapsed_seconds = time.perf_counter() - started
    return stats
