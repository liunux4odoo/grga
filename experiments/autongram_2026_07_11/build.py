"""Tracer bullet — auto_ngram tokenization experiment.

Contract: SPEC.md (autongram_2026_07_11)

Three modes (single-variable isolation, see SPEC §3):
  baseline_regex : main-branch _TOKEN_RE regex, len>=2 CJK runs of 2-20 chars
  naive_jieba    : jieba precise mode, no user_dict, no len<2 filter
  auto_ngram     : jieba + iterative auto_dict, no len<2 filter

Build/rebuild ordering (SPEC §4.1, user-frozen 2026-07-11):
  1. load existing auto_dict (if any and not --rebuild) -> jieba.add_word
  2. jieba.cut over corpus using the dict loaded in step 1
  3. run ngram phrase detection on the token stream from step 2
  4. append newly discovered phrases to auto_dict (upsert)
  5. use step-2 tokenization to build keyword co-occurrence graph

Detection statistics are aggregated per-document (SPEC §4.3). During detection
the dictionary is NOT reloaded (user constraint #3, 2026-07-11).

Frozen defaults (SPEC §7):
  min_count = 3, npmi_threshold = 0.3, ent_threshold = 1.0 nat, max_ngram_n = 4
  n>2 solidity = min-split NPMI (Q4)
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import re
import sys
import time
from collections import Counter, defaultdict
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import jieba

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CORPUS = Path("/media/lqw/DATA/sync/temp/三体.txt")
STOPWORDS_FILE = DATA / "stopwords_v2.txt"
GOLDEN_FILE = DATA / "golden_santi.json"
AUTO_DICT_FILE = DATA / "auto_dict.jsonl"

MIN_PAIR_COUNT = 3
TOP_K = 100

# Baseline regex (mirror of main branch src/grga/graph/build.py:45)
_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_+#.-]{0,38}[A-Za-z0-9_+#]|[\u4e00-\u9fff]{2,20}"
)

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
CJK_ONLY_RE = re.compile(r"[\u4e00-\u9fff]+")   # NQ5: 判定整个 token 是否纯 CJK
ASCII_RE = re.compile(r"[A-Za-z]{2,}")

# Detection-only 边界：极小核心虚词集（NQ2 修正 2026-07-11）
# 只包含语法层无构词能力的结构助词/连词；不含 者/人/号/家 等构词后缀，
# 避免误伤 面壁者/破壁人/万有引力号 这类合成实体。
# 与图构建的 STOPWORDS（cn_stopwords.txt, 746 词）解耦：
#   - detection 阶段（此集合）：保守，只挡真结构助词
#   - keyword_chunks 建图阶段（stopwords_v2.txt）：激进，检索时不作结果
CORE_STOP_BOUNDARIES = frozenset(
    "的了和与或是在这那有被把将从对为之其而且但也就都还只很才又已"
)


# ------------------------------------------------------------------ tokenize
def valid_token_naive(t: str) -> bool:
    """No len<2 filter (SPEC §3: valid_token 过滤移除)."""
    if not t or t.isspace():
        return False
    if CJK_RE.search(t):
        return True
    if ASCII_RE.fullmatch(t):
        return True
    return False


def tokenize_regex(lines: list[str]) -> list[list[str]]:
    return [[m.group(0) for m in _TOKEN_RE.finditer(line)] for line in lines]


def tokenize_jieba(lines: list[str], user_phrases: list[str] | None = None) -> list[list[str]]:
    if user_phrases:
        for w in user_phrases:
            jieba.add_word(w)
    jieba.initialize()
    return [
        [t for t in jieba.cut(line, cut_all=False) if valid_token_naive(t)]
        for line in lines
    ]


# ---------------------------------------------------------- auto_dict I/O
def load_auto_dict() -> list[dict]:
    if not AUTO_DICT_FILE.exists():
        return []
    out = []
    for raw in AUTO_DICT_FILE.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        out.append(json.loads(raw))
    return out


def write_auto_dict(entries: list[dict]) -> None:
    with AUTO_DICT_FILE.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


# ---------------------------------------------------------- phrase detection
def detect_phrases(
    lines_tokens: list[list[str]],
    *,
    max_n: int,
    min_count: int,
    npmi_threshold: float,
    ent_threshold: float,
    stop_boundaries: set[str],
) -> list[dict]:
    """Detect phrases using frequency + min-split-NPMI + branch entropy.

    Returns list of {phrase, n, count, npmi, branch_ent} dicts.
    Statistics are aggregated across all lines (single doc corpus).
    """
    # unigram counts
    uni: Counter[str] = Counter()
    for toks in lines_tokens:
        uni.update(toks)
    total_tokens = sum(uni.values())
    if total_tokens == 0:
        return []

    log_total = math.log(total_tokens)

    @lru_cache(maxsize=None)
    def log_uni(t: str) -> float:
        c = uni.get(t, 0)
        if c <= 0:
            return math.nan
        return math.log(c)

    # ngram count + left/right context per (2..max_n)
    ngram_count: Counter[tuple[str, ...]] = Counter()
    left_ctx: defaultdict[tuple[str, ...], Counter[str]] = defaultdict(Counter)
    right_ctx: defaultdict[tuple[str, ...], Counter[str]] = defaultdict(Counter)

    for toks in lines_tokens:
        L = len(toks)
        for i in range(L):
            for n in range(2, max_n + 1):
                if i + n > L:
                    break
                win = tuple(toks[i:i + n])
                if any(t in stop_boundaries for t in win):
                    continue
                ngram_count[win] += 1
                left = toks[i - 1] if i > 0 else None
                right = toks[i + n] if i + n < L else None
                if left is not None and left not in stop_boundaries:
                    left_ctx[win][left] += 1
                if right is not None and right not in stop_boundaries:
                    right_ctx[win][right] += 1

    def entropy(counter: Counter[str]) -> float:
        tot = sum(counter.values())
        if tot == 0:
            return 0.0
        h = 0.0
        for c in counter.values():
            p = c / tot
            h -= p * math.log(p)
        return h

    @lru_cache(maxsize=None)
    def log_ngram_c(c: int) -> float:
        return math.log(c)

    def _log_count(x) -> float:
        """log(count) for a unigram str or an ngram tuple side."""
        if isinstance(x, str):
            return log_uni(x)
        c = ngram_count.get(x, 0)
        return log_ngram_c(c) if c > 0 else math.nan

    def _count(x) -> int:
        return uni.get(x, 0) if isinstance(x, str) else ngram_count.get(x, 0)

    def npmi_pair(a, b, joint_c: int) -> float:
        """Bouma 2009 NPMI over a single token population (N = total_tokens).

        a/b may be unigram str or ngram tuple. All counts are treated as draws
        from the same N-token space (standard phrase-mining convention); this
        keeps NPMI bounded in [-1, 1] for every split, including n>2 min-split.
        Using per-n event spaces would break that bound, so a single N is used.
        """
        c_a = _count(a)
        c_b = _count(b)
        if c_a <= 0 or c_b <= 0 or joint_c <= 0:
            return -1.0
        log_a = _log_count(a)
        log_b = _log_count(b)
        log_j = log_ngram_c(joint_c)
        # pmi = log(N * P(x,y) / (P(x)P(y))) with all P over the same N
        pmi = log_total + log_j - log_a - log_b
        denom = log_total - log_j  # = -log(c(x,y)/N) > 0 since c(x,y) < N
        if denom <= 0:
            return 0.0
        return pmi / denom

    def solidity(win: tuple[str, ...], joint_c: int) -> float:
        n = len(win)
        if n < 2:
            return 1.0
        vals = []
        for k in range(1, n):
            left = win[0] if k == 1 else win[:k]
            right = win[-1] if k == n - 1 else win[k:]
            vals.append(npmi_pair(left, right, joint_c))
        return min(vals)

    results: list[dict] = []
    for win, c in ngram_count.items():
        if c < min_count:
            continue
        sol = solidity(win, c)
        if sol < npmi_threshold:
            continue
        h_l = entropy(left_ctx[win])
        h_r = entropy(right_ctx[win])
        be = min(h_l, h_r)
        if be < ent_threshold:
            continue
        # NQ5 fix: rejoin per adjacent-pair rule so the resulting phrase
        # matches the ORIGINAL text jieba will re-tokenize:
        #   CJK + CJK   -> no space (章+北海 -> 章北海)
        #   CJK + ASCII -> no space (艾+AA -> 艾AA; PDC+主席 -> PDC主席)
        #   ASCII + CJK -> no space (same as above, reversed)
        #   ASCII + ASCII -> single space (nano+center -> "nano center")
        # Chinese text puts CJK and ASCII directly adjacent (no space between),
        # while consecutive English words in prose are space-separated.
        def _is_ascii_tok(t: str) -> bool:
            return ASCII_RE.fullmatch(t) is not None
        parts = [win[0]]
        for prev, cur in zip(win, win[1:]):
            if _is_ascii_tok(prev) and _is_ascii_tok(cur):
                parts.append(" ")
            parts.append(cur)
        phrase = "".join(parts)
        results.append({
            "phrase": phrase,
            "n": len(win),
            "count": c,
            "npmi": round(sol, 4),
            "branch_ent": round(be, 4),
            "h_left": round(h_l, 4),
            "h_right": round(h_r, 4),
        })

    results.sort(key=lambda x: (-x["npmi"], -x["count"]))
    return results


# ---------------------------------------------------------- graph build
def load_stopwords() -> set[str]:
    out = set()
    for raw in STOPWORDS_FILE.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            out.add(s)
    return out


def filter_dedup(tokens: list[str], stopwords: set[str], drop_short: bool) -> list[str]:
    seen = set()
    out = []
    for t in tokens:
        if drop_short and len(t) < 2:
            continue
        if t in stopwords:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def make_npmi(N: int, df: dict[str, int]):
    logN = math.log(N) if N > 0 else 0.0

    @lru_cache(maxsize=None)
    def logdf(term: str) -> float:
        return math.log(df[term])

    @lru_cache(maxsize=None)
    def log_co(co: int) -> float:
        return math.log(co)

    def npmi(x: str, y: str, co: int) -> float:
        pmi = logN + log_co(co) - logdf(x) - logdf(y)
        denom = -(log_co(co) - logN)
        if denom <= 0:
            return 0.0
        return pmi / denom

    return npmi


def build_graph(
    lines_tokens: list[list[str]],
    stopwords: set[str],
    seeds: list[str],
    *,
    drop_short: bool,
    min_pair_count: int = MIN_PAIR_COUNT,
) -> dict:
    t0 = time.time()
    df: Counter[str] = Counter()
    pair_count: Counter[tuple[str, str]] = Counter()
    effective_lines = 0

    for toks in lines_tokens:
        filtered = filter_dedup(toks, stopwords, drop_short)
        if not filtered:
            continue
        effective_lines += 1
        for t in filtered:
            df[t] += 1
        if len(filtered) < 2:
            continue
        for a, b in combinations(sorted(filtered), 2):
            pair_count[(a, b)] += 1

    seeds_set = set(seeds)
    adj_co: dict[str, dict[str, int]] = {}
    total_edges = 0
    for (a, b), c in pair_count.items():
        if c < min_pair_count:
            continue
        total_edges += 1
        if a in seeds_set:
            adj_co.setdefault(a, {})[b] = c
        if b in seeds_set:
            adj_co.setdefault(b, {})[a] = c

    N = effective_lines
    npmi_fn = make_npmi(N, df)

    queries = []
    for seed in seeds:
        seed_df = int(df.get(seed, 0))
        if seed not in adj_co or not adj_co[seed]:
            queries.append({
                "seed": seed,
                "seed_in_graph": False,
                "seed_df": seed_df,
                "returned_neighbors": [],
            })
            continue
        scored = []
        for term, co in adj_co[seed].items():
            if term == seed:
                continue
            val = npmi_fn(seed, term, co)
            scored.append((term, val, co, df[term]))
        scored.sort(key=lambda x: (-x[1], -x[2], x[0]))
        neighbors = [
            {"rank": i + 1, "term": t, "npmi": round(float(v), 6),
             "w": int(c), "df_target": int(dft)}
            for i, (t, v, c, dft) in enumerate(scored[:TOP_K])
        ]
        queries.append({
            "seed": seed,
            "seed_in_graph": True,
            "seed_df": seed_df,
            "returned_neighbors": neighbors,
        })

    build_time = round(time.time() - t0, 2)
    return {
        "corpus_stats": {
            "effective_lines": effective_lines,
            "total_terms": len(df),
            "total_edges": total_edges,
            "build_time_sec": build_time,
        },
        "queries": queries,
        "_df": df,
    }


def kw_length_distribution(df: Counter) -> dict:
    """Character-length distribution of the keyword vocabulary."""
    dist: Counter[int] = Counter()
    for k in df:
        dist[len(k)] += 1
    total = sum(dist.values())
    return {
        "total_terms": total,
        "by_length": {str(L): dist[L] for L in sorted(dist)},
        "ge15_count": sum(c for L, c in dist.items() if L >= 15),
        "ge15_ratio": round(sum(c for L, c in dist.items() if L >= 15) / total, 4) if total else 0.0,
    }


# ---------------------------------------------------------- main
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="autongram experiment tracer")
    p.add_argument("--mode", required=True,
                   choices=["baseline_regex", "naive_jieba", "auto_ngram"])
    p.add_argument("--rebuild", action="store_true",
                   help="clear auto_dict before this run (only affects auto_ngram)")
    p.add_argument("--max-ngram-n", type=int, default=4,
                   help="max n-gram length for phrase detection; default 4 "
                        "captures 外来音译名/长复合专名(如赫尔辛根默斯肯) "
                        "in one pass. 2 = only bigrams (rely on iteration).")
    p.add_argument("--min-count", type=int, default=3)
    p.add_argument("--npmi-threshold", type=float, default=0.3)
    p.add_argument("--ent-threshold", type=float, default=1.0)
    p.add_argument("--out-suffix", type=str, default="",
                   help="suffix for results file, e.g. '_iter1'")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    print(f"[mode] {args.mode}  rebuild={args.rebuild}  suffix={args.out_suffix!r}")
    t_all = time.time()

    stopwords = load_stopwords()
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    seeds = [s["seed"] for s in golden["seeds"]]
    phrase_check = golden.get("phrase_check_seeds", [])

    lines = CORPUS.read_text(encoding="utf-8").splitlines()
    print(f"[corpus] {CORPUS.name} lines={len(lines)}")

    detection_out: list[dict] | None = None

    if args.mode == "baseline_regex":
        lines_tokens = tokenize_regex(lines)
        # _TOKEN_RE already guarantees len>=2 (CJK {2,20}, ASCII 2-40),
        # so filter_dedup's drop_short is a no-op here — kept False for clarity.
        drop_short = False
    elif args.mode == "naive_jieba":
        lines_tokens = tokenize_jieba(lines, user_phrases=None)
        drop_short = False
    elif args.mode == "auto_ngram":
        if args.rebuild and AUTO_DICT_FILE.exists():
            AUTO_DICT_FILE.unlink()
            print("[auto_dict] cleared (rebuild)")

        existing = load_auto_dict()
        phrases_to_load = [e["phrase"] for e in existing]
        print(f"[auto_dict] loaded {len(phrases_to_load)} existing phrases")

        # Step 1-2: load dict then tokenize
        lines_tokens = tokenize_jieba(lines, user_phrases=phrases_to_load)

        # Step 3: detect new phrases on this token stream
        t_det = time.time()
        detection_out = detect_phrases(
            lines_tokens,
            max_n=args.max_ngram_n,
            min_count=args.min_count,
            npmi_threshold=args.npmi_threshold,
            ent_threshold=args.ent_threshold,
            stop_boundaries=CORE_STOP_BOUNDARIES,  # NQ2: detection 用核心虚词集，非 746 表
        )
        print(f"[detect] new candidates={len(detection_out)} elapsed={time.time()-t_det:.2f}s")

        # Step 4: upsert into auto_dict (SPEC §4.5 INSERT OR REPLACE semantics)
        merged: dict[str, dict] = {e["phrase"]: e for e in existing}
        for entry in detection_out:
            merged[entry["phrase"]] = entry  # new run's stats overwrite
        merged_list = sorted(merged.values(), key=lambda x: (-x["count"], x["phrase"]))
        write_auto_dict(merged_list)
        print(f"[auto_dict] persisted total={len(merged_list)} phrases")

        drop_short = False
    else:
        print(f"unknown mode: {args.mode}", file=sys.stderr)
        sys.exit(2)

    # Sanity for auto_ngram: seeds should ideally be single tokens after dict load
    # (report only, not assert — first iter will not have them yet)
    all_terms = {t for toks in lines_tokens for t in toks}
    covered = {s: (s in all_terms) for s in phrase_check}
    print(f"[phrase_check] {sum(covered.values())}/{len(phrase_check)} single-token")

    # Step 5: graph
    r = build_graph(lines_tokens, stopwords, seeds, drop_short=drop_short)
    df = r.pop("_df")
    length_dist = kw_length_distribution(df)

    payload = {
        "mode": args.mode,
        "args": {
            "max_ngram_n": args.max_ngram_n,
            "min_count": args.min_count,
            "npmi_threshold": args.npmi_threshold,
            "ent_threshold": args.ent_threshold,
            "rebuild": args.rebuild,
        },
        "corpus_stats": r["corpus_stats"],
        "length_distribution": length_dist,
        "phrase_check": {
            "seeds": phrase_check,
            "single_token_hit": covered,
            "hit_rate": round(sum(covered.values()) / len(phrase_check), 4) if phrase_check else 0.0,
        },
        "queries": r["queries"],
    }
    if detection_out is not None:
        payload["detection_summary"] = {
            "new_or_updated_phrases": len(detection_out),
            "top20_by_npmi": detection_out[:20],
        }

    out_file = ROOT / f"results_{args.mode}{args.out_suffix}.json"
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {out_file.name}")

    stats = r["corpus_stats"]
    print(f"[stats] eff_lines={stats['effective_lines']} terms={stats['total_terms']} "
          f"edges={stats['total_edges']} build={stats['build_time_sec']}s")
    print(f"[length] total={length_dist['total_terms']} ge15_ratio={length_dist['ge15_ratio']}")
    for q in r["queries"]:
        hit = "y" if q["seed_in_graph"] else "N"
        top5 = ",".join(n["term"] for n in q["returned_neighbors"][:5]) if q["seed_in_graph"] else "-"
        print(f"    {hit} {q['seed']:8s} df={q['seed_df']:6d} -> {top5}")

    print(f"[total] wallclock={time.time()-t_all:.2f}s")


if __name__ == "__main__":
    main()
