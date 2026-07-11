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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

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
            "rga_reads": self.rga_reads,
            "chunks_written": self.chunks_written,
            "keyword_chunks_written": self.keyword_chunks_written,
            "pairs_written": self.pairs_written,
            "heading_docs_written": self.heading_docs_written,
            "md_files_indexed": self.md_files_indexed,
            "ngram_phrases_written": self.ngram_phrases_written,
            "ngram_iters_run": self.ngram_iters_run,
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
    ngram_iters: int = DEFAULT_NGRAM_ITERS,
    max_ngram_n: int = DEFAULT_MAX_NGRAM_N,
    progress: Optional[Callable[[str], None]] = None,
    on_event: Optional[Callable[[dict], None]] = None,
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
        progress: optional status callback (plain string, legacy).
        on_event: optional structured progress callback. Emits dicts of
            the form ``{"phase": <str>, ...}``. Phases: ``scan_start`` (no
            total known), ``scan_advance`` (``current`` = files read so
            far, ``path`` = current file), ``scan_end`` (``total_docs`` /
            ``total_chunks``), ``ngram_iter_start`` (``iter`` / ``iters``
            / ``chunks``), ``ngram_iter_end`` (same + ``phrases``),
            ``materialise_start`` (``total_docs``), ``materialise_advance``
            (``current`` = doc index just finished, ``total``, ``path``),
            ``pairs_start`` (``candidate_pairs``), ``pairs_done``
            (``pairs_written``), ``done`` (final ``BuildStats.to_dict()``).

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
        if replace:
            # Clear this scope's rows. ``keyword_chunks`` / ``chunks`` /
            # ``heading_docs`` are keyed by ``doc_id`` which resolves through
            # ``docs.dir_id``; delete via the docs belonging to this scope.
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
            conn.execute(
                "DELETE FROM keyword_pairs WHERE dir_id = ? AND source = ?",
                (scope_dir_id, CHUNK_COOCCUR_SOURCE),
            )
            conn.execute(
                "DELETE FROM auto_ngram_dict WHERE dir_id = ?",
                (scope_dir_id,),
            )
            conn.commit()

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # ------------------------------------------------------------------
        # Pass 0: enumerate files, split chunks, load chunk text into memory.
        # The hybrid tokeniser needs corpus-global n-gram statistics, so we
        # cannot stream chunk-by-chunk like the old regex path did. Each entry
        # is (doc_id, rel_path, path, is_md, [(chunk, text), ...]).
        # ------------------------------------------------------------------
        docs: List[tuple] = []
        _emit("scan_start")
        for path in iter_text_files(
            scope_path,
            max_file_bytes=max_file_bytes,
            ignore_dirs=ignore_dirs,
            on_skip=stats.bump_reason,
        ):
            stats.files_scanned += 1
            # Dispatch on suffix inside :func:`read_file_lines_ex`: text
            # files use plain open(), rga-supported binaries route through
            # ``rga --passthru``. A ``FAILED`` source means we could not
            # extract anything (missing rga, timeout, hard error) — treat
            # it exactly like the legacy OSError branch.
            read_result = read_file_lines_ex(path)
            if read_result.source == ReadSource.FAILED:
                stats.files_skipped += 1
                stats.bump_reason("read_error")
                continue
            if read_result.source == ReadSource.RGA:
                stats.rga_reads += 1
            text = "\n".join(read_result.lines)
            try:
                rel_path = str(path.resolve().relative_to(scope_path))
            except ValueError:
                rel_path = str(path.resolve())
            is_md = path.suffix.lower() in _MARKDOWN_SUFFIXES
            chunks = split_chunks(
                text, mode=chunk_mode, is_markdown=is_md,
                max_chars=chunk_max_chars, min_chars=chunk_min_chars,
            )
            doc_id = interner.doc_id(scope_dir_id, rel_path)
            docs.append((doc_id, rel_path, path, is_md, chunks))
            _emit(
                "scan_advance",
                current=stats.files_scanned,
                path=str(path),
                chunks=len(chunks),
            )

        total_scanned_chunks = sum(len(entry[4]) for entry in docs)
        _emit(
            "scan_end",
            total_docs=len(docs),
            total_chunks=total_scanned_chunks,
        )

        # ------------------------------------------------------------------
        # Pass 1: iterative auto n-gram dictionary refinement. Each iteration
        # (a) builds a fresh jieba tokenizer seeded with the current dict,
        # (b) tokenises every chunk (hybrid: ASCII regex + jieba CJK),
        # (c) detects new CJK phrases and upserts them into the dict.
        # Iterations converge fast (2 is the frozen default): iter 1 seeds the
        # dict from a bare jieba, iter 2 re-tokenises with it so 章北海-type
        # compounds collapse to single tokens. ``chunk_tokens`` from the LAST
        # iteration feeds keyword_chunks materialisation.
        # ------------------------------------------------------------------
        auto_dict: Dict[str, dict] = {} if replace else _load_auto_dict(conn, scope_dir_id)
        chunk_tokens: Dict[tuple, List[str]] = {}
        iters = max(1, int(ngram_iters))
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
            for doc_id, rel_path, path, is_md, chunks in docs:
                for chunk in chunks:
                    toks_pos = tokenize_hybrid_with_pos(chunk.text, jieba_tok)
                    # Merge previously-discovered phrases (incl. mixed
                    # CJK/ASCII like 艾AA that jieba alone cannot form).
                    toks = merge_auto_phrases(
                        toks_pos, phrase_set, max_n=max_ngram_n
                    )
                    chunk_tokens[(doc_id, chunk.chunk_id)] = toks
                    if toks:
                        line_token_lists.append(toks)
            detected = detect_ngram_phrases(
                line_token_lists, max_n=max_ngram_n,
            )
            for entry in detected:
                auto_dict[entry["phrase"]] = entry
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
        _persist_auto_dict(conn, scope_dir_id, auto_dict.values(), now)
        stats.ngram_phrases_written = len(auto_dict)

        # ------------------------------------------------------------------
        # Pass 2: materialise chunks / keyword_chunks / heading_docs and
        # accumulate chunk-df + pair counts from the FINAL tokenisation.
        # ------------------------------------------------------------------
        chunk_df: Counter = Counter()
        pair_count: Counter = Counter()
        n_chunks_with_keywords = 0

        _emit("materialise_start", total_docs=len(docs))
        for doc_index, (doc_id, rel_path, path, is_md, chunks) in enumerate(docs, start=1):
            # Per-doc keyword counts derived from the FINAL chunk_tokens.
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
                n_chunks_with_keywords += 1
                for kw, occ in kw_counts.items():
                    kw_chunk_rows.append(
                        (kw_id_map[kw], doc_id, chunk.chunk_id, int(occ))
                    )
                    chunk_df[kw] += 1
                distinct_kws = sorted(kw_counts.keys())
                for a, b in combinations(distinct_kws, 2):
                    pair_count[(a, b)] += 1

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

        # Materialise keyword_pairs (chunk_cooccur) from the accumulated
        # pair counts + chunk-df, applying npmi and min_pair_count. Batch-
        # intern every distinct keyword mentioned in a surviving pair with
        # one call to ``kw_ids`` before materialising the row list, so this
        # phase never blocks on N ``INSERT + SELECT`` round-trips.
        n_chunks = n_chunks_with_keywords
        surviving_pairs = [
            (a, b, c) for (a, b), c in pair_count.items()
            if c >= min_pair_count
        ]
        _emit(
            "pairs_start",
            candidate_pairs=len(pair_count),
            surviving_pairs=len(surviving_pairs),
        )
        pair_rows = []
        if surviving_pairs:
            pair_kws = {kw for (a, b, _c) in surviving_pairs for kw in (a, b)}
            pair_kw_ids = interner.kw_ids(sorted(pair_kws))
            for a, b, c in surviving_pairs:
                npmi = _npmi(c, chunk_df[a], chunk_df[b], n_chunks)
                pair_rows.append(
                    (scope_dir_id, pair_kw_ids[a], pair_kw_ids[b],
                     float(c), npmi, now)
                )
        if pair_rows:
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
            stats.pairs_written += len(pair_rows)

        conn.commit()
    finally:
        conn.close()

    stats.elapsed_seconds = time.perf_counter() - started
    _emit("pairs_done", pairs_written=stats.pairs_written)
    _emit("done", stats=stats.to_dict())
    return stats
