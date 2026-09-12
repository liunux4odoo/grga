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

import fnmatch
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
from typing import (
    Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple, Union,
)

from grga.file_read import (
    RGA_SUPPORTED_SUFFIXES,
    TEXT_SUFFIXES,
    ReadSource,
    indexable_suffixes,
    read_file_lines_ex,
)
from grga.search import _normalize_glob_patterns
from grga.graph.ids import IdInterner, sql_var_chunks
from grga.graph.schema import (
    BUILD_COMPLEMENTARY_MAX_P,
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
_DIGIT_RUN = r"\d+(?=[\u4e00-\u9fff])|(?<=[\u4e00-\u9fff])\d+"
_CJK_RUN = r"[\u4e00-\u9fff]+"
_COMBINED_RE = re.compile(_ASCII_TOKEN + "|" + _DIGIT_RUN + "|" + _CJK_RUN)
_CJK_ONLY_RE = re.compile(_CJK_RUN)
_DIGIT_ONLY_RE = re.compile(r"^\d+$")
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

# Complementary-pair noise filtering (plan-2026-09-12-complementary-noise-filtering).
# Phase 1: global stopword gate — pairs touching an _INDEX_STOPWORDS member
# are dropped before any significance testing.
# Phase 2 (segregation): pairs whose negative npmi is driven mainly by
# "file-type isolation" (two words live in different document genres) are
# filtered out, keeping only true semantic-complementarity signals.
COMP_SEGREGATION_NPMI_GAP = 0.1
COMP_SEGREGATION_MIN_CO_DOC_RATIO = 0.1
COMP_SEGREGATION_MIN_CO_DOC_FLOOR = 2

# Iteration 2 (plan §迭代 2): strength gate. With large corpora, pairs whose
# observed/expected co-occurrence ratio is only mildly below 1 still reach a
# significant p-value via sample size alone; they are "statistically
# significant weak negative correlation", not semantic complementarity. A
# pair must satisfy cooccur_count / expected_count < this constant to be
# written to ``complementary_pairs``. Pure subtraction, always on, no CLI
# switch, and independent of ``--no-filter-comp-segregation``.
COMPLEMENTARY_MAX_C_OVER_E = 0.25

# Iteration 2: complementary-pair-only narrative stopwords. Unlike
# ``_INDEX_STOPWORDS`` this set is NOT applied during tokenisation — the main
# index and pair statistics keep collecting these words — it only blocks the
# complementary extraction. It targets high-frequency, near-zero-content
# fiction/narrative words (pronouns, chapter markers, discourse particles,
# speech verbs) that the code/general-corpus-oriented ``_INDEX_STOPWORDS``
# never covered. Kept separate so single-CJK-char scope-adaptive policy for
# the main index is untouched.
_COMPLEMENTARY_STOPWORDS = frozenset(
    {
        # 人称代词
        "他", "他们", "她", "她们", "它", "它们",
        "我", "你", "你们", "我们", "咱们", "自己", "本人",
        # 疑问 / 语气助词
        "吗", "呢", "吧", "啊", "哦", "嗯", "呀",
        # 章节 / 量词标记
        "第", "章", "回", "节", "卷", "篇",
        # 叙事高频副词
        "顿时", "忽然", "突然", "然后", "接着", "之后", "后来",
        "这时", "此刻", "那时", "当时",
        # 叙事高频动词（言说 / 视角）
        "说道", "问道", "回答", "说", "道", "看", "见", "只见", "看来",
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


def _compile_glob_matcher(patterns: Iterable[str]):
    """Compile fnmatch-style patterns into a case-insensitive matcher.

    ``*`` crosses ``/`` (loose fnmatch semantics — the same mental model as
    ``rg --iglob``), and matching is case-insensitive regardless of the host
    platform. The pattern list must already be normalized via
    :func:`grga.search._normalize_glob_patterns` (or be empty). Returns a
    callable ``matcher(rel_path: str) -> bool``, or ``None`` when no patterns
    remain (the "no gate" sentinel callers can test for).
    """
    pats = [p for p in patterns if p]
    if not pats:
        return None
    regexes = [re.compile(fnmatch.translate(p), re.IGNORECASE) for p in pats]

    def _matches(rel: str) -> bool:
        return any(r.match(rel) for r in regexes)

    return _matches


def iter_text_files(
    scope: Path,
    *,
    max_file_bytes: int,
    ignore_dirs: Iterable[str],
    on_skip: Optional[Callable[[str], None]] = None,
    include_globs: Iterable[str] = (),
    exclude_globs: Iterable[str] = (),
    on_glob_excluded: Optional[Callable[[Path], None]] = None,
) -> Iterable[Path]:
    """Yield candidate indexable files under ``scope``.

    "Indexable" is the union of the plain-text whitelist and the
    ripgrep-all-supported binary formats (PDF, Office, archives, ...);
    see :func:`grga.file_read.indexable_suffixes`. Files whose suffix
    is neither are reported via ``on_skip("bad_suffix")`` and skipped.

    ``max_file_bytes <= 0`` means *no size limit* (sentinel semantics): every
    indexable file passes the size gate. A positive value skips files larger
    than it and reports them via ``on_skip("too_large")``.

    ``include_globs`` / ``exclude_globs`` (plan-2026-09-07-build-glob-filter)
    filter the candidate set by the **path relative to ``scope``** using loose,
    case-insensitive fnmatch semantics (``*`` crosses ``/``). Exclude wins over
    include. The filter runs after the suffix gate and before the size gate.
    Files rejected by the glob gates are reported via
    ``on_glob_excluded(path)`` when provided — callers that must not treat
    them as "disappeared from disk" (weak-semantics incremental builds) use
    this to keep their seen-set accurate. A single-file ``scope`` is matched
    against its basename.
    """
    accepted_suffixes = indexable_suffixes()
    ignore_set = {name for name in ignore_dirs}
    matches_include = _compile_glob_matcher(_normalize_glob_patterns(include_globs))
    matches_exclude = _compile_glob_matcher(_normalize_glob_patterns(exclude_globs))

    def _glob_skipped(rel: str) -> bool:
        if matches_exclude is not None and matches_exclude(rel):
            return True
        if matches_include is not None and not matches_include(rel):
            return True
        return False

    if scope.is_file():
        if matches_include is not None or matches_exclude is not None:
            if _glob_skipped(scope.name):
                if on_glob_excluded is not None:
                    on_glob_excluded(scope)
                return
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
            if matches_include is not None or matches_exclude is not None:
                try:
                    rel = str(entry.relative_to(scope)).replace(os.sep, "/")
                except ValueError:
                    rel = name
                if _glob_skipped(rel):
                    if on_glob_excluded is not None:
                        on_glob_excluded(entry)
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


def _tokenise_chunk(
    chunk_text: str, jieba_tok, phrase_set: set, max_n: int,
) -> List[str]:
    """Tokenise one chunk: hybrid CJK/ASCII split + greedy phrase merge.

    Single source of truth for the tokenisation pipeline used at all three
    build sites (Phase A detection generator, Phase B stopword-df pass,
    Pass 2 keyword materialisation). PR-4's "bit-exact by construction"
    guarantee — Phase B and Pass 2 must produce the *same* token stream for
    re-tokenisation to safely replace the old ``chunk_tokens`` cache — holds
    mechanically because all three call this one helper. Do not inline the
    ``tokenize_hybrid_with_pos`` + ``merge_auto_phrases`` pair at call sites;
    a divergence between the stopword pass and the keyword pass would be
    silent (each is only compared to a frozen baseline, never to each other).
    """
    toks_pos = tokenize_hybrid_with_pos(chunk_text, jieba_tok)
    return merge_auto_phrases(toks_pos, phrase_set, max_n=max_n)


def _keywords_from_tokens(tokens: Iterable[str]) -> Counter:
    """Casefold + stopword-filter a token list into ``{keyword: count}``.

    Pure-digit tokens (``^\\d+$``) are excluded from the index layer —
    they participate in n-gram detection (so ``404避难所`` can be
    discovered) but never become standalone keywords, preventing digit
    hub pollution of ``keyword_pairs`` (plan-2026-09-11 §3.3).
    """
    counts: Counter = Counter()
    for tok in tokens:
        key = tok.casefold()
        if key in _INDEX_STOPWORDS:
            continue
        if _DIGIT_ONLY_RE.fullmatch(key):
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
            conn.execute("DROP TABLE IF EXISTS _ng_freq")
            conn.execute("DROP TABLE IF EXISTS _ng_passed")
            conn.execute("DROP TABLE IF EXISTS _ng_ent")
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

# PR-8: SQLite VM-instruction budget between progress-handler callbacks.
# 1M keeps callback overhead negligible (a huge SQL runs billions of VM
# ops → thousands of callbacks × ~microseconds) while still ticking a
# spinner every fraction of a second on slow statements.
_SQL_PROGRESS_STEPS = 1_000_000


class _sql_progress:
    """Context manager: emit ``<phase>_advance`` heartbeats while a slow
    SQL statement runs on ``conn``.

    SQLite calls the registered progress handler every
    ``_SQL_PROGRESS_STEPS`` VM instructions; we forward that to the
    caller's ``emit(phase_name, **info)`` callable as an
    indeterminate-progress tick (rising ``step`` counter, no ``total``)
    so the CLI spinner keeps moving during the multi-minute Stage-2
    aggregate / Stage-4 UNION / pair self-join instead of appearing
    frozen. Handler is uninstalled on exit; fast statements never
    trigger a single callback.

    ``emit`` signature ``(phase: str, **info) -> None`` matches both the
    inner ``tick`` helper inside ``detect_ngram_phrases`` and the outer
    ``_emit`` helper used elsewhere in the build pipeline.
    """

    def __init__(self, conn, phase: str,
                 emit: Optional[Callable[..., None]]):
        self._conn = conn
        self._phase = phase
        self._emit = emit
        self._step = 0

    def _handler(self) -> int:
        self._step += 1
        if self._emit is not None:
            try:
                self._emit(self._phase + "_advance", step=self._step)
            except Exception:
                pass
        return 0  # non-zero would abort the query

    def __enter__(self):
        if self._emit is not None:
            try:
                self._emit(self._phase + "_start")
            except Exception:
                pass
        try:
            self._conn.set_progress_handler(self._handler, _SQL_PROGRESS_STEPS)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        try:
            self._conn.set_progress_handler(None, 0)
        except Exception:
            pass
        if self._emit is not None:
            try:
                self._emit(self._phase + "_end", steps=self._step)
            except Exception:
                pass
        return False

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

    # --- Stage 2: materialise freq aggregate INTO a TEMP TABLE (PR-6). ---
    # Previously ``fetchall()`` returned a Python list of every
    # freq-passing (phrase_key, n, c) — with 1.7M+ chunk corpora this
    # explodes past a GB (see experiments/oom_build_fix4_2026_07_15).
    # Store it in SQLite instead; ``mr`` (= MIN(rowid) per group) is
    # unique across groups and preserves the mainline dict insertion
    # order for downstream tiebreaks.
    conn.execute("DROP TABLE IF EXISTS _ng_freq")
    conn.execute(
        "CREATE TEMP TABLE _ng_freq ("
        "  phrase_key TEXT NOT NULL PRIMARY KEY,"
        "  n INTEGER NOT NULL, c INTEGER NOT NULL, mr INTEGER NOT NULL"
        ") WITHOUT ROWID"
    )
    # The GROUP BY over ``_ng_ev`` (100M+ rows on a 3M-chunk corpus)
    # takes several minutes with no natural progress signal. Wrap with a
    # progress-handler heartbeat so the CLI spinner keeps ticking.
    with _sql_progress(conn, "agg_freq", tick):
        conn.execute(
            "INSERT INTO _ng_freq (phrase_key, n, c, mr) "
            "SELECT phrase_key, n, COUNT(*) AS c, MIN(rowid) AS mr "
            "FROM _ng_ev GROUP BY phrase_key, n HAVING c >= ?",
            (min_count,),
        )
    conn.execute("CREATE INDEX _idx_ng_freq_mr ON _ng_freq(mr)")
    total_windows = int(conn.execute(
        "SELECT COUNT(*) FROM _ng_freq"
    ).fetchone()[0])
    tick("window_end", windows=total_windows)

    # --- Stage 3: keyset-paginated solidity filter (PR-6 streaming). ---
    # No full ``freq_rows`` list, no ``ngram_count`` dict, no
    # ``solid_pass`` list. Peak Python heap = batch size × ~200 B.
    # Split counts are queried per-batch via a single
    # ``WHERE phrase_key IN (...)`` and cached in a per-batch dict.
    conn.execute("DROP TABLE IF EXISTS _ng_passed")
    conn.execute(
        "CREATE TEMP TABLE _ng_passed ("
        "  phrase_key TEXT NOT NULL PRIMARY KEY,"
        "  c INTEGER NOT NULL, sol REAL NOT NULL, mr INTEGER NOT NULL"
        ") WITHOUT ROWID"
    )
    tick("filter_start", windows=total_windows)

    def _log_uni_local(t: str) -> float:
        c = uni.get(t, 0)
        return math.log(c) if c > 0 else math.nan

    _SOLIDITY_BATCH = 4096
    last_mr = -1  # rowid is >= 1 in SQLite
    idx = 0
    solid_pass_count = 0
    while True:
        rows = conn.execute(
            "SELECT phrase_key, n, c, mr FROM _ng_freq "
            "WHERE mr > ? ORDER BY mr LIMIT ?",
            (last_mr, _SOLIDITY_BATCH),
        ).fetchall()
        if not rows:
            break
        # Collect distinct split phrase_keys needed by this batch (only
        # tuple splits — unigram splits come from ``uni``).
        need_keys: set = set()
        batch: List[Tuple[tuple, int, float, int]] = []
        for phrase_key, n, c, mr in rows:
            win = tuple(phrase_key.split(_NGRAM_KEY_SEP))
            batch.append((win, int(c), 0.0, int(mr)))
            for k in range(1, n):
                left = win[0] if k == 1 else win[:k]
                right = win[-1] if k == n - 1 else win[k:]
                if not isinstance(left, str):
                    need_keys.add(_NGRAM_KEY_SEP.join(left))
                if not isinstance(right, str):
                    need_keys.add(_NGRAM_KEY_SEP.join(right))
        # Batch-fetch split counts. All splits of freq-passers are
        # themselves freq-passers (each container occurrence carries a
        # split occurrence, so count(sub) >= count(super) >= min_count),
        # so every needed key is guaranteed to be in ``_ng_freq``.
        split_counts: Dict[str, int] = {}
        if need_keys:
            key_list = list(need_keys)
            for sub in sql_var_chunks(key_list):
                q = (
                    "SELECT phrase_key, c FROM _ng_freq "
                    "WHERE phrase_key IN (" + ",".join("?" * len(sub)) + ")"
                )
                for pk, ck in conn.execute(q, sub):
                    split_counts[pk] = int(ck)

        # Solidity per candidate, using local split_counts dict.
        to_insert: List[Tuple[str, int, float, int]] = []
        for win, c, _, mr in batch:
            n = len(win)
            if n < 2:
                sol = 1.0
            else:
                vals = []
                joint_log = _log_c(c)
                joint_p_log_ratio_denom = log_total - joint_log
                # Only proceed with real npmi if denom > 0; else sol = 0
                for k in range(1, n):
                    left = win[0] if k == 1 else win[:k]
                    right = win[-1] if k == n - 1 else win[k:]
                    # left/right count + log_count (unigram vs tuple)
                    if isinstance(left, str):
                        ca = uni.get(left, 0)
                        log_ca = _log_uni_local(left)
                    else:
                        ca = split_counts.get(_NGRAM_KEY_SEP.join(left), 0)
                        log_ca = _log_c(ca) if ca > 0 else math.nan
                    if isinstance(right, str):
                        cb = uni.get(right, 0)
                        log_cb = _log_uni_local(right)
                    else:
                        cb = split_counts.get(_NGRAM_KEY_SEP.join(right), 0)
                        log_cb = _log_c(cb) if cb > 0 else math.nan
                    if ca <= 0 or cb <= 0 or c <= 0:
                        vals.append(-1.0)
                        continue
                    pmi = log_total + joint_log - log_ca - log_cb
                    if joint_p_log_ratio_denom <= 0:
                        vals.append(0.0)
                    else:
                        vals.append(pmi / joint_p_log_ratio_denom)
                sol = min(vals)
            if sol < npmi_threshold:
                continue
            to_insert.append((_NGRAM_KEY_SEP.join(win), c, float(sol), mr))

        if to_insert:
            conn.executemany(
                "INSERT INTO _ng_passed (phrase_key, c, sol, mr) "
                "VALUES (?, ?, ?, ?)",
                to_insert,
            )
            solid_pass_count += len(to_insert)

        # Advance keyset + progress
        idx += len(rows)
        last_mr = int(rows[-1][3])
        # Progress emit throttled per batch (batches are ~4k rows so
        # naturally aligned with the old ``idx % _PROGRESS_TICK`` cadence).
        tick("filter_advance", current=idx, total=total_windows,
             solid_pass=solid_pass_count)

    # --- Stage 4: streaming per-phrase branch entropy (PR-6). ---
    # Old code built two full ``defaultdict[str, Counter]`` (left_dist,
    # right_dist) over ALL solidity-passers — the same O(passing) explosion
    # as Stage 3 at scale. Replace with a UNION-scan of L/R neighbour
    # groups ordered by phrase_key + side; accumulate one phrase's
    # neighbours at a time, compute entropy, INSERT into ``_ng_ent``.
    conn.execute("DROP TABLE IF EXISTS _ng_ent")
    conn.execute(
        "CREATE TEMP TABLE _ng_ent ("
        "  phrase_key TEXT NOT NULL PRIMARY KEY, be REAL NOT NULL"
        ") WITHOUT ROWID"
    )
    _ENT_INSERT_BATCH = 4096

    def _entropy_from_counter(counter: Counter) -> float:
        tot = sum(counter.values())
        if tot == 0:
            return 0.0
        h = 0.0
        for cnt in counter.values():
            pr = cnt / tot
            h -= pr * math.log(pr)
        return h

    # side='L' for left neighbour, 'R' for right. ORDER BY phrase_key, side
    # groups all L-neighbours then all R-neighbours per phrase; break on
    # phrase-key or side change → finalise the (phrase, side) entropy.
    # ``_ent_current[pk] = (be_left, be_right)`` accumulator, but we
    # only ever hold ONE phrase in memory at a time (streaming).
    _ent_buf: List[Tuple[str, float]] = []
    _cur_pk: Optional[str] = None
    _cur_left: Counter = Counter()
    _cur_right: Counter = Counter()

    def _flush_current_phrase():
        nonlocal _ent_buf, _cur_pk, _cur_left, _cur_right
        if _cur_pk is None:
            return
        be = min(
            _entropy_from_counter(_cur_left),
            _entropy_from_counter(_cur_right),
        )
        _ent_buf.append((_cur_pk, float(be)))
        _cur_left = Counter()
        _cur_right = Counter()
        if len(_ent_buf) >= _ENT_INSERT_BATCH:
            conn.executemany(
                "INSERT INTO _ng_ent (phrase_key, be) VALUES (?, ?)",
                _ent_buf,
            )
            _ent_buf = []

    # Merge L/R streams ordered by (phrase_key, side). For phrases with
    # zero L neighbours or zero R neighbours, the missing side's entropy
    # is 0.0 (matches the old Counter()-default semantics).
    #
    # ``ORDER BY`` in this UNION forces SQLite to fully materialise the
    # aggregate before the first row can be yielded — a multi-minute pause
    # on large corpora. Wrap the whole iteration with a progress heartbeat.
    with _sql_progress(conn, "agg_neighbors", tick):
        for pk, side, tok, cnt in conn.execute(
            "SELECT phrase_key, side, tok, COUNT(*) AS cnt FROM ("
            "  SELECT e.phrase_key AS phrase_key, 'L' AS side, e.left_tok AS tok "
            "  FROM _ng_ev e JOIN _ng_passed p ON e.phrase_key = p.phrase_key "
            "  WHERE e.left_tok IS NOT NULL "
            "  UNION ALL "
            "  SELECT e.phrase_key, 'R', e.right_tok "
            "  FROM _ng_ev e JOIN _ng_passed p ON e.phrase_key = p.phrase_key "
            "  WHERE e.right_tok IS NOT NULL"
            ") GROUP BY phrase_key, side, tok "
            "ORDER BY phrase_key, side"
        ):
            if pk != _cur_pk:
                _flush_current_phrase()
                _cur_pk = pk
            if side == "L":
                _cur_left[tok] = int(cnt)
            else:
                _cur_right[tok] = int(cnt)
        _flush_current_phrase()
    if _ent_buf:
        conn.executemany(
            "INSERT INTO _ng_ent (phrase_key, be) VALUES (?, ?)",
            _ent_buf,
        )
        _ent_buf = []

    # --- Stage 5: entropy gate — stream from _ng_passed + _ng_ent. ---
    # Iterate in ``mr`` order (freq MIN(rowid) → mainline insertion order)
    # so the final ``results.sort`` tiebreak matches the pre-refactor
    # ordering. Left-join _ng_ent because phrases with zero neighbours on
    # both sides never got inserted (be would be 0.0 → correct default).
    results: List[dict] = []
    entropy_rejects: List[Tuple[tuple, int, float, float]] = []
    _stage5_base = total_windows - solid_pass_count
    _s5_idx = 0
    for phrase_key, c, sol, be in conn.execute(
        "SELECT p.phrase_key, p.c, p.sol, COALESCE(e.be, 0.0) "
        "FROM _ng_passed p LEFT JOIN _ng_ent e "
        "ON p.phrase_key = e.phrase_key "
        "ORDER BY p.mr"
    ):
        if _s5_idx % _PROGRESS_TICK == 0:
            tick("filter_advance", current=_stage5_base + _s5_idx,
                 total=total_windows, kept=len(results))
        win = tuple(phrase_key.split(_NGRAM_KEY_SEP))
        c = int(c); sol = float(sol); be = float(be)
        if be < ent_threshold:
            entropy_rejects.append((win, c, sol, be))
            _s5_idx += 1
            continue
        results.append({
            "phrase": _join_by_pair_rule(win),
            "n": len(win),
            "count": c,
            "npmi": round(sol, 4),
            "branch_ent": round(be, 4),
        })
        _s5_idx += 1
    tick("filter_end", windows=total_windows, kept=len(results))

    # --- Stage 6: substring salvage (PR-7: reversed direction). ---
    # The rule is unchanged: a reject is kept iff its rejoined text is a
    # *proper* substring of some survivor's text AND its independent-margin
    # ``count(reject) - max(count(container)) >= 1``. The old algorithm
    # iterated rejects and, for each, linearly scanned a by-first-char
    # bucket of survivors — on a 3M-chunk corpus that bucket is huge for
    # common CJK chars (的 / 是 / 一), giving ~235 rejects/s ≈ 3.8 h total.
    #
    # Reversed: index reject texts once (a set), then enumerate each
    # survivor's proper substrings and record the max container count per
    # reject text. Complexity O(survivors × L^3 + rejects) with L = phrase
    # char length (bounded by max_ngram_n tokens ≈ tens of chars), i.e.
    # minutes instead of hours. The final salvage pass iterates
    # ``entropy_rejects`` in original order so the append order into
    # ``results`` — and thus the Stage-7 sort tiebreak — is bit-identical.
    tick("salvage_start", rejects=len(entropy_rejects))
    if entropy_rejects and results:
        # Reject text set for O(1) substring membership. Deduped, so this
        # is ≤ len(entropy_rejects) distinct strings.
        reject_texts: set = set()
        for win, _c, _sol, _be in entropy_rejects:
            reject_texts.add(_join_by_pair_rule(win))

        # For each survivor, enumerate proper substrings; record the
        # largest container count per reject-text. ``max_container_c[sub]``
        # is a pure function of ``sub`` (max over all containers), so the
        # per-reject margin test below is order-independent and matches the
        # old max-over-candidates semantics exactly.
        max_container_c: Dict[str, int] = {}
        n_surv = len(results)
        for idx, r in enumerate(results):
            if idx % _PROGRESS_TICK == 0:
                tick("salvage_advance", current=idx, total=n_surv,
                     salvaged=len(max_container_c))
            kt = r["phrase"]
            L = len(kt)
            if L < 2:
                continue  # no proper substring can match a (≥1-char) reject
            kc = int(r["count"])
            # Distinct proper substrings of ``kt`` that are reject texts.
            local_seen: set = set()
            for i in range(L):
                for j in range(i + 1, L + 1):
                    if i == 0 and j == L:
                        continue  # full string is not a *proper* substring
                    sub = kt[i:j]
                    if sub in reject_texts and sub not in local_seen:
                        local_seen.add(sub)
                        if kc > max_container_c.get(sub, 0):
                            max_container_c[sub] = kc

        # Final salvage pass in original reject order (append-order stable).
        salvaged = 0
        for win, c, sol, be in entropy_rejects:
            text = _join_by_pair_rule(win)
            mcc = max_container_c.get(text, 0)
            if mcc == 0:
                continue
            if c - mcc < 1:
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


def _hypergeom_lower_tail(k: int, N: int, K: int, n: int) -> float:
    """Hypergeometric lower-tail cumulative probability P(X ≤ k).

    Population size ``N``, success states ``K``, draws ``n``, observed
    successes ``k``.  Returns the probability of observing *at most* ``k``
    successes under the null of independent sampling without replacement.

    Computed via the iterative ratio formula to avoid computing large
    factorials directly.  ``k`` is clamped to ``[0, min(K, n)]`` and
    ``N`` must be positive; otherwise returns ``1.0`` (ill-defined input
    → treat as "not significant").

    This is a pure mathematical identity in the same spirit as ``_npmi``
    — no semantic prior, just a significance statistic derived from
    chunk-df counts.
    """
    if N <= 0 or K <= 0 or n <= 0:
        return 1.0
    if k < 0:
        return 0.0
    k_max = min(K, n)
    if k >= k_max:
        return 1.0
    # Swap K and n so K ≤ n — same distribution, fewer iterations when
    # k is small relative to K (typical for the complementary case where
    # co-occurrence is far below expectation).
    if K > n:
        K, n = n, K
    # p(0) = C(N-K, n) / C(N, n)
    # Computed via log-gamma for numerical stability on large N.
    def _log_binom(a: int, b: int) -> float:
        if b < 0 or b > a:
            return float("-inf")
        return (
            math.lgamma(a + 1)
            - math.lgamma(b + 1)
            - math.lgamma(a - b + 1)
        )

    log_p_0 = _log_binom(N - K, n) - _log_binom(N, n)
    total = math.exp(log_p_0)
    if k == 0:
        return min(1.0, total)
    # Iterative: p(i+1) = p(i) * (K - i) * (n - i) / ((i + 1) * (N - K - n + i + 1))
    log_p = log_p_0
    for i in range(k):
        numerator = (K - i) * (n - i)
        denominator = (i + 1) * (N - K - n + i + 1)
        if denominator <= 0:
            break
        log_p += math.log(numerator) - math.log(denominator)
        total += math.exp(log_p)
    return min(1.0, total)


def _npmi_doc(co_doc: int, doc_df_a: int, doc_df_b: int, n_docs: int) -> Optional[float]:
    """Document-level npmi.

    Thin delegate to :func:`_npmi` so both granularities share identical
    edge-case semantics — gate B compares the two values, so any drift in
    one would silently change which pairs are filtered.
    """
    return _npmi(co_doc, doc_df_a, doc_df_b, n_docs)


def _kw_ids_for(conn, words: FrozenSet[str]) -> FrozenSet[int]:
    """Map a set of keyword texts to the set of matching keyword ids."""
    if not words:
        return frozenset()
    ph = ",".join("?" for _ in words)
    rows = conn.execute(
        f"SELECT id FROM keywords WHERE kw IN ({ph})",
        tuple(words),
    ).fetchall()
    return frozenset(int(r[0]) for r in rows)


def _get_complementary_stopword_kw_ids(conn) -> FrozenSet[int]:
    """Keyword ids blocked at the complementary-pair entry point.

    Union of :data:`_INDEX_STOPWORDS` (phase-1 gate) and
    :data:`_COMPLEMENTARY_STOPWORDS` (iteration-2 narrative stopwords).
    The complementary extractor is the only consumer: the narrative set
    must not leak into main-index tokenisation / pair statistics.
    """
    return _kw_ids_for(conn, _INDEX_STOPWORDS | _COMPLEMENTARY_STOPWORDS)


def _passes_strength_gate(cooccur_count: float, expected_count: float) -> bool:
    """Iteration-2 c/E strength gate (plan §迭代 2).

    A pair is only complementary enough when its observed/expected
    co-occurrence ratio is *below* :data:`COMPLEMENTARY_MAX_C_OVER_E`.
    In a large corpus a mildly-below-random pair still reaches a
    significant p-value through sample size alone, so the p-value gate
    cannot tell weak negative correlation from semantic complementarity.
    Pure subtraction and independent of the segregation switch. Shared by
    both backends so the two can never drift.
    """
    if expected_count <= 0:
        return False
    return cooccur_count / expected_count < COMPLEMENTARY_MAX_C_OVER_E


def _evaluate_complementary_candidate(
    *,
    cooccur_count: int,
    df_a: int,
    df_b: int,
    npmi: Optional[float],
    n_chunks: int,
) -> Optional[Tuple[float, float]]:
    """Shared per-candidate gate for both complementary backends.

    Returns ``(npmi, p_value)`` when the pair passes the glued,
    negative-npmi, c/E strength and significance gates, else ``None``.
    Callers keep stopword filtering and their own row-streaming control
    flow; everything that decides *which statistics get stored* lives
    here so the SQL and batch backends cannot drift.
    """
    if n_chunks <= 0 or df_a <= 0 or df_b <= 0:
        return None
    if npmi is None or npmi >= 0.0:
        return None
    if df_a == df_b == cooccur_count:
        return None
    if not _passes_strength_gate(cooccur_count, df_a * df_b / n_chunks):
        return None
    p_val = _hypergeom_lower_tail(cooccur_count, n_chunks, df_a, df_b)
    if p_val >= BUILD_COMPLEMENTARY_MAX_P:
        return None
    return (float(npmi), float(p_val))


def _complementary_rows(
    *,
    scope_id: int,
    candidates: Sequence[Tuple[int, int, float, float, float]],
    chunk_df: Dict[int, int],
    n_chunks: int,
    now: str,
) -> List[Tuple[int, int, int, float, float, float, float, str]]:
    """Build the ``complementary_pairs`` INSERT tuples for survivors.

    Shared by both backends so the stored columns (including the
    recomputed ``expected_count``) are assembled identically.
    """
    return [
        (
            scope_id, a, b, float(co),
            float(chunk_df.get(a, 0) * chunk_df.get(b, 0) / n_chunks),
            float(npmi_val), float(p_val), now,
        )
        for a, b, co, npmi_val, p_val in candidates
    ]


def _has_doc_evidence(conn, scope_id: int) -> bool:
    """True when the scope has doc-level evidence (``keyword_docs``).

    The evidence layer is machine-local and is intentionally wiped on a
    knowledge-pack transfer or by ``grga index forget --all``. When it is
    absent, document-level co-occurrence is unknowable and the
    segregation gate must be skipped (never used to filter everything
    out). Knowledge-only scopes (post-``import``) hit this path.
    """
    return conn.execute(
        "SELECT 1 FROM keyword_docs WHERE scope_id = ? LIMIT 1",
        (scope_id,),
    ).fetchone() is not None


def _compute_co_doc_batch(
    conn,
    scope_id: int,
    pairs: Sequence[Tuple[int, int]],
) -> Dict[Tuple[int, int], int]:
    """Compute document-level co-occurrence for a batch of keyword pairs.

    Uses a temp-table + self-join on ``keyword_docs``. Returns a dict
    ``{(a, b): co_doc}`` keyed by the *normalised* pair (smaller id
    first) because ``keyword_docs`` is undirected. Pairs with zero
    co-doc are not included in the result.
    """
    if not pairs:
        return {}
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _comp_pair_probe ("
        "  kw_a_id INTEGER NOT NULL,"
        "  kw_b_id INTEGER NOT NULL"
        ")"
    )
    conn.execute("DELETE FROM _comp_pair_probe")
    conn.executemany(
        "INSERT INTO _comp_pair_probe (kw_a_id, kw_b_id) VALUES (?, ?)",
        [(int(a), int(b)) for a, b in pairs],
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS _idx_comp_probe_a "
        "ON _comp_pair_probe (kw_a_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS _idx_comp_probe_b "
        "ON _comp_pair_probe (kw_b_id)"
    )
    rows = conn.execute(
        """
        SELECT p.kw_a_id, p.kw_b_id, COUNT(*) AS co_doc
        FROM _comp_pair_probe p
        CROSS JOIN keyword_docs ka
          ON ka.scope_id = ? AND ka.kw_id = p.kw_a_id
        CROSS JOIN keyword_docs kb
          ON kb.scope_id = ? AND kb.kw_id = p.kw_b_id
         AND kb.doc_id = ka.doc_id
        GROUP BY p.kw_a_id, p.kw_b_id
        """,
        (scope_id, scope_id),
    ).fetchall()
    result: Dict[Tuple[int, int], int] = {}
    for r in rows:
        a, b = int(r[0]), int(r[1])
        key = (a, b) if a < b else (b, a)
        result[key] = int(r[2])
    return result


def _is_segregation_pair(
    *,
    co_doc: int,
    doc_df_a: int,
    doc_df_b: int,
    n_docs: int,
    npmi_chunk: float,
) -> bool:
    """Return True if the pair's negative npmi is driven by document-type
    segregation rather than semantic complementarity.

    Two-gate check (plan-2026-09-12 §阶段 2):
    - Gate A (weak): co_doc must be at least a small fraction of the
      smaller doc_df, with a hard floor (capped at 3) — purely an
      existence-of-overlap sanity check, not a quality gate on its own.
    - Gate B (strong): if doc-level npmi is significantly *lower*
      (more negative) than chunk-level npmi, the negative signal is
      dominated by file-type separation → drop.

    For very small document counts (≤2), segregation detection is
    statistically meaningless; we short-circuit to False (not
    segregation) so all complementary pairs survive.
    """
    if n_docs <= 0 or doc_df_a <= 0 or doc_df_b <= 0:
        return True
    if n_docs <= 2:
        return False
    min_df = min(doc_df_a, doc_df_b)
    min_co_doc = max(
        COMP_SEGREGATION_MIN_CO_DOC_FLOOR,
        int(math.floor(math.sqrt(min_df) * COMP_SEGREGATION_MIN_CO_DOC_RATIO)),
    )
    min_co_doc = min(3, min_co_doc)
    if co_doc < min_co_doc:
        return True
    npmi_doc = _npmi_doc(co_doc, doc_df_a, doc_df_b, n_docs)
    if npmi_doc is None:
        return True
    if npmi_doc < npmi_chunk - COMP_SEGREGATION_NPMI_GAP:
        return True
    return False


def _filter_segregation_candidates(
    conn,
    *,
    scope_id: int,
    n_docs: int,
    candidates: Sequence[Tuple[int, int, float, float, float]],
    doc_df: Optional[Dict[int, int]] = None,
) -> List[Tuple[int, int, float, float, float]]:
    """Drop complementary candidates driven by file-type segregation.

    Shared by both complementary backends (SQL and batch) so the gate can
    never drift between them. ``candidates`` are 5-tuples
    ``(kw_a_id, kw_b_id, cooccur, npmi_chunk, p_value)``; survivors are
    returned in the same shape with identical statistics (pure
    subtraction). Lazily computes document-level co-occurrence only for
    the candidates handed in.

    ``doc_df`` is the scope's ``{kw_id: doc_df}`` map. Callers that invoke
    this per pagination batch should load it once and pass it in, so the
    full ``keyword_stats`` scan is not repeated per batch; when omitted it
    is read here (direct/test callers).
    """
    if not candidates:
        return []
    if doc_df is None:
        doc_df = {
            int(r[0]): int(r[1]) for r in conn.execute(
                "SELECT kw_id, doc_df FROM keyword_stats WHERE scope_id = ?",
                (scope_id,),
            )
        }
    co_doc_map = _compute_co_doc_batch(
        conn, scope_id, [(a, b) for a, b, *_ in candidates],
    )
    survivors: List[Tuple[int, int, float, float, float]] = []
    for a, b, co, npmi_chunk, p_val in candidates:
        key = (a, b) if a < b else (b, a)
        if _is_segregation_pair(
            co_doc=co_doc_map.get(key, 0),
            doc_df_a=doc_df.get(a, 0),
            doc_df_b=doc_df.get(b, 0),
            n_docs=n_docs,
            npmi_chunk=npmi_chunk,
        ):
            continue
        survivors.append((a, b, co, npmi_chunk, p_val))
    return survivors


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
    conn, scope_id: int, directory: Optional[str] = None,
) -> Dict[str, Dict[str, object]]:
    """Load ``{doc_path: {id, size, mtime, content_hash}}`` for a scope.

    When ``directory`` is provided, only rows for that binding are
    returned — otherwise the incremental-build classifier would mix up
    docs from other directory bindings attached to the same scope.
    When ``directory`` is ``None`` (legacy / caller convenience) all
    scope docs are loaded; callers that participate in incremental
    builds should always pass the binding they are scanning.

    Used by ``mode='build'`` to classify each file on disk against the
    stored record without an N+1 SELECT loop. ``id`` is included so
    per-doc DELETEs (removed / updated) do not need a second lookup.
    """
    if directory is not None:
        rows = conn.execute(
            "SELECT id, doc_path, size, mtime, content_hash "
            "FROM docs WHERE scope_id = ? AND directory = ?",
            (scope_id, directory),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, doc_path, size, mtime, content_hash "
            "FROM docs WHERE scope_id = ?",
            (scope_id,),
        ).fetchall()
    out: Dict[str, Dict[str, object]] = {}
    for row in rows:
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
    ids = list(doc_ids)
    for chunk in sql_var_chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM docs WHERE id IN ({placeholders})",
            chunk,
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
    ids = list(doc_ids)
    for chunk in sql_var_chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM keyword_docs WHERE doc_id IN ({placeholders})",
            chunk,
        )
        conn.execute(
            f"DELETE FROM heading_docs WHERE doc_id IN ({placeholders})",
            chunk,
        )
        # Zero the per-doc chunk count so ``scope_stats.n_chunks`` recomputes
        # correctly after pass 2 refills it.
        conn.execute(
            f"UPDATE docs SET n_chunks_with_kw = 0 WHERE id IN ({placeholders})",
            chunk,
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


def _resolve_cache_mb() -> int:
    """Resolve SQLite page cache budget in MB.

    Priority:
    1. ``GRGA_BUILD_CACHE_MB`` env var — explicit override, clamped to [1, 8192].
    2. **Adaptive** (default): 1/4 of available system RAM, floor 512 MB,
       ceiling 4096 MB. Uses ``psutil.virtual_memory().available``.
    3. Fallback (psutil unavailable): 256 MB.
    """
    env_val = os.environ.get("GRGA_BUILD_CACHE_MB")
    if env_val is not None:
        try:
            cache_mb = int(env_val)
            if cache_mb <= 0:
                raise ValueError
            return min(8192, cache_mb)
        except (TypeError, ValueError):
            pass
    try:
        import psutil  # noqa: PLC0415
        avail_mb = psutil.virtual_memory().available / (1024 * 1024)
        cache_mb = max(512, min(int(avail_mb / 4), 4096))
        return cache_mb
    except ImportError:
        return 256


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
    # PR-8: enlarge the page cache. Default is **adaptive**:
    # 1/4 of available RAM, floor 512 MB, ceiling 4096 MB.
    # Override via ``GRGA_BUILD_CACHE_MB`` env var.
    cache_mb = _resolve_cache_mb()
    conn.execute(f"PRAGMA cache_size = {-cache_mb * 1024}")
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


# ---------------------------------------------------------------------------
# Batched build pipeline (batched_build_2026_07_16).
#
# Ported from ``experiments/batched_build_2026_07_16/batched_bench.py``
# ``batched_build()``. Only engaged when ``build_graph_index`` is called
# with ``batch_chunks > 0`` (rebuild + refresh_ngram="replace" only for
# this first integration). Design + frozen decisions live in
# ``experiments/batched_build_2026_07_16/{SPEC,QUESTIONS,FINDINGS}.md``.
#
# The batched path processes the corpus in file-aligned greedy batches,
# running per-batch phrase detection + top-N (direction-3 npmi-ranked)
# keyword sampling, dumping (doc_id, chunk_id, kw_id) triples into the
# same cross-batch ``_build_chunk_kws`` TEMP TABLE the single-pass path
# uses. ``_finalize_pairs_from_temp`` (unchanged) does global pair
# aggregation at the end. See SPEC.md §2.
# ---------------------------------------------------------------------------

# Direction-3 fallback score for a kw with no prior-batch npmi evidence.
# Slightly above the default min_npmi=0.2 so unseen-but-plausible kws are
# not starved out of the top-N cap on cold batches.
_BATCHED_UNKNOWN_KW_PRIOR = 0.25


def _resolve_top_n_kw(top_n_kw: Optional[int]) -> int:
    """Resolve top_n_kw: explicit arg > GRGA_BATCH_TOP_N env > default 64."""
    if top_n_kw is not None:
        return max(1, int(top_n_kw))
    try:
        v = int(os.environ.get("GRGA_BATCH_TOP_N", "64"))
        return v if v > 0 else 64
    except (TypeError, ValueError):
        return 64


def _pack_batches_by_docs(
    docs: List[tuple], target_chunks: int,
) -> List[List[tuple]]:
    """File-aligned greedy batch packing over Pass-0 ``docs`` entries.

    Adapted from the prototype's ``pack_batches`` to consume already-scanned
    ``docs`` entries (``(doc_id, rel_path, path, is_md, chunks, ...)``)
    rather than re-reading files. Sorts by ``rel_path`` so same-directory
    files stay together, then greedily fills batches to ``~target_chunks``
    non-empty chunks. A whole file never straddles a batch; a single file
    larger than ``target_chunks`` becomes its own over-sized batch (Q1
    soft cap).
    """
    # Sort by rel_path (entry[1]) for locality; ties fall back to doc_id.
    ordered = sorted(docs, key=lambda e: (str(e[1]), int(e[0])))
    batches: List[List[tuple]] = []
    cur: List[tuple] = []
    cur_chunks = 0
    for entry in ordered:
        n = sum(1 for c in entry[4] if c.text.strip())
        if n <= 0:
            # Keep the doc in a batch anyway so its metadata is refreshed,
            # but it contributes zero chunks toward the target.
            if not cur:
                cur.append(entry)
                continue
        if cur_chunks > 0 and cur_chunks + n > target_chunks:
            batches.append(cur)
            cur = []
            cur_chunks = 0
        cur.append(entry)
        cur_chunks += n
    if cur:
        batches.append(cur)
    return batches


def _compute_prior_kw_scores(
    conn, scope_id: int, n_chunks_prior: int,
) -> Dict[int, float]:
    """Compute ``{kw_id -> max npmi across pairs it participates in}``.

    Uses the accumulated ``keyword_pairs.cooccur_count`` +
    ``keyword_stats.chunk_df`` from PRIOR batches. Returns ``{}`` on
    cold start (batch 1, ``n_chunks_prior <= 0``). npmi is computed
    on-the-fly here because ``keyword_pairs.npmi`` is NULL during the
    batch loop (``_finalize_pairs_from_temp`` step 4 has not run yet).
    """
    if n_chunks_prior <= 0:
        return {}
    kw_score: Dict[int, float] = {}
    for kw_a, kw_b, cc, df_a, df_b in conn.execute(
        """
        SELECT p.kw_a_id, p.kw_b_id, p.cooccur_count, sa.chunk_df, sb.chunk_df
        FROM keyword_pairs p
        JOIN keyword_stats sa ON sa.kw_id=p.kw_a_id AND sa.scope_id=?
        JOIN keyword_stats sb ON sb.kw_id=p.kw_b_id AND sb.scope_id=?
        WHERE p.scope_id=? AND p.source=?
        """,
        (scope_id, scope_id, scope_id, CHUNK_COOCCUR_SOURCE),
    ):
        cc = int(cc); df_a = int(df_a); df_b = int(df_b)
        if cc <= 0 or df_a <= 0 or df_b <= 0:
            continue
        p_ab = cc / n_chunks_prior
        if p_ab <= 0 or p_ab >= 1.0:
            continue
        denom = -math.log(p_ab)
        if denom <= 0:
            continue
        pmi = math.log(cc * n_chunks_prior / (df_a * df_b))
        npmi = pmi / denom
        if npmi > kw_score.get(int(kw_a), -1.0):
            kw_score[int(kw_a)] = npmi
        if npmi > kw_score.get(int(kw_b), -1.0):
            kw_score[int(kw_b)] = npmi
    return kw_score


def _rank_and_cap_kws(
    kw_ids: List[int],
    phrase_id_set: set,
    global_chunk_df: Dict[int, int],
    prior_kw_npmi: Dict[int, float],
    prior_kw_seen: set,
    n: int,
) -> List[int]:
    """Direction-3 top-N cap: rank kws by their max participating npmi.

    Sort keys (all ASC; ``(bool, negated_score)`` yields DESC semantics):

      1. is_new (0=new, 1=seen) — Q1: new kws always win, so cross-batch
         connections stay discoverable.
      2. neg_npmi — DESC by prior participating npmi; kws with no prior
         evidence fall back to ``_BATCHED_UNKNOWN_KW_PRIOR``.
      3. is_phrase (0=phrase, 1=not) — multi-char learned phrases first.
      4. chunk_df ASC — rarer kws first (tiebreaker).
      5. kw_id ASC — deterministic final tiebreaker.
    """
    if len(kw_ids) <= n:
        return kw_ids

    def _key(k):
        is_new = 0 if k not in prior_kw_seen else 1
        neg_score = -prior_kw_npmi.get(k, _BATCHED_UNKNOWN_KW_PRIOR)
        is_phrase = 0 if k in phrase_id_set else 1
        df = global_chunk_df.get(k, 0)
        return (is_new, neg_score, is_phrase, df, k)

    return sorted(kw_ids, key=_key)[:n]


def _run_batched_pipeline(
    conn,
    *,
    scope_id: int,
    docs: List[tuple],
    refresh_ngram: str = "replace",
    total_scanned_chunks: int,
    batch_target_chunks: int,
    top_n_kw: int,
    max_ngram_n: int,
    ngram_iters: int,
    interner: "IdInterner",
    auto_dict: Dict[str, dict],
    stats: "BuildStats",
    now: str,
    on_event: Optional[Callable[[dict], None]] = None,
) -> None:
    """Batched build pipeline — produces the same output state as the
    single-pass Phase A/B + Pass 1.5 + Pass 2 block.

    Populates ``_build_chunk_kws`` (doc_id, chunk_id, kw_id triples),
    ``keyword_docs``, ``heading_docs``, ``docs.n_chunks_with_kw``,
    ``docs`` metadata, ``auto_ngram_dict`` (via count-add UPSERT), mutates
    ``auto_dict`` in place, and updates the relevant ``stats`` counters.
    Pair aggregation (``_finalize_pairs_from_temp``) + keyword_stats /
    scope_stats refresh run in the caller after this returns, reading the
    triples this function staged.
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

    # Cross-batch persistent TEMP TABLE for pair aggregation (Q9). The
    # single-pass path creates this inside Pass 2; the batched path owns
    # it here. Clear in case of a prior aborted run on a reused conn.
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _build_chunk_kws ("
        "  doc_id INTEGER NOT NULL, "
        "  chunk_id INTEGER NOT NULL, "
        "  kw_id INTEGER NOT NULL"
        ")"
    )
    conn.execute("DELETE FROM _build_chunk_kws")

    prior_dict_size = len(auto_dict)
    # Persist any seed / pre-existing dict entries up front so a phrase that
    # never re-detects in a later batch still lands in auto_ngram_dict.
    if auto_dict:
        _persist_auto_dict(conn, scope_id, auto_dict.values(), now)

    batches = _pack_batches_by_docs(docs, batch_target_chunks)
    n_batches = len(batches)
    _emit(
        "materialise_start",
        total_docs=len(docs),
        total_chunks=total_scanned_chunks,
    )

    n_chunks_total = 0
    kw_docs_rows: List[Tuple[int, int, int, int, int]] = []
    TRIPLES_FLUSH = 100_000

    for bi, batch in enumerate(batches):
        _emit(
            "batch_start",
            batch=bi + 1,
            total=n_batches,
            batch_files=len(batch),
        )
        _t0 = _t_last = time.perf_counter()
        _emit(
            "ngram_iter_start",
            iter=1, iters=1,
            chunks=total_scanned_chunks,
            phrases=len(auto_dict),
        )

        # 1. First-tokenize each chunk with the current accumulated dict.
        jieba_tok = _new_jieba_tokenizer(auto_dict.keys())
        phrase_set = set(auto_dict.keys())
        batch_chunk_meta: List[Tuple[int, int, str, bool]] = []
        _t_read = time.perf_counter()
        line_tokens_for_detect: List[List[str]] = []
        for entry in batch:
            doc_id, rel_path, path, is_md, chunks = entry[:5]
            for chunk in chunks:
                if not chunk.text.strip():
                    continue
                toks = _tokenise_chunk(
                    chunk.text, jieba_tok, phrase_set, max_ngram_n
                )
                batch_chunk_meta.append(
                    (doc_id, chunk.chunk_id, chunk.text, is_md)
                )
                if toks:
                    line_tokens_for_detect.append(toks)

        # 2/3. Detect phrases for this batch, UPSERT-add into auto_dict + DB.
        # ``refresh_ngram="never"`` skips detection entirely and reuses the
        # loaded auto_dict as-is (still re-tokenises + stages triples below).
        if refresh_ngram == "never":
            new_phrases = []
        else:
            new_phrases = detect_ngram_phrases(
                line_tokens_for_detect, max_n=max_ngram_n, conn=conn,
            )
        _t_ngram = time.perf_counter()
        del line_tokens_for_detect
        added_any = False
        for pdata in new_phrases:
            ph = pdata["phrase"]
            if ph not in auto_dict:
                auto_dict[ph] = dict(pdata)
                added_any = True
            else:
                auto_dict[ph]["count"] = (
                    int(auto_dict[ph].get("count", 0)) + int(pdata["count"])
                )
        # Count-only merge into auto_ngram_dict (Q3): existing rows keep
        # first-seen npmi/entropy, only count accumulates.
        if new_phrases:
            merge_rows = [
                (scope_id, e["phrase"], int(e["n"]), int(e["count"]),
                 float(e["npmi"]), float(e["branch_ent"]), now)
                for e in new_phrases
            ]
            conn.executemany(
                """
                INSERT INTO auto_ngram_dict
                    (scope_id, phrase, n, count, npmi, branch_ent, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id, phrase) DO UPDATE SET
                    count = count + excluded.count,
                    updated_at = excluded.updated_at
                """,
                merge_rows,
            )

        # 4. Re-tokenize with updated dict (PR-4 pattern, bit-exact det.).
        if added_any:
            jieba_tok = _new_jieba_tokenizer(auto_dict.keys())
            phrase_set = set(auto_dict.keys())
        chunk_tokens: Dict[Tuple[int, int], List[str]] = {}
        for doc_id, chunk_id, text, _is_md in batch_chunk_meta:
            chunk_tokens[(doc_id, chunk_id)] = _tokenise_chunk(
                text, jieba_tok, phrase_set, max_ngram_n
            )

        batch_n_chunks = len(batch_chunk_meta)
        n_chunks_total += batch_n_chunks

        # 5. Per-batch stopwords derived from this batch's tokens.
        stopwords = _derive_scope_stopword_candidates(chunk_tokens)
        _emit(
            "stopword_derived",
            count=len(stopwords),
            chars=sorted(stopwords),
            threshold=STOPWORD_DF_THRESHOLD,
            batch=bi + 1,
        )

        # Intern all kw ids for this batch in one shot.
        all_kws: set = set()
        for toks in chunk_tokens.values():
            if toks:
                all_kws.update(_keywords_from_tokens(toks).keys())
        kw_id_map = interner.kw_ids(sorted(all_kws)) if all_kws else {}

        # 6/7. Prior evidence for direction-3 ranking (from PRIOR batches).
        global_chunk_df: Dict[int, int] = {}
        prior_kw_seen: set = set()
        for r in conn.execute(
            "SELECT kw_id, chunk_df FROM keyword_stats WHERE scope_id = ?",
            (scope_id,),
        ):
            global_chunk_df[int(r[0])] = int(r[1])
            prior_kw_seen.add(int(r[0]))
        n_chunks_prior = n_chunks_total - batch_n_chunks
        prior_kw_npmi = _compute_prior_kw_scores(
            conn, scope_id, n_chunks_prior,
        )
        phrase_id_set: set = set()
        for ph in auto_dict.keys():
            kid = kw_id_map.get(ph)
            if kid is not None and len(ph) > 1:
                phrase_id_set.add(kid)

        # 8. Per-chunk: kw extraction, 3b filter, top-N cap, dump triples,
        #    accumulate per-doc kw_counts for keyword_docs.
        per_doc_agg: Dict[int, Dict[int, Tuple[int, int]]] = defaultdict(dict)
        per_doc_n_chunks_with_kw: Counter = Counter()
        docs_in_batch: Dict[int, tuple] = {}
        for entry in batch:
            docs_in_batch[int(entry[0])] = entry
        triples_buf: List[Tuple[int, int, int]] = []
        for (doc_id, chunk_id), toks in chunk_tokens.items():
            if not toks:
                continue
            kw_counts = _keywords_from_tokens(toks)
            if not kw_counts:
                continue
            per_doc_n_chunks_with_kw[doc_id] += 1
            agg = per_doc_agg[doc_id]
            for kw, occ in kw_counts.items():
                kid = kw_id_map[kw]
                prev_occ, prev_chunks = agg.get(kid, (0, 0))
                agg[kid] = (prev_occ + int(occ), prev_chunks + 1)
            # Pair enumeration uses the 3b-filtered + top-N-capped set.
            pair_kws = _filter_chunk_kws_for_pairs(
                list(kw_counts.keys()), stopword_candidates=stopwords,
            )
            if pair_kws:
                pids = list({kw_id_map[k] for k in pair_kws})
                capped = _rank_and_cap_kws(
                    pids, phrase_id_set, global_chunk_df,
                    prior_kw_npmi, prior_kw_seen, top_n_kw,
                )
                for kid in capped:
                    triples_buf.append((doc_id, chunk_id, kid))
                    if len(triples_buf) >= TRIPLES_FLUSH:
                        conn.executemany(
                            "INSERT INTO _build_chunk_kws "
                            "(doc_id, chunk_id, kw_id) VALUES (?, ?, ?)",
                            triples_buf,
                        )
                        triples_buf.clear()
        if triples_buf:
            conn.executemany(
                "INSERT INTO _build_chunk_kws (doc_id, chunk_id, kw_id) "
                "VALUES (?, ?, ?)",
                triples_buf,
            )
            triples_buf.clear()

        # 9. keyword_docs UPSERT-add + heading_docs + docs metadata.
        for doc_id, entry in docs_in_batch.items():
            _, rel_path, path, is_md, chunks = entry[:5]
            _, st_size, st_mtime, new_hash = entry[5:]
            agg = per_doc_agg.get(doc_id, {})
            for kid, (occ_sum, chunk_cnt) in agg.items():
                kw_docs_rows.append(
                    (scope_id, kid, doc_id, occ_sum, chunk_cnt)
                )
            n_chunks_with_kw_for_doc = int(per_doc_n_chunks_with_kw.get(doc_id, 0))
            conn.execute(
                "UPDATE docs SET n_chunks_with_kw = ? WHERE id = ?",
                (n_chunks_with_kw_for_doc, int(doc_id)),
            )
            if agg:
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

            _update_doc_metadata(conn, doc_id, st_size, st_mtime, new_hash)

        _t_write = time.perf_counter()
        # 10. Per-batch self-join  UPSERT-add into keyword_pairs.
        _run_self_join(
            conn, scope_id=scope_id,
            min_pair_count=resolve_min_pair_count(batch_n_chunks),
            now=now,
        )
        _t_pairs = time.perf_counter()
        cumulative_pairs = int(conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source = ?",
            (scope_id, CHUNK_COOCCUR_SOURCE),
        ).fetchone()[0])
        _emit(
            "pairs_batch_advance",
            batch=bi + 1, n_batches=n_batches,
            cumulative_pairs=cumulative_pairs,
            current=bi + 1, total=n_batches,
        )
        conn.execute("DELETE FROM _build_chunk_kws")

        # Flush this batch's keyword_docs rows so peak Python heap stays
        # bounded by one batch instead of the whole corpus.
        if kw_docs_rows:
            conn.executemany(
                """
                INSERT INTO keyword_docs
                    (scope_id, kw_id, doc_id, occurrence_count, chunk_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(kw_id, doc_id) DO UPDATE SET
                    occurrence_count = occurrence_count + excluded.occurrence_count,
                    chunk_count = chunk_count + excluded.chunk_count
                """,
                kw_docs_rows,
            )
            kw_docs_rows.clear()

        # Refresh keyword_stats deltas from this batch so the NEXT batch's
        # direction-3 ranking sees accumulated chunk_df / prior evidence.
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
        conn.commit()

        _emit(
            "ngram_iter_end",
            iter=1, iters=1,
            chunks=batch_n_chunks,
            phrases=len(auto_dict),
        )
        _emit(
            "batch_end",
            batch=bi + 1,
            total=n_batches,
            batch_files=len(batch),
            batch_chunks=batch_n_chunks,
            new_phrases_this_batch=len(new_phrases),
            dict_size=len(auto_dict),
        )
        _emit("debug_timing", batch=bi+1, n_batches=n_batches,
              wall_read_ms=round((_t_ngram - _t0) * 1000),
              wall_ngram_ms=round((_t_write - _t_ngram) * 1000),
              wall_write_ms=round((_t_pairs - _t_write) * 1000),
              wall_pairs_ms=round((time.perf_counter() - _t_pairs) * 1000),
              wall_batch_ms=round((time.perf_counter() - _t0) * 1000))
        del chunk_tokens, kw_id_map, global_chunk_df, prior_kw_seen
        del prior_kw_npmi, phrase_id_set, per_doc_agg, batch_chunk_meta

    # ngram stats: batched does one detection iter per batch; report 1
    # conceptually for the whole run.
    stats.ngram_iters_run = 1 if n_batches else 0
    stats.ngram_phrases_written = len(auto_dict)
    stats.ngram_dict_size = len(auto_dict)
    stats.ngram_dict_added = max(0, len(auto_dict) - prior_dict_size)
    stats.keyword_chunks_written = int(conn.execute(
        "SELECT COUNT(*) FROM keyword_docs WHERE scope_id = ?",
        (scope_id,),
    ).fetchone()[0])
    _emit(
        "materialise_end",
        total_docs=len(docs),
        n_batches=n_batches,
        n_chunks=n_chunks_total,
    )


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
    batch_chunks: int = 0,
    top_n_kw: Optional[int] = None,
    replace: Optional[bool] = None,
    ngram_iters: int = DEFAULT_NGRAM_ITERS,
    max_ngram_n: int = DEFAULT_MAX_NGRAM_N,
    seed_ngram_from: Optional[str] = None,
    tags: Sequence[str] = (),
    include_globs: Iterable[str] = (),
    exclude_globs: Iterable[str] = (),
    progress: Optional[Callable[[str], None]] = None,
    on_event: Optional[Callable[[dict], None]] = None,
    keep_dict: bool = False,
    keep_pairs: bool = False,
    recompute_pairs: bool = False,
    filter_comp_segregation: bool = True,
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
        include_globs / exclude_globs: optional path filters
            (plan-2026-09-07-build-glob-filter). Each is normalized via
            :func:`grga.search._normalize_glob_patterns` and matched
            against the **relative** path (from ``directory``) with loose,
            case-insensitive fnmatch semantics (``*`` crosses ``/``).
            Empty include = accept all; exclude wins over include. Files
            rejected by the glob gates still count as "seen in this scan"
            so weak-semantics incremental builds never delete their rows.
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

    # Batched build (batched_build_2026_07_16) restrictions. First
    # integration only supports rebuild + refresh_ngram="replace" — the
    # exact configuration the M10 experiment validated. Incremental
    # support (Q1 grow-and-refine batches) is a follow-up.
    if batch_chunks > 0:
        # Batched now supports both rebuild and incremental (build) modes.
        # The only genuinely-invalid combo is mode='build' + refresh='replace'
        # (replace clears the whole dict — meaningless on incremental), and
        # that is already rejected by the shared guard below (line ~2425).
        if mode not in ("rebuild", "build"):
            raise ValueError(
                f"batch_chunks > 0: unknown mode {mode!r} "
                "(expected 'rebuild' or 'build')"
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
            existing_docs = _load_scope_doc_rows(
                conn, scope_id, directory=directory_str,
            )

        if mode == "rebuild":
            # Full clear of index + derived layers for this scope.
            # ``keyword_chunks`` / ``chunks`` / ``heading_docs`` are keyed
            # by ``doc_id`` which resolves through ``docs.scope_id``. The
            # shared helper keeps the three-table set in one place.
            #
            # With --keep-pairs, keyword_pairs / complementary_pairs /
            # keyword_stats / scope_stats are preserved (knowledge
            # assets); new evidence is merged in via UPSERT-add
            # semantics in the pair finalization step.
            #
            # With --keep-dict, auto_ngram_dict is preserved (equivalent
            # to refresh_ngram='never' but expressed as a high-level
            # knowledge-preservation flag).
            _delete_scope_index_rows(conn, scope_id)
            if not keep_pairs:
                conn.execute(
                    "DELETE FROM keyword_pairs WHERE scope_id = ? AND source = ?",
                    (scope_id, CHUNK_COOCCUR_SOURCE),
                )
                conn.execute(
                    "DELETE FROM complementary_pairs WHERE scope_id = ?",
                    (scope_id,),
                )
                conn.execute(
                    "DELETE FROM keyword_stats WHERE scope_id = ?",
                    (scope_id,),
                )
                conn.execute(
                    "DELETE FROM scope_stats WHERE scope_id = ?",
                    (scope_id,),
                )
            if not keep_dict and refresh_ngram == "replace":
                conn.execute(
                    "DELETE FROM auto_ngram_dict WHERE scope_id = ?",
                    (scope_id,),
                )
            if keep_dict and refresh_ngram == "replace":
                # --keep-dict overrides refresh_ngram=replace — keep the
                # dict and use it as-is (same as refresh_ngram='never').
                refresh_ngram = "never"
                stats.refresh_ngram = "never"
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

        def _rel_path_of(path: Path) -> str:
            try:
                return str(path.resolve().relative_to(scope_path))
            except ValueError:
                return str(path.resolve())

        # Weak-semantics glob filtering (plan-2026-09-07 §2.3): a file
        # excluded by ``include_globs`` / ``exclude_globs`` still exists on
        # disk and must count as "seen in this scan" — otherwise the
        # removed-detection pass below would misjudge it as disappeared and
        # delete its rows. ``iter_text_files`` reports exactly those files
        # through this callback.
        def _mark_seen(path: Path) -> None:
            stats.files_scanned += 1
            seen_rel_paths.add(_rel_path_of(path))

        _emit("scan_start")
        for path in iter_text_files(
            scope_path,
            max_file_bytes=max_file_bytes,
            ignore_dirs=ignore_dirs,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            on_skip=stats.bump_reason,
            on_glob_excluded=_mark_seen,
        ):
            stats.files_scanned += 1
            rel_path = _rel_path_of(path)
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
                doc_id = int(prev_row["id"]) if prev_row else interner.doc_id(scope_id, rel_path, directory=directory_str)
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
                doc_id = interner.doc_id(scope_id, rel_path, directory=directory_str)
                stats.docs_added += 1
            else:  # DOC_UPDATED
                doc_id = int(prev_row["id"]) if prev_row else interner.doc_id(scope_id, rel_path, directory=directory_str)
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
        # Dispatch: batched pipeline (batched_build_2026_07_16) if
        # ``batch_chunks > 0``, else the default single-pass path.
        # The batched pipeline replaces Pass 1 (Phase A + B), Pass 1.5
        # (stopword derivation), and Pass 2 (chunk-kw materialisation +
        # keyword_docs / heading_docs / docs metadata + _build_chunk_kws
        # staging). Downstream (Pass 3 = ``_finalize_pairs_from_temp``,
        # scope_stats stamp, toolchain fingerprint) is shared.
        # ------------------------------------------------------------------
        if batch_chunks > 0:
            # ``refresh_ngram="replace"`` wiped the scope's auto_ngram_dict
            # rows above → loads empty. ``append``/``never`` (incremental
            # or additive rebuild) load the accumulated dict so per-batch
            # detection UPSERT-adds onto prior phrases (and ``never`` reuses
            # them without any detection).
            auto_dict: Dict[str, dict] = _load_auto_dict(conn, scope_id)
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
            # Incremental: clear changed docs' stale index rows (keyword_docs
            # / heading_docs / n_chunks_with_kw) before re-materialising them,
            # exactly as the single-pass Pass 2 does. Rebuild already wiped
            # the whole scope above, so this is a no-op there.
            if mode == "build" and docs:
                _delete_doc_index_rows_keep_docs(
                    conn, [entry[0] for entry in docs]
                )
            resolved_top_n = _resolve_top_n_kw(top_n_kw)
            _run_batched_pipeline(
                conn,
                scope_id=scope_id,
                docs=docs,
                refresh_ngram=refresh_ngram,
                total_scanned_chunks=total_scanned_chunks,
                batch_target_chunks=int(batch_chunks),
                top_n_kw=resolved_top_n,
                max_ngram_n=max_ngram_n,
                ngram_iters=max(1, int(ngram_iters)),
                interner=interner,
                auto_dict=auto_dict,
                stats=stats,
                now=now,
                on_event=on_event,
            )
            # Metadata-only refresh (touch / stable rga output) — the
            # single-pass path does this inline after Pass 2; batched does
            # it here to stay parity.
            for doc_id, st_size, st_mtime, new_hash in metadata_only_updates:
                _update_doc_metadata(conn, doc_id, st_size, st_mtime, new_hash)
            # Remove docs that disappeared from disk. Not currently exercised
            # (batched is rebuild-only in this integration), but symmetric
            # with the single-pass tail so a future incremental+batched path
            # inherits the correct semantics for free.
            if removed_doc_ids:
                _delete_doc_index_rows(conn, removed_doc_ids)
        else:
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
                            toks = _tokenise_chunk(
                                chunk.text, _jt, _ps, max_ngram_n
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
                    # Unconditional raise (not ``assert``) so the invariant
                    # survives ``python -O`` — under -O an assert is stripped and
                    # this branch would silently no-op (generator never consumed,
                    # ngram_iter_advance emits lost, chunks_done stuck at 0).
                    raise AssertionError(
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
                    toks = _tokenise_chunk(
                        chunk.text, jieba_tok, phrase_set, max_ngram_n
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
                    toks = _tokenise_chunk(
                        chunk.text, jieba_tok, phrase_set, max_ngram_n
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

            # 4. Derive pairs from the TEMP TABLE self-join.
            #    Single-pass path: one global self-join via
            #    ``_finalize_pairs_from_temp`` (which also runs step 3
            #    global floor DELETE + step 4 npmi recompute).
            #    Batched path: each batch already ran its own self-join
            #    inside ``_run_batched_pipeline``, so we skip the
            #    self-join and go directly to the global floor DELETE
            #    + npmi recompute (steps 3-4 of``_finalize_pairs_from_temp``).
            #    ``min_pair_count=None`` (the default) resolves the write
            #    floor from this scope's n_chunks (:func:`resolve_min_pair_count`);
            #    an explicit value overrides the adaptive ladder.
            effective_min_pair_count = (
                resolve_min_pair_count(n_chunks)
                if min_pair_count is None
                else int(min_pair_count)
            )
            _emit("pairs_start", candidate_pairs=0, surviving_pairs=0)
            stopword_kw_ids = _get_complementary_stopword_kw_ids(conn)
            if batch_chunks > 0:
                # Batched path: per-batch self-join already ran. Apply
                # global floor DELETE + npmi recompute (steps 3-4 of
                # ``_finalize_pairs_from_temp``).
                conn.execute(
                    "DELETE FROM keyword_pairs WHERE scope_id = ? AND source = ? "
                    "AND cooccur_count < ?",
                    (scope_id, CHUNK_COOCCUR_SOURCE, int(effective_min_pair_count)),
                )
                if _sqlite_has_ln(conn):
                    _step4_sql(
                        conn, scope_id=scope_id, min_npmi=float(min_npmi),
                        n_chunks=n_chunks, n_docs=n_docs, now=now, on_event=on_event,
                        stopword_kw_ids=stopword_kw_ids,
                        filter_segregation=filter_comp_segregation,
                    )
                else:
                    _step4_batch(
                        conn, scope_id=scope_id, min_npmi=float(min_npmi),
                        n_chunks=n_chunks, n_docs=n_docs, now=now, on_event=on_event,
                        batch_size=_NPMI_BATCH_SIZE,
                        stopword_kw_ids=stopword_kw_ids,
                        filter_segregation=filter_comp_segregation,
                    )
                surviving = int(conn.execute(
                    "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ? AND source = ?",
                    (scope_id, CHUNK_COOCCUR_SOURCE),
                ).fetchone()[0])
                _emit("pairs_scan_end", surviving=surviving)
            else:
                surviving = _finalize_pairs_from_temp(
                    conn,
                    scope_id=scope_id,
                    min_pair_count=effective_min_pair_count,
                    min_npmi=float(min_npmi),
                    n_chunks=n_chunks,
                    n_docs=n_docs,
                    now=now,
                    on_event=on_event,
                    stopword_kw_ids=stopword_kw_ids,
                    filter_segregation=filter_comp_segregation,
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

        # --keep-pairs --recompute-pairs: after the build merged in new
        # evidence, re-derive npmi for ALL pairs in this scope (not just
        # the ones touched by this build).  Used when callers want to
        # refresh statistics after rebuilding evidence while preserving
        # the knowledge asset as a whole.
        if keep_pairs and recompute_pairs:
            _recompute_scope_pairs(
                conn, scope_id, min_npmi=float(min_npmi),
                filter_comp_segregation=filter_comp_segregation,
            )
            stats.pairs_written = int(conn.execute(
                "SELECT COUNT(*) FROM keyword_pairs "
                "WHERE scope_id = ? AND source = ?",
                (scope_id, CHUNK_COOCCUR_SOURCE),
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

        # ------------------------------------------------------------------
        # Refresh query-planner statistics. Without ``sqlite_stat1`` SQLite
        # falls back to heuristics; on this table set the pathological
        # example (2026-07-16) was ``related --mode graph`` with no
        # ``--scope`` — the b-side ``llm_promoted`` query picked the
        # composite PK over ``idx_kp_b_npmi`` and scanned an entire scope
        # partition (5s+ on a 76M-row ``keyword_pairs``).
        #
        # We use the SQLite-recommended ``PRAGMA analysis_limit`` +
        # ``PRAGMA optimize`` pair instead of a bare ``ANALYZE``:
        #
        # * A bare ``ANALYZE`` is a **full per-index b-tree scan** (cost
        #   ≈ index_count × table_size); on a 9.6G DB that is ~1 minute,
        #   which would be an unacceptable tax on the no-op incremental
        #   branch (``build.py`` ``else`` at ~3384, no index-layer change).
        # * ``PRAGMA analysis_limit=400`` caps each per-index scan at
        #   ~400 sampled pages — sub-second, still statistically sound
        #   (SQLite docs § 8.3).
        # * ``PRAGMA optimize`` only re-ANALYZEs tables whose row-count
        #   changed materially since the last stats snapshot, so no-op
        #   incremental builds pay effectively nothing.
        #
        # If ``sqlite_stat1`` is missing entirely (first-ever build or
        # DB from a pre-fix release), ``PRAGMA optimize`` will run its
        # sampled ANALYZE once to seed it, then converge on the cheap
        # incremental refresh path.
        _emit("analyze_start")
        conn.execute("PRAGMA analysis_limit=400")
        conn.execute("PRAGMA optimize")
        _emit("analyze_done")

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


def _run_self_join(
    conn,
    *,
    scope_id: int,
    min_pair_count: int,
    now: str,
) -> None:
    """Self-join ``_build_chunk_kws``  ``keyword_pairs`` UPSERT-add.

    The self-join ``SELECT a.kw_id, b.kw_id, COUNT(*) FROM _build_chunk_kws
    a JOIN _build_chunk_kws b ON a.doc_id=b.doc_id AND a.chunk_id=b.chunk_id
    AND a.kw_id<b.kw_id GROUP BY a.kw_id, b.kw_id HAVING COUNT(*) >= ?``
    is the core pair-aggregation primitive. Called per-batch in the
    batched pipeline and once globally in the single-pass path.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS _idx_bcki_chunk "
        "ON _build_chunk_kws (doc_id, chunk_id)"
    )
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



def _finalize_pairs_from_temp(
    conn,
    *,
    scope_id: int,
    min_pair_count: int,
    min_npmi: float,
    n_chunks: int,
    n_docs: int,
    now: str,
    on_event: Optional[Callable[[dict], None]] = None,
    stopword_kw_ids: FrozenSet[int],
    filter_segregation: bool,
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
    #
    #    O(chunks × kw_per_chunk²) intermediate rows before GROUP BY —
    #    on a multi-million-chunk corpus this is the single most
    #    expensive SQL statement in the whole build. Progress heartbeat
    #    so the spinner keeps moving instead of showing a blank
    #    ``pairs_scan_start → pairs_scan_end`` gap.
    def _emit_pair_join(phase: str, **info) -> None:
        # Route the self-join heartbeat through the existing
        # ``pairs_advance`` phase the CLI already renders (indeterminate:
        # step counter as ``current``, no ``total``). ``_start`` / ``_end``
        # are dropped — ``pairs_scan_start`` already fired just above and
        # the NPMI-recompute stage emits its own ``pairs_scan_end``.
        if on_event is None or not phase.endswith("_advance"):
            return
        try:
            on_event({"phase": "pairs_advance",
                      "current": info.get("step", 0), "total": None})
        except Exception:
            pass
    with _sql_progress(conn, "pairs_join", _emit_pair_join):
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
            n_chunks=n_chunks, n_docs=n_docs, now=now, on_event=on_event,
            stopword_kw_ids=stopword_kw_ids,
            filter_segregation=filter_segregation,
        )
    else:
        _step4_batch(
            conn, scope_id=scope_id, min_npmi=min_npmi,
            n_chunks=n_chunks, n_docs=n_docs, now=now, on_event=on_event,
            batch_size=_NPMI_BATCH_SIZE,
            stopword_kw_ids=stopword_kw_ids,
            filter_segregation=filter_segregation,
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


def _extract_complementary_sql(
    conn,
    *,
    scope_id: int,
    n_chunks: int,
    n_docs: int,
    batch_size: int,
    now: str,
    stopword_kw_ids: FrozenSet[int],
    filter_segregation: bool,
) -> None:
    """Move significantly complementary (negative-npmi) pairs into
    ``complementary_pairs`` before they are deleted from ``keyword_pairs``.

    The SQL backend recomputes npmi in pure SQL, so the negative-npmi
    rows already live in ``keyword_pairs`` when we reach the DELETE step.
    This helper streams them out in batches, evaluates the
    hypergeometric lower-tail p-value in Python, and parks the
    significant ones in ``complementary_pairs``.

    Only pairs with ``npmi < 0`` AND ``p_value < BUILD_COMPLEMENTARY_MAX_P``
    are stored.  Glued pairs (df_a == df_b == cooccur_count) are
    skipped — they are trivially correlated, not complementary.

    Noise filters applied on the way to ``complementary_pairs``, all pure
    subtraction (surviving pairs keep identical statistics):

    * **Stopword gate** (phase 1 + iteration 2, always on), before the
      p-value test: pairs where either keyword is a member of
      ``_INDEX_STOPWORDS`` or the complementary-only
      ``_COMPLEMENTARY_STOPWORDS`` are dropped.
    * **Strength gate** (iteration 2, always on), before the p-value
      test: pairs whose c/E is not below ``COMPLEMENTARY_MAX_C_OVER_E``
      are dropped.
    * **Segregation gate** (phase 2, ``filter_segregation=True``), after
      the p-value test: pairs whose negative npmi is driven mainly by
      file-type isolation (different document genres) rather than
      semantic complementarity are dropped.

    The batch size bounds peak Python heap the same way ``_step4_batch``
    does.
    """
    if n_chunks <= 0:
        return
    chunk_df: Dict[int, int] = {
        int(r[0]): int(r[1]) for r in conn.execute(
            "SELECT kw_id, chunk_df FROM keyword_stats WHERE scope_id = ?",
            (scope_id,),
        )
    }

    use_segregation = filter_segregation and n_docs > 0 and _has_doc_evidence(
        conn, scope_id
    )
    # Loaded once (not per batch) — see _filter_segregation_candidates.
    doc_df: Optional[Dict[int, int]] = None
    if use_segregation:
        doc_df = {
            int(r[0]): int(r[1]) for r in conn.execute(
                "SELECT kw_id, doc_df FROM keyword_stats WHERE scope_id = ?",
                (scope_id,),
            )
        }

    last_a = -1
    last_b = -1
    while True:
        rows = conn.execute(
            """
            SELECT kw_a_id, kw_b_id, cooccur_count, npmi
            FROM keyword_pairs
            WHERE scope_id = ? AND source = ?
              AND npmi < 0.0
              AND (kw_a_id, kw_b_id) > (?, ?)
            ORDER BY kw_a_id, kw_b_id
            LIMIT ?
            """,
            (scope_id, CHUNK_COOCCUR_SOURCE, last_a, last_b, batch_size),
        ).fetchall()
        if not rows:
            break

        candidate_pairs: List[Tuple[int, int, float, float, float]] = []
        for row in rows:
            a, b, co = int(row[0]), int(row[1]), float(row[2])
            npmi_val = float(row[3]) if row[3] is not None else None
            co_int = int(co)
            # Phase 1: stopword gate — either side is a stopword → skip
            if stopword_kw_ids and (a in stopword_kw_ids or b in stopword_kw_ids):
                last_a, last_b = a, b
                continue
            evaluated = _evaluate_complementary_candidate(
                cooccur_count=co_int,
                df_a=chunk_df.get(a, 0),
                df_b=chunk_df.get(b, 0),
                npmi=npmi_val,
                n_chunks=n_chunks,
            )
            if evaluated is not None:
                npmi_out, p_val = evaluated
                candidate_pairs.append((a, b, co, npmi_out, p_val))
            last_a, last_b = a, b

        if use_segregation and candidate_pairs:
            candidate_pairs = _filter_segregation_candidates(
                conn, scope_id=scope_id, n_docs=n_docs,
                candidates=candidate_pairs, doc_df=doc_df,
            )

        if candidate_pairs:
            conn.executemany(
                "INSERT OR REPLACE INTO complementary_pairs "
                "(scope_id, kw_a_id, kw_b_id, cooccur_count, "
                " expected_count, npmi, p_value, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                _complementary_rows(
                    scope_id=scope_id, candidates=candidate_pairs,
                    chunk_df=chunk_df, n_chunks=n_chunks, now=now,
                ),
            )


def _step4_sql(
    conn,
    *,
    scope_id: int,
    min_npmi: float,
    n_chunks: int,
    n_docs: int,
    now: str,
    on_event: Optional[Callable[[dict], None]],
    stopword_kw_ids: FrozenSet[int],
    filter_segregation: bool,
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
    #
    # Rebuild complementary_pairs for this scope from scratch.
    # Pairs can transition between complementary and positive across
    # incremental builds (cooccurrence counts change), so we clear the
    # table first and re-populate from the current negative-npmi set in
    # keyword_pairs.  This preserves the invariant that
    # complementary_pairs contains ONLY pairs that are significantly
    # anti-co-occurring right now.
    conn.execute(
        "DELETE FROM complementary_pairs WHERE scope_id = ?",
        (scope_id,),
    )
    # Before deleting the negative-npmi rows, sieve for
    # *significantly* complementary pairs and park them in
    # ``complementary_pairs``.  We can't do the hypergeometric test
    # efficiently in pure SQL (it needs an iterative sum), so we stream
    # the negative-npmi candidates in batches and evaluate them in
    # Python.  Only pairs that pass the p-value gate survive; the rest
    # are simply deleted with the rest of the below-threshold rows.
    _extract_complementary_sql(
        conn, scope_id=scope_id, n_chunks=n_chunks,
        n_docs=n_docs,
        batch_size=_NPMI_BATCH_SIZE, now=now,
        stopword_kw_ids=stopword_kw_ids,
        filter_segregation=filter_segregation,
    )
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
    n_docs: int,
    now: str,
    on_event: Optional[Callable[[dict], None]],
    batch_size: int,
    stopword_kw_ids: FrozenSet[int],
    filter_segregation: bool,
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

    use_segregation = filter_segregation and n_docs > 0 and _has_doc_evidence(
        conn, scope_id
    )
    # Loaded once (not per batch) — see _filter_segregation_candidates.
    doc_df: Optional[Dict[int, int]] = None
    if use_segregation:
        doc_df = {
            int(r[0]): int(r[1]) for r in conn.execute(
                "SELECT kw_id, doc_df FROM keyword_stats WHERE scope_id = ?",
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

    # Rebuild complementary_pairs for this scope from scratch.
    conn.execute(
        "DELETE FROM complementary_pairs WHERE scope_id = ?",
        (scope_id,),
    )
    while True:
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
        comp_candidates: List[Tuple[int, int, int, float, float]] = []
        for row in rows:
            a, b, co = int(row[0]), int(row[1]), int(row[2])
            df_a = chunk_df.get(a, 0)
            df_b = chunk_df.get(b, 0)
            npmi = _npmi(co, df_a, df_b, n_chunks)
            glued = df_a > 0 and df_b > 0 and df_a == df_b == co
            if npmi is None or glued or (min_npmi > 0.0 and npmi < min_npmi):
                drops.append((scope_id, a, b))
                # Complementary-pair gate: only for pairs that have
                # negative npmi and that are being dropped by the positive
                # npmi filter.
                if stopword_kw_ids and (a in stopword_kw_ids or b in stopword_kw_ids):
                    last_a, last_b = a, b
                    processed += 1
                    if processed == total or processed % step_emit == 0:
                        _emit("pairs_advance", current=processed, total=total)
                    continue
                evaluated = _evaluate_complementary_candidate(
                    cooccur_count=co, df_a=df_a, df_b=df_b,
                    npmi=npmi, n_chunks=n_chunks,
                )
                if evaluated is not None:
                    npmi_out, p_val = evaluated
                    comp_candidates.append((a, b, co, npmi_out, p_val))
            else:
                updates.append((float(npmi), scope_id, a, b))
            last_a, last_b = a, b
            processed += 1
            if processed == total or processed % step_emit == 0:
                _emit("pairs_advance", current=processed, total=total)

        if use_segregation and comp_candidates:
            comp_candidates = _filter_segregation_candidates(
                conn, scope_id=scope_id, n_docs=n_docs,
                candidates=comp_candidates, doc_df=doc_df,
            )

        comp_rows = _complementary_rows(
            scope_id=scope_id, candidates=comp_candidates,
            chunk_df=chunk_df, n_chunks=n_chunks, now=now,
        )

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
        if comp_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO complementary_pairs "
                "(scope_id, kw_a_id, kw_b_id, cooccur_count, "
                " expected_count, npmi, p_value, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                comp_rows,
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
    keep_dict: bool = False
    keep_pairs: bool = False
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "ngram_dict_removed": self.ngram_dict_removed,
            "keyword_chunks_removed": self.keyword_chunks_removed,
            "pairs_recomputed": self.pairs_recomputed,
            "phrases_missing": list(self.phrases_missing),
            "all_wiped": self.all_wiped,
            "keep_dict": self.keep_dict,
            "keep_pairs": self.keep_pairs,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
        }


def _recompute_scope_pairs(
    conn,
    scope_id: int,
    *,
    min_npmi: float = BUILD_MIN_NPMI,
    filter_comp_segregation: bool = True,
) -> None:
    """Recompute ``npmi`` (and complementary pairs) for all
    ``chunk_cooccur`` pairs in ``scope_id`` using the current
    ``keyword_stats.chunk_df`` and ``scope_stats.n_chunks``.

    Used by ``--keep-pairs --recompute-pairs`` in forget and rebuild
    paths.  Mirrors the build pipeline's step-4 logic: UPDATE npmi
    in place, drop rows that fall below ``min_npmi`` or are degenerate,
    and rebuild ``complementary_pairs`` for this scope.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    n_chunks = int(conn.execute(
        "SELECT COALESCE(n_chunks, 0) FROM scope_stats WHERE scope_id = ?",
        (scope_id,),
    ).fetchone()[0])
    n_docs = int(conn.execute(
        "SELECT COALESCE(n_docs, 0) FROM scope_stats WHERE scope_id = ?",
        (scope_id,),
    ).fetchone()[0])
    if n_chunks <= 0:
        conn.execute(
            "UPDATE keyword_pairs SET npmi = NULL "
            "WHERE scope_id = ? AND source = ?",
            (scope_id, CHUNK_COOCCUR_SOURCE),
        )
        conn.execute(
            "DELETE FROM complementary_pairs WHERE scope_id = ?",
            (scope_id,),
        )
        return
    stopword_kw_ids = _get_complementary_stopword_kw_ids(conn)
    if _sqlite_has_ln(conn):
        _step4_sql(
            conn,
            scope_id=scope_id,
            min_npmi=min_npmi,
            n_chunks=n_chunks,
            n_docs=n_docs,
            now=now,
            on_event=None,
            stopword_kw_ids=stopword_kw_ids,
            filter_segregation=filter_comp_segregation,
        )
    else:
        _step4_batch(
            conn,
            scope_id=scope_id,
            min_npmi=min_npmi,
            n_chunks=n_chunks,
            n_docs=n_docs,
            now=now,
            on_event=None,
            batch_size=_NPMI_BATCH_SIZE,
            stopword_kw_ids=stopword_kw_ids,
            filter_segregation=filter_comp_segregation,
        )


def forget_scope_phrases(
    scope: str,
    *,
    db_path: Union[str, Path, None] = None,
    phrases: Optional[Sequence[str]] = None,
    all_phrases: bool = False,
    keep_dict: bool = False,
    keep_pairs: bool = False,
    recompute_pairs: bool = False,
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
      With ``--keep-dict`` / ``--keep-pairs`` / ``--keep-assets``
      the corresponding knowledge-layer tables are preserved
      (frozen: npmi / cooccur counts are left as-is unless
      ``recompute_pairs=True``).

    ``phrases`` that do not exist in ``auto_ngram_dict`` for this scope
    are collected into ``ForgetStats.phrases_missing`` and skipped
    silently — this makes the CLI safe to run repeatedly.
    """
    if bool(phrases) == bool(all_phrases):
        raise ValueError(
            "forget_scope_phrases: pass either phrases=[...] or "
            "all_phrases=True (exactly one)"
        )
    if recompute_pairs and not keep_pairs:
        raise ValueError(
            "forget_scope_phrases: recompute_pairs only makes sense "
            "with keep_pairs (otherwise no pairs remain to recompute)"
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
    stats.keep_dict = keep_dict
    stats.keep_pairs = keep_pairs

    target_db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    conn = connect(target_db)
    try:
        ensure_schema(conn)
        interner = IdInterner(conn)
        scope_id_ro = interner.scope_id_ro(scope_name)
        if scope_id_ro is None:
            stats.elapsed_seconds = time.perf_counter() - started
            return stats
        scope_id = scope_id_ro

        conn.execute("BEGIN IMMEDIATE")
        try:
            if all_phrases:
                doc_ids = [
                    int(row[0]) for row in conn.execute(
                        "SELECT id FROM docs WHERE scope_id = ?",
                        (scope_id,),
                    ).fetchall()
                ]
                stats.keyword_chunks_removed = int(conn.execute(
                    "SELECT COUNT(*) FROM keyword_docs WHERE scope_id = ?",
                    (scope_id,),
                ).fetchone()[0])
                if not keep_dict:
                    stats.ngram_dict_removed = int(conn.execute(
                        "SELECT COUNT(*) FROM auto_ngram_dict WHERE scope_id = ?",
                        (scope_id,),
                    ).fetchone()[0])
                _delete_doc_index_rows(conn, doc_ids)
                if not keep_pairs:
                    conn.execute(
                        "DELETE FROM keyword_pairs "
                        "WHERE scope_id = ? AND source = ?",
                        (scope_id, CHUNK_COOCCUR_SOURCE),
                    )
                    conn.execute(
                        "DELETE FROM complementary_pairs WHERE scope_id = ?",
                        (scope_id,),
                    )
                if not keep_dict:
                    conn.execute(
                        "DELETE FROM auto_ngram_dict WHERE scope_id = ?",
                        (scope_id,),
                    )
                if not keep_pairs:
                    conn.execute(
                        "DELETE FROM keyword_stats WHERE scope_id = ?",
                        (scope_id,),
                    )
                    conn.execute(
                        "DELETE FROM scope_stats WHERE scope_id = ?",
                        (scope_id,),
                    )
                if keep_pairs and recompute_pairs:
                    _recompute_scope_pairs(conn, scope_id)
                if keep_pairs:
                    stats.pairs_recomputed = int(conn.execute(
                        "SELECT COUNT(*) FROM keyword_pairs "
                        "WHERE scope_id = ? AND source = ?",
                        (scope_id, CHUNK_COOCCUR_SOURCE),
                    ).fetchone()[0])
                else:
                    stats.pairs_recomputed = 0
            else:
                # Per-phrase cascade. Resolve kw_ids in one batch.
                phrase_list = list(dict.fromkeys(phrases or []))
                if not phrase_list:
                    conn.execute("COMMIT")
                    stats.elapsed_seconds = time.perf_counter() - started
                    return stats
                kw_lookup = interner.kw_ids_ro([p.casefold() for p in phrase_list])
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
                    conn.execute(
                        f"DELETE FROM keyword_stats "
                        f"WHERE kw_id IN ({kw_placeholders}) AND scope_id = ?",
                        [*target_kw_ids, scope_id],
                    )
                if dict_present:
                    dict_list = sorted(dict_present)
                    dp = ",".join("?" for _ in dict_list)
                    conn.execute(
                        f"DELETE FROM auto_ngram_dict "
                        f"WHERE scope_id = ? AND phrase IN ({dp})",
                        [scope_id, *dict_list],
                    )
                    stats.ngram_dict_removed = len(dict_list)
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

    Resolution uses the managed-tool PATH (grga's own pandoc/rga live in
    the tools cache, not on the shell PATH). ``add_tools_to_path`` is
    called here so the fingerprint resolves the *same* binaries whether
    or not file reads happened earlier in the process — otherwise a
    no-op incremental build (no reads → PATH unstamped) would spuriously
    differ from the data-bearing build that preceded it.
    """
    from grga.toolchain import add_tools_to_path

    add_tools_to_path()
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
