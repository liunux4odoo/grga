"""Build co-occurrence graph variants (V0-V3) and dump results_V*.json.

Contract: see /media/lqw/DATA/ai_work/qwenpaw-data/workspaces/ai-developer/memory/2026-07-10-cooccur-experiment-spec.md

Assumptions (see QUESTIONS.md):
- DF auto-drop: use rank top-N by DF (see AUTO_DROP_TOP_N) instead of literal DF>0.8.
- NPMI threshold V3: 0.3.
- V0..V2 weight = raw count; V3 weight = NPMI (float).
- Per-line pair generation: full C(n,2) over deduped valid tokens.
- V3 Top-K per line: keep top-K tokens by *global* frequency to bias toward
  more informative pairs; ties broken by term.
"""

from __future__ import annotations

import json
import math
import pickle
import re
import sys
import time
from collections import Counter
from itertools import combinations
from pathlib import Path

import jieba

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CORPUS = Path("/media/lqw/DATA/sync/temp/《凡人修仙传》（精校全本）作者：忘语.txt")
GOLDEN = ROOT / "golden.json"

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ASCII_RE = re.compile(r"[A-Za-z]{2,}")

AUTO_DROP_TOP_N = 30  # DF-adaptive: top-N most-frequent-by-DF tokens dropped

# Bump when valid_token / tokenization logic changes so the pickle cache
# invalidates instead of silently returning stale tokens.
TOKENIZER_VERSION = 1

# ---------- tokenization ----------

def valid_token(t: str) -> bool:
    if len(t) < 2:
        return False
    if CJK_RE.search(t):
        return True
    if ASCII_RE.fullmatch(t):
        return True
    return False


def tokenize_corpus() -> list[list[str]]:
    cache = DATA / "line_tokens.pkl"
    corpus_mtime = CORPUS.stat().st_mtime
    if cache.exists():
        with open(cache, "rb") as f:
            payload = pickle.load(f)
        # validate cache is consistent with current tokenizer + corpus
        if (
            isinstance(payload, dict)
            and payload.get("version") == TOKENIZER_VERSION
            and payload.get("corpus_mtime") == corpus_mtime
        ):
            return payload["tokens"]
        # stale cache -> fall through to re-tokenize
    jieba.initialize()
    text = CORPUS.read_text(encoding="utf-8")
    lines = text.split("\n")
    out = []
    for line in lines:
        toks = [t for t in jieba.cut(line, cut_all=False) if valid_token(t)]
        out.append(toks)
    with open(cache, "wb") as f:
        pickle.dump(
            {"version": TOKENIZER_VERSION, "corpus_mtime": corpus_mtime, "tokens": out},
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


STOPWORDS_SHORT = load_stopwords(DATA / "llm_short_stopwords.txt")
STOPWORDS_GENERAL = load_stopwords(DATA / "cn_stopwords.txt")


# ---------- graph builder ----------

def filter_line(tokens: list[str], stopwords: set[str], extra_drop: set[str]) -> list[str]:
    """Return deduped, filtered token list, preserving first-seen order."""
    seen = set()
    out = []
    for t in tokens:
        if t in stopwords or t in extra_drop:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def compute_df(lines_tokens: list[list[str]], stopwords: set[str]) -> Counter[str]:
    df = Counter()
    for toks in lines_tokens:
        seen = set()
        for t in toks:
            if t in stopwords or t in seen:
                continue
            seen.add(t)
            df[t] += 1
    return df


def build_variant(
    variant: str,
    lines_tokens: list[list[str]],
    stopwords: set[str],
    df_adaptive: bool,
    top_k_per_line: int | None,
    min_pair_count: int,
    npmi_threshold: float | None,
    stopword_source: str,
) -> dict:
    t0 = time.time()

    # --- DF-adaptive auto-drop (top-N by DF among non-stopword tokens) ---
    auto_dropped: list[str] = []
    extra_drop: set[str] = set()
    if df_adaptive:
        df_after_stop = compute_df(lines_tokens, stopwords)
        # sort by (-df, term) for stability
        top = sorted(df_after_stop.items(), key=lambda x: (-x[1], x[0]))[:AUTO_DROP_TOP_N]
        auto_dropped = [term for term, _ in top]
        extra_drop = set(auto_dropped)

    # --- pass 2: filter and count pairs ---
    total_lines = len(lines_tokens)
    effective_lines = 0
    term_count = Counter()  # for NPMI: number of lines term appears in (post-filter)
    pair_count: Counter[tuple[str, str]] = Counter()

    # For Top-K per line, we need global frequency ordering; use term_count from
    # a preliminary pass over filtered tokens.
    if top_k_per_line is not None:
        # First compute post-filter DF to rank tokens
        prelim_df = compute_df(lines_tokens, stopwords | extra_drop)
    else:
        prelim_df = None

    for toks in lines_tokens:
        filtered = filter_line(toks, stopwords, extra_drop)
        if len(filtered) < 2:
            if filtered:
                effective_lines += 1
                for t in filtered:
                    term_count[t] += 1
            continue
        if top_k_per_line is not None and len(filtered) > top_k_per_line:
            # keep top-K by global frequency (ties: term asc for stability)
            filtered = sorted(
                filtered,
                key=lambda t: (-prelim_df[t], t),
            )[:top_k_per_line]
        effective_lines += 1
        for t in filtered:
            term_count[t] += 1
        for a, b in combinations(sorted(filtered), 2):
            pair_count[(a, b)] += 1

    # --- prune by min_pair_count ---
    if min_pair_count > 1:
        pair_count = Counter({p: c for p, c in pair_count.items() if c >= min_pair_count})

    # --- compute NPMI if requested (V3) ---
    if npmi_threshold is not None:
        N_lines = effective_lines
        edges = {}
        for (a, b), c in pair_count.items():
            pa = term_count[a] / N_lines
            pb = term_count[b] / N_lines
            pab = c / N_lines
            # PMI = log(pab / (pa*pb))
            denom = pa * pb
            if denom <= 0 or pab <= 0:
                continue
            pmi = math.log(pab / denom)
            log_pab = math.log(pab)
            if log_pab == 0:
                continue
            npmi = pmi / (-log_pab)
            if npmi >= npmi_threshold:
                edges[(a, b)] = round(npmi, 4)
        pair_weights = edges
    else:
        pair_weights = {p: float(c) for p, c in pair_count.items()}

    # --- build adjacency for seeds ---
    adj: dict[str, list[tuple[str, float]]] = {}
    for (a, b), w in pair_weights.items():
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))

    unique_terms = len(term_count)
    total_edges = len(pair_weights)
    build_time = round(time.time() - t0, 2)

    return {
        "variant": variant,
        "config": {
            "tokenizer": "jieba",
            "stopword_source": stopword_source,
            "stopword_count": len(stopwords),
            "df_adaptive": df_adaptive,
            "df_auto_drop_top_n": AUTO_DROP_TOP_N if df_adaptive else None,
            "top_k_per_line": top_k_per_line,
            "min_pair_count": min_pair_count,
            "npmi_threshold": npmi_threshold,
        },
        "corpus_stats": {
            "total_lines": total_lines,
            "effective_lines": effective_lines,
            "unique_terms": unique_terms,
            "total_edges": total_edges,
            "build_time_seconds": build_time,
        },
        "auto_dropped_terms": auto_dropped,
        "_adj": adj,  # internal, stripped before dump
    }


def top_neighbors(adj: dict[str, list[tuple[str, float]]], seed: str, k: int = 20):
    if seed not in adj:
        return None
    nbrs = adj[seed]
    # dedup (defensive): keep max weight per term, exclude seed
    best: dict[str, float] = {}
    for term, w in nbrs:
        if term == seed:
            continue
        if term not in best or w > best[term]:
            best[term] = w
    sorted_nbrs = sorted(best.items(), key=lambda x: (-x[1], x[0]))
    return sorted_nbrs[:k]


def run_queries(variant_result: dict, seeds: list[str]) -> list[dict]:
    adj = variant_result["_adj"]
    out = []
    for seed in seeds:
        nbrs = top_neighbors(adj, seed, k=20)
        if nbrs is None:
            out.append({
                "seed": seed,
                "seed_in_graph": False,
                "returned_neighbors": [],
            })
        else:
            out.append({
                "seed": seed,
                "seed_in_graph": True,
                "returned_neighbors": [
                    {"term": t, "weight": round(float(w), 4), "rank": i + 1}
                    for i, (t, w) in enumerate(nbrs)
                ],
            })
    return out


# ---------- main ----------

VARIANTS = [
    dict(variant="V0", stopwords=STOPWORDS_SHORT, stopword_source="llm_short",
         df_adaptive=False, top_k_per_line=None, min_pair_count=2, npmi_threshold=None),
    dict(variant="V1", stopwords=STOPWORDS_GENERAL, stopword_source="cn_stopwords",
         df_adaptive=False, top_k_per_line=None, min_pair_count=2, npmi_threshold=None),
    dict(variant="V2", stopwords=STOPWORDS_GENERAL, stopword_source="cn_stopwords",
         df_adaptive=True, top_k_per_line=None, min_pair_count=2, npmi_threshold=None),
    dict(variant="V3", stopwords=STOPWORDS_GENERAL, stopword_source="cn_stopwords",
         df_adaptive=True, top_k_per_line=8, min_pair_count=3, npmi_threshold=0.3),
]


def main():
    only = sys.argv[1:] or [v["variant"] for v in VARIANTS]
    print(f"[build] tokenizing corpus (cached: {(DATA/'line_tokens.pkl').exists()})")
    lines_tokens = tokenize_corpus()
    print(f"[build] lines={len(lines_tokens)}")

    seeds_data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    seeds = [s["seed"] for s in seeds_data["seeds"]]

    for vcfg in VARIANTS:
        vname = vcfg["variant"]
        if vname not in only:
            continue
        print(f"\n=== {vname} ===")
        result = build_variant(
            variant=vname,
            lines_tokens=lines_tokens,
            stopwords=vcfg["stopwords"],
            df_adaptive=vcfg["df_adaptive"],
            top_k_per_line=vcfg["top_k_per_line"],
            min_pair_count=vcfg["min_pair_count"],
            npmi_threshold=vcfg["npmi_threshold"],
            stopword_source=vcfg["stopword_source"],
        )
        result["queries"] = run_queries(result, seeds)
        # drop internal
        result.pop("_adj")
        # dump
        out_path = ROOT / f"results_{vname}.json"
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        stats = result["corpus_stats"]
        print(f"[{vname}] effective_lines={stats['effective_lines']} "
              f"unique_terms={stats['unique_terms']} edges={stats['total_edges']} "
              f"build_time={stats['build_time_seconds']}s "
              f"auto_dropped={len(result['auto_dropped_terms'])}")
        # sanity: print seed hits
        for q in result["queries"]:
            hit = "y" if q["seed_in_graph"] else "N"
            top5 = ",".join(n["term"] for n in q["returned_neighbors"][:5]) if q["seed_in_graph"] else "-"
            print(f"    {hit} {q['seed']:8s} -> {top5}")


if __name__ == "__main__":
    main()
