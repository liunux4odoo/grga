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

import hashlib
import math
import re
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from grga.file_read import (
    RGA_SUPPORTED_SUFFIXES,
    TEXT_SUFFIXES,
    ReadSource,
    indexable_suffixes,
    read_file_lines_ex,
)
from grga.graph.ids import IdInterner
from grga.graph.schema import CHUNK_COOCCUR_SOURCE, connect, ensure_schema

# Hybrid tokeniser (schema v6): ASCII code identifiers keep their original
# regex shape (so ``foo-bar`` / ``v1.2.3`` / ``c++`` stay whole for code
# search), while CJK runs are segmented by jieba + an auto-discovered n-gram
# dictionary (so ``章北海`` / ``面壁者`` become single tokens instead of a
# greedy 2-20 char blob). The two branches are matched in one pass and merged
# in positional order; the ASCII branch is the *previous* mainline regex.
_ASCII_TOKEN = r"[A-Za-z][A-Za-z0-9_+#.-]{0,38}[A-Za-z0-9_+#]"
_CJK_RUN = r"[\u4e00-\u9fff]+"
_COMBINED_RE = re.compile(_ASCII_TOKEN + "|" + _CJK_RUN)
_CJK_ONLY_RE = re.compile(_CJK_RUN)
# Back-compat: the old single-token regex (ASCII branch + bounded CJK run).
# Retained for :func:`extract_chunk_keywords` callers that expect the pure
# regex behaviour without a jieba dependency at call time.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]{0,38}[A-Za-z0-9_+#]|[\u4e00-\u9fff]{2,20}")

# Detection-only boundary set (26 structural particles, no 构词后缀 like
# 者/人/号/家). Keeps 面壁者 / 破壁人 discoverable while still cutting
# collocations like 罗辑说. Separate from _STOPWORDS (the index filter).
_CORE_STOP_BOUNDARIES = frozenset(
    "的了和与或是在这那有被把将从对为之其而且但也就都还只很才又已"
)

# Auto n-gram detection thresholds (frozen by experiments/autongram_2026_07_11).
DEFAULT_NGRAM_ITERS = 2
DEFAULT_MAX_NGRAM_N = 4
_NGRAM_MIN_COUNT = 3
_NGRAM_NPMI_THRESHOLD = 0.3
_NGRAM_ENT_THRESHOLD = 1.0

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
# Legacy alias: prior code used this local set both for the enumeration
# gate in ``iter_text_files`` and (implicitly) for "text vs binary"
# routing. The gate is now driven by :func:`grga.file_read.indexable_suffixes`
# so PDFs / Office docs make it through; ``_TEXT_SUFFIXES`` is kept only
# for existing external callers and unit tests that imported it.
_TEXT_SUFFIXES = TEXT_SUFFIXES

VALID_CHUNK_MODES = ("line", "chars")
DEFAULT_CHUNK_MAX_CHARS = 600
DEFAULT_CHUNK_MIN_CHARS = 300
DEFAULT_MIN_PAIR_COUNT = 3


@dataclass
class BuildStats:
    """Summary produced by :func:`build_graph_index`."""

    scope: str
    chunk_mode: str = "line"
    # ``mode`` = the incremental / full-scope mode this run executed.
    # See docs/design/plan-2026-07-11-build-vs-rebuild.md §3.
    mode: str = "rebuild"
    refresh_ngram: str = "replace"
    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    # ``rga_reads`` counts documents whose lines were pulled via
    # ripgrep-all's adapter chain (PDF / Office / archives / ...). It
    # measures the *slow path*: each such read forks rga, so a spike
    # here explains a slow build. Text files opened natively do not
    # contribute.
    rga_reads: int = 0
    chunks_written: int = 0
    keyword_chunks_written: int = 0
    pairs_written: int = 0
    heading_docs_written: int = 0
    md_files_indexed: int = 0
    ngram_phrases_written: int = 0
    ngram_iters_run: int = 0
    # v7 incremental / life-cycle counters.
    # In ``mode='build'`` these classify each on-disk file against the
    # existing ``docs`` row (see :func:`_classify_doc`). In
    # ``mode='rebuild'`` every scanned file counts as ``docs_added``
    # because the index layer was cleared first.
    docs_added: int = 0
    docs_updated: int = 0
    docs_metadata_only: int = 0
    docs_removed: int = 0
    docs_unchanged: int = 0
    # ``ngram_dict_size`` is the total row count after the build;
    # ``ngram_dict_added`` is the delta (rows first inserted this run).
    ngram_dict_size: int = 0
    ngram_dict_added: int = 0
    ngram_dict_removed: int = 0  # always 0 for build/rebuild (only forget removes)
    # Human-readable warnings surfaced in ``--json`` output.
    warnings: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    reasons: Dict[str, int] = field(default_factory=dict)

    def bump_reason(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "chunk_mode": self.chunk_mode,
            "mode": self.mode,
            "refresh_ngram": self.refresh_ngram,
            "files_scanned": self.files_scanned,
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "rga_reads": self.rga_reads,
            "chunks_written": self.chunks_written,
            "keyword_chunks_written": self.keyword_chunks_written,
            "pairs_written": self.pairs_written,
            "heading_docs_written": self.heading_docs_written,
            "md_files_indexed": self.md_files_indexed,
            "ngram_phrases_written": self.ngram_phrases_written,
            "ngram_iters_run": self.ngram_iters_run,
            "docs_added": self.docs_added,
            "docs_updated": self.docs_updated,
            "docs_metadata_only": self.docs_metadata_only,
            "docs_removed": self.docs_removed,
            "docs_unchanged": self.docs_unchanged,
            "ngram_dict_size": self.ngram_dict_size,
            "ngram_dict_added": self.ngram_dict_added,
            "ngram_dict_removed": self.ngram_dict_removed,
            "warnings": list(self.warnings),
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
    """Yield candidate indexable files under ``scope``.

    "Indexable" is the union of the plain-text whitelist and the
    ripgrep-all-supported binary formats (PDF, Office, archives, ...);
    see :func:`grga.file_read.indexable_suffixes`. Files whose suffix
    is neither are reported via ``on_skip("bad_suffix")`` and skipped.

    ``max_file_bytes <= 0`` means *no size limit* (sentinel semantics): every
    indexable file passes the size gate. A positive value skips files larger
    than it and reports them via ``on_skip("too_large")``.
    """
    accepted_suffixes = indexable_suffixes()
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
            if suffix not in accepted_suffixes:
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


# ---------------------------------------------------------------------------
# Hybrid tokeniser (schema v6): ASCII regex + jieba-segmented CJK.
# ---------------------------------------------------------------------------

def _new_jieba_tokenizer(phrases: Iterable[str]):
    """Return a fresh ``jieba.Tokenizer`` seeded with ``phrases``.

    A per-build instance (not the module singleton) so concurrent builds and
    repeated test runs never leak dictionary state into each other.
    """
    import jieba

    tok = jieba.Tokenizer()
    for phrase in phrases:
        tok.add_word(phrase)
    tok.initialize()
    return tok


def tokenize_hybrid(text: str, jieba_tok) -> List[str]:
    """Tokenise ``text`` keeping ASCII identifiers whole, CJK via jieba.

    ASCII / mixed code tokens (``foo-bar``, ``v1.2.3``, ``c++``) are emitted
    verbatim by the regex branch. Each maximal CJK run is handed to
    ``jieba_tok`` (which carries the auto n-gram dictionary), so ``章北海``
    collapses to one token once discovered. Matches are merged in positional
    order.
    """
    out: List[str] = []
    for m in _COMBINED_RE.finditer(text):
        piece = m.group(0)
        if _CJK_ONLY_RE.fullmatch(piece):
            for t in jieba_tok.cut(piece, cut_all=False):
                if t and not t.isspace():
                    out.append(t)
        else:
            out.append(piece)
    return out


# ASCII-shaped token pattern (matches the regex ASCII branch exactly).
_ASCII_TOK_ONLY_RE = re.compile(_ASCII_TOKEN)


def _is_ascii_tok(tok: str) -> bool:
    return _ASCII_TOK_ONLY_RE.fullmatch(tok) is not None


def _join_by_pair_rule(win: Tuple[str, ...]) -> str:
    """Rejoin token tuple honouring CJK/ASCII adjacency conventions.

    Rule (matches experiment F11):
      - ASCII + ASCII: single space  ("nano", "center") -> "nano center"
      - anything else: direct concat ("章", "北海") -> "章北海",
                                    ("艾", "AA") -> "艾AA",
                                    ("PDC", "主席") -> "PDC主席"
    """
    if not win:
        return ""
    parts = [win[0]]
    for prev, cur in zip(win, win[1:]):
        if _is_ascii_tok(prev) and _is_ascii_tok(cur):
            parts.append(" ")
        parts.append(cur)
    return "".join(parts)


def tokenize_hybrid_with_pos(text: str, jieba_tok) -> List[Tuple[str, int, int]]:
    """Same as :func:`tokenize_hybrid` but yields ``(token, start, end)`` in
    original-text character offsets. Consumers of
    :func:`merge_auto_phrases` rely on positional adjacency to avoid merging
    tokens that were separated by whitespace/punctuation in the source.
    """
    out: List[Tuple[str, int, int]] = []
    for m in _COMBINED_RE.finditer(text):
        piece = m.group(0)
        base = m.start()
        if _CJK_ONLY_RE.fullmatch(piece):
            offset = 0
            for t in jieba_tok.cut(piece, cut_all=False):
                if t and not t.isspace():
                    out.append((t, base + offset, base + offset + len(t)))
                offset += len(t)
        else:
            out.append((piece, m.start(), m.end()))
    return out


def merge_auto_phrases(
    tokens_with_pos: List[Tuple[str, int, int]],
    phrase_set: set,
    *,
    max_n: int = DEFAULT_MAX_NGRAM_N,
) -> List[str]:
    """Greedy longest-match merge of adjacent tokens against ``phrase_set``.

    Only merges when tokens are **positionally adjacent** in the source
    (``prev.end == next.start``); this prevents ``"艾, AA"`` (comma between)
    from being merged into ``"艾AA"``. Longest match wins; falls back to the
    single token when no match. ``phrase_set`` should contain the rejoin
    string form (see :func:`_join_by_pair_rule`).
    """
    if not phrase_set:
        return [t[0] for t in tokens_with_pos]
    out: List[str] = []
    i = 0
    L = len(tokens_with_pos)
    while i < L:
        merged = False
        max_try = min(max_n, L - i)
        for n in range(max_try, 1, -1):
            win = tokens_with_pos[i:i + n]
            adjacent = all(
                win[k][2] == win[k + 1][1] for k in range(len(win) - 1)
            )
            if not adjacent:
                continue
            joined = _join_by_pair_rule(tuple(t[0] for t in win))
            if joined in phrase_set:
                out.append(joined)
                i += n
                merged = True
                break
        if not merged:
            out.append(tokens_with_pos[i][0])
            i += 1
    return out


def _keywords_from_tokens(tokens: Iterable[str]) -> Counter:
    """Casefold + stopword-filter a token list into ``{keyword: count}``."""
    counts: Counter = Counter()
    for tok in tokens:
        key = tok.casefold()
        if key in _STOPWORDS:
            continue
        counts[key] += 1
    return counts


# ---------------------------------------------------------------------------
# Unsupervised n-gram phrase detection (frequency + NPMI + branch entropy).
# ---------------------------------------------------------------------------

def detect_ngram_phrases(
    line_tokens: List[List[str]],
    *,
    max_n: int = DEFAULT_MAX_NGRAM_N,
    min_count: int = _NGRAM_MIN_COUNT,
    npmi_threshold: float = _NGRAM_NPMI_THRESHOLD,
    ent_threshold: float = _NGRAM_ENT_THRESHOLD,
    stop_boundaries: frozenset = _CORE_STOP_BOUNDARIES,
) -> List[dict]:
    """Detect CJK compound phrases from tokenised lines.

    Three pure-mathematical gates (no semantic prior, AGENTS.md 铁律 2):

    * frequency: ``count >= min_count``
    * solidity: ``min over splits { NPMI(left, right) }`` (Bouma 2009); a
      single N (total token count) keeps NPMI bounded in ``[-1, 1]`` for
      every split, including n>2 min-split.
    * branch entropy: ``min(H_left, H_right) >= ent_threshold`` (nat).

    Windows may mix ASCII and CJK tokens (so hybrid entities like 艾AA /
    PDC主席 can be discovered). Detected phrases are rejoined by the
    adjacent-pair rule (CJK+anything -> no separator; ASCII+ASCII -> single
    space). Callers that use these phrases in a jieba user_dict only benefit
    from CJK-only ones (jieba sees only CJK runs), so mixed phrases exist
    for the post-tokenisation merge step (see :func:`merge_auto_phrases`).
    """
    uni: Counter = Counter()
    for toks in line_tokens:
        uni.update(toks)
    total = sum(uni.values())
    if total == 0:
        return []
    log_total = math.log(total)

    @lru_cache(maxsize=None)
    def _log_uni(t: str) -> float:
        c = uni.get(t, 0)
        return math.log(c) if c > 0 else math.nan

    @lru_cache(maxsize=None)
    def _log_c(c: int) -> float:
        return math.log(c)

    ngram_count: Counter = Counter()
    left_ctx: "defaultdict[tuple, Counter]" = defaultdict(Counter)
    right_ctx: "defaultdict[tuple, Counter]" = defaultdict(Counter)

    for toks in line_tokens:
        L = len(toks)
        for i in range(L):
            for n in range(2, max_n + 1):
                if i + n > L:
                    break
                win = tuple(toks[i:i + n])
                # No structural-particle boundary; mixed CJK/ASCII allowed
                # so 艾AA / PDC主席 kind of hybrid entities get discovered.
                if any(t in stop_boundaries for t in win):
                    continue
                ngram_count[win] += 1
                if i > 0 and toks[i - 1] not in stop_boundaries:
                    left_ctx[win][toks[i - 1]] += 1
                if i + n < L and toks[i + n] not in stop_boundaries:
                    right_ctx[win][toks[i + n]] += 1

    def _entropy(counter: Counter) -> float:
        tot = sum(counter.values())
        if tot == 0:
            return 0.0
        h = 0.0
        for c in counter.values():
            pr = c / tot
            h -= pr * math.log(pr)
        return h

    def _count(x) -> int:
        return uni.get(x, 0) if isinstance(x, str) else ngram_count.get(x, 0)

    def _log_count(x) -> float:
        if isinstance(x, str):
            return _log_uni(x)
        c = ngram_count.get(x, 0)
        return _log_c(c) if c > 0 else math.nan

    def _npmi_pair(a, b, joint_c: int) -> float:
        # Single-N NPMI: all P over ``total`` -> value stays in [-1, 1].
        ca, cb = _count(a), _count(b)
        if ca <= 0 or cb <= 0 or joint_c <= 0:
            return -1.0
        pmi = log_total + _log_c(joint_c) - _log_count(a) - _log_count(b)
        denom = log_total - _log_c(joint_c)
        if denom <= 0:
            return 0.0
        return pmi / denom

    def _solidity(win: tuple, joint_c: int) -> float:
        n = len(win)
        if n < 2:
            return 1.0
        vals = []
        for k in range(1, n):
            left = win[0] if k == 1 else win[:k]
            right = win[-1] if k == n - 1 else win[k:]
            vals.append(_npmi_pair(left, right, joint_c))
        return min(vals)

    results: List[dict] = []
    for win, c in ngram_count.items():
        if c < min_count:
            continue
        sol = _solidity(win, c)
        if sol < npmi_threshold:
            continue
        be = min(_entropy(left_ctx[win]), _entropy(right_ctx[win]))
        if be < ent_threshold:
            continue
        results.append({
            "phrase": _join_by_pair_rule(win),
            "n": len(win),
            "count": c,
            "npmi": round(sol, 4),
            "branch_ent": round(be, 4),
        })
    results.sort(key=lambda x: (-x["npmi"], -x["count"]))
    return results


def _load_auto_dict(conn, dir_id: int) -> Dict[str, dict]:
    """Load a scope's persisted auto n-gram dictionary as ``{phrase: row}``."""
    out: Dict[str, dict] = {}
    for row in conn.execute(
        "SELECT phrase, n, count, npmi, branch_ent FROM auto_ngram_dict "
        "WHERE dir_id = ?",
        (dir_id,),
    ):
        out[row["phrase"]] = {
            "phrase": row["phrase"], "n": row["n"], "count": row["count"],
            "npmi": row["npmi"], "branch_ent": row["branch_ent"],
        }
    return out


def _persist_auto_dict(conn, dir_id: int, entries: Iterable[dict], now: str) -> None:
    """Upsert ``entries`` into ``auto_ngram_dict`` for ``dir_id``."""
    rows = [
        (dir_id, e["phrase"], int(e["n"]), int(e["count"]),
         float(e["npmi"]), float(e["branch_ent"]), now)
        for e in entries
    ]
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO auto_ngram_dict
            (dir_id, phrase, n, count, npmi, branch_ent, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dir_id, phrase) DO UPDATE SET
            n = excluded.n,
            count = excluded.count,
            npmi = excluded.npmi,
            branch_ent = excluded.branch_ent,
            updated_at = excluded.updated_at
        """,
        rows,
    )


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


# ---------------------------------------------------------------------------
# Incremental build helpers (schema v7).
# Design: docs/design/plan-2026-07-11-build-vs-rebuild.md
# ---------------------------------------------------------------------------

_HASH_DIGEST_SIZE = 16  # BLAKE2b-128 hex (32 chars) — collision-safe well beyond
                        # per-scope doc counts we handle in practice.


def _hash_output_lines(lines: List[str]) -> str:
    """Return a stable BLAKE2b-128 hex digest of the joined rga output text.

    Joins with a single ``\\n`` to match how the builder later reconstructs
    ``text = "\\n".join(read_result.lines)`` before ``split_chunks``. The
    hash is over the *rga output*, not the source bytes (see design doc
    §4.1): the index consumes rga output, so change-detection must too.
    """
    h = hashlib.blake2b(digest_size=_HASH_DIGEST_SIZE)
    h.update("\n".join(lines).encode("utf-8", errors="replace"))
    return h.hexdigest()


# Classification kinds returned by :func:`_classify_doc`. Kept as plain
# strings so BuildStats reasons/counters can key on them without an enum
# import cycle.
DOC_UNCHANGED = "unchanged"
DOC_METADATA_ONLY = "metadata_only"
DOC_UPDATED = "updated"
DOC_ADDED = "added"
DOC_READ_ERROR = "read_error"


def _classify_doc(
    path: Path,
    doc_row: Optional[Dict[str, object]],
) -> Tuple[str, Optional["ReadResult"], Optional[str]]:  # noqa: F821 - forward ref
    """Three-level change detection for a single file.

    Returns ``(kind, read_result_or_none, new_hash_or_none)``:

    * ``added`` — no prior row; caller must read + tokenise. Returns
      ``(kind, read_result, hash)`` with rga already invoked.
    * ``unchanged`` — size + mtime match the DB; skip everything.
      ``read_result`` is ``None`` (rga never called).
    * ``metadata_only`` — size/mtime moved but rga output hash matches
      (touch / element-only metadata edit). Caller only refreshes
      ``docs.mtime`` / ``docs.size``. Returns ``(kind, read_result, hash)``.
    * ``updated`` — real content change; caller re-tokenises.
      Returns ``(kind, read_result, hash)``.
    * ``read_error`` — rga refused to read the file; caller skips.

    Levels 1/2 correspond to the algorithm in design doc §5.
    """
    if doc_row is None:
        # Level 3 direct: file is new, always read + hash.
        read_result = read_file_lines_ex(path)
        if read_result.source == ReadSource.FAILED:
            return DOC_READ_ERROR, None, None
        new_hash = _hash_output_lines(read_result.lines)
        return DOC_ADDED, read_result, new_hash

    # Level 1: cheapest. size + mtime match → skip.
    try:
        st = path.stat()
    except OSError:
        return DOC_READ_ERROR, None, None
    prev_size = doc_row.get("size")
    prev_mtime = doc_row.get("mtime")
    if (prev_size is not None and prev_mtime is not None
            and int(prev_size) == int(st.st_size)
            and float(prev_mtime) == float(st.st_mtime)):
        return DOC_UNCHANGED, None, None

    # Level 2: metadata moved → read via rga, hash the OUTPUT.
    read_result = read_file_lines_ex(path)
    if read_result.source == ReadSource.FAILED:
        return DOC_READ_ERROR, None, None
    new_hash = _hash_output_lines(read_result.lines)
    prev_hash = doc_row.get("content_hash")
    if prev_hash is not None and str(prev_hash) == new_hash:
        # Level 2 hit: touch / metadata change / stable rga output.
        return DOC_METADATA_ONLY, read_result, new_hash

    # Level 3: real content change.
    return DOC_UPDATED, read_result, new_hash


def _load_scope_doc_rows(
    conn, scope_dir_id: int
) -> Dict[str, Dict[str, object]]:
    """Load ``{doc_path: {id, size, mtime, content_hash}}`` for a scope.

    Used by ``mode='build'`` to classify each file on disk against the
    stored record without an N+1 SELECT loop. ``id`` is included so
    per-doc DELETEs (removed / updated) do not need a second lookup.
    """
    rows = conn.execute(
        "SELECT id, doc_path, size, mtime, content_hash "
        "FROM docs WHERE dir_id = ?",
        (scope_dir_id,),
    ).fetchall()
    out: Dict[str, Dict[str, object]] = {}
    for row in rows:
        # sqlite3.Row supports dict-like access via keys.
        out[str(row["doc_path"])] = {
            "id": int(row["id"]),
            "size": row["size"],
            "mtime": row["mtime"],
            "content_hash": row["content_hash"],
        }
    return out


def _update_doc_metadata(
    conn, doc_id: int, size: int, mtime: float, content_hash: str
) -> None:
    """Refresh the v7 change-detection columns for one doc."""
    conn.execute(
        "UPDATE docs SET size = ?, mtime = ?, content_hash = ? WHERE id = ?",
        (int(size), float(mtime), str(content_hash), int(doc_id)),
    )


def _delete_doc_index_rows(conn, doc_ids: Sequence[int]) -> None:
    """Delete every index-layer row for the given docs.

    Wipes ``chunks`` / ``keyword_chunks`` / ``heading_docs`` for the doc
    ids, then the ``docs`` rows themselves. Use this for the ``removed``
    branch of incremental build (files gone from disk) and as the
    "per-doc reset" step before re-inserting ``updated`` content (where
    the docs row itself is kept, so pass an empty list to skip the last
    stage or call :func:`_delete_doc_index_rows_keep_docs` instead).
    """
    if not doc_ids:
        return
    _delete_doc_index_rows_keep_docs(conn, doc_ids)
    placeholders = ",".join("?" for _ in doc_ids)
    conn.execute(
        f"DELETE FROM docs WHERE id IN ({placeholders})",
        list(doc_ids),
    )


def _delete_doc_index_rows_keep_docs(
    conn, doc_ids: Sequence[int]
) -> None:
    """Wipe index-layer rows for ``doc_ids`` but keep the ``docs`` entries.

    Used when a doc's content changed: we replace its chunks/keyword rows
    but preserve the ``docs.id`` so all foreign keys remain valid.
    """
    if not doc_ids:
        return
    placeholders = ",".join("?" for _ in doc_ids)
    ids = list(doc_ids)
    conn.execute(
        f"DELETE FROM keyword_chunks WHERE doc_id IN ({placeholders})",
        ids,
    )
    conn.execute(
        f"DELETE FROM chunks WHERE doc_id IN ({placeholders})",
        ids,
    )
    conn.execute(
        f"DELETE FROM heading_docs WHERE doc_id IN ({placeholders})",
        ids,
    )


def _delete_scope_index_rows(conn, scope_dir_id: int) -> None:
    """Wipe every index-layer row for the given scope.

    Uses subquery-scoped DELETEs so we do not have to shuttle the full
    doc_id list into Python for large scopes. Keeps the three-table set
    (``chunks`` / ``keyword_chunks`` / ``heading_docs``) in exactly one
    place — rebuild and forget --all both go through here so any new
    ``doc_id``-keyed table gets picked up automatically.
    """
    conn.execute(
        "DELETE FROM keyword_chunks WHERE doc_id IN "
        "(SELECT id FROM docs WHERE dir_id = ?)",
        (scope_dir_id,),
    )
    conn.execute(
        "DELETE FROM chunks WHERE doc_id IN "
        "(SELECT id FROM docs WHERE dir_id = ?)",
        (scope_dir_id,),
    )
    conn.execute(
        "DELETE FROM heading_docs WHERE doc_id IN "
        "(SELECT id FROM docs WHERE dir_id = ?)",
        (scope_dir_id,),
    )


VALID_BUILD_MODES = ("build", "rebuild")
VALID_REFRESH_NGRAM = ("never", "append", "replace")


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
    mode: str = "rebuild",
    refresh_ngram: str = "replace",
    replace: Optional[bool] = None,
    ngram_iters: int = DEFAULT_NGRAM_ITERS,
    max_ngram_n: int = DEFAULT_MAX_NGRAM_N,
    progress: Optional[Callable[[str], None]] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> BuildStats:
    """Build ``chunks`` / ``keyword_chunks`` / ``keyword_pairs`` for ``scope``.

    Two top-level modes (design doc §3):

    * ``mode="build"`` — incremental (schema v7). Uses the
      ``docs.size/mtime/content_hash`` metadata to skip unchanged files,
      re-tokenise only content-changed ones, and delete rows for files
      that disappeared from disk. Vocabulary is monotonic-add.
    * ``mode="rebuild"`` (default) — full-scope re-scan. Clears the
      index + derived layers first, then re-indexes every file.

    ``refresh_ngram`` controls the ``auto_ngram_dict`` asset layer:

    * ``"never"`` — do not run n-gram detection; use whatever phrases
      already live in ``auto_ngram_dict``.
    * ``"append"`` — detect and union new phrases into the existing
      dictionary. In ``mode="build"`` runs a single detection pass on
      the changed files' tokens (per-doc local aggregate); in
      ``mode="rebuild"`` runs the full multi-iter loop.
    * ``"replace"`` — clear ``auto_ngram_dict`` for this scope first,
      then run detection from scratch. Only meaningful with
      ``mode="rebuild"``; combined with ``mode="build"`` this raises
      ``ValueError``.

    Args:
        scope: root directory (or single file) to index.
        db_path: SQLite path; defaults to
            ``grga.deep_search.DEFAULT_DB_PATH``.
        chunk_mode / chunk_max_chars / chunk_min_chars / min_pair_count
        / max_file_bytes / ignore_dirs: unchanged pre-refactor.
        mode: ``"build"`` (incremental) or ``"rebuild"`` (full re-scan).
        refresh_ngram: ``"never"`` / ``"append"`` / ``"replace"``.
        replace: **deprecated**. Legacy compatibility flag: ``True``
            maps to ``(mode="rebuild", refresh_ngram="replace")``,
            ``False`` maps to ``(mode="rebuild",
            refresh_ngram="append")``. When set, overrides both new
            params. New callers should pass ``mode`` / ``refresh_ngram``
            directly.
        ngram_iters / max_ngram_n: detection tuning; ``ngram_iters`` is
            capped to 1 automatically in ``mode="build"`` (see design
            §3.1 step 3).
        progress: optional status callback.
        on_event: structured progress callback. Same phases as before
            with two new fields on ``done``: ``mode``, ``refresh_ngram``.
    """
    if chunk_mode not in VALID_CHUNK_MODES:
        raise ValueError(
            f"unknown chunk_mode: {chunk_mode!r} (expected one of {VALID_CHUNK_MODES})"
        )

    # Back-compat: translate ``replace`` to the new params. This is the
    # only path that emits a semantic change to legacy callers — they
    # get the exact same behaviour they had before.
    if replace is not None:
        mode = "rebuild"
        refresh_ngram = "replace" if replace else "append"

    if mode not in VALID_BUILD_MODES:
        raise ValueError(
            f"unknown mode: {mode!r} (expected one of {VALID_BUILD_MODES})"
        )
    if refresh_ngram not in VALID_REFRESH_NGRAM:
        raise ValueError(
            f"unknown refresh_ngram: {refresh_ngram!r} "
            f"(expected one of {VALID_REFRESH_NGRAM})"
        )
    if mode == "build" and refresh_ngram == "replace":
        # "replace" on incremental would delete the vocabulary asset that
        # incremental build depends on. Force the user to be explicit.
        raise ValueError(
            "mode='build' + refresh_ngram='replace' is not allowed. "
            "Use `grga index forget --all` followed by `grga index build`, "
            "or run `grga index rebuild --refresh-ngram=replace`."
        )

    from grga.deep_search import DEFAULT_DB_PATH

    started = time.perf_counter()
    scope_path = Path(scope).expanduser().resolve()
    scope_key = str(scope_path)
    stats = BuildStats(
        scope=scope_key, chunk_mode=chunk_mode,
        mode=mode, refresh_ngram=refresh_ngram,
    )

    def _emit(phase: str, **fields) -> None:
        if on_event is None:
            return
        payload = {"phase": phase}
        payload.update(fields)
        try:
            on_event(payload)
        except Exception:
            # Progress reporting must never break a build.
            pass

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        interner = IdInterner(conn)
        scope_dir_id = interner.dir_id(scope_key)

        # ------------------------------------------------------------------
        # Setup: load existing docs (build mode) and clear scope (rebuild).
        # ------------------------------------------------------------------
        existing_docs: Dict[str, Dict[str, object]] = {}
        if mode == "build":
            existing_docs = _load_scope_doc_rows(conn, scope_dir_id)

        if mode == "rebuild":
            # Full clear of index + derived layers for this scope.
            # ``keyword_chunks`` / ``chunks`` / ``heading_docs`` are keyed
            # by ``doc_id`` which resolves through ``docs.dir_id``. The
            # shared helper keeps the three-table set in one place.
            _delete_scope_index_rows(conn, scope_dir_id)
            conn.execute(
                "DELETE FROM keyword_pairs WHERE dir_id = ? AND source = ?",
                (scope_dir_id, CHUNK_COOCCUR_SOURCE),
            )
            if refresh_ngram == "replace":
                conn.execute(
                    "DELETE FROM auto_ngram_dict WHERE dir_id = ?",
                    (scope_dir_id,),
                )
            conn.commit()

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # ------------------------------------------------------------------
        # Pass 0: enumerate files. In build mode we also classify against
        # existing docs and only read/tokenise the changed ones. The
        # ``docs`` list entries are (doc_id, rel_path, path, is_md,
        # chunks, kind, size, mtime, content_hash) — the last four are
        # None for kinds we skip (unchanged). ``removed_doc_ids`` holds
        # doc ids that must be deleted after Pass 2.
        # ------------------------------------------------------------------
        docs: List[tuple] = []  # entries needing tokenisation
        metadata_only_updates: List[Tuple[int, int, float, str]] = []
        removed_doc_ids: List[int] = []
        seen_rel_paths: set = set()

        _emit("scan_start")
        for path in iter_text_files(
            scope_path,
            max_file_bytes=max_file_bytes,
            ignore_dirs=ignore_dirs,
            on_skip=stats.bump_reason,
        ):
            stats.files_scanned += 1
            try:
                rel_path = str(path.resolve().relative_to(scope_path))
            except ValueError:
                rel_path = str(path.resolve())
            seen_rel_paths.add(rel_path)

            prev_row = existing_docs.get(rel_path) if mode == "build" else None
            kind, read_result, new_hash = _classify_doc(path, prev_row)

            if kind == DOC_UNCHANGED:
                stats.docs_unchanged += 1
                continue
            if kind == DOC_READ_ERROR:
                stats.files_skipped += 1
                stats.bump_reason("read_error")
                continue
            if kind == DOC_METADATA_ONLY:
                # Content byte-identical (post-rga); refresh only the
                # metadata columns so future Level 1 checks hit fast.
                stats.docs_metadata_only += 1
                try:
                    st_size = path.stat().st_size
                    st_mtime = path.stat().st_mtime
                except OSError:
                    continue
                doc_id = int(prev_row["id"]) if prev_row else interner.doc_id(scope_dir_id, rel_path)
                metadata_only_updates.append(
                    (doc_id, st_size, st_mtime, new_hash)
                )
                continue

            # kind is DOC_ADDED or DOC_UPDATED — must tokenise.
            assert read_result is not None
            if read_result.source == ReadSource.RGA:
                stats.rga_reads += 1
            text = "\n".join(read_result.lines)
            is_md = path.suffix.lower() in _MARKDOWN_SUFFIXES
            chunks = split_chunks(
                text, mode=chunk_mode, is_markdown=is_md,
                max_chars=chunk_max_chars, min_chars=chunk_min_chars,
            )
            if kind == DOC_ADDED:
                doc_id = interner.doc_id(scope_dir_id, rel_path)
                stats.docs_added += 1
            else:  # DOC_UPDATED
                doc_id = int(prev_row["id"]) if prev_row else interner.doc_id(scope_dir_id, rel_path)
                stats.docs_updated += 1
            try:
                st_size = path.stat().st_size
                st_mtime = path.stat().st_mtime
            except OSError:
                # Race: file went away between classify and stat. Treat
                # as read_error so we neither insert stale rows nor break.
                stats.files_skipped += 1
                stats.bump_reason("read_error")
                continue
            docs.append((
                doc_id, rel_path, path, is_md, chunks,
                kind, st_size, st_mtime, new_hash,
            ))
            _emit(
                "scan_advance",
                current=stats.files_scanned,
                path=str(path),
                chunks=len(chunks),
            )

        # In build mode, detect removed files (in DB but not on disk).
        if mode == "build":
            for rel_path, row in existing_docs.items():
                if rel_path in seen_rel_paths:
                    continue
                removed_doc_ids.append(int(row["id"]))
            stats.docs_removed = len(removed_doc_ids)

        total_scanned_chunks = sum(len(entry[4]) for entry in docs)
        _emit(
            "scan_end",
            total_docs=len(docs),
            total_chunks=total_scanned_chunks,
        )

        # ------------------------------------------------------------------
        # Pass 1: n-gram dictionary refinement.
        # rebuild + append/replace: multi-iter as before.
        # rebuild + never:          skip detection; tokenise once with
        #                           current dict for Pass 2 consumption.
        # build + append:           single detection iter on changed docs.
        # build + never:            skip detection; tokenise changed docs.
        # ------------------------------------------------------------------
        prior_dict_size = int(conn.execute(
            "SELECT COUNT(*) FROM auto_ngram_dict WHERE dir_id = ?",
            (scope_dir_id,),
        ).fetchone()[0])

        # Seed the working dict. ``refresh_ngram="replace"`` on rebuild
        # already dropped the DB rows above, so this loads empty.
        auto_dict: Dict[str, dict] = _load_auto_dict(conn, scope_dir_id)

        chunk_tokens: Dict[tuple, List[str]] = {}
        if mode == "rebuild" and refresh_ngram in ("append", "replace"):
            iters = max(1, int(ngram_iters))
        elif mode == "build" and refresh_ngram == "append":
            iters = 1
        else:
            # ``refresh_ngram="never"`` for either mode. Still need one
            # tokenisation pass so Pass 2 has ``chunk_tokens``.
            iters = 1

        run_detection = refresh_ngram in ("append", "replace")
        for iteration in range(iters):
            _emit(
                "ngram_iter_start",
                iter=iteration + 1,
                iters=iters,
                chunks=total_scanned_chunks,
                phrases=len(auto_dict),
            )
            jieba_tok = _new_jieba_tokenizer(auto_dict.keys())
            phrase_set = set(auto_dict.keys())
            chunk_tokens = {}
            line_token_lists: List[List[str]] = []
            chunks_done = 0
            for entry in docs:
                doc_id, rel_path, path, is_md, chunks = entry[:5]
                for chunk in chunks:
                    toks_pos = tokenize_hybrid_with_pos(chunk.text, jieba_tok)
                    toks = merge_auto_phrases(
                        toks_pos, phrase_set, max_n=max_ngram_n
                    )
                    chunk_tokens[(doc_id, chunk.chunk_id)] = toks
                    if toks:
                        line_token_lists.append(toks)
                    chunks_done += 1
                    _emit(
                        "ngram_iter_advance",
                        iter=iteration + 1,
                        iters=iters,
                        current=chunks_done,
                        total=total_scanned_chunks,
                    )
            if run_detection:
                detected = detect_ngram_phrases(
                    line_token_lists, max_n=max_ngram_n,
                )
                for d_entry in detected:
                    auto_dict[d_entry["phrase"]] = d_entry
            stats.ngram_iters_run = iteration + 1
            if progress is not None:
                progress(
                    f"ngram iter {iteration + 1}/{iters}: "
                    f"{len(auto_dict)} phrases, {len(chunk_tokens)} chunks tokenised"
                )
            _emit(
                "ngram_iter_end",
                iter=iteration + 1,
                iters=iters,
                chunks=len(chunk_tokens),
                phrases=len(auto_dict),
            )
        # Persist dictionary. In ``refresh_ngram="never"`` we still call
        # this: the dict was loaded from DB unchanged and re-persisting
        # is a no-op (INSERT OR REPLACE on the same rows).
        _persist_auto_dict(conn, scope_dir_id, auto_dict.values(), now)
        stats.ngram_phrases_written = len(auto_dict)
        stats.ngram_dict_size = len(auto_dict)
        stats.ngram_dict_added = max(0, len(auto_dict) - prior_dict_size)

        # ------------------------------------------------------------------
        # Pass 2: materialise chunks / keyword_chunks / heading_docs for
        # the docs list. In build mode we replace the per-doc rows first
        # (chunk counts can shrink on edit); rebuild already cleared the
        # whole scope above.
        # ------------------------------------------------------------------
        if mode == "build" and docs:
            _delete_doc_index_rows_keep_docs(
                conn, [entry[0] for entry in docs]
            )

        _emit("materialise_start", total_docs=len(docs))
        for doc_index, entry in enumerate(docs, start=1):
            doc_id, rel_path, path, is_md, chunks = entry[:5]
            _, st_size, st_mtime, new_hash = entry[5:]
            per_chunk_kw: List[Tuple[int, Dict[str, int]]] = []
            file_distinct_kws: set = set()
            for chunk in chunks:
                toks = chunk_tokens.get((doc_id, chunk.chunk_id), [])
                kw_counts = _keywords_from_tokens(toks) if toks else Counter()
                if kw_counts:
                    per_chunk_kw.append((chunk.chunk_id, kw_counts))
                    file_distinct_kws.update(kw_counts.keys())
                else:
                    per_chunk_kw.append((chunk.chunk_id, {}))
            kw_id_map = (
                interner.kw_ids(sorted(file_distinct_kws))
                if file_distinct_kws else {}
            )

            chunk_rows = []
            kw_chunk_rows = []
            file_had_keywords = False
            for chunk, (_probe, kw_counts) in zip(chunks, per_chunk_kw):
                chunk_rows.append(
                    (doc_id, chunk.chunk_id,
                     chunk.start_line, chunk.end_line, chunk.char_count)
                )
                if not kw_counts:
                    continue
                file_had_keywords = True
                for kw, occ in kw_counts.items():
                    kw_chunk_rows.append(
                        (kw_id_map[kw], doc_id, chunk.chunk_id, int(occ))
                    )

            if chunk_rows:
                conn.executemany(
                    """
                    INSERT INTO chunks
                        (doc_id, chunk_id, start_line, end_line, char_count)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(doc_id, chunk_id) DO UPDATE SET
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
                        (kw_id, doc_id, chunk_id, occurrence_count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(kw_id, doc_id, chunk_id) DO UPDATE SET
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
                        (doc_id, heading_path_key, chapter.chapter_id,
                         int(chapter.start_line), int(chapter.end_line), now)
                    )
                if head_rows:
                    conn.executemany(
                        """
                        INSERT INTO heading_docs
                            (doc_id, heading_path, chapter_id,
                             start_line, end_line, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(doc_id, heading_path, chapter_id)
                        DO UPDATE SET
                            start_line = excluded.start_line,
                            end_line = excluded.end_line,
                            updated_at = excluded.updated_at
                        """,
                        head_rows,
                    )
                    stats.heading_docs_written += len(head_rows)
                    stats.md_files_indexed += 1

            # v7 metadata: refresh docs.size/mtime/content_hash so the
            # next incremental build's Level 1 check can skip this file.
            _update_doc_metadata(conn, doc_id, st_size, st_mtime, new_hash)

            if progress is not None and stats.files_indexed % 25 == 0 and stats.files_indexed:
                progress(
                    f"materialised {stats.files_indexed} files "
                    f"({stats.chunks_written} chunks, {stats.keyword_chunks_written} kw rows)"
                )
            _emit(
                "materialise_advance",
                current=doc_index,
                total=len(docs),
                path=str(path),
            )

        # Metadata-only refresh (touch / stable rga output).
        for doc_id, st_size, st_mtime, new_hash in metadata_only_updates:
            _update_doc_metadata(conn, doc_id, st_size, st_mtime, new_hash)

        # Remove docs that disappeared from disk.
        if removed_doc_ids:
            _delete_doc_index_rows(conn, removed_doc_ids)

        # ``docs_added`` was already incremented per-file in the scan loop
        # for both modes (in ``rebuild`` mode ``existing_docs`` is empty
        # so every readable file classifies as ``DOC_ADDED``). No mode
        # override needed — the per-file counts are the authoritative
        # accounting and they include ``no_keywords`` files whose docs
        # rows do exist in the DB. Docstring at line 121 documents this.

        # ------------------------------------------------------------------
        # Pass 3: derived layer. Recompute keyword_pairs from the DB
        # (single SQL scan + npmi arithmetic; see design §6). Uniform
        # across both modes so unchanged docs' contributions are always
        # accounted for.
        #
        # Skip the recompute entirely on a no-op incremental build (no
        # added / updated / removed docs): the pair table already matches
        # the unchanged index layer, so a full clear+rebuild would be
        # wasted work — a no-op ``build`` should be near-free. Report the
        # existing pair count so stats stay accurate.
        # ------------------------------------------------------------------
        index_layer_changed = bool(docs) or bool(removed_doc_ids) or mode == "rebuild"
        if index_layer_changed:
            candidate_count = int(conn.execute(
                "SELECT COUNT(DISTINCT kw_id) FROM keyword_chunks kc "
                "JOIN docs d ON d.id = kc.doc_id WHERE d.dir_id = ?",
                (scope_dir_id,),
            ).fetchone()[0])
            _emit(
                "pairs_start",
                candidate_pairs=candidate_count,
                surviving_pairs=0,
            )
            stats.pairs_written = recompute_pairs(
                conn, scope_dir_id, min_pair_count=min_pair_count, now=now,
                on_event=on_event,
            )
        else:
            # No index-layer change: keep the existing pairs untouched and
            # report their current count.
            stats.pairs_written = int(conn.execute(
                "SELECT COUNT(*) FROM keyword_pairs "
                "WHERE dir_id = ? AND source = ?",
                (scope_dir_id, CHUNK_COOCCUR_SOURCE),
            ).fetchone()[0])

        # ------------------------------------------------------------------
        # Toolchain fingerprint: emit a warning if it changed vs. the
        # previous build recorded in ``meta``. Never a judgement input;
        # design §7.
        # ------------------------------------------------------------------
        current_fp = _compute_toolchain_fingerprint()
        prev_fp = _read_meta(conn, "toolchain_fingerprint")
        if prev_fp is not None and prev_fp != current_fp:
            stats.warnings.append(
                "toolchain fingerprint changed since last build; "
                "if rga-processed files show stale extractions, "
                "run `grga index rebuild`."
            )
        _write_meta(conn, "toolchain_fingerprint", current_fp)

        conn.commit()
    finally:
        conn.close()

    stats.elapsed_seconds = time.perf_counter() - started
    _emit("pairs_done", pairs_written=stats.pairs_written)
    _emit("done", stats=stats.to_dict())
    return stats


# ---------------------------------------------------------------------------
# keyword_pairs full recompute (schema v7 derived layer).
#
# Design: docs/design/plan-2026-07-11-build-vs-rebuild.md §6.
# The pair table is derived (not asset): every build / rebuild / forget
# rebuilds it from the current keyword_chunks + chunks contents so it stays
# strictly consistent with the index layer.  ``llm_promoted`` rows are not
# touched.
# ---------------------------------------------------------------------------


def recompute_pairs(
    conn,
    scope_dir_id: int,
    *,
    min_pair_count: int = DEFAULT_MIN_PAIR_COUNT,
    now: Optional[str] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> int:
    """Rematerialise ``keyword_pairs(chunk_cooccur)`` for one scope from DB.

    Uses SQL aggregation end-to-end (design §6): a self-join on
    ``keyword_chunks`` groups by ``(a.kw_id, b.kw_id)`` with
    ``a.kw_id < b.kw_id`` to enumerate distinct pairs, sparsifies via
    ``HAVING COUNT(*) >= min_pair_count``, and a separate scan produces
    ``chunk_df`` and ``n_chunks``. This keeps the intermediate row set
    inside SQLite instead of materialising every keyword_chunks row
    into Python — the old in-Python path was O(rows) memory and had a
    quadratic per-chunk pair enumeration in pure Python.

    ``on_event`` (optional) receives fine-grained sub-phase events:
    ``pairs_scan_start`` / ``pairs_scan_end`` bracket the heavy
    self-join; ``pairs_advance`` fires during NPMI computation +
    insert with ``current`` / ``total`` fields.

    Returns the number of pair rows written. Caller owns the transaction.
    """
    if now is None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _emit(phase: str, **fields) -> None:
        if on_event is None:
            return
        payload = {"phase": phase}
        payload.update(fields)
        try:
            on_event(payload)
        except Exception:
            pass

    # 1. Clear chunk_cooccur rows for this scope (llm_promoted survives).
    conn.execute(
        "DELETE FROM keyword_pairs WHERE dir_id = ? AND source = ?",
        (scope_dir_id, CHUNK_COOCCUR_SOURCE),
    )

    # 2. chunk_df — one row per keyword giving the number of distinct
    #    chunks that contain it. ``keyword_chunks`` primary key is
    #    ``(kw_id, doc_id, chunk_id)`` so COUNT(*) grouping by kw_id is
    #    already the per-kw distinct-chunk count.
    chunk_df: Dict[int, int] = {
        int(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT kc.kw_id, COUNT(*) FROM keyword_chunks kc "
            "JOIN docs d ON d.id = kc.doc_id "
            "WHERE d.dir_id = ? GROUP BY kc.kw_id",
            (scope_dir_id,),
        ).fetchall()
    }
    if not chunk_df:
        return 0

    # 3. n_chunks — chunks that contain at least one keyword (matches
    #    the pre-refactor ``n_chunks_with_keywords`` denominator).
    n_chunks = int(conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM keyword_chunks kc "
        "JOIN docs d ON d.id = kc.doc_id WHERE d.dir_id = ? "
        "GROUP BY kc.doc_id, kc.chunk_id)",
        (scope_dir_id,),
    ).fetchone()[0])
    if n_chunks == 0:
        return 0

    # 4. Pair counts — self-join keyword_chunks on same chunk, restrict
    #    to ``a.kw_id < b.kw_id`` to enumerate each unordered pair
    #    exactly once, aggregate + sparsify in SQL. This keeps peak
    #    memory bounded by the surviving pair count, not the raw
    #    keyword_chunks row count.
    _emit("pairs_scan_start")
    surviving = conn.execute(
        "SELECT a.kw_id, b.kw_id, COUNT(*) AS cooccur "
        "FROM keyword_chunks a "
        "JOIN keyword_chunks b "
        "  ON a.doc_id = b.doc_id AND a.chunk_id = b.chunk_id "
        "  AND a.kw_id < b.kw_id "
        "JOIN docs d ON d.id = a.doc_id "
        "WHERE d.dir_id = ? "
        "GROUP BY a.kw_id, b.kw_id "
        "HAVING COUNT(*) >= ?",
        (scope_dir_id, int(min_pair_count)),
    ).fetchall()
    _emit("pairs_scan_end", surviving=len(surviving))
    if not surviving:
        return 0

    total_surviving = len(surviving)
    pair_rows = []
    step = max(1, total_surviving // 200)
    for idx, row in enumerate(surviving, start=1):
        a = int(row[0])
        b = int(row[1])
        c = int(row[2])
        npmi = _npmi(c, chunk_df.get(a, 0), chunk_df.get(b, 0), n_chunks)
        pair_rows.append((scope_dir_id, a, b, float(c), npmi, now))
        if idx == total_surviving or idx % step == 0:
            _emit(
                "pairs_advance",
                current=idx,
                total=total_surviving,
            )

    conn.executemany(
        """
        INSERT INTO keyword_pairs
            (dir_id, kw_a_id, kw_b_id, source,
             cooccur_count, npmi, updated_at)
        VALUES (?, ?, ?, 'chunk_cooccur', ?, ?, ?)
        ON CONFLICT(dir_id, kw_a_id, kw_b_id, source) DO UPDATE SET
            cooccur_count = excluded.cooccur_count,
            npmi = excluded.npmi,
            updated_at = excluded.updated_at
        """,
        pair_rows,
    )
    return len(pair_rows)


# ---------------------------------------------------------------------------
# forget() — asset-layer removal + cascade (schema v7).
# Design: docs/design/plan-2026-07-11-build-vs-rebuild.md §2.1 + §3.3-3.4.
# ---------------------------------------------------------------------------


@dataclass
class ForgetStats:
    """Summary produced by :func:`forget_scope_phrases`."""

    scope: str
    ngram_dict_removed: int = 0
    keyword_chunks_removed: int = 0
    pairs_recomputed: int = 0
    phrases_missing: List[str] = field(default_factory=list)
    all_wiped: bool = False
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "ngram_dict_removed": self.ngram_dict_removed,
            "keyword_chunks_removed": self.keyword_chunks_removed,
            "pairs_recomputed": self.pairs_recomputed,
            "phrases_missing": list(self.phrases_missing),
            "all_wiped": self.all_wiped,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
        }


def forget_scope_phrases(
    scope: Union[str, Path],
    *,
    db_path: Union[str, Path, None] = None,
    phrases: Optional[Sequence[str]] = None,
    all_phrases: bool = False,
    min_pair_count: int = DEFAULT_MIN_PAIR_COUNT,
) -> ForgetStats:
    """Remove entries from the asset layer with cascading cleanup.

    Two modes (mutually exclusive):

    * ``phrases=[...]`` — remove those specific phrases from
      ``auto_ngram_dict``. For each phrase, delete the matching
      ``keyword_chunks`` rows in this scope and the ``keyword_pairs``
      edges with that phrase on either endpoint (``source='chunk_cooccur'``
      only; ``llm_promoted`` is a separate asset). After cascades, rerun
      :func:`recompute_pairs` because npmi depends on scope-global
      ``chunk_df`` / ``n_chunks``.

    * ``all_phrases=True`` — nuke every asset row for this scope:
      the whole ``auto_ngram_dict`` + the index layer three tables +
      ``chunk_cooccur`` pairs. Equivalent to the legacy
      ``build --replace`` reset step (does NOT re-run a build). Follow
      with ``grga index build`` to relearn from scratch.

    ``phrases`` that do not exist in ``auto_ngram_dict`` for this scope
    are collected into ``ForgetStats.phrases_missing`` and skipped
    silently — this makes the CLI safe to run repeatedly.
    """
    if bool(phrases) == bool(all_phrases):
        raise ValueError(
            "forget_scope_phrases: pass either phrases=[...] or "
            "all_phrases=True (exactly one)"
        )

    from grga.deep_search import DEFAULT_DB_PATH

    started = time.perf_counter()
    scope_path = Path(scope).expanduser().resolve()
    scope_key = str(scope_path)
    stats = ForgetStats(scope=scope_key)
    if all_phrases:
        stats.all_wiped = True

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        interner = IdInterner(conn)
        scope_dir_id_ro = interner.dir_id_ro(scope_key)
        if scope_dir_id_ro is None:
            # Scope was never indexed; nothing to do. Return empty stats
            # rather than raising so scripts are idempotent.
            stats.elapsed_seconds = time.perf_counter() - started
            return stats
        scope_dir_id = scope_dir_id_ro

        conn.execute("BEGIN IMMEDIATE")
        try:
            if all_phrases:
                # Wipe the whole asset + index + derived for this scope.
                doc_ids = [
                    int(row[0]) for row in conn.execute(
                        "SELECT id FROM docs WHERE dir_id = ?",
                        (scope_dir_id,),
                    ).fetchall()
                ]
                # Count what we are about to delete for observability.
                stats.keyword_chunks_removed = int(conn.execute(
                    "SELECT COUNT(*) FROM keyword_chunks "
                    "WHERE doc_id IN (SELECT id FROM docs WHERE dir_id = ?)",
                    (scope_dir_id,),
                ).fetchone()[0])
                stats.ngram_dict_removed = int(conn.execute(
                    "SELECT COUNT(*) FROM auto_ngram_dict WHERE dir_id = ?",
                    (scope_dir_id,),
                ).fetchone()[0])
                _delete_doc_index_rows(conn, doc_ids)
                conn.execute(
                    "DELETE FROM keyword_pairs "
                    "WHERE dir_id = ? AND source = ?",
                    (scope_dir_id, CHUNK_COOCCUR_SOURCE),
                )
                conn.execute(
                    "DELETE FROM auto_ngram_dict WHERE dir_id = ?",
                    (scope_dir_id,),
                )
                stats.pairs_recomputed = 0
            else:
                # Per-phrase cascade. Resolve kw_ids in one batch.
                phrase_list = list(dict.fromkeys(phrases or []))
                if not phrase_list:
                    conn.execute("COMMIT")
                    stats.elapsed_seconds = time.perf_counter() - started
                    return stats
                # keywords are stored casefolded; auto_ngram_dict phrases
                # are stored as-detected (mixed CJK + case). Look up both
                # sides case-insensitively so ``forget --phrase pdc主席``
                # matches a stored ``PDC主席`` — otherwise the cascade
                # would fire (casefolded kw_id resolves) but the asset
                # row would silently survive, leaving an orphaned dict
                # entry that resurfaces on the next build.
                kw_lookup = interner.kw_ids_ro([p.casefold() for p in phrase_list])
                # Detect phrases present in the dict for this scope by
                # LOWER(phrase) match, then remember the *actual stored
                # casings* so the DELETE targets exactly those rows.
                lowered_inputs = [p.casefold() for p in phrase_list]
                placeholders = ",".join("?" for _ in lowered_inputs)
                dict_present_pairs = conn.execute(
                    f"SELECT phrase FROM auto_ngram_dict "
                    f"WHERE dir_id = ? AND LOWER(phrase) IN ({placeholders})",
                    [scope_dir_id, *lowered_inputs],
                ).fetchall()
                dict_present = {str(row[0]) for row in dict_present_pairs}
                dict_present_lowered = {p.casefold() for p in dict_present}
                for phrase in phrase_list:
                    cf = phrase.casefold()
                    if cf not in dict_present_lowered and cf not in kw_lookup:
                        stats.phrases_missing.append(phrase)
                # Cascade delete keyword_chunks rows for the matching kw
                # ids in this scope.
                target_kw_ids = [
                    kw_lookup[p.casefold()] for p in phrase_list
                    if p.casefold() in kw_lookup
                ]
                if target_kw_ids:
                    kw_placeholders = ",".join("?" for _ in target_kw_ids)
                    stats.keyword_chunks_removed = int(conn.execute(
                        f"SELECT COUNT(*) FROM keyword_chunks "
                        f"WHERE kw_id IN ({kw_placeholders}) "
                        f"AND doc_id IN (SELECT id FROM docs WHERE dir_id = ?)",
                        [*target_kw_ids, scope_dir_id],
                    ).fetchone()[0])
                    conn.execute(
                        f"DELETE FROM keyword_chunks "
                        f"WHERE kw_id IN ({kw_placeholders}) "
                        f"AND doc_id IN (SELECT id FROM docs WHERE dir_id = ?)",
                        [*target_kw_ids, scope_dir_id],
                    )
                    conn.execute(
                        f"DELETE FROM keyword_pairs "
                        f"WHERE dir_id = ? AND source = ? "
                        f"AND (kw_a_id IN ({kw_placeholders}) "
                        f"OR kw_b_id IN ({kw_placeholders}))",
                        [scope_dir_id, CHUNK_COOCCUR_SOURCE,
                         *target_kw_ids, *target_kw_ids],
                    )
                # Delete the asset-layer rows.
                if dict_present:
                    dict_list = sorted(dict_present)
                    dp = ",".join("?" for _ in dict_list)
                    conn.execute(
                        f"DELETE FROM auto_ngram_dict "
                        f"WHERE dir_id = ? AND phrase IN ({dp})",
                        [scope_dir_id, *dict_list],
                    )
                    stats.ngram_dict_removed = len(dict_list)
                # Recompute pairs (n_chunks / chunk_df changed).
                stats.pairs_recomputed = recompute_pairs(
                    conn, scope_dir_id, min_pair_count=min_pair_count,
                )

            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
            raise
    finally:
        conn.close()

    stats.elapsed_seconds = time.perf_counter() - started
    return stats


# ---------------------------------------------------------------------------
# Toolchain fingerprint (schema v7 meta table).
# Design: docs/design/plan-2026-07-11-build-vs-rebuild.md §4.2 + §7.
# Only ever produces a warning; never a judgement input.
# ---------------------------------------------------------------------------


def _binary_version(binary: str, args: Sequence[str] = ("--version",)) -> str:
    """Return the first-line version output of ``binary``, or ``''``.

    Best-effort: any exception (missing binary, timeout, non-zero exit)
    resolves to the empty string so ``toolchain_fingerprint`` degrades to
    "ignore this component" rather than crashing the build.
    """
    path = shutil.which(binary)
    if not path:
        return ""
    try:
        result = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except Exception:
        return ""
    first_line = (result.stdout or result.stderr or "").splitlines()
    return first_line[0].strip() if first_line else ""


def _compute_toolchain_fingerprint() -> str:
    """Return a stable digest over the versions of every external tool.

    Included: ``rga``, ``pandoc``, ``pdftotext``, ``rga-preproc``. Order
    is fixed so a re-run on the same machine yields the same hash. Any
    binary that is missing contributes an empty string, which is still
    stable across runs. See design §4.2.
    """
    parts = [
        _binary_version("rga"),
        _binary_version("pandoc"),
        _binary_version("pdftotext", args=("-v",)),
        _binary_version("rga-preproc"),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _read_meta(conn, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (key,)
    ).fetchone()
    return None if row is None else str(row[0])


def _write_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )

