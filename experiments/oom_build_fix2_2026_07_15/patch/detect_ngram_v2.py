"""SQL-backed rewrite of ``detect_ngram_phrases`` (PR-2 / method A).

Replaces the three in-Python aggregation structures (``ngram_count`` /
``left_ctx`` / ``right_ctx``) with a TEMP TABLE + SQL GROUP BY. The
frequency / solidity / branch-entropy gates and the substring-salvage
pass keep their exact Python semantics — only the *aggregation* moves to
SQLite, whose hash aggregate spills to disk (via the caller's
``temp_store_directory``) instead of blowing up the Python heap.

Bit-exact equivalence with the mainline implementation is the acceptance
bar (see SPEC §0). Verified against ``experiments/oom_build_fix2_2026_07_15``.
"""
from __future__ import annotations

import math
import sqlite3
from collections import Counter
from functools import lru_cache
from typing import Callable, Dict, List, Optional, Tuple

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "src"))

from grga.graph.build import (  # noqa: E402
    DEFAULT_MAX_NGRAM_N,
    _CORE_STOP_BOUNDARIES,
    _NGRAM_MIN_COUNT,
    _NGRAM_NPMI_THRESHOLD,
    _NGRAM_ENT_THRESHOLD,
    _PROGRESS_TICK,
    _join_by_pair_rule,
)

# Unit separator — cannot appear in jieba/regex tokens, so it gives a
# bijective tuple<->key encoding without the rejoin-rule ambiguity.
_KEY_SEP = "\x1f"

_EVENT_FLUSH = 200_000


def _key(win: tuple) -> str:
    return _KEY_SEP.join(win)


def _unkey(key: str) -> tuple:
    return tuple(key.split(_KEY_SEP))


def detect_ngram_phrases_sql(
    line_tokens: List[List[str]],
    *,
    conn: Optional[sqlite3.Connection] = None,
    max_n: int = DEFAULT_MAX_NGRAM_N,
    min_count: int = _NGRAM_MIN_COUNT,
    npmi_threshold: float = _NGRAM_NPMI_THRESHOLD,
    ent_threshold: float = _NGRAM_ENT_THRESHOLD,
    stop_boundaries: frozenset = _CORE_STOP_BOUNDARIES,
    on_progress: Optional[Callable[[str, dict], None]] = None,
) -> List[dict]:
    """SQL-backed drop-in for ``detect_ngram_phrases``. See module docstring."""
    def _tick(phase: str, **info) -> None:
        if on_progress is not None:
            on_progress(phase, info)

    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(":memory:")

    try:
        return _run(
            conn, line_tokens,
            max_n=max_n, min_count=min_count,
            npmi_threshold=npmi_threshold, ent_threshold=ent_threshold,
            stop_boundaries=stop_boundaries, tick=_tick,
        )
    finally:
        conn.execute("DROP TABLE IF EXISTS _ng_ev")
        conn.execute("DROP TABLE IF EXISTS _ng_passed")
        if owns_conn:
            conn.close()


def _run(
    conn, line_tokens, *, max_n, min_count, npmi_threshold,
    ent_threshold, stop_boundaries, tick,
) -> List[dict]:
    n_lines = len(line_tokens)

    # --- Stage 0: fresh TEMP TABLE ---
    conn.execute("DROP TABLE IF EXISTS _ng_ev")
    conn.execute(
        "CREATE TEMP TABLE _ng_ev ("
        "  phrase_key TEXT NOT NULL, n INTEGER NOT NULL, "
        "  left_tok TEXT, right_tok TEXT"
        ")"
    )

    # --- Stage 1: stream events + unigram counts (single pass) ---
    tick("unigram_start", lines=n_lines)
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
                buf.append((_key(win), n, left, right))
                if len(buf) >= _EVENT_FLUSH:
                    conn.executemany(
                        "INSERT INTO _ng_ev VALUES (?, ?, ?, ?)", buf
                    )
                    buf.clear()
        if idx % _PROGRESS_TICK == 0:
            tick("window_advance", current=idx, total=n_lines, windows=-1)
    if buf:
        conn.executemany("INSERT INTO _ng_ev VALUES (?, ?, ?, ?)", buf)
        buf.clear()

    total = sum(uni.values())
    tick("unigram_end", lines=n_lines, vocab=len(uni))
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
    # ORDER BY MIN(rowid) reproduces the mainline dict insertion order so
    # downstream tie-breaks (final sort of equal npmi+count) match exactly.
    freq_rows = conn.execute(
        "SELECT phrase_key, n, COUNT(*) AS c "
        "FROM _ng_ev GROUP BY phrase_key, n "
        "HAVING c >= ? ORDER BY MIN(rowid)",
        (min_count,),
    ).fetchall()
    tick("window_end", windows=len(freq_rows))

    # ngram_count over post-freq entries only. Insight (SPEC §2.2): every
    # split of a freq-passing window also passes freq (each container
    # occurrence carries one occurrence of each split), so this dict has
    # every count _solidity() will look up.
    ngram_count: Dict[tuple, int] = {}
    for phrase_key, n, c in freq_rows:
        ngram_count[_unkey(phrase_key)] = int(c)

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

    # --- Stage 3: freq (already applied) + solidity filter ---
    total_windows = len(freq_rows)
    tick("filter_start", windows=total_windows)
    # Candidates passing freq + solidity; we defer entropy to stage 5 once
    # we have neighbour distributions.
    solid_pass: List[Tuple[tuple, int, float]] = []  # (win, count, solidity)
    for idx, (phrase_key, n, c) in enumerate(freq_rows):
        if idx % _PROGRESS_TICK == 0:
            tick("filter_advance", current=idx, total=total_windows, kept=len(solid_pass))
        win = _unkey(phrase_key)
        sol = _solidity(win, int(c))
        if sol < npmi_threshold:
            continue
        solid_pass.append((win, int(c), sol))

    # --- Stage 4: neighbour distributions for solidity-passing windows ---
    conn.execute("DROP TABLE IF EXISTS _ng_passed")
    conn.execute("CREATE TEMP TABLE _ng_passed (phrase_key TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO _ng_passed VALUES (?)",
        [(_key(win),) for win, _, _ in solid_pass],
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

    # --- Stage 5: branch entropy gate + entropy_rejects stash ---
    results: List[dict] = []
    entropy_rejects: List[Tuple[tuple, int, float, float]] = []
    for win, c, sol in solid_pass:
        pk = _key(win)
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
        by_first_char: Dict[str, list] = {}
        for r in results:
            kt = r["phrase"]
            kc = int(r["count"])
            for ch in set(kt):
                by_first_char.setdefault(ch, []).append((kt, kc))
        salvaged = 0
        for idx, (win, c, sol, be) in enumerate(entropy_rejects):
            if idx % _PROGRESS_TICK == 0:
                tick("salvage_advance", current=idx, total=len(entropy_rejects), salvaged=salvaged)
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
