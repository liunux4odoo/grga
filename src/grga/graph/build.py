"""Offline chunk-level graph index builder (chunk migration).

``build_graph_index`` walks a directory scope, splits every text file into
*chunks* (line-level or multi-line char windows), extracts keyword
occurrences per chunk, and fills the v4 tables:

* ``chunks``          — one row per chunk (line range + char count).
* ``keyword_chunks``  — keyword → chunk inverted index (segment-df原料).
* ``keyword_pairs``   — ``source='chunk_cooccur'`` rows: for every keyword
  pair co-occurring inside a chunk, the raw ``cooccur_count`` and the
  segment-level ``npmi`` (Bouma 2009).
* ``heading_docs``    — Markdown chapter structure (unchanged).

Design constraints (plan §5 + HC2):

* Pure Python keyword extraction (no LLM, no embedding, no BM25). The
  tokeniser keeps alphanumeric / CJK chunks and drops a *small, general*
  stopword set (not extended for the chunk migration).
* npmi is a pure mathematical identity over chunk-df — no semantic prior,
  no corpus-specific dictionary.
* ``min_pair_count`` sparsifies ``keyword_pairs`` so it does not blow up on
  large single-file corpora.

Segment boundary rules (plan §5.2, ``chars`` mode only):

    heading (Markdown ``#``) > blank line > hard char cut

``line`` mode keeps ripgrep semantics: 1 line = 1 chunk.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

from grga.graph.schema import CHUNK_COOCCUR_SOURCE, connect, ensure_schema

# The tokeniser deliberately allows CJK code points alongside ASCII words.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]{0,38}[A-Za-z0-9_+#]|[\u4e00-\u9fff]{2,20}")

# Markdown ATX heading, e.g. ``## Title``.
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")

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

VALID_CHUNK_MODES = ("line", "chars")
DEFAULT_CHUNK_MAX_CHARS = 600
DEFAULT_CHUNK_MIN_CHARS = 300
DEFAULT_MIN_PAIR_COUNT = 3


@dataclass
class BuildStats:
    """Summary produced by :func:`build_graph_index`."""

    scope: str
    chunk_mode: str = "line"
    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    chunks_written: int = 0
    keyword_chunks_written: int = 0
    pairs_written: int = 0
    heading_docs_written: int = 0
    md_files_indexed: int = 0
    elapsed_seconds: float = 0.0
    reasons: Dict[str, int] = field(default_factory=dict)

    def bump_reason(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "chunk_mode": self.chunk_mode,
            "files_scanned": self.files_scanned,
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "chunks_written": self.chunks_written,
            "keyword_chunks_written": self.keyword_chunks_written,
            "pairs_written": self.pairs_written,
            "heading_docs_written": self.heading_docs_written,
            "md_files_indexed": self.md_files_indexed,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "reasons": dict(self.reasons),
        }


@dataclass
class Chunk:
    """A single chunk of a document.

    ``chunk_id`` is the 0-based order within the doc; ``start_line`` /
    ``end_line`` are 1-based inclusive; ``char_count`` counts characters in
    the joined chunk text (excluding the synthetic newline separators).
    """

    chunk_id: int
    start_line: int
    end_line: int
    char_count: int
    text: str


def iter_text_files(
    scope: Path,
    *,
    max_file_bytes: int,
    ignore_dirs: Iterable[str],
    on_skip: Optional[Callable[[str], None]] = None,
) -> Iterable[Path]:
    """Yield candidate text files under ``scope`` (unchanged from doc-level).

    ``max_file_bytes <= 0`` means *no size limit* (sentinel semantics): every
    text file passes the size gate. A positive value skips files larger than
    it and reports them via ``on_skip("too_large")``.
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
                if on_skip is not None:
                    on_skip("bad_suffix")
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if max_file_bytes > 0 and size > max_file_bytes:
                if on_skip is not None:
                    on_skip("too_large")
                continue
            yield entry


def split_chunks(
    text: str,
    *,
    mode: str = "line",
    is_markdown: bool = False,
    max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
    min_chars: int = DEFAULT_CHUNK_MIN_CHARS,
) -> List[Chunk]:
    """Split ``text`` into :class:`Chunk` objects.

    ``line`` mode: 1 line = 1 chunk (empty lines produce empty chunks that
    contribute no keyword rows). ``chars`` mode: multi-line windows whose
    boundaries follow the priority ``heading > blank line > hard char cut``
    (plan §5.2), targeting ``min_chars..max_chars``. A single line longer
    than ``max_chars`` is never split mid-line — it becomes an over-sized
    chunk on its own.
    """
    lines = text.splitlines()
    if mode == "line":
        chunks: List[Chunk] = []
        for i, line in enumerate(lines):
            chunks.append(
                Chunk(
                    chunk_id=i,
                    start_line=i + 1,
                    end_line=i + 1,
                    char_count=len(line),
                    text=line,
                )
            )
        return chunks

    if mode != "chars":
        raise ValueError(f"unknown chunk mode: {mode!r} (expected one of {VALID_CHUNK_MODES})")

    chunks = []
    chunk_id = 0
    buf_lines: List[str] = []
    buf_start = 1  # 1-based first line of the current buffer
    buf_chars = 0

    def _flush(end_line: int) -> None:
        nonlocal chunk_id, buf_lines, buf_start, buf_chars
        if not buf_lines:
            return
        body = "\n".join(buf_lines)
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                start_line=buf_start,
                end_line=end_line,
                char_count=len(body),
                text=body,
            )
        )
        chunk_id += 1
        buf_lines = []
        buf_chars = 0

    for idx, line in enumerate(lines):
        line_no = idx + 1
        is_heading = is_markdown and bool(_HEADING_RE.match(line))

        # heading boundary: flush the buffer *before* the heading so the
        # heading starts a fresh chunk (even if the current buffer is short).
        if is_heading and buf_lines:
            _flush(line_no - 1)
            buf_start = line_no

        if not buf_lines:
            buf_start = line_no

        # blank line boundary: once we reached min_chars, a blank line ends
        # the chunk. The blank line itself is consumed as the chunk end.
        if line.strip() == "" and buf_chars >= min_chars:
            buf_lines.append(line)
            _flush(line_no)
            continue

        buf_lines.append(line)
        # +1 accounts for the newline joining this line to the previous one.
        buf_chars += len(line) + (1 if len(buf_lines) > 1 else 0)

        # hard char cut: over max, cut at this line boundary (never mid-line).
        if buf_chars >= max_chars:
            _flush(line_no)

    _flush(len(lines))
    return chunks


def extract_chunk_keywords(text: str) -> Counter:
    """Return ``{folded_keyword: occurrence_count}`` for a single chunk.

    Occurrences are raw token counts within the chunk. Stopwords are
    dropped. Keywords are stored casefolded (matches ``keyword_chunks``).
    """
    counts: Counter = Counter()
    for match in _TOKEN_RE.finditer(text):
        key = match.group(0).casefold()
        if key in _STOPWORDS:
            continue
        counts[key] += 1
    return counts


def _npmi(cooccur: int, df_a: int, df_b: int, n_chunks: int) -> Optional[float]:
    """Segment-level npmi (Bouma 2009); ``None`` when undefined.

    ``p`` values are chunk-df normalised by ``n_chunks``. Returns ``None``
    if any input is degenerate (avoids log(0) / div-by-zero).
    """
    if cooccur <= 0 or df_a <= 0 or df_b <= 0 or n_chunks <= 0:
        return None
    p_ab = cooccur / n_chunks
    if p_ab >= 1.0:
        # co-occurs in every chunk: pmi denominator -log(p_ab) -> 0.
        return None
    p_a = df_a / n_chunks
    p_b = df_b / n_chunks
    pmi = math.log(p_ab / (p_a * p_b))
    denom = -math.log(p_ab)
    if denom <= 0:
        return None
    return pmi / denom


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
    chunk_mode: str = "line",
    chunk_max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
    chunk_min_chars: int = DEFAULT_CHUNK_MIN_CHARS,
    min_pair_count: int = DEFAULT_MIN_PAIR_COUNT,
    max_file_bytes: int = 0,
    ignore_dirs: Iterable[str] = (".git", ".grga", ".venv", "node_modules",
                                   "__pycache__", "dist", "build", "target",
                                   ".mypy_cache", ".pytest_cache"),
    replace: bool = True,
    progress: Optional[Callable[[str], None]] = None,
) -> BuildStats:
    """Build ``chunks`` / ``keyword_chunks`` / ``keyword_pairs`` for ``scope``.

    Args:
        scope: root directory (or single file) to index.
        db_path: SQLite path; defaults to
            ``grga.deep_search.DEFAULT_DB_PATH`` so v1 and v2 share the
            same artefact.
        chunk_mode: ``line`` (1 line = 1 chunk) or ``chars`` (multi-line
            windows, see :func:`split_chunks`).
        chunk_max_chars / chunk_min_chars: target char window for ``chars``.
        min_pair_count: minimum chunk co-occurrence count for a pair to be
            stored in ``keyword_pairs`` (sparsification, plan §5.4).
        max_file_bytes: files larger than this are skipped. ``0`` (the
            default) disables the size gate entirely; pass a positive value
            for a defensive upper bound.
        ignore_dirs: directory names pruned during the walk.
        replace: when ``True`` (default) all pre-existing rows for this
            scope in ``chunks`` / ``keyword_chunks`` / ``heading_docs`` and
            the ``chunk_cooccur`` rows in ``keyword_pairs`` are cleared
            first. ``llm_promoted`` pairs are never touched here.
        progress: optional status callback.

    Returns:
        A :class:`BuildStats` describing the run.
    """
    if chunk_mode not in VALID_CHUNK_MODES:
        raise ValueError(
            f"unknown chunk_mode: {chunk_mode!r} (expected one of {VALID_CHUNK_MODES})"
        )

    from grga.deep_search import DEFAULT_DB_PATH

    started = time.perf_counter()
    scope_path = Path(scope).expanduser().resolve()
    scope_key = str(scope_path)
    stats = BuildStats(scope=scope_key, chunk_mode=chunk_mode)

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        if replace:
            conn.execute("DELETE FROM chunks WHERE directory = ?", (scope_key,))
            conn.execute("DELETE FROM keyword_chunks WHERE directory = ?", (scope_key,))
            conn.execute("DELETE FROM heading_docs WHERE directory = ?", (scope_key,))
            conn.execute(
                "DELETE FROM keyword_pairs WHERE directory = ? AND source = ?",
                (scope_key, CHUNK_COOCCUR_SOURCE),
            )
            conn.commit()

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Scope-level co-occurrence accumulators. Chunk-df is the number of
        # distinct chunks a keyword appears in; pair_count is the number of
        # chunks two keywords co-occur in.
        chunk_df: Counter = Counter()
        pair_count: Counter = Counter()
        n_chunks_with_keywords = 0

        for path in iter_text_files(
            scope_path,
            max_file_bytes=max_file_bytes,
            ignore_dirs=ignore_dirs,
            on_skip=stats.bump_reason,
        ):
            stats.files_scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stats.files_skipped += 1
                stats.bump_reason("read_error")
                continue

            try:
                rel_path = str(path.resolve().relative_to(scope_path))
            except ValueError:
                rel_path = str(path.resolve())

            is_md = path.suffix.lower() in _MARKDOWN_SUFFIXES
            chunks = split_chunks(
                text,
                mode=chunk_mode,
                is_markdown=is_md,
                max_chars=chunk_max_chars,
                min_chars=chunk_min_chars,
            )

            chunk_rows = []
            kw_chunk_rows = []
            file_had_keywords = False
            for chunk in chunks:
                chunk_rows.append(
                    (
                        scope_key, scope_key, rel_path, chunk.chunk_id,
                        chunk.start_line, chunk.end_line, chunk.char_count,
                    )
                )
                kw_counts = extract_chunk_keywords(chunk.text)
                if not kw_counts:
                    continue
                file_had_keywords = True
                n_chunks_with_keywords += 1
                for kw, occ in kw_counts.items():
                    kw_chunk_rows.append(
                        (scope_key, kw, rel_path, chunk.chunk_id, int(occ))
                    )
                    chunk_df[kw] += 1
                # Pairwise chunk co-occurrence (canonical unordered pairs).
                distinct_kws = sorted(kw_counts.keys())
                for a, b in combinations(distinct_kws, 2):
                    pair_count[(a, b)] += 1

            if chunk_rows:
                conn.executemany(
                    """
                    INSERT INTO chunks
                        (scope, directory, doc_path, chunk_id, start_line,
                         end_line, char_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(directory, doc_path, chunk_id) DO UPDATE SET
                        start_line = excluded.start_line,
                        end_line = excluded.end_line,
                        char_count = excluded.char_count
                    """,
                    chunk_rows,
                )
                stats.chunks_written += len(chunk_rows)
            if kw_chunk_rows:
                conn.executemany(
                    """
                    INSERT INTO keyword_chunks
                        (directory, keyword, doc_path, chunk_id, occurrence_count)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(directory, keyword, doc_path, chunk_id) DO UPDATE SET
                        occurrence_count = excluded.occurrence_count
                    """,
                    kw_chunk_rows,
                )
                stats.keyword_chunks_written += len(kw_chunk_rows)

            if file_had_keywords:
                stats.files_indexed += 1
            else:
                stats.files_skipped += 1
                stats.bump_reason("no_keywords")

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
                    f"({stats.chunks_written} chunks, {stats.keyword_chunks_written} kw rows)"
                )

        # Materialise keyword_pairs (chunk_cooccur) from the accumulated
        # pair counts + chunk-df, applying npmi and min_pair_count.
        n_chunks = n_chunks_with_keywords
        pair_rows = []
        for (a, b), c in pair_count.items():
            if c < min_pair_count:
                continue
            npmi = _npmi(c, chunk_df[a], chunk_df[b], n_chunks)
            pair_rows.append((scope_key, a, b, float(c), npmi, now))
        if pair_rows:
            conn.executemany(
                """
                INSERT INTO keyword_pairs
                    (scope, directory, keyword_a, keyword_b, source,
                     cooccur_count, npmi, updated_at)
                VALUES ('', ?, ?, ?, 'chunk_cooccur', ?, ?, ?)
                ON CONFLICT(directory, keyword_a, keyword_b, source) DO UPDATE SET
                    cooccur_count = excluded.cooccur_count,
                    npmi = excluded.npmi,
                    updated_at = excluded.updated_at
                """,
                pair_rows,
            )
            stats.pairs_written += len(pair_rows)

        conn.commit()
    finally:
        conn.close()

    stats.elapsed_seconds = time.perf_counter() - started
    return stats
