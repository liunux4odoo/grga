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
import os
import re
import shutil
import sqlite3
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
from grga.graph.schema import (
    BUILD_MIN_NPMI,
    BUILD_MIN_PAIR_COUNT,
    CHUNK_COOCCUR_SOURCE,
    connect,
    ensure_schema,
    resolve_scope_name,
)

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
# Progress tick granularity for detect_ngram_phrases. 2048 keeps callback
# overhead <1% on 1M-line corpora while still updating every ~50-200 ms
# for a spinner.
_PROGRESS_TICK = 2048

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

# Index-layer stopword filter (plan-2026-07-12-related-perf §2.6 / P1.1).
# Detection and index used to consume separate stopword sets — detection
# had a 26-char CJK boundary set (``_CORE_STOP_BOUNDARIES``) purpose-built
# for n-gram edge cutting, while the index re-used the tokenizer-era
# 14-item Chinese filler list above. After the v6 schema migration, index
# ended up letting hub function words (在/是/也/就/但/这/有/都/后/上/...)
# through, and they dominated PPR neighbourhoods on the read path. The
# union re-uses the *existing* 26-char structural-particle set — no new
# corpus prior (HC2), just a widened subscription. Detection still owns
# ``_CORE_STOP_BOUNDARIES`` unchanged (it must not swallow 面壁**者** or
# 破壁**人**), so the two-layer semantic split is preserved.
#
# 单字构词后缀（者/性/化/式/...）刻意不加入 _INDEX_STOPWORDS —— 全局静态
# 表无法区分「特定语料里它就是一个概念」（太极拳教程里的『化』/化工语料
# 里的『氯』）和「它是 tokenisation artefact」两种情况。这一层由
# _derive_scope_stopword_candidates + _filter_chunk_kws_for_pairs 在
# build 期语料自适应地决定，见 Pass 1.5 / Pass 3。
_INDEX_STOPWORDS = _STOPWORDS | _CORE_STOP_BOUNDARIES

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

# Sentinel that requests scope-adaptive resolution of ``min_pair_count`` —
# see :func:`resolve_min_pair_count`. Kept as a public constant so tests
# and diagnostic tools can spell the intent explicitly.
ADAPTIVE_MIN_PAIR_COUNT = None


def resolve_min_pair_count(n_chunks: int) -> int:
    """Stepwise adaptive floor for ``min_pair_count`` given scope size.

    A fixed ``min_pair_count=5`` starves small corpora (a novella of ~200
    chunks has few pairs clearing 5) while adding no value on the large
    end (real strong pairs there co-occur hundreds of times). Since v3.2
    the write-time floor scales with ``scope_stats.n_chunks``:

    ======  ==========
    chunks  min_pair_count
    ======  ==========
    ≤ 200   2
    ≤ 2000  3
    ≤ 20000 4
    > 20000 5
    ======  ==========

    The step function (not a smooth curve) is deliberate: it is easy to
    reason about, easy to grep in logs, and makes the migration diff from
    the pre-v3.2 fixed floor obvious. Corpus size boundaries at 10× are
    chosen for the same reason.

    NPMI floor stays scope-independent (see :data:`BUILD_MIN_NPMI`): npmi
    is already a probabilistic normalization; scaling its threshold with
    corpus size would double-count the size dependence.

    The chunk-level independence gate (``df_a == df_b == co``, i.e. the two
    words are perfectly glued) applied in
    :func:`_finalize_pairs_from_temp` is orthogonal to this and always on —
    it targets the npmi=1.0 degenerate case caused by "two words that
    always co-occur", which is a *frequency-independent* noise source and
    cannot be defended against by a pair-count floor alone.
    """
    if n_chunks <= 200:
        return 2
    if n_chunks <= 2000:
        return 3
    if n_chunks <= 20000:
        return 4
    return 5

# 3a threshold (2026-07-13): single-CJK-char tokens whose chunk-df ratio
# exceeds this become *candidates* for the pair-time containment filter
# (:func:`_filter_chunk_kws_for_pairs`). Not a global stopword table —
# every candidate still needs per-chunk structural evidence to be dropped.
# 0.15 is a conservative starting point (a truly functional morpheme like
# 者/性 in natural Chinese easily appears in ≫15% of chunks; a domain
# concept like 氯 in a chemistry corpus typically does not). Expose as a
# CLI flag only when real corpora show the constant needs tuning.
STOPWORD_DF_THRESHOLD = 0.15

# Below this many chunks the chunk-df ratio is statistically meaningless:
# with e.g. n_chunks=6 the ``df_threshold * n_chunks`` limit drops below 1,
# so *every* single CJK char that appears once would be flagged. A tiny
# scope has no basis for corpus-adaptive inference, so we skip 3a entirely
# (returning an empty candidate set). 3b then has nothing to gate — pairs
# pass through unfiltered, which is the safe behaviour for sparse data.
STOPWORD_MIN_CHUNKS = 20

# Pair-staging TEMP TABLE batching (v9.1). Pass 2 stages one
# (doc_id, chunk_id, kw_id) row per post-3b-filter kw per chunk into
# ``_build_chunk_kws``. Rows accumulate in a Python list and get bulk
# INSERTed every ``CHUNK_KW_TEMP_FLUSH`` rows so peak RAM stays bounded.
# 200k rows × (~30 B per Python tuple) ≈ 6 MB list peak — small enough to
# be dwarfed by SQLite's own page cache, large enough to amortise the
# executemany overhead across many chunks. Referenced inline in
# :func:`build_graph_index`; not a stable API.


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
    chunks_written: int = 0  # v9: always 0 (chunks table dropped); kept
                             # for JSON contract stability across v7/v8/v9
                             # readers. New readers should use scope_stats.
                             # n_chunks or docs.n_chunks_with_kw instead.
    keyword_chunks_written: int = 0
    pairs_written: int = 0
    # v8 (plan-2026-07-12-keyword-primary.md §3): rows populated in the
    # keyword-primary asset tables during this build. ``scope_stats`` is
    # always exactly 1 (a scalar per scope), so we only report
    # keyword_stats explicitly.
    keyword_stats_written: int = 0
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
            "keyword_stats_written": self.keyword_stats_written,
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
    dropped (see :data:`_INDEX_STOPWORDS`). Keywords are stored casefolded
    (matches ``keyword_chunks``).
    """
    counts: Counter = Counter()
    for match in _TOKEN_RE.finditer(text):
        key = match.group(0).casefold()
        if key in _INDEX_STOPWORDS:
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
        if key in _INDEX_STOPWORDS:
            continue
        counts[key] += 1
    return counts


# ---------------------------------------------------------------------------
# Scope-local stopword handling (2026-07-13). A single CJK character like
# 者/性/化 cannot go in the global _INDEX_STOPWORDS: whether it is a bare
# function morpheme or a genuine domain concept depends entirely on the
# corpus (『化』 is a stopword in generic docs but a core term in a 太极拳
# tutorial; 『氯』 is meaningful in a chemistry corpus). So we derive a
# per-scope *candidate* set from chunk-df statistics (3a) and only drop a
# candidate from pair enumeration when *this chunk* also contains a longer
# keyword that has the candidate as a substring (3b) — i.e. structural
# evidence that the standalone char is a tokenisation artefact. keyword_stats
# keeps the raw counts untouched; only keyword_pairs is gated.
# ---------------------------------------------------------------------------


def _is_single_cjk(tok: str) -> bool:
    """True when ``tok`` is exactly one CJK Unified Ideograph."""
    return len(tok) == 1 and "\u4e00" <= tok <= "\u9fff"


def _derive_scope_stopword_candidates_from_df(
    df: Counter,
    n_chunks: int,
    *,
    df_threshold: float = STOPWORD_DF_THRESHOLD,
    min_chunks: int = STOPWORD_MIN_CHUNKS,
) -> frozenset:
    """Streaming variant of :func:`_derive_scope_stopword_candidates`.

    Takes a precomputed chunk-df ``Counter`` (single-CJK-char keys,
    already deduplicated per chunk) plus an explicit ``n_chunks`` count.
    Callers that stream through the corpus once (see Phase B in
    :func:`build_graph_index`) accumulate ``df`` directly and skip the
    O(corpus) ``chunk_tokens`` dict materialisation.

    Returns the same frozenset as the dict-based variant for identical
    inputs. Kept as a separate function so the dict-based API stays
    stable for its unit-test callers.
    """
    if n_chunks < min_chunks:
        return frozenset()
    limit = df_threshold * n_chunks
    return frozenset(t for t, c in df.items() if c > limit)


def _derive_scope_stopword_candidates(
    chunk_tokens: Dict[tuple, List[str]],
    *,
    df_threshold: float = STOPWORD_DF_THRESHOLD,
    min_chunks: int = STOPWORD_MIN_CHUNKS,
) -> frozenset:
    """Return single-CJK-char tokens whose chunk-df ratio exceeds threshold.

    "chunk-df" counts the number of chunks that contain the token at least
    once; the ratio is ``df / n_chunks``. Only single CJK characters are
    considered — multi-char tokens are trusted, ASCII tokens are handled by
    the static stopword list. The returned set is a *candidate* set for the
    3b containment filter (see :func:`_filter_chunk_kws_for_pairs`); it does
    not filter ``keyword_stats``.

    Small scopes (``n_chunks < min_chunks``) short-circuit to an empty set:
    the chunk-df ratio has no statistical power at that size, and 3b's
    substring evidence is a weaker safety net when the candidate set is
    saturated. Skipping 3a entirely is the safe behaviour for sparse data.

    Since PR-4 this is a thin wrapper over
    :func:`_derive_scope_stopword_candidates_from_df` — the build pipeline
    uses the streaming variant directly. This one stays for unit tests and
    any external caller with a chunk_tokens dict in hand.
    """
    n_chunks = len(chunk_tokens)
    if n_chunks < min_chunks:
        return frozenset()
    df: Counter = Counter()
    for toks in chunk_tokens.values():
        seen = {t.casefold() for t in toks if _is_single_cjk(t)}
        for t in seen:
            df[t] += 1
    return _derive_scope_stopword_candidates_from_df(
        df, n_chunks, df_threshold=df_threshold, min_chunks=min_chunks,
    )


def _filter_chunk_kws_for_pairs(
    chunk_kws: Sequence[str],
    *,
    stopword_candidates: frozenset,
) -> List[str]:
    """Drop stopword candidates that are substrings of other kws in the chunk.

    Implements 3b: a scope-local stopword candidate is only treated as a
    tokenisation artefact when *this specific chunk* also holds a longer
    keyword that contains it (e.g. 面壁者 present ⇒ standalone 者 dropped).
    A candidate with no containing sibling in this chunk is kept — it may be
    a domain concept in isolation (e.g. 氯 alongside 钠 but no 氯化钠 here).
    Non-candidate keywords are always kept.
    """
    if not stopword_candidates:
        return list(chunk_kws)
    # Candidates from :func:`_derive_scope_stopword_candidates` are always
    # single CJK chars — a single char can only be contained by a strictly
    # longer kw. Pre-filter the substring pool to skip len==1 kws; this
    # both trims scan cost and lets us drop the ``kw != other`` guard.
    containing_pool = [k for k in chunk_kws if len(k) > 1]
    survivors: List[str] = []
    for kw in chunk_kws:
        if kw not in stopword_candidates:
            survivors.append(kw)
            continue
        if not any(kw in other for other in containing_pool):
            survivors.append(kw)
    return survivors


# ---------------------------------------------------------------------------
# Unsupervised n-gram phrase detection (frequency + NPMI + branch entropy).
# ---------------------------------------------------------------------------

def detect_ngram_phrases(
    line_tokens: "Iterable[Iterable[str]]",
    *,
    max_n: int = DEFAULT_MAX_NGRAM_N,
    min_count: int = _NGRAM_MIN_COUNT,
    npmi_threshold: float = _NGRAM_NPMI_THRESHOLD,
    ent_threshold: float = _NGRAM_ENT_THRESHOLD,
    stop_boundaries: frozenset = _CORE_STOP_BOUNDARIES,
    on_progress: Optional[Callable[[str, dict], None]] = None,
    conn: Optional[sqlite3.Connection] = None,
    n_lines_hint: Optional[int] = None,
) -> List[dict]:
    """Detect CJK compound phrases from tokenised lines.

    Three pure-mathematical gates (no semantic prior, AGENTS.md 铁律 2):

    * frequency: ``count >= min_count``
    * solidity: ``min over splits { NPMI(left, right) }`` (Bouma 2009); a
      single N (total token count) keeps NPMI bounded in ``[-1, 1]`` for
      every split, including n>2 min-split.
    * branch entropy: ``min(H_left, H_right) >= ent_threshold`` (nat).

    Substring salvage: a window that clears frequency + solidity but fails
    *only* the branch-entropy gate is kept anyway when (a) its rejoined text
    is a proper substring — prefix, infix, or suffix — of some phrase that
    passed all three gates, **and** (b) it has at least one occurrence its
    containing survivor(s) cannot explain — an *independence margin*
    ``count(sub) - max(count(container)) >= 1``. This recovers standalone
    referents that are almost always glued to one neighbour yet still
    surface on their own (苍坤 appears 86× but only 80× inside 苍坤上人 ⇒
    right entropy ≈ 0, margin 6 ≥ 1 ⇒ kept) without loosening the gate
    globally, while boundary fragments that *only* ever appear inside the
    container (苍坤上 / 坤上人, margin = 0) stay rejected — they carry no
    independent co-occurrence evidence, so admitting them just bloats the
    vocab and pair tables.

    The margin uses ``max`` (not ``sum``) over containers: a smaller window
    is counted once per position, so its total count is ``>=`` any single
    container's count but can be ``<`` the sum when containers overlap;
    subtracting the largest container is the conservative floor that never
    over-subtracts and therefore never drops a genuinely independent sub. A
    sub reached through *two* distinct containers (苍坤 in both 苍坤上人 and
    苍坤真人) naturally clears the margin even with zero fully-standalone
    hits — combining with several neighbours is itself independence
    evidence. The check is otherwise textual + counting only: no semantic
    prior, no positional constraint (AGENTS.md 铁律 2). Salvaged entries
    keep their real (sub-threshold) ``branch_ent``. Frequency and solidity
    are never bypassed, so boundary-crossing fragments (器学 ⊂ 机器学习,
    killed on solidity) never become candidates.

    Windows may mix ASCII and CJK tokens (so hybrid entities like 艾AA /
    PDC主席 can be discovered). Detected phrases are rejoined by the
    adjacent-pair rule (CJK+anything -> no separator; ASCII+ASCII -> single
    space). Callers that use these phrases in a jieba user_dict only benefit
    from CJK-only ones (jieba sees only CJK runs), so mixed phrases exist
    for the post-tokenisation merge step (see :func:`merge_auto_phrases`).

    ``on_progress`` is an optional callback invoked with ``(phase, info)``
    at the boundaries of the internal stages: ``unigram``, ``window``,
    ``filter``, ``salvage``, ``sort``. The ``window``, ``filter`` and
    ``salvage`` stages also send per-tick updates every
    :data:`_PROGRESS_TICK` items so the caller can drive a spinner /
    progress bar. Passing ``None`` is a no-op.

    ``conn`` is an optional sqlite3.Connection. When provided, the
    aggregation of raw n-gram window occurrences moves into a
    ``CREATE TEMP TABLE`` + ``GROUP BY`` + ``HAVING`` pipeline whose peak
    memory is bounded by SQLite's cache (spills to disk via the caller's
    ``PRAGMA temp_store_directory``) rather than by three Python dicts
    (see docs/design/plan-2026-07-15-build-memory-oom.md §3 for the
    ~2 GB → ~100 MB Python-heap reduction and bit-exact equivalence
    proof). When ``conn is None`` a private ``:memory:`` connection is
    opened for the call — safe for tests / small inputs, but the
    ``temp_store_directory`` route is preferred for large corpora.

    ``line_tokens`` may be any ``Iterable[Iterable[str]]`` — a list (as
    tests pass) or a one-shot generator (as the streaming build pipeline
    passes to keep the token list off the Python heap). It is consumed
    exactly once. ``n_lines_hint`` supplies the progress-total when the
    input is a generator (``len()`` unavailable); it is cosmetic only.
    """
    def _tick(phase: str, **info) -> None:
        if on_progress is not None:
            on_progress(phase, info)

    # ``line_tokens`` may be a one-shot generator (PR-4 streaming path),
    # so avoid ``len()``. ``n_lines`` is cosmetic (progress totals only);
    # the empty-corpus guard uses ``sum(uni.values())`` which is exact.
    try:
        n_lines = len(line_tokens)  # type: ignore[arg-type]
    except TypeError:
        n_lines = n_lines_hint or 0
    _tick("unigram_start", lines=n_lines)

    _owns_conn = conn is None
    if _owns_conn:
        conn = sqlite3.connect(":memory:")

    try:
        return _detect_ngram_phrases_impl(
            conn, line_tokens,
            max_n=max_n, min_count=min_count,
            npmi_threshold=npmi_threshold, ent_threshold=ent_threshold,
            stop_boundaries=stop_boundaries, tick=_tick,
            n_lines=n_lines,
        )
    finally:
        # Always drop the TEMP tables so a shared connection doesn't
        # carry state into the next call. DROP TEMP TABLE is per-conn
        # metadata only, cheap.
        try:
            conn.execute("DROP TABLE IF EXISTS _ng_ev")
            conn.execute("DROP TABLE IF EXISTS _ng_passed")
        except sqlite3.Error:
            pass
        if _owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Unit separator for the ``_ng_ev.phrase_key`` column. U+001F cannot appear
# in jieba / regex output, giving a bijective tuple ↔ string mapping without
# the rejoin-rule ambiguity of ``_join_by_pair_rule``. Two windows with
# different token tuples but identical rejoined text (e.g. ``("章","北海")``
# vs ``("章北","海")`` if both were ever produced) keep distinct keys.
# ---------------------------------------------------------------------------
_NGRAM_KEY_SEP = "\x1f"

# Event-buffer batch size for the pass-1 executemany flush. 200k rows ×
# ~50 B / tuple ≈ 10 MB Python peak, small enough to be dwarfed by
# SQLite's own cache. Not user-tunable — it's a performance micro-knob
# not a correctness one.
_NGRAM_EVENT_FLUSH = 200_000


def _detect_ngram_phrases_impl(
    conn: sqlite3.Connection,
    line_tokens: List[List[str]],
    *,
    max_n: int,
    min_count: int,
    npmi_threshold: float,
    ent_threshold: float,
    stop_boundaries: frozenset,
    tick: Callable[..., None],
    n_lines: int,
) -> List[dict]:
    """Body of :func:`detect_ngram_phrases`. See docstring there.

    Stages (per SPEC §2.2):

    1. Stream ``(phrase_key, n, left_tok, right_tok)`` events + unigrams
       in one scan of ``line_tokens``.
    2. SQL ``GROUP BY ... HAVING COUNT(*) >= min_count`` over ``_ng_ev``
       — only freq-passing windows come back to Python. Note that every
       *split* of a freq-passing window also passes freq (each container
       occurrence carries one split occurrence), so this smaller dict is
       sufficient for :func:`_npmi_pair` lookups later.
    3. Solidity gate (Python; reads the post-freq dict).
    4. Fetch left/right neighbour distributions for solidity-passing
       phrases only, via a JOIN against a small ``_ng_passed`` table.
    5. Branch-entropy gate (Python; input is 100× smaller than before).
    6. Substring salvage (verbatim mainline logic).
    7. Sort + return (verbatim).
    """
    # --- Stage 0: fresh TEMP TABLE (in case caller reuses conn) ---
    conn.execute("DROP TABLE IF EXISTS _ng_ev")
    conn.execute(
        "CREATE TEMP TABLE _ng_ev ("
        "  phrase_key TEXT NOT NULL, n INTEGER NOT NULL, "
        "  left_tok TEXT, right_tok TEXT"
        ")"
    )

    # --- Stage 1: stream events + unigram counts (single pass) ---
    tick("window_start", lines=n_lines, max_n=max_n)
    uni: Counter = Counter()
    buf: List[Tuple[str, int, Optional[str], Optional[str]]] = []
    for idx, toks in enumerate(line_tokens):
        uni.update(toks)
        L = len(toks)
        for i in range(L):
            for n in range(2, max_n + 1):
                if i + n > L:
                    break
                win = tuple(toks[i:i + n])
                if any(t in stop_boundaries for t in win):
                    continue
                left = toks[i - 1] if (i > 0 and toks[i - 1] not in stop_boundaries) else None
                right = toks[i + n] if (i + n < L and toks[i + n] not in stop_boundaries) else None
                buf.append((_NGRAM_KEY_SEP.join(win), n, left, right))
                if len(buf) >= _NGRAM_EVENT_FLUSH:
                    conn.executemany(
                        "INSERT INTO _ng_ev VALUES (?, ?, ?, ?)", buf
                    )
                    buf.clear()
        if idx % _PROGRESS_TICK == 0:
            tick("unigram_advance", current=idx, total=n_lines)
            # ``windows`` field intentionally omitted: the SQL backend
            # doesn't materialise the pre-freq window count in Python (it
            # lives in ``_ng_ev`` on disk). Consumers should guard the
            # field's presence rather than expect a running count here.
            tick("window_advance", current=idx, total=n_lines)
    if buf:
        conn.executemany("INSERT INTO _ng_ev VALUES (?, ?, ?, ?)", buf)
        buf.clear()

    # ``unigram_end`` intentionally not emitted here: unigram counting
    # is now fused into the same loop as window enumeration (Stage 1), so
    # the two logically end together. Emitting a separate ``unigram_end``
    # after ``window_start`` used to flip the CLI's progress-bar note
    # back to "统计词频完成" mid-enumeration. ``window_end`` (below) is
    # the single end-of-Stage-1 marker; consumers that key on
    # ``unigram_end`` should switch to it. ``uni`` / ``vocab`` size is
    # available for the informational tail if needed.
    total = sum(uni.values())
    if total == 0:
        tick("empty", lines=n_lines)
        return []
    log_total = math.log(total)

    @lru_cache(maxsize=None)
    def _log_uni(t: str) -> float:
        c = uni.get(t, 0)
        return math.log(c) if c > 0 else math.nan

    @lru_cache(maxsize=None)
    def _log_c(c: int) -> float:
        return math.log(c)

    # --- Stage 2: freq aggregate + HAVING, ordered by first occurrence ---
    # ``ORDER BY MIN(rowid)`` reproduces the mainline dict insertion
    # order so downstream tie-breaks (final ``sort`` of equal npmi+count)
    # match Python-for-Python bit-exact.
    freq_rows = conn.execute(
        "SELECT phrase_key, n, COUNT(*) AS c "
        "FROM _ng_ev GROUP BY phrase_key, n "
        "HAVING c >= ? ORDER BY MIN(rowid)",
        (min_count,),
    ).fetchall()
    tick("window_end", windows=len(freq_rows))

    # Post-freq ngram_count. Contains every split's count too (splits of
    # freq-passers are themselves freq-passers — proof in the impl
    # docstring). Bounded by post-freq vocab, not raw window count.
    ngram_count: Dict[tuple, int] = {}
    for phrase_key, _n, c in freq_rows:
        ngram_count[tuple(phrase_key.split(_NGRAM_KEY_SEP))] = int(c)

    def _count(x) -> int:
        return uni.get(x, 0) if isinstance(x, str) else ngram_count.get(x, 0)

    def _log_count(x) -> float:
        if isinstance(x, str):
            return _log_uni(x)
        c = ngram_count.get(x, 0)
        return _log_c(c) if c > 0 else math.nan

    def _npmi_pair(a, b, joint_c: int) -> float:
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

    # --- Stage 3: solidity filter (freq already applied by HAVING) ---
    total_windows = len(freq_rows)
    tick("filter_start", windows=total_windows)
    solid_pass: List[Tuple[tuple, int, float]] = []
    for idx, (phrase_key, _n, c) in enumerate(freq_rows):
        if idx % _PROGRESS_TICK == 0:
            # ``solid_pass``: phrases past freq + solidity so far. The
            # old ``kept`` field counted phrases past all three gates —
            # renamed here to avoid consumers mis-reading it as final
            # survivor count. ``kept`` is still emitted at ``filter_end``
            # with its original all-three-gates meaning (post entropy).
            tick("filter_advance", current=idx, total=total_windows,
                 solid_pass=len(solid_pass))
        win = tuple(phrase_key.split(_NGRAM_KEY_SEP))
        sol = _solidity(win, int(c))
        if sol < npmi_threshold:
            continue
        solid_pass.append((win, int(c), sol))

    # --- Stage 4: neighbour distributions for solidity-passing windows ---
    conn.execute("DROP TABLE IF EXISTS _ng_passed")
    conn.execute("CREATE TEMP TABLE _ng_passed (phrase_key TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO _ng_passed VALUES (?)",
        [(_NGRAM_KEY_SEP.join(win),) for win, _, _ in solid_pass],
    )
    left_dist: Dict[str, "Counter"] = {}
    right_dist: Dict[str, "Counter"] = {}
    for pk, tok, cnt in conn.execute(
        "SELECT e.phrase_key, e.left_tok, COUNT(*) "
        "FROM _ng_ev e JOIN _ng_passed p ON e.phrase_key = p.phrase_key "
        "WHERE e.left_tok IS NOT NULL "
        "GROUP BY e.phrase_key, e.left_tok"
    ):
        left_dist.setdefault(pk, Counter())[tok] = int(cnt)
    for pk, tok, cnt in conn.execute(
        "SELECT e.phrase_key, e.right_tok, COUNT(*) "
        "FROM _ng_ev e JOIN _ng_passed p ON e.phrase_key = p.phrase_key "
        "WHERE e.right_tok IS NOT NULL "
        "GROUP BY e.phrase_key, e.right_tok"
    ):
        right_dist.setdefault(pk, Counter())[tok] = int(cnt)

    def _entropy(counter: "Counter") -> float:
        tot = sum(counter.values())
        if tot == 0:
            return 0.0
        h = 0.0
        for c in counter.values():
            pr = c / tot
            h -= pr * math.log(pr)
        return h

    # --- Stage 5: branch-entropy gate + entropy_rejects stash ---
    # Emits ``filter_advance`` here too so consumers keep a live progress
    # signal across the full Stages 3+5 span (Stage 4 is a single SQL
    # aggregate with no sub-progress). ``current`` starts at the count
    # of freq-passers already rejected by solidity so the ticks form a
    # single monotonic sequence across both stages.
    results: List[dict] = []
    entropy_rejects: List[Tuple[tuple, int, float, float]] = []
    _stage5_base = total_windows - len(solid_pass)  # solidity-rejects
    for idx, (win, c, sol) in enumerate(solid_pass):
        if idx % _PROGRESS_TICK == 0:
            tick("filter_advance", current=_stage5_base + idx,
                 total=total_windows, kept=len(results))
        pk = _NGRAM_KEY_SEP.join(win)
        be = min(_entropy(left_dist.get(pk, Counter())),
                 _entropy(right_dist.get(pk, Counter())))
        if be < ent_threshold:
            entropy_rejects.append((win, c, sol, be))
            continue
        results.append({
            "phrase": _join_by_pair_rule(win),
            "n": len(win),
            "count": c,
            "npmi": round(sol, 4),
            "branch_ent": round(be, 4),
        })
    tick("filter_end", windows=total_windows, kept=len(results))

    # --- Stage 6: substring salvage (verbatim mainline logic) ---
    tick("salvage_start", rejects=len(entropy_rejects))
    if entropy_rejects and results:
        by_first_char: "defaultdict[str, list[Tuple[str, int]]]" = defaultdict(list)
        for r in results:
            kt = r["phrase"]
            kc = int(r["count"])
            for ch in set(kt):
                by_first_char[ch].append((kt, kc))
        salvaged = 0
        for idx, (win, c, sol, be) in enumerate(entropy_rejects):
            if idx % _PROGRESS_TICK == 0:
                tick("salvage_advance", current=idx,
                     total=len(entropy_rejects), salvaged=salvaged)
            text = _join_by_pair_rule(win)
            t_len = len(text)
            candidates = by_first_char.get(text[0], ()) if text else ()
            max_container_c = 0
            for kt, kc in candidates:
                if len(kt) > t_len and text in kt and kc > max_container_c:
                    max_container_c = kc
            if max_container_c == 0:
                continue
            if c - max_container_c < 1:
                continue
            results.append({
                "phrase": text,
                "n": len(win),
                "count": c,
                "npmi": round(sol, 4),
                "branch_ent": round(be, 4),
            })
            salvaged += 1
        tick("salvage_end", rejects=len(entropy_rejects), salvaged=salvaged)
    else:
        tick("salvage_end", rejects=len(entropy_rejects), salvaged=0)

    # --- Stage 7: sort + return (verbatim) ---
    tick("sort_start", phrases=len(results))
    results.sort(key=lambda x: (-x["npmi"], -x["count"]))
    tick("sort_end", phrases=len(results))
    return results


def _load_auto_dict(conn, scope_id: int) -> Dict[str, dict]:
    """Load a scope's persisted auto n-gram dictionary as ``{phrase: row}``."""
    out: Dict[str, dict] = {}
    for row in conn.execute(
        "SELECT phrase, n, count, npmi, branch_ent FROM auto_ngram_dict "
        "WHERE scope_id = ?",
        (scope_id,),
    ):
        out[row["phrase"]] = {
            "phrase": row["phrase"], "n": row["n"], "count": row["count"],
            "npmi": row["npmi"], "branch_ent": row["branch_ent"],
        }
    return out


def _load_seed_auto_dict(
    conn,
    target_scope_id: int,
    seed_scope_name: str,
) -> Dict[str, dict]:
    """Load ``seed_scope_name``'s ``auto_ngram_dict`` as seed vocabulary.

    Used by the ``--seed-ngram-from`` flow (plan-2026-07-14 §6): after
    ``grga index import``, the target scope has knowledge (pairs / stats)
    but a blank auto_ngram_dict. Passing an existing sibling scope here
    lets the tokeniser cold-start with its domain vocabulary instead of
    re-detecting phrases from scratch.

    Returns ``{}`` when the seed scope does not exist, so the build
    silently falls back to normal detection.
    """
    row = conn.execute(
        "SELECT id FROM scopes WHERE name = ?", (seed_scope_name,)
    ).fetchone()
    if row is None:
        return {}
    seed_scope_id = int(row[0])
    if seed_scope_id == target_scope_id:
        # Seeding from self is a no-op — the normal load already picks
        # up the target's own rows.
        return {}
    return _load_auto_dict(conn, seed_scope_id)


def _persist_auto_dict(conn, scope_id: int, entries: Iterable[dict], now: str) -> None:
    """Upsert ``entries`` into ``auto_ngram_dict`` for ``scope_id``."""
    rows = [
        (scope_id, e["phrase"], int(e["n"]), int(e["count"]),
         float(e["npmi"]), float(e["branch_ent"]), now)
        for e in entries
    ]
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO auto_ngram_dict
            (scope_id, phrase, n, count, npmi, branch_ent, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scope_id, phrase) DO UPDATE SET
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
    conn, scope_id: int
) -> Dict[str, Dict[str, object]]:
    """Load ``{doc_path: {id, size, mtime, content_hash}}`` for a scope.

    Used by ``mode='build'`` to classify each file on disk against the
    stored record without an N+1 SELECT loop. ``id`` is included so
    per-doc DELETEs (removed / updated) do not need a second lookup.
    """
    rows = conn.execute(
        "SELECT id, doc_path, size, mtime, content_hash "
        "FROM docs WHERE scope_id = ?",
        (scope_id,),
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

    Used when a doc's content changed: we drop its ``keyword_docs`` +
    ``heading_docs`` rows and zero its ``docs.n_chunks_with_kw`` so pass 2
    can re-populate them without carrying stale state. ``docs.id`` stays
    valid so all foreign keys remain intact.

    v9 note: ``keyword_chunks`` / ``chunks`` no longer exist, and pair
    contributions (``keyword_pairs.cooccur_count``) are *not* subtracted
    here — pairs are an accumulating knowledge asset per design D6
    (docs/design/plan-2026-07-12-keyword-primary.md §1.1). A ``rebuild``
    (which wipes the whole scope's pair rows via
    :func:`_delete_scope_index_rows`) is the only path that also cleans
    pair evidence.
    """
    if not doc_ids:
        return
    placeholders = ",".join("?" for _ in doc_ids)
    ids = list(doc_ids)
    conn.execute(
        f"DELETE FROM keyword_docs WHERE doc_id IN ({placeholders})",
        ids,
    )
    conn.execute(
        f"DELETE FROM heading_docs WHERE doc_id IN ({placeholders})",
        ids,
    )
    # Zero the per-doc chunk count so ``scope_stats.n_chunks`` recomputes
    # correctly after pass 2 refills it.
    conn.execute(
        f"UPDATE docs SET n_chunks_with_kw = 0 WHERE id IN ({placeholders})",
        ids,
    )


def _delete_scope_index_rows(conn, scope_id: int) -> None:
    """Wipe every index-layer row for the given scope.

    Uses subquery-scoped DELETEs so we do not have to shuttle the full
    doc_id list into Python for large scopes. v9 shape: only
    ``keyword_docs`` + ``heading_docs`` remain as per-doc index tables;
    ``keyword_pairs`` is wiped separately in the ``rebuild`` path so this
    helper stays "doc-keyed only" and every doc-keyed table shows up
    here in exactly one place.
    """
    conn.execute(
        "DELETE FROM keyword_docs WHERE doc_id IN "
        "(SELECT id FROM docs WHERE scope_id = ?)",
        (scope_id,),
    )
    conn.execute(
        "DELETE FROM heading_docs WHERE doc_id IN "
        "(SELECT id FROM docs WHERE scope_id = ?)",
        (scope_id,),
    )
    # Reset per-doc chunk count too — scope_stats.n_chunks will be
    # repopulated during pass 2.
    conn.execute(
        "UPDATE docs SET n_chunks_with_kw = 0 WHERE scope_id = ?",
        (scope_id,),
    )


VALID_BUILD_MODES = ("build", "rebuild")
VALID_REFRESH_NGRAM = ("never", "append", "replace")


# ---------------------------------------------------------------------------
# Build-local temp directory for SQLite's own temp files (2026-07-15).
#
# Motivation: SQLite's TEMP TABLE + query engine spill (sorters, hash
# aggregates for GROUP BY / self-join) write to the OS temp dir by
# default (typically ``/tmp``). On distros where ``/tmp`` is tmpfs
# (Fedora, Arch, some Ubuntu configs) that's actually RAM — a spill
# meant to bound peak RSS silently becomes RAM again. We can't detect
# fstype cross-platform, so instead we point SQLite at a dir on the
# **same filesystem as the main DB**, which we know the user chose
# for durable storage. Cleaned up on connection close + rmdir.
#
# Per-build subdir (mkdtemp) so concurrent builds don't step on each
# other's cleanup. ``temp_store = FILE`` forces on-disk temp regardless
# of how SQLite was compiled (some builds default to MEMORY).
# See docs/design/plan-2026-07-15-build-memory-oom.md §6 for the wider
# spill-config discussion; this helper is the minimal first step.
# ---------------------------------------------------------------------------

_BUILD_TEMP_ROOT_NAME = "db-temp"


def _setup_build_temp_dir(conn: "sqlite3.Connection", main_db_path: Path) -> Path:
    """Create ``<main_db.parent>/db-temp/<uniq>/`` and point ``conn`` there.

    Uses :func:`tempfile.mkdtemp` so concurrent builds get isolated
    subdirs. Sets ``PRAGMA temp_store_directory`` (deprecated but still
    works on all supported SQLite versions) so all TEMP TABLE storage
    and query-engine spill for **this connection** lands under the
    returned path. Also forces ``PRAGMA temp_store = FILE`` — otherwise
    a SQLite compiled with ``TEMP_STORE=3`` (memory-only) would ignore
    ``temp_store_directory`` entirely.
    """
    import tempfile
    root = main_db_path.parent / _BUILD_TEMP_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    build_dir = Path(tempfile.mkdtemp(prefix="build-", dir=str(root)))
    # PRAGMA doesn't accept bound parameters; escape single quotes.
    escaped = str(build_dir).replace("'", "''")
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute(f"PRAGMA temp_store_directory = '{escaped}'")
    return build_dir


def _cleanup_build_temp_dir(build_dir: Optional[Path]) -> None:
    """Remove ``build_dir`` after connection close. Best-effort.

    SQLite unlinks its own temp files on connection close, so this dir
    is expected to be empty. ``shutil.rmtree(..., ignore_errors=True)``
    handles the (rare) case where an orphan file remains — better a
    tiny leak than a build that crashes on cleanup.

    We deliberately do **not** try to remove the shared ``db-temp`` root
    itself: with concurrent builds, the sequence
    ``B.mkdir(exist_ok=True) → A.rmdir(empty) → B.mkdtemp(...)``
    races and crashes B with ``FileNotFoundError``. An empty shared root
    directory is harmless and gets reused by the next build.
    """
    if build_dir is None:
        return
    shutil.rmtree(str(build_dir), ignore_errors=True)


def build_graph_index(
    scope: str,
    *,
    directory: Union[str, Path],
    db_path: Union[str, Path, None] = None,
    chunk_mode: str = "line",
    chunk_max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
    chunk_min_chars: int = DEFAULT_CHUNK_MIN_CHARS,
    min_pair_count: Optional[int] = ADAPTIVE_MIN_PAIR_COUNT,
    min_npmi: float = BUILD_MIN_NPMI,
    max_file_bytes: int = 0,
    ignore_dirs: Iterable[str] = (".git", ".grga", ".venv", "node_modules",
                                   "__pycache__", "dist", "build", "target",
                                   ".mypy_cache", ".pytest_cache"),
    mode: str = "rebuild",
    refresh_ngram: str = "replace",
    replace: Optional[bool] = None,
    ngram_iters: int = DEFAULT_NGRAM_ITERS,
    max_ngram_n: int = DEFAULT_MAX_NGRAM_N,
    seed_ngram_from: Optional[str] = None,
    tags: Sequence[str] = (),
    progress: Optional[Callable[[str], None]] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> BuildStats:
    """Build the v10 knowledge asset + evidence layers for ``scope``.

    v10 scope / directory split (plan-2026-07-14): the logical scope
    name (``scope``) and the physical corpus directory (``directory``)
    are now **independent parameters**. The scope name is the shard key
    written into every knowledge asset row (portable across machines);
    the directory is where files are actually enumerated from on this
    machine, and its binding to the scope is recorded in
    ``scope_directories`` (evidence layer). A scope may have multiple
    directory bindings on the same machine — build one at a time; the
    bindings accumulate.

    Two top-level modes (design doc plan-2026-07-11-build-vs-rebuild §3):

    * ``mode="build"`` — incremental. Uses ``docs.size/mtime/content_hash``
      to skip unchanged files, re-tokenise only content-changed ones, and
      delete rows for files that disappeared. Vocabulary is monotonic-add.
    * ``mode="rebuild"`` (default) — full-scope re-scan. Clears the
      index + derived layers first, then re-indexes every file.

    ``refresh_ngram`` controls the ``auto_ngram_dict`` asset layer:

    * ``"never"``   — do not run n-gram detection; use existing phrases.
    * ``"append"``  — detect and union new phrases into the dictionary.
    * ``"replace"`` — clear ``auto_ngram_dict`` for this scope first,
      then run detection from scratch. Only meaningful with
      ``mode="rebuild"``; combined with ``mode="build"`` raises
      ``ValueError``.

    Args:
        scope: logical scope name (e.g. ``physics-cn``). Portable across
            machines — the same name identifies the same domain
            regardless of where the corpus lives. Must be non-empty.
        directory: physical corpus root (or single file) on this
            machine. Recorded in ``scope_directories`` so caller
            follow-up (grga related, ripgrep) can locate the source
            files.
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
            params.
        ngram_iters / max_ngram_n: detection tuning; ``ngram_iters`` is
            capped to 1 automatically in ``mode="build"``.
        tags: optional multi-value scope labels (schema v11). UPSERTed
            (``INSERT OR IGNORE``, accumulate-only) onto ``scope_tags``
            so query-side ``--tag`` filters can resolve this scope. Empty
            / whitespace items are skipped. Existing tags survive a
            rebuild — use ``grga scope tag rm`` to remove them.
        progress: optional status callback.
        on_event: structured progress callback.
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

    scope_name = resolve_scope_name(scope)
    if scope_name is None:
        raise ValueError(
            "build_graph_index requires a non-empty logical scope name; "
            "pass e.g. scope='physics-cn'"
        )
    started = time.perf_counter()
    scope_path = Path(directory).expanduser().resolve()
    directory_str = str(scope_path)
    stats = BuildStats(
        scope=scope_name, chunk_mode=chunk_mode,
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
    build_temp_dir: Optional[Path] = None
    try:
        build_temp_dir = _setup_build_temp_dir(conn, target_db)
        ensure_schema(conn)
        interner = IdInterner(conn)
        scope_id = interner.scope_id(scope_name)
        # Record (or refresh) the scope -> directory binding in the
        # evidence layer so ``grga scope list`` and caller follow-up can
        # locate the physical corpus on this machine.
        interner.bind_directory(scope_id, directory_str)
        # Attach multi-value tag labels (schema v11). Accumulate-only:
        # a rebuild never clears existing tags — ``grga scope tag rm``
        # is the removal path. Empty items are dropped inside bind_tags.
        if tags:
            interner.bind_tags(scope_id, tags)

        # ------------------------------------------------------------------
        # Setup: load existing docs (build mode) and clear scope (rebuild).
        # ------------------------------------------------------------------
        existing_docs: Dict[str, Dict[str, object]] = {}
        if mode == "build":
            existing_docs = _load_scope_doc_rows(conn, scope_id)

        if mode == "rebuild":
            # Full clear of index + derived layers for this scope.
            # ``keyword_chunks`` / ``chunks`` / ``heading_docs`` are keyed
            # by ``doc_id`` which resolves through ``docs.scope_id``. The
            # shared helper keeps the three-table set in one place.
            _delete_scope_index_rows(conn, scope_id)
            conn.execute(
                "DELETE FROM keyword_pairs WHERE scope_id = ? AND source = ?",
                (scope_id, CHUNK_COOCCUR_SOURCE),
            )
            if refresh_ngram == "replace":
                conn.execute(
                    "DELETE FROM auto_ngram_dict WHERE scope_id = ?",
                    (scope_id,),
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
                doc_id = int(prev_row["id"]) if prev_row else interner.doc_id(scope_id, rel_path)
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
                doc_id = interner.doc_id(scope_id, rel_path)
                stats.docs_added += 1
            else:  # DOC_UPDATED
                doc_id = int(prev_row["id"]) if prev_row else interner.doc_id(scope_id, rel_path)
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
        # rebuild + append/replace: multi-iter detection as before.
        # rebuild + never:          skip detection.
        # build + append:           single detection iter on changed docs.
        # build + never:            skip detection.
        #
        # Two-phase design (2026-07-13, fixes "detected phrases invisible
        # in same-build keyword_stats"):
        #
        #   Phase A — detection loop: each iteration tokenises with the
        #     current ``auto_dict``, feeds ``line_token_lists`` (flat,
        #     detection-only view) into ``detect_ngram_phrases``, and merges
        #     the result back into ``auto_dict``. ``chunk_tokens`` is NOT
        #     materialised here — Pass 2 would use a stale copy anyway once
        #     the next iteration or the final tokenise runs. Halving peak
        #     memory: only one token view lives at a time.
        #
        #   Phase B — final tokenisation: once ``auto_dict`` is frozen for
        #     this build, tokenise every chunk exactly once with the
        #     finalised dict. The resulting ``chunk_tokens`` feeds Pass 1.5
        #     (scope stopword derivation) and Pass 2 (keyword_docs /
        #     keyword_pairs). This is what makes "苍坤上人" queryable in
        #     the same ``build`` that discovered it — previously the
        #     compound was persisted to ``auto_ngram_dict`` but never
        #     applied to ``chunk_tokens``, so ``keyword_stats`` only saw
        #     "苍坤" and ``related 苍坤上人`` returned empty.
        #
        # Cost accounting: same total work as before. Phase A does N
        # tokenisation passes for N detection iters (unchanged). Phase B
        # adds one tokenisation pass that used to be the last iter's
        # discarded work. The multi-iter convergence promise (final dict
        # is fixed-point of "detect from tokens produced by this dict") is
        # preserved for ``rebuild``: any phrases emergent only after
        # Phase B's re-tokenisation are picked up on the next build, same
        # as any pre-existing iter's residual.
        # ------------------------------------------------------------------
        prior_dict_size = int(conn.execute(
            "SELECT COUNT(*) FROM auto_ngram_dict WHERE scope_id = ?",
            (scope_id,),
        ).fetchone()[0])

        # Seed the working dict. ``refresh_ngram="replace"`` on rebuild
        # already dropped the DB rows above, so this loads empty.
        auto_dict: Dict[str, dict] = _load_auto_dict(conn, scope_id)

        # v10 optional cold-start (plan-2026-07-14 §6): borrow a sibling
        # scope's auto_ngram_dict as a starting vocabulary. Typical use
        # after ``grga index import`` when the target scope has no local
        # phrases yet; layers the seed *under* the target's own entries so
        # the target always wins on collisions (its own npmi/count are
        # more accurate for its corpus).
        if seed_ngram_from:
            seed_dict = _load_seed_auto_dict(
                conn, scope_id, seed_ngram_from,
            )
            if seed_dict:
                seeded = 0
                for phrase, entry in seed_dict.items():
                    if phrase not in auto_dict:
                        auto_dict[phrase] = entry
                        seeded += 1
                _emit(
                    "seed_ngram_loaded",
                    from_scope=seed_ngram_from,
                    phrases_seeded=seeded,
                    phrases_skipped=len(seed_dict) - seeded,
                )

        if mode == "rebuild" and refresh_ngram in ("append", "replace"):
            iters = max(1, int(ngram_iters))
        elif mode == "build" and refresh_ngram == "append":
            iters = 1
        else:
            # ``refresh_ngram="never"``: skip detection loop entirely.
            iters = 0

        run_detection = refresh_ngram in ("append", "replace")

        # Track whether the last Phase A iteration's ``jieba_tok`` /
        # ``phrase_set`` are still valid for Phase B. They are valid iff
        # the *last* detection iteration added zero new phrases (or if
        # detection is off entirely, in which case Phase A never mutated
        # ``auto_dict``). Saves one ~1 s ``jieba.Tokenizer`` init on
        # steady-state builds where the dict has converged.
        phase_a_tok = None  # type: ignore[assignment]
        phase_a_phrase_set: Optional[set] = None
        last_iter_added = 0

        # Phase A: detection-only iterations. Each iter tokenises + detects
        # without keeping the per-chunk view; only the flat token list is
        # retained for the detector.
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
            # PR-4: stream tokens to ``detect_ngram_phrases`` instead of
            # materialising a corpus-wide List[List[str]]. Keeps Python
            # heap flat during iter 1 / iter 2 (previously the ~360 MB
            # ``line_token_lists`` floor on M-corpus, per PR-4 SPEC §2).
            # Empty-chunk skip + throttled progress emit preserved.
            _chunks_done_holder = [0]

            def _stream_iter_tokens(
                _iteration=iteration, _iters=iters,
                _jt=jieba_tok, _ps=phrase_set,
            ):
                for entry in docs:
                    doc_id, rel_path, path, is_md, chunks = entry[:5]
                    for chunk in chunks:
                        toks_pos = tokenize_hybrid_with_pos(chunk.text, _jt)
                        toks = merge_auto_phrases(
                            toks_pos, _ps, max_n=max_ngram_n
                        )
                        _chunks_done_holder[0] += 1
                        if (
                            _chunks_done_holder[0] % _PROGRESS_TICK == 0
                            or _chunks_done_holder[0] == total_scanned_chunks
                        ):
                            _emit(
                                "ngram_iter_advance",
                                iter=_iteration + 1,
                                iters=_iters,
                                current=_chunks_done_holder[0],
                                total=total_scanned_chunks,
                            )
                        # Yield every chunk (including empty toks): the
                        # detector's inner loops (``range(len(toks))``) are
                        # no-ops on empty, so aggregate results are
                        # unchanged, but this keeps ``n_lines_hint``
                        # (= total_scanned_chunks) matching the enumerate
                        # count inside the detector — otherwise the
                        # ``unigram/window_advance`` totals over-report.
                        yield toks
            if run_detection:
                # Forward detector internal phases to the caller so long
                # scopes don't appear frozen between iter_end and the next
                # iter_start. The detector's own phase name goes into
                # ``stage`` (not ``phase``) to avoid colliding with the
                # top-level event ``phase`` slot.
                def _detect_progress(stage: str, info: dict,
                                     _it=iteration + 1, _n=iters) -> None:
                    _emit(
                        "ngram_detect",
                        iter=_it,
                        iters=_n,
                        stage=stage,
                        **info,
                    )
                    # Plain-text fallback (rich bar off): announce the two
                    # slow analysis stages so the terminal doesn't look
                    # frozen after tokenisation finishes.
                    if progress is not None and stage in (
                        "window_start", "filter_start"
                    ):
                        _lbl = {
                            "window_start": "枚举 n-gram 窗口",
                            "filter_start": "过滤候选短语",
                        }[stage]
                        progress(f"ngram iter {_it}/{_n}: {_lbl}…")
                detected = detect_ngram_phrases(
                    _stream_iter_tokens(), max_n=max_ngram_n,
                    on_progress=_detect_progress,
                    conn=conn,
                    n_lines_hint=total_scanned_chunks,
                )
                last_iter_added = 0
                for d_entry in detected:
                    if d_entry["phrase"] not in auto_dict:
                        last_iter_added += 1
                    auto_dict[d_entry["phrase"]] = d_entry
            else:
                # Unreachable in practice: ``iters > 0`` iff
                # ``refresh_ngram in ('append','replace')``, and
                # ``run_detection`` is set by the same condition. Left as
                # a defensive no-op with a clear invariant note so a
                # future ``refresh_ngram`` mode that decouples the two
                # doesn't silently skip generator consumption and lose
                # ``ngram_iter_advance`` emits.
                last_iter_added = 0
                assert False, (
                    "invariant broken: iters > 0 implies run_detection; "
                    "see build.py Phase A comments"
                )
            chunks_done = _chunks_done_holder[0]
            # Remember this iteration's tokeniser state for possible Phase B
            # reuse (only the final iteration's values survive the loop).
            phase_a_tok = jieba_tok
            phase_a_phrase_set = phrase_set
            stats.ngram_iters_run = iteration + 1
            if progress is not None:
                progress(
                    f"ngram iter {iteration + 1}/{iters}: "
                    f"{len(auto_dict)} phrases"
                )
            _emit(
                "ngram_iter_end",
                iter=iteration + 1,
                iters=iters,
                chunks=chunks_done,
                phrases=len(auto_dict),
            )

        # Phase B (PR-4 streaming): one final tokenisation with the
        # frozen dict. Instead of materialising a per-chunk ``chunk_tokens``
        # dict (the ~360 MB floor on M-corpus, per PR-4 SPEC §2), stream
        # tokenised chunks through two things:
        #   * a single-CJK-char ``chunk-df`` counter for stopword derivation
        #   * (implicitly) Pass 2 re-tokenises on-the-fly using the same
        #     ``jieba_tok`` + ``phrase_set`` — bit-exact by construction
        # ``refresh_ngram="never"`` path also uses this streaming loop.
        _emit(
            "ngram_finalize_start",
            phrases=len(auto_dict),
            chunks=total_scanned_chunks,
        )
        # Reuse the last Phase A tokeniser when the dict converged (final
        # iteration added nothing), else build fresh. ``iters == 0``
        # (refresh_ngram="never") has no Phase A state, so always fresh.
        if phase_a_tok is not None and last_iter_added == 0:
            jieba_tok = phase_a_tok
            phrase_set = phase_a_phrase_set
        else:
            jieba_tok = _new_jieba_tokenizer(auto_dict.keys())
            phrase_set = set(auto_dict.keys())
        # Streaming stopword-df accumulation. Only single-CJK chars go
        # into the counter (matches :func:`_derive_scope_stopword_candidates`).
        stopword_chunk_df: Counter = Counter()
        chunks_done = 0
        for entry in docs:
            doc_id, rel_path, path, is_md, chunks = entry[:5]
            for chunk in chunks:
                toks_pos = tokenize_hybrid_with_pos(chunk.text, jieba_tok)
                toks = merge_auto_phrases(
                    toks_pos, phrase_set, max_n=max_ngram_n
                )
                seen_single_cjk = {
                    t.casefold() for t in toks if _is_single_cjk(t)
                }
                for t in seen_single_cjk:
                    stopword_chunk_df[t] += 1
                chunks_done += 1
                if (
                    chunks_done % _PROGRESS_TICK == 0
                    or chunks_done == total_scanned_chunks
                ):
                    _emit(
                        "ngram_finalize_advance",
                        current=chunks_done,
                        total=total_scanned_chunks,
                    )
        if progress is not None:
            progress(
                f"ngram finalize: {chunks_done} chunks tokenised "
                f"with {len(auto_dict)} phrases (streaming)"
            )
        _emit(
            "ngram_finalize_end",
            chunks=chunks_done,
            phrases=len(auto_dict),
        )
        # Persist dictionary. In ``refresh_ngram="never"`` we still call
        # this: the dict was loaded from DB unchanged and re-persisting
        # is a no-op (INSERT OR REPLACE on the same rows).
        _persist_auto_dict(conn, scope_id, auto_dict.values(), now)
        stats.ngram_phrases_written = len(auto_dict)
        stats.ngram_dict_size = len(auto_dict)
        stats.ngram_dict_added = max(0, len(auto_dict) - prior_dict_size)

        # ------------------------------------------------------------------
        # Pass 1.5 (2026-07-13): derive scope-local stopword candidates from
        # the tokenised corpus (3a). Single CJK chars with a high chunk-df
        # ratio are candidates for the pair-time containment filter (3b);
        # this set never touches keyword_stats. See :func:
        # `_derive_scope_stopword_candidates`.
        # ------------------------------------------------------------------
        # PR-4: streaming variant — inputs already accumulated in
        # ``stopword_chunk_df`` during Phase B; chunk_tokens not needed.
        scope_stopword_candidates = _derive_scope_stopword_candidates_from_df(
            stopword_chunk_df, chunks_done,
        )
        _emit(
            "stopword_derived",
            count=len(scope_stopword_candidates),
            chars=sorted(scope_stopword_candidates),
            threshold=STOPWORD_DF_THRESHOLD,
        )

        # ------------------------------------------------------------------
        # Pass 2 (v9.1 keyword-primary): tokenise chunks in RAM and derive
        # the doc-level inverted index (``keyword_docs``) + per-doc
        # ``n_chunks_with_kw`` directly from ``chunk_tokens``, while staging
        # (doc_id, chunk_id, kw_id) triples into the ``_build_chunk_kws``
        # TEMP TABLE for pass 3's self-join. The deprecated ``chunks`` /
        # ``keyword_chunks`` permanent tables no longer exist (dropped by
        # v8→v9 migration); their signal is replaced by
        # ``keyword_docs.chunk_count`` + ``docs.n_chunks_with_kw``.
        #
        # Pairs are NOT enumerated in Python here (that was the v9 approach
        # whose ``pair_delta`` Counter / ``touched_pair_keys`` set ballooned
        # to GB-scale RAM on large corpora). Instead the raw chunk-kw
        # membership goes to the TEMP TABLE and SQLite's hash-aggregate does
        # the pair counting + min_pair_count filter in one pass (design D6
        # accumulate semantics preserved via UPSERT-add). See
        # :func:`_finalize_pairs_from_temp`.
        # ------------------------------------------------------------------
        if mode == "build" and docs:
            _delete_doc_index_rows_keep_docs(
                conn, [entry[0] for entry in docs]
            )

        _emit(
            "materialise_start",
            total_docs=len(docs),
            total_chunks=total_scanned_chunks,
        )
        # Deferred UPSERTs collected across pass 2; flushed after the loop.
        kw_docs_rows: List[Tuple[int, int, int, int, int]] = []
        # v9.1 (2026-07-13): pair aggregation returns to SQL self-join over
        # a build-local TEMP TABLE. Pre-v9 used a persistent ``keyword_chunks``
        # table; v9 dropped it and moved pair aggregation into a Python
        # ``pair_delta`` Counter + ``touched_pair_keys`` set. That caused two
        # regressions on large corpora (see ``_finalize_pairs_from_temp``):
        #   * ``touched_pair_keys`` grew unbounded (2-3 GB on a 22 MB novel).
        #   * every pair (incl. count=1) got UPSERTed into keyword_pairs
        #     BEFORE the min_pair_count filter — GB-scale disk churn.
        # The TEMP TABLE keeps the v9 read-path shape (no permanent
        # ``keyword_chunks`` asset) while restoring SQLite's hash-aggregate
        # to compute + filter pair counts in one pass. Dropped at end.
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _build_chunk_kws ("
            "  doc_id INTEGER NOT NULL, "
            "  chunk_id INTEGER NOT NULL, "
            "  kw_id INTEGER NOT NULL"
            ")"
        )
        conn.execute("DELETE FROM _build_chunk_kws")  # in case of prior aborted run
        chunk_kw_temp_batch: List[Tuple[int, int, int]] = []
        CHUNK_KW_TEMP_FLUSH = 200_000  # rows per batch INSERT
        touched_kw_ids: set = set()
        # Chunk-level progress throttling: emit at most ~500 updates across
        # the whole materialise phase, so a corpus with millions of chunks
        # does not drown the CLI progress bar in redraw work. Falls back to
        # "every chunk" for tiny corpora so short builds still animate.
        chunks_done_total = 0
        chunk_progress_step = max(1, total_scanned_chunks // 500) if total_scanned_chunks else 1
        next_progress_emit = chunk_progress_step
        for doc_index, entry in enumerate(docs, start=1):
            doc_id, rel_path, path, is_md, chunks = entry[:5]
            _, st_size, st_mtime, new_hash = entry[5:]
            per_chunk_kw: List[Tuple[int, Dict[str, int]]] = []
            file_distinct_kws: set = set()
            for chunk in chunks:
                # PR-4: re-tokenise on the fly with the finalised tokeniser
                # (same ``jieba_tok`` + ``phrase_set`` as Phase B; jieba
                # tokenisation is deterministic, so this is bit-exact
                # against the pre-refactor ``chunk_tokens.get(...)`` read).
                toks_pos = tokenize_hybrid_with_pos(chunk.text, jieba_tok)
                toks = merge_auto_phrases(
                    toks_pos, phrase_set, max_n=max_ngram_n
                )
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

            # Aggregate to (kw_id, doc_id) → (occurrence_count, chunk_count)
            # and stage (doc_id, chunk_id, kw_id) rows into the TEMP TABLE.
            per_doc_kw_agg: Dict[int, Tuple[int, int]] = {}
            n_chunks_with_kw_for_doc = 0
            file_had_keywords = False
            for _chunk_id, kw_counts in per_chunk_kw:
                chunks_done_total += 1
                if kw_counts:
                    file_had_keywords = True
                    n_chunks_with_kw_for_doc += 1
                    # Per-doc aggregation (raw kw_counts — keyword_stats is
                    # never gated by the 3a/3b filter, only pair enumeration is).
                    for kw, occ in kw_counts.items():
                        kid = kw_id_map[kw]
                        prev_occ, prev_chunks = per_doc_kw_agg.get(kid, (0, 0))
                        per_doc_kw_agg[kid] = (prev_occ + int(occ), prev_chunks + 1)
                    # 3b containment filter over kw *strings*, then map to
                    # ids and stage into the TEMP TABLE for the pass-3
                    # self-join. Substring-contained candidates like 者 vs
                    # 面壁者 are dropped from *pair* eligibility only.
                    pair_kws = _filter_chunk_kws_for_pairs(
                        list(kw_counts.keys()),
                        stopword_candidates=scope_stopword_candidates,
                    )
                    if pair_kws:
                        distinct_ids = {kw_id_map[k] for k in pair_kws}
                        for kid in distinct_ids:
                            chunk_kw_temp_batch.append((doc_id, _chunk_id, kid))
                        if len(chunk_kw_temp_batch) >= CHUNK_KW_TEMP_FLUSH:
                            conn.executemany(
                                "INSERT INTO _build_chunk_kws "
                                "(doc_id, chunk_id, kw_id) VALUES (?, ?, ?)",
                                chunk_kw_temp_batch,
                            )
                            chunk_kw_temp_batch.clear()

                # Chunk-level progress: throttle emits. Counter increments
                # for *every* chunk (including empty / no-keyword ones) so
                # the completion guard reaches total_scanned_chunks — the
                # doc-side denominator counts split_chunks output uniformly.
                if chunks_done_total >= next_progress_emit or chunks_done_total == total_scanned_chunks:
                    _emit(
                        "materialise_advance",
                        current=doc_index,
                        total=len(docs),
                        chunks_done=chunks_done_total,
                        total_chunks=total_scanned_chunks,
                        path=str(path),
                    )
                    next_progress_emit = chunks_done_total + chunk_progress_step

            for kid, (occ_sum, chunk_cnt) in per_doc_kw_agg.items():
                kw_docs_rows.append(
                    (scope_id, kid, doc_id, occ_sum, chunk_cnt)
                )
                touched_kw_ids.add(kid)

            # Update per-doc chunk count for scope_stats.n_chunks accounting.
            conn.execute(
                "UPDATE docs SET n_chunks_with_kw = ? WHERE id = ?",
                (int(n_chunks_with_kw_for_doc), int(doc_id)),
            )

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
                    f"({len(kw_docs_rows)} kw-doc rows)"
                )
            _emit(
                "materialise_advance",
                current=doc_index,
                total=len(docs),
                chunks_done=chunks_done_total,
                total_chunks=total_scanned_chunks,
                path=str(path),
            )

        # Flush the deferred keyword_docs INSERTs in one batch. UPSERT-shape
        # so a rebuild path (where DELETE FROM keyword_docs WHERE scope_id=?
        # already ran) or an incremental path (where DELETE for docs list
        # already ran in _delete_doc_index_rows_keep_docs above) both land
        # on clean rows.
        if kw_docs_rows:
            conn.executemany(
                """
                INSERT INTO keyword_docs
                    (scope_id, kw_id, doc_id, occurrence_count, chunk_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(kw_id, doc_id) DO UPDATE SET
                    occurrence_count = excluded.occurrence_count,
                    chunk_count = excluded.chunk_count
                """,
                kw_docs_rows,
            )
        # Flush the remaining TEMP TABLE batch (the tail below CHUNK_KW_TEMP_FLUSH).
        if chunk_kw_temp_batch:
            conn.executemany(
                "INSERT INTO _build_chunk_kws (doc_id, chunk_id, kw_id) "
                "VALUES (?, ?, ?)",
                chunk_kw_temp_batch,
            )
            chunk_kw_temp_batch.clear()
        # ``stats.keyword_chunks_written`` is kept as a deprecated field
        # for JSON output back-compat and reports the doc-level row count
        # (30× smaller than the pre-v9 chunk-level count).
        stats.keyword_chunks_written = len(kw_docs_rows)

        # Metadata-only refresh (touch / stable rga output).
        for doc_id, st_size, st_mtime, new_hash in metadata_only_updates:
            _update_doc_metadata(conn, doc_id, st_size, st_mtime, new_hash)

        # Remove docs that disappeared from disk.
        if removed_doc_ids:
            _delete_doc_index_rows(conn, removed_doc_ids)

        # ------------------------------------------------------------------
        # Pass 3 (v9.1): derive pairs from the TEMP TABLE self-join.
        # The pass-2 loop staged (doc_id, chunk_id, kw_id) rows into
        # ``_build_chunk_kws``. Here we self-join them on the same chunk to
        # count co-occurrences, apply ``min_pair_count`` in a single
        # HAVING (SQLite hash-aggregate, bounded RAM), UPSERT-add into
        # ``keyword_pairs``, then recompute npmi. See
        # :func:`_finalize_pairs_from_temp`.
        # ------------------------------------------------------------------
        index_layer_changed = bool(docs) or bool(removed_doc_ids) or mode == "rebuild"
        if index_layer_changed:
            # 1. Update scope_stats from the (possibly changed) per-doc
            #    ``n_chunks_with_kw`` values.
            n_chunks = int(conn.execute(
                "SELECT COALESCE(SUM(n_chunks_with_kw), 0) FROM docs "
                "WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()[0])
            n_docs = int(conn.execute(
                "SELECT COUNT(*) FROM docs WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()[0])

            # 2. Refresh keyword_stats from keyword_docs (idempotent
            #    aggregate; O(rows in keyword_docs for this scope)).
            conn.execute(
                "DELETE FROM keyword_stats WHERE scope_id = ?", (scope_id,)
            )
            conn.execute(
                """
                INSERT INTO keyword_stats
                    (scope_id, kw_id, chunk_df, doc_df,
                     total_occurrences, updated_at)
                SELECT scope_id, kw_id,
                       COALESCE(SUM(chunk_count), 0) AS chunk_df,
                       COUNT(*) AS doc_df,
                       COALESCE(SUM(occurrence_count), 0) AS total_occ,
                       ?
                FROM keyword_docs
                WHERE scope_id = ?
                GROUP BY scope_id, kw_id
                """,
                (now, scope_id),
            )
            vocab_size = int(conn.execute(
                "SELECT COUNT(*) FROM keyword_stats WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()[0])
            stats.keyword_stats_written = vocab_size

            # 3. Stamp scope_stats.
            conn.execute(
                """
                INSERT INTO scope_stats
                    (scope_id, n_chunks, n_docs, vocab_size, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    n_chunks = excluded.n_chunks,
                    n_docs = excluded.n_docs,
                    vocab_size = excluded.vocab_size,
                    updated_at = excluded.updated_at
                """,
                (scope_id, n_chunks, n_docs, vocab_size, now),
            )

            # 4. Derive pairs from the TEMP TABLE self-join. In ``rebuild``
            #    mode the scope's chunk_cooccur rows were wiped upstream
            #    (see the mode dispatch above), so the UPSERT lands on empty
            #    rows. In ``build`` mode existing pairs accumulate (design
            #    D6: pairs grow-only across add/update).
            #    ``min_pair_count=None`` (the default) resolves the write
            #    floor from this scope's n_chunks (:func:`resolve_min_pair_count`);
            #    an explicit value overrides the adaptive ladder.
            effective_min_pair_count = (
                resolve_min_pair_count(n_chunks)
                if min_pair_count is None
                else int(min_pair_count)
            )
            _emit("pairs_start", candidate_pairs=0, surviving_pairs=0)
            surviving = _finalize_pairs_from_temp(
                conn,
                scope_id=scope_id,
                min_pair_count=effective_min_pair_count,
                min_npmi=float(min_npmi),
                n_chunks=n_chunks,
                now=now,
                on_event=on_event,
            )
            stats.pairs_written = surviving
        else:
            # No index-layer change: everything already sits in the tables
            # from the previous build. Report current counts.
            stats.pairs_written = int(conn.execute(
                "SELECT COUNT(*) FROM keyword_pairs "
                "WHERE scope_id = ? AND source = ?",
                (scope_id, CHUNK_COOCCUR_SOURCE),
            ).fetchone()[0])
            stats.keyword_stats_written = int(conn.execute(
                "SELECT COUNT(*) FROM keyword_stats WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()[0])

        # Drop the build-local TEMP TABLE regardless of the branch taken so
        # a long-lived connection (tests reuse one) does not carry stale
        # rows into the next build.
        conn.execute("DROP TABLE IF EXISTS _build_chunk_kws")

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
        # Connection close releases all SQLite temp files under this build's
        # private temp dir; remove the now-empty dir. Best-effort — a
        # concurrent build with its own subdir is unaffected (each gets a
        # unique dir via mkdtemp).
        _cleanup_build_temp_dir(build_temp_dir)

    stats.elapsed_seconds = time.perf_counter() - started
    _emit("pairs_done", pairs_written=stats.pairs_written)
    _emit("done", stats=stats.to_dict())
    return stats



# ---------------------------------------------------------------------------
# _finalize_pairs_from_temp — v9.1 TEMP-TABLE-based pair aggregation.
#
# Pass 2 stages (doc_id, chunk_id, kw_id) triples into a build-local TEMP
# TABLE ``_build_chunk_kws`` (post-3b-filter kw ids only). This function
# runs the pre-v9-shaped self-join against that table to derive
# ``keyword_pairs``:
#
#   SELECT a.kw_id, b.kw_id, COUNT(*)
#   FROM _build_chunk_kws a
#   JOIN _build_chunk_kws b
#     ON a.doc_id = b.doc_id AND a.chunk_id = b.chunk_id
#    AND a.kw_id < b.kw_id
#   GROUP BY a.kw_id, b.kw_id
#   HAVING COUNT(*) >= min_pair_count
#
# Bounds vs. the deprecated Python ``pair_delta`` Counter path:
#
# * RAM: SQLite hash-aggregate is a C-level hash table (~30% of Python's
#   size per entry); HAVING filters below-threshold pairs before they
#   ever get materialised as rows.
# * Disk: only surviving (count >= min_pair_count) rows enter
#   ``keyword_pairs``. Pre-v9 kept this invariant via the same SQL;
#   v9's Python enumeration lost it and let count=1 pairs bloat the
#   table by 10-100× before a post-hoc DELETE.
#
# In ``rebuild`` mode the caller wiped the scope's chunk_cooccur rows
# upstream, so the UPSERT lands on empty rows. In ``build`` mode existing
# pairs accumulate via ``cooccur_count = cooccur_count + excluded.cooccur_count``
# (design D6 — pairs grow-only across add/update). After the accumulate,
# a scope-wide ``DELETE ... WHERE cooccur_count < min_pair_count`` re-applies
# the write-time floor to any legacy rows that dropped below threshold.
# ---------------------------------------------------------------------------


def _finalize_pairs_from_temp(
    conn,
    *,
    scope_id: int,
    min_pair_count: int,
    min_npmi: float,
    n_chunks: int,
    now: str,
    on_event: Optional[Callable[[dict], None]] = None,
) -> int:
    """Aggregate ``_build_chunk_kws`` into ``keyword_pairs`` and refresh npmi.

    Returns the surviving pair-row count in the scope after the min_npmi
    filter runs. ``_build_chunk_kws`` is expected to hold this build's
    contribution only — the caller is responsible for creating and
    populating it (see pass 2).
    """
    def _emit(phase: str, **fields) -> None:
        if on_event is None:
            return
        payload = {"phase": phase}
        payload.update(fields)
        try:
            on_event(payload)
        except Exception:
            pass

    # 1. Index for the self-join. Composite index on (doc_id, chunk_id)
    #    is the join key. Created after all rows are inserted so we do
    #    not pay per-INSERT maintenance during pass 2.
    _emit("pairs_scan_start")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS _idx_bcki_chunk "
        "ON _build_chunk_kws (doc_id, chunk_id)"
    )

    # 2. Self-join + GROUP BY + HAVING + UPSERT-add in one statement.
    #    SQLite hash-aggregates the join output; HAVING drops below-floor
    #    pairs BEFORE they cross into ``keyword_pairs``. ``ON CONFLICT``
    #    handles the incremental (mode='build') case where a pair already
    #    exists from an earlier build; rebuild starts from empty.
    conn.execute(
        """
        INSERT INTO keyword_pairs
            (scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at)
        SELECT ?, a.kw_id, b.kw_id, ?, COUNT(*), NULL, ?
        FROM _build_chunk_kws a
        JOIN _build_chunk_kws b
          ON a.doc_id = b.doc_id
         AND a.chunk_id = b.chunk_id
         AND a.kw_id < b.kw_id
        GROUP BY a.kw_id, b.kw_id
        HAVING COUNT(*) >= ?
        ON CONFLICT(scope_id, kw_a_id, kw_b_id, source) DO UPDATE SET
            cooccur_count = cooccur_count + excluded.cooccur_count,
            npmi = NULL,
            updated_at = excluded.updated_at
        """,
        (scope_id, CHUNK_COOCCUR_SOURCE, now, int(min_pair_count)),
    )

    # 3. Re-apply the write-time floor to legacy rows. In incremental mode
    #    a previously-present pair might now sit below ``min_pair_count``
    #    (e.g. min_pair_count raised between builds); the invariant is
    #    "every row in keyword_pairs has cooccur_count >= min_pair_count".
    conn.execute(
        "DELETE FROM keyword_pairs "
        "WHERE scope_id = ? AND source = ? AND cooccur_count < ?",
        (scope_id, CHUNK_COOCCUR_SOURCE, int(min_pair_count)),
    )

    # 4. Recompute npmi for all surviving pairs in this scope. Two backends:
    #
    # * ``_step4_sql``: one SQL ``UPDATE`` (npmi) + one SQL ``DELETE``
    #   (glued / below-floor). Needs SQLite ``ln()`` from the optional
    #   math-functions module (``-DSQLITE_ENABLE_MATH_FUNCTIONS``).
    # * ``_step4_batch``: paginated cursor + ``executemany``. Peak Python
    #   heap bounded by ``batch_size`` instead of total surviving pairs.
    #   Same semantics as the previous full-Python loop.
    #
    # Both are bit-exact against each other and against the pre-refactor
    # Python loop (verified in ``experiments/oom_build_fix_2026_07_15``:
    # 293k-pair M-corpus dumps compare with ``max_npmi_diff = 0.0``,
    # ``set jaccard = 1.0``, ``count`` int-strict equal).
    #
    # Motivation: the pre-refactor loop fetched every surviving pair into
    # Python and built ``updates`` / ``drops`` tuple lists — ~800 MB peak
    # on the M-corpus (see ``FINDINGS.md`` F5). Both backends drop that to
    # ~4 MB Δ RSS across the ``pairs_scan_start`` → ``pairs_scan_end``
    # window while keeping the emit contract intact.
    if _sqlite_has_ln(conn):
        _step4_sql(
            conn, scope_id=scope_id, min_npmi=min_npmi,
            n_chunks=n_chunks, on_event=on_event,
        )
    else:
        _step4_batch(
            conn, scope_id=scope_id, min_npmi=min_npmi,
            n_chunks=n_chunks, on_event=on_event,
            batch_size=_NPMI_BATCH_SIZE,
        )

    return int(conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs "
        "WHERE scope_id = ? AND source = ?",
        (scope_id, CHUNK_COOCCUR_SOURCE),
    ).fetchone()[0])


# ---------------------------------------------------------------------------
# _finalize_pairs_from_temp step 4 backends (2026-07-15).
# Design: docs/design/plan-2026-07-15-build-memory-oom.md §4
# Experiment: experiments/oom_build_fix_2026_07_15/ (C2/C5/C6 pass)
# ---------------------------------------------------------------------------

# Batch size for the fallback ``_step4_batch`` cursor. Bounds peak Python
# heap in the fallback path to ``batch_size × ~40 B`` (kw_a, kw_b,
# cooccur, npmi tuple ~ 40 B), well under 10 MB at the default 50k.
# Callers may override via ``GRGA_BUILD_NPMI_BATCH`` (see
# ``docs/env-vars.md``). The constant is read at import time; the value is
# **clamped to a positive int** — a non-positive setting would silently
# break the pagination loop (``LIMIT 0`` skips step-4 entirely; a negative
# ``LIMIT`` degenerates to unbounded and defeats the memory bound).
_NPMI_BATCH_DEFAULT = 50000
try:
    _NPMI_BATCH_SIZE = int(os.environ.get("GRGA_BUILD_NPMI_BATCH",
                                          str(_NPMI_BATCH_DEFAULT)))
    if _NPMI_BATCH_SIZE <= 0:
        raise ValueError("must be positive")
except (TypeError, ValueError):
    _NPMI_BATCH_SIZE = _NPMI_BATCH_DEFAULT


def _sqlite_has_ln(conn) -> bool:
    """Probe for SQLite's natural-log function.

    ``ln()`` is part of the optional math-functions module
    (``-DSQLITE_ENABLE_MATH_FUNCTIONS``, SQLite ≥ 3.35). Not all Python
    distributions bundle a build with it, so the SQL backend needs a
    graceful fallback. Presence probing (not return value) — a
    ``base-10 log()`` build would still return a number, just the wrong
    one; the missing-function case raises ``OperationalError``.
    """
    import sqlite3
    try:
        conn.execute("SELECT ln(1.0)")
        return True
    except sqlite3.OperationalError:
        return False


def _step4_sql(
    conn,
    *,
    scope_id: int,
    min_npmi: float,
    n_chunks: int,
    on_event: Optional[Callable[[dict], None]],
) -> None:
    """Recompute npmi via one UPDATE + delete degenerates via one DELETE.

    The npmi SQL expression mirrors :func:`_npmi`'s evaluation order
    exactly (per-log intermediate divisions), which is what makes the
    result bit-exact against the pre-refactor Python loop. Algebraic
    simplification (e.g. ``ln(co*n / (df_a*df_b))``) reshuffles the
    floating-point rounding sequence and would move ~1 ulp on some rows.
    """
    def _emit(phase: str, **fields) -> None:
        if on_event is None:
            return
        payload = {"phase": phase}
        payload.update(fields)
        try:
            on_event(payload)
        except Exception:
            pass

    # Pre-count for the pairs_advance emit contract (observers expect a
    # ``current/total`` tick even when Step 4 is a single SQL statement).
    total = int(conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source = ?",
        (scope_id, CHUNK_COOCCUR_SOURCE),
    ).fetchone()[0])

    # UPDATE: recompute npmi for every surviving chunk_cooccur row in
    # this scope. Degenerate cases (df zero, p_ab >= 1, denom <= 0) get
    # NULL — deleted in the DELETE below.
    conn.execute(
        """
        WITH df AS (
            SELECT kw_id, chunk_df FROM keyword_stats WHERE scope_id = ?
        )
        UPDATE keyword_pairs
        SET npmi = (
            SELECT CASE
                WHEN kp_co <= 0 OR df_a IS NULL OR df_b IS NULL
                     OR df_a <= 0 OR df_b <= 0 OR ? <= 0 THEN NULL
                WHEN (kp_co * 1.0 / ?) >= 1.0 THEN NULL
                WHEN (-ln(kp_co * 1.0 / ?)) <= 0 THEN NULL
                ELSE ln((kp_co * 1.0 / ?)
                         / ((df_a * 1.0 / ?) * (df_b * 1.0 / ?)))
                     / (-ln(kp_co * 1.0 / ?))
            END
            FROM (
                SELECT keyword_pairs.cooccur_count AS kp_co,
                       (SELECT chunk_df FROM df WHERE df.kw_id = keyword_pairs.kw_a_id) AS df_a,
                       (SELECT chunk_df FROM df WHERE df.kw_id = keyword_pairs.kw_b_id) AS df_b
            )
        )
        WHERE scope_id = ? AND source = ?
        """,
        (scope_id, n_chunks, n_chunks, n_chunks, n_chunks, n_chunks, n_chunks, n_chunks,
         scope_id, CHUNK_COOCCUR_SOURCE),
    )

    # DELETE: (a) npmi NULL from the UPDATE; (b) below the min_npmi
    # floor; (c) Gate 1 "glued" — three-way integer equality
    # df_a = df_b = cooccur_count. Using EXISTS(SELECT ... FROM df JOIN df)
    # keeps the glued check as an int comparison (mainline invariant).
    conn.execute(
        """
        WITH df AS (
            SELECT kw_id, chunk_df FROM keyword_stats WHERE scope_id = ?
        )
        DELETE FROM keyword_pairs
        WHERE scope_id = ? AND source = ?
          AND (
              npmi IS NULL
              OR (? > 0.0 AND npmi < ?)
              OR EXISTS (
                  SELECT 1 FROM df da JOIN df db
                    ON da.kw_id = keyword_pairs.kw_a_id
                   AND db.kw_id = keyword_pairs.kw_b_id
                  WHERE da.chunk_df > 0 AND db.chunk_df > 0
                    AND da.chunk_df = db.chunk_df
                    AND da.chunk_df = keyword_pairs.cooccur_count
              )
          )
        """,
        (scope_id, scope_id, CHUNK_COOCCUR_SOURCE, min_npmi, min_npmi),
    )

    # No ``pairs_advance`` here: the SQL backend runs the UPDATE + DELETE
    # as two atomic statements, so there is no intra-step progress signal
    # to emit. Consumers should render an indeterminate spinner between
    # ``pairs_scan_start`` and ``pairs_scan_end`` on installs with math
    # functions available. The batch fallback still emits progress ticks.
    surviving = int(conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source = ?",
        (scope_id, CHUNK_COOCCUR_SOURCE),
    ).fetchone()[0])
    _emit("pairs_scan_end", surviving=surviving)


def _step4_batch(
    conn,
    *,
    scope_id: int,
    min_npmi: float,
    n_chunks: int,
    on_event: Optional[Callable[[dict], None]],
    batch_size: int,
) -> None:
    """Fallback: paginate through surviving pairs in ``batch_size`` chunks.

    Peak Python heap bounded by the batch. Pagination key is
    ``(kw_a_id, kw_b_id)`` (the second/third columns of the primary key)
    — ``keyword_pairs`` is ``WITHOUT ROWID``, so ``rowid`` isn't
    available. The primary key gives a stable order for the tuple-compare
    ``(kw_a_id > last_a OR (= last_a AND kw_b_id > last_b))`` cursor.
    """
    def _emit(phase: str, **fields) -> None:
        if on_event is None:
            return
        payload = {"phase": phase}
        payload.update(fields)
        try:
            on_event(payload)
        except Exception:
            pass

    chunk_df: Dict[int, int] = {
        int(r[0]): int(r[1]) for r in conn.execute(
            "SELECT kw_id, chunk_df FROM keyword_stats WHERE scope_id = ?",
            (scope_id,),
        )
    }

    total = int(conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source = ?",
        (scope_id, CHUNK_COOCCUR_SOURCE),
    ).fetchone()[0])

    step_emit = max(1, total // 200) if total else 1
    processed = 0
    last_a = -1
    last_b = -1

    while True:
        # Row-value tuple compare is index-aware since SQLite 3.15 (2016):
        # ``(kw_a_id, kw_b_id) > (?, ?)`` becomes a true PK-range seek. The
        # OR-expanded form (``kw_a_id > ? OR (kw_a_id = ? AND kw_b_id > ?)``)
        # does **not** — the planner leaves it as a filter after
        # ``PRIMARY KEY (scope_id=?)``, so every batch re-scans the whole
        # scope, giving O(N²/batch_size) total cost that defeats the OOM
        # bound the fallback exists to enforce.
        rows = conn.execute(
            """
            SELECT kw_a_id, kw_b_id, cooccur_count
            FROM keyword_pairs
            WHERE scope_id = ? AND source = ?
              AND (kw_a_id, kw_b_id) > (?, ?)
            ORDER BY kw_a_id, kw_b_id
            LIMIT ?
            """,
            (scope_id, CHUNK_COOCCUR_SOURCE, last_a, last_b, batch_size),
        ).fetchall()
        if not rows:
            break

        updates: List[Tuple[float, int, int, int]] = []
        drops: List[Tuple[int, int, int]] = []
        for row in rows:
            a, b, co = int(row[0]), int(row[1]), int(row[2])
            df_a = chunk_df.get(a, 0)
            df_b = chunk_df.get(b, 0)
            npmi = _npmi(co, df_a, df_b, n_chunks)
            # Gate 1 (glued): int-strict three-way equality; see mainline
            # comment in the pre-refactor loop for the incremental-mode
            # rationale (must not confuse legitimate one-way attachments
            # with the true glued artefact).
            glued = df_a > 0 and df_b > 0 and df_a == df_b == co
            if npmi is None or glued or (min_npmi > 0.0 and npmi < min_npmi):
                drops.append((scope_id, a, b))
            else:
                updates.append((float(npmi), scope_id, a, b))
            last_a, last_b = a, b
            processed += 1
            if processed == total or processed % step_emit == 0:
                _emit("pairs_advance", current=processed, total=total)

        if drops:
            conn.executemany(
                "DELETE FROM keyword_pairs "
                "WHERE scope_id = ? AND kw_a_id = ? AND kw_b_id = ? "
                "AND source = 'chunk_cooccur'",
                drops,
            )
        if updates:
            conn.executemany(
                "UPDATE keyword_pairs SET npmi = ? "
                "WHERE scope_id = ? AND kw_a_id = ? AND kw_b_id = ? "
                "AND source = 'chunk_cooccur'",
                updates,
            )

    surviving = int(conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source = ?",
        (scope_id, CHUNK_COOCCUR_SOURCE),
    ).fetchone()[0])
    _emit("pairs_scan_end", surviving=surviving)




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
    scope: str,
    *,
    db_path: Union[str, Path, None] = None,
    phrases: Optional[Sequence[str]] = None,
    all_phrases: bool = False,
) -> ForgetStats:
    """Remove entries from the asset layer with cascading cleanup.

    v10: ``scope`` is the logical scope name (not a filesystem path).

    Two modes (mutually exclusive):

    * ``phrases=[...]`` — remove those specific phrases from
      ``auto_ngram_dict``. For each phrase, delete the matching
      ``keyword_docs`` rows in this scope and the ``keyword_pairs``
      edges with that phrase on either endpoint (``source='chunk_cooccur'``
      only; ``llm_promoted`` is a separate asset), plus the
      ``keyword_stats`` row.

    * ``all_phrases=True`` — nuke every asset row for this scope:
      ``auto_ngram_dict`` + the index layer + ``chunk_cooccur`` pairs.
      Follow with ``grga index build`` to relearn from scratch.

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
    scope_name = resolve_scope_name(scope)
    if scope_name is None:
        raise ValueError(
            "forget_scope_phrases requires a non-empty logical scope name"
        )
    stats = ForgetStats(scope=scope_name)
    if all_phrases:
        stats.all_wiped = True

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        interner = IdInterner(conn)
        scope_id_ro = interner.scope_id_ro(scope_name)
        if scope_id_ro is None:
            # Scope was never indexed; nothing to do. Return empty stats
            # rather than raising so scripts are idempotent.
            stats.elapsed_seconds = time.perf_counter() - started
            return stats
        scope_id = scope_id_ro

        conn.execute("BEGIN IMMEDIATE")
        try:
            if all_phrases:
                # Wipe the whole asset + index + derived for this scope.
                doc_ids = [
                    int(row[0]) for row in conn.execute(
                        "SELECT id FROM docs WHERE scope_id = ?",
                        (scope_id,),
                    ).fetchall()
                ]
                # Count what we are about to delete for observability.
                # v9: ``keyword_chunks_removed`` now reports the doc-level
                # keyword_docs rows (30× smaller than the pre-v9 count) —
                # field name kept for JSON contract stability.
                stats.keyword_chunks_removed = int(conn.execute(
                    "SELECT COUNT(*) FROM keyword_docs WHERE scope_id = ?",
                    (scope_id,),
                ).fetchone()[0])
                stats.ngram_dict_removed = int(conn.execute(
                    "SELECT COUNT(*) FROM auto_ngram_dict WHERE scope_id = ?",
                    (scope_id,),
                ).fetchone()[0])
                _delete_doc_index_rows(conn, doc_ids)
                conn.execute(
                    "DELETE FROM keyword_pairs "
                    "WHERE scope_id = ? AND source = ?",
                    (scope_id, CHUNK_COOCCUR_SOURCE),
                )
                conn.execute(
                    "DELETE FROM auto_ngram_dict WHERE scope_id = ?",
                    (scope_id,),
                )
                # v8 asset tables must be wiped too — leaving them behind
                # after a full forget would give a stale doc_df / n_chunks
                # signal to ``related``.
                conn.execute(
                    "DELETE FROM keyword_stats WHERE scope_id = ?",
                    (scope_id,),
                )
                conn.execute(
                    "DELETE FROM scope_stats WHERE scope_id = ?",
                    (scope_id,),
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
                    f"WHERE scope_id = ? AND LOWER(phrase) IN ({placeholders})",
                    [scope_id, *lowered_inputs],
                ).fetchall()
                dict_present = {str(row[0]) for row in dict_present_pairs}
                dict_present_lowered = {p.casefold() for p in dict_present}
                for phrase in phrase_list:
                    cf = phrase.casefold()
                    if cf not in dict_present_lowered and cf not in kw_lookup:
                        stats.phrases_missing.append(phrase)
                # Cascade delete keyword_docs rows for the matching kw
                # ids in this scope.
                target_kw_ids = [
                    kw_lookup[p.casefold()] for p in phrase_list
                    if p.casefold() in kw_lookup
                ]
                if target_kw_ids:
                    kw_placeholders = ",".join("?" for _ in target_kw_ids)
                    stats.keyword_chunks_removed = int(conn.execute(
                        f"SELECT COUNT(*) FROM keyword_docs "
                        f"WHERE kw_id IN ({kw_placeholders}) AND scope_id = ?",
                        [*target_kw_ids, scope_id],
                    ).fetchone()[0])
                    conn.execute(
                        f"DELETE FROM keyword_docs "
                        f"WHERE kw_id IN ({kw_placeholders}) AND scope_id = ?",
                        [*target_kw_ids, scope_id],
                    )
                    conn.execute(
                        f"DELETE FROM keyword_pairs "
                        f"WHERE scope_id = ? AND source = ? "
                        f"AND (kw_a_id IN ({kw_placeholders}) "
                        f"OR kw_b_id IN ({kw_placeholders}))",
                        [scope_id, CHUNK_COOCCUR_SOURCE,
                         *target_kw_ids, *target_kw_ids],
                    )
                    # Also drop the affected keyword_stats rows; the pair
                    # rows for OTHER kws are left as-is (design D6: pairs
                    # only shrink when explicitly forgotten, chunk_df of
                    # unaffected kws is unchanged since we did not modify
                    # any doc's ``n_chunks_with_kw`` here).
                    conn.execute(
                        f"DELETE FROM keyword_stats "
                        f"WHERE kw_id IN ({kw_placeholders}) AND scope_id = ?",
                        [*target_kw_ids, scope_id],
                    )
                # Delete the asset-layer rows.
                if dict_present:
                    dict_list = sorted(dict_present)
                    dp = ",".join("?" for _ in dict_list)
                    conn.execute(
                        f"DELETE FROM auto_ngram_dict "
                        f"WHERE scope_id = ? AND phrase IN ({dp})",
                        [scope_id, *dict_list],
                    )
                    stats.ngram_dict_removed = len(dict_list)
                # Report the surviving pair count as pairs_recomputed
                # (contract-stable field). No full recompute needed in v9:
                # deleting a kw only removes its own edges; other pairs'
                # cooccur_count / chunk_df are unaffected. npmi for
                # surviving pairs is stable because n_chunks did not change
                # (no docs were touched).
                stats.pairs_recomputed = int(conn.execute(
                    "SELECT COUNT(*) FROM keyword_pairs "
                    "WHERE scope_id = ? AND source = ?",
                    (scope_id, CHUNK_COOCCUR_SOURCE),
                ).fetchone()[0])

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

