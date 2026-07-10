"""Build V5_npmi variant: line-level window, NO DF-adaptive pruning, npmi ranking.

Contract: /media/lqw/DATA/ai_work/qwenpaw-data/workspaces/ai-developer/memory/2026-07-10-cooccur-experiment-v3-spec.md
Schema: spec §6.1. See QUESTIONS.md for assumptions.

Single-variable isolation vs v2 V2_retok (spec §2):
  1. ranking key: raw count w -> npmi(a, b)   (Bouma 2009)
  2. DF-adaptive pruning: REMOVED entirely

Everything else held constant with v2 V2_retok:
  window = line, tokenizer = jieba + v2 user_dict (19 words),
  stopwords = v2 conservative table, min_pair_count = 3, same 9 seeds/golden.
"""

from __future__ import annotations

import json
import math
import pickle
import re
import time
from collections import Counter
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import jieba

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CORPUS = Path("/media/lqw/DATA/sync/temp/《凡人修仙传》（精校全本）作者：忘语.txt")
GOLDEN = ROOT / "golden_v2.json"
USER_DICT = DATA / "user_dict.txt"

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ASCII_RE = re.compile(r"[A-Za-z]{2,}")

TOKENIZER_VERSION = 2  # same as v2: with user dict
MIN_PAIR_COUNT = 3     # spec §2, frozen
TOP_K = 100            # spec §6.1

# ---------- tokenization (identical to v2) ----------

def valid_token(t: str) -> bool:
    if len(t) < 2:
        return False
    if CJK_RE.search(t):
        return True
    if ASCII_RE.fullmatch(t):
        return True
    return False


def _load_user_dict():
    for raw in USER_DICT.read_text(encoding="utf-8").splitlines():
        w = raw.strip()
        if w and not w.startswith("#"):
            jieba.add_word(w)


def tokenize_corpus() -> list[list[str]]:
    """Per-line token lists (valid_token filtered, not stopword filtered)."""
    cache = DATA / "line_tokens.pkl"
    corpus_mtime = CORPUS.stat().st_mtime
    dict_mtime = USER_DICT.stat().st_mtime
    if cache.exists():
        with open(cache, "rb") as f:
            payload = pickle.load(f)
        if (
            isinstance(payload, dict)
            and payload.get("version") == TOKENIZER_VERSION
            and payload.get("corpus_mtime") == corpus_mtime
            and payload.get("dict_mtime") == dict_mtime
        ):
            return payload["tokens"]
    jieba.initialize()
    _load_user_dict()
    text = CORPUS.read_text(encoding="utf-8")
    lines = text.split("\n")
    out = []
    for line in lines:
        toks = [t for t in jieba.cut(line, cut_all=False) if valid_token(t)]
        out.append(toks)
    with open(cache, "wb") as f:
        pickle.dump(
            {
                "version": TOKENIZER_VERSION,
                "corpus_mtime": corpus_mtime,
                "dict_mtime": dict_mtime,
                "tokens": out,
            },
            f,
        )
    return out


# ---------- stopwords ----------

def load_stopwords(path: Path) -> set[str]:
    result = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        result.add(s)
    return result


STOPWORDS_GENERAL = load_stopwords(DATA / "cn_stopwords.txt")


# ---------- shared helpers ----------

def filter_dedup(tokens: list[str], stopwords: set[str]) -> list[str]:
    """Stopword filter + per-line dedup. NO DF-adaptive extra_drop (spec §2)."""
    seen = set()
    out = []
    for t in tokens:
        if t in stopwords:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


# ---------- npmi ----------

def make_npmi(N: int, df: dict[str, int]):
    logN = math.log(N)

    @lru_cache(maxsize=None)
    def logdf(term: str) -> float:
        return math.log(df[term])

    @lru_cache(maxsize=None)
    def log_co(co: int) -> float:
        return math.log(co)

    def npmi(x: str, y: str, co: int) -> float:
        # pmi = log(N*co/(df_x*df_y)); npmi = pmi / (-log(co/N))
        pmi = logN + log_co(co) - logdf(x) - logdf(y)
        denom = -(log_co(co) - logN)  # = -log(co/N) > 0 since co < N
        if denom <= 0:
            return 0.0
        return pmi / denom

    return npmi


# ---------- query ----------

def top_neighbors_npmi(adj_co, seed, npmi_fn, df, k=TOP_K):
    """Return list of dicts sorted by npmi desc, up to k. adj_co[seed] = {term: co}."""
    if seed not in adj_co:
        return None
    scored = []
    for term, co in adj_co[seed].items():
        if term == seed:
            continue
        val = npmi_fn(seed, term, co)
        scored.append((term, val, co, df[term]))
    # sort: npmi desc, then co desc, then term asc for determinism
    scored.sort(key=lambda x: (-x[1], -x[2], x[0]))
    return scored[:k]


def top_neighbors_w(adj_co, seed, df, k=30):
    """Sanity ranking by raw co-count (for hub_diff by-w column).

    Returns list of (term, co, df_target) tuples sorted by (-co, term).
    """
    if seed not in adj_co:
        return None
    scored = [(term, co, df[term]) for term, co in adj_co[seed].items() if term != seed]
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:k]


# ---------- build ----------

def build_v5(lines_tokens, stopwords, seeds, min_pair_count=MIN_PAIR_COUNT):
    t0 = time.time()
    total_lines = len(lines_tokens)
    effective_lines = 0
    df: Counter[str] = Counter()
    pair_count: Counter[tuple[str, str]] = Counter()

    for toks in lines_tokens:
        filtered = filter_dedup(toks, stopwords)
        if not filtered:
            continue
        effective_lines += 1
        for t in filtered:
            df[t] += 1
        if len(filtered) < 2:
            continue
        for a, b in combinations(sorted(filtered), 2):
            pair_count[(a, b)] += 1

    # filter pairs by min_pair_count (co >= 3)
    total_edges = 0
    seeds_set = set(seeds)
    adj_co: dict[str, dict[str, int]] = {s: {} for s in seeds if s in df}
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
        scored = top_neighbors_npmi(adj_co, seed, npmi_fn, df, k=TOP_K)
        neighbors = [
            {
                "rank": i + 1,
                "term": term,
                "npmi": round(float(val), 6),
                "w": int(co),
                "df_target": int(dft),
            }
            for i, (term, val, co, dft) in enumerate(scored)
        ]
        queries.append({
            "seed": seed,
            "seed_in_graph": True,
            "seed_df": seed_df,
            "returned_neighbors": neighbors,
        })

    build_time = round(time.time() - t0, 2)
    result = {
        "variant": "V5_npmi",
        "top_k": TOP_K,
        "config": {
            "tokenizer": "jieba+user_dict",
            "stopword_source": "cn_stopwords",
            "stopword_count": len(stopwords),
            "df_adaptive": False,
            "ranking_key": "npmi",
            "min_pair_count": min_pair_count,
            "window": "line",
        },
        "corpus_stats": {
            "total_lines": total_lines,
            "effective_lines": effective_lines,
            "total_terms": len(df),
            "total_edges": total_edges,
            "build_time_sec": build_time,
        },
        "queries": queries,
        "_adj_co": adj_co,   # kept in-memory for hub_diff/topk generation
        "_df": df,
        "_N": N,
    }
    return result


def main():
    t_all = time.time()
    print(f"[build] tokenizing (cache exists: {(DATA/'line_tokens.pkl').exists()})")
    lines_tokens = tokenize_corpus()
    print(f"[build] lines={len(lines_tokens)}")

    seeds_data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    seeds = [s["seed"] for s in seeds_data["seeds"]]

    # sanity: 韩立 must be tokenizable (core test point, spec §10)
    all_terms = {t for toks in lines_tokens for t in toks}
    if "韩立" not in all_terms:
        raise RuntimeError("韩立 不在词表：tokenizer 失败，核心测试点 miss，abort")

    r = build_v5(lines_tokens, STOPWORDS_GENERAL, seeds)

    # strip in-memory helpers before writing JSON
    adj_co = r.pop("_adj_co")
    df = r.pop("_df")
    N = r.pop("_N")

    (ROOT / "results_V5_npmi.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")

    # Side output: 韩立 top-30 by raw w (for hub_diff.md comparison; spec §6.2).
    # Also emit 韩立 top-100 by npmi (already in results) as-is; we specifically
    # need the by-w column that would otherwise be filtered out by npmi ranking.
    hanli = "韩立"
    if hanli in adj_co:
        npmi_fn = make_npmi(N, df)
        by_w = top_neighbors_w(adj_co, hanli, df, k=30)
        hanli_by_w = [
            {
                "rank": i + 1,
                "term": t,
                "w": int(c),
                "df_target": int(dft),
                "npmi": round(float(npmi_fn(hanli, t, c)), 6),
            }
            for i, (t, c, dft) in enumerate(by_w)
        ]
        (ROOT / "hanli_by_w_top30.json").write_text(
            json.dumps({"seed": hanli, "seed_df": int(df[hanli]),
                        "ranking": "raw_w", "top_k": 30,
                        "neighbors": hanli_by_w},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Where do golden associate entities rank for 韩立 (npmi & by rank)?
        golden_ents = ["墨大夫", "李化元", "极阴祖师", "黄枫谷", "七玄门", "曲魂"]
        all_scored = top_neighbors_npmi(adj_co, hanli, npmi_fn, df, k=10**9)
        rank_by_npmi = {t: i + 1 for i, (t, _, _, _) in enumerate(all_scored)}
        ent_rows = []
        for e in golden_ents:
            if e in adj_co[hanli]:
                co = adj_co[hanli][e]
                ent_rows.append({
                    "entity": e,
                    "co_with_hanli": int(co),
                    "df": int(df[e]),
                    "npmi": round(float(npmi_fn(hanli, e, co)), 6),
                    "npmi_rank": rank_by_npmi.get(e),
                    "total_neighbors": len(all_scored),
                })
            else:
                ent_rows.append({"entity": e, "co_with_hanli": 0,
                                 "note": "co < min_pair_count 或未共现"})
        (ROOT / "hanli_golden_entity_ranks.json").write_text(
            json.dumps(ent_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    # report
    stats = r["corpus_stats"]
    print(f"[V5_npmi] eff_lines={stats['effective_lines']} terms={stats['total_terms']} "
          f"edges={stats['total_edges']} build_time={stats['build_time_sec']}s")
    hanli_df = df.get("韩立", 0)
    print(f"[V5_npmi] 韩立 df={hanli_df} (N={N}, ratio={hanli_df/N:.3f})")
    for q in r["queries"]:
        hit = "y" if q["seed_in_graph"] else "N"
        top5 = ",".join(n["term"] for n in q["returned_neighbors"][:5]) if q["seed_in_graph"] else "-"
        print(f"    {hit} {q['seed']:8s} df={q['seed_df']:6d} -> {top5}")

    total_time = round(time.time() - t_all, 2)
    print(f"[build] total wallclock = {total_time}s")


if __name__ == "__main__":
    main()
