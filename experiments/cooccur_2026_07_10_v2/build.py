"""Build V2_retok (line-level, new tokenizer) and V4 (chapter-level) variants.

Contract: /media/lqw/DATA/ai_work/qwenpaw-data/workspaces/ai-developer/memory/2026-07-10-cooccur-experiment-v2-spec.md
Schema base: prior spec §4.1. See QUESTIONS.md for assumptions.

Only ONE variable changes between V2_retok and V4: the co-occurrence window
definition ("line" vs "chapter"). Everything else (tokenizer w/ user dict,
stopwords) is held constant. V2_retok keeps DF-adaptive; V4 disables it per
spec §2.3.
"""

from __future__ import annotations

import json
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
GOLDEN = ROOT / "golden_v2.json"
USER_DICT = DATA / "user_dict.txt"

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ASCII_RE = re.compile(r"[A-Za-z]{2,}")

# Chapter split regex (spec §2.3, contract-frozen)
CHAPTER_RE = re.compile(r"^\s*第[〇零一二三四五六七八九十百千万\d]+章[\s\u3000　]")

AUTO_DROP_TOP_N = 30  # DF-adaptive top-N (same semantics as prior V2)

# tokenizer version 2 = with user dict; invalidates prior cache
TOKENIZER_VERSION = 2
CHAPTER_TRUNCATE_LIMIT = 10000   # spec §4 boundary
CHAPTER_TRUNCATE_KEEP = 500

# ---------- tokenization ----------

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
    """Return per-line token lists (valid_token filtered, not stopword filtered)."""
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


def raw_lines() -> list[str]:
    return CORPUS.read_text(encoding="utf-8").split("\n")


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

def filter_dedup(tokens: list[str], stopwords: set[str], extra_drop: set[str]) -> list[str]:
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


def compute_df_lines(lines_tokens: list[list[str]], stopwords: set[str]) -> Counter[str]:
    df = Counter()
    for toks in lines_tokens:
        seen = set()
        for t in toks:
            if t in stopwords or t in seen:
                continue
            seen.add(t)
            df[t] += 1
    return df


def top_neighbors(adj, seed, k=20):
    if seed not in adj:
        return None
    best: dict[str, float] = {}
    for term, w in adj[seed]:
        if term == seed:
            continue
        if term not in best or w > best[term]:
            best[term] = w
    return sorted(best.items(), key=lambda x: (-x[1], x[0]))[:k]


def run_queries(adj, seeds):
    out = []
    for seed in seeds:
        nbrs = top_neighbors(adj, seed, k=30)
        if nbrs is None:
            out.append({"seed": seed, "seed_in_graph": False, "returned_neighbors": []})
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


# ---------- V2_retok: line-level window (DF-adaptive on) ----------

def build_line_variant(variant, lines_tokens, stopwords, df_adaptive, min_pair_count):
    t0 = time.time()
    auto_dropped: list[str] = []
    extra_drop: set[str] = set()
    if df_adaptive:
        df_after_stop = compute_df_lines(lines_tokens, stopwords)
        top = sorted(df_after_stop.items(), key=lambda x: (-x[1], x[0]))[:AUTO_DROP_TOP_N]
        auto_dropped = [term for term, _ in top]
        extra_drop = set(auto_dropped)

    total_lines = len(lines_tokens)
    effective_lines = 0
    term_count = Counter()
    pair_count: Counter[tuple[str, str]] = Counter()

    for toks in lines_tokens:
        filtered = filter_dedup(toks, stopwords, extra_drop)
        if len(filtered) < 2:
            if filtered:
                effective_lines += 1
                term_count[filtered[0]] += 1
            continue
        effective_lines += 1
        for t in filtered:
            term_count[t] += 1
        for a, b in combinations(sorted(filtered), 2):
            pair_count[(a, b)] += 1

    if min_pair_count > 1:
        pair_count = Counter({p: c for p, c in pair_count.items() if c >= min_pair_count})

    adj: dict[str, list[tuple[str, float]]] = {}
    for (a, b), c in pair_count.items():
        w = float(c)
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))

    build_time = round(time.time() - t0, 2)
    return {
        "variant": variant,
        "config": {
            "tokenizer": "jieba+user_dict",
            "stopword_source": "cn_stopwords",
            "stopword_count": len(stopwords),
            "df_adaptive": df_adaptive,
            "df_auto_drop_top_n": AUTO_DROP_TOP_N if df_adaptive else None,
            "top_k_per_line": None,
            "min_pair_count": min_pair_count,
            "npmi_threshold": None,
            "window": "line",
        },
        "corpus_stats": {
            "total_lines": total_lines,
            "effective_lines": effective_lines,
            "unique_terms": len(term_count),
            "total_edges": len(pair_count),
            "build_time_seconds": build_time,
        },
        "auto_dropped_terms": auto_dropped,
        "_adj": adj,
    }


# ---------- V4: chapter-level window (DF-adaptive off) ----------

def split_chapters(lines: list[str]) -> list[list[int]]:
    """Return list of chapters; each is a list of line indices. Chapter 0 =
    lines before first heading (may be empty). Raises if no heading matched."""
    heading_idxs = [i for i, l in enumerate(lines) if CHAPTER_RE.match(l)]
    if not heading_idxs:
        raise RuntimeError("未匹配到章节标题：CHAPTER_RE 对本语料 0 命中，请检查正文格式")
    chapters: list[list[int]] = []
    # chapter 0: before first heading
    chapters.append(list(range(0, heading_idxs[0])))
    bounds = heading_idxs + [len(lines)]
    for i in range(len(heading_idxs)):
        start = bounds[i]        # heading line included in its chapter
        end = bounds[i + 1]
        chapters.append(list(range(start, end)))
    return chapters, set(heading_idxs)


def build_chapter_variant(variant, lines_tokens, lines_raw, stopwords, min_pair_count, seeds):
    """Chapter-level co-occurrence via sparse (chapter x term) binary matrix.

    Pair weight = number of chapters where both terms appear.
    Memory-safety: prefilter terms to chapter-DF >= min_pair_count (a pair's
    chapter count cannot exceed either term's chapter DF, so terms with DF < K
    cannot participate in kept pairs). Then compute M.T @ M blockwise to bound
    peak memory. Only seed adjacency is retained (queries only need seeds), so
    the full 1e7-edge graph never materializes in Python-level dicts.
    """
    import numpy as np
    from scipy.sparse import csr_matrix

    t0 = time.time()
    chapters, heading_set = split_chapters(lines_raw)
    total_chapters = len(chapters)
    if total_chapters <= 1:
        raise RuntimeError(
            f"章节切分退化：total_chapters={total_chapters}，等价整文档共现，abort"
        )

    # Pass 1: per-chapter token set + chapter DF (all terms, no prefilter).
    chapter_token_sets: list[set[str]] = []
    truncations = 0
    term_ch_df = Counter()
    for ch_lines in chapters:
        seen: set[str] = set()
        for li in ch_lines:
            if li in heading_set:
                continue
            for t in lines_tokens[li]:
                if t in stopwords or t in seen:
                    continue
                seen.add(t)
        if len(seen) < 2:
            chapter_token_sets.append(seen if len(seen) == 1 else set())
            if len(seen) == 1:
                term_ch_df[next(iter(seen))] += 1
            continue
        if len(seen) > CHAPTER_TRUNCATE_LIMIT:
            truncations += 1
            # keep top-K by CURRENT term_ch_df (which was built streaming); tie: term asc
            kept = sorted(seen, key=lambda t: (-term_ch_df.get(t, 0), t))[:CHAPTER_TRUNCATE_KEEP]
            seen = set(kept)
        chapter_token_sets.append(seen)
        for t in seen:
            term_ch_df[t] += 1

    effective_chapters = sum(1 for s in chapter_token_sets if len(s) >= 2)
    unique_terms = len(term_ch_df)

    # Prefilter terms: keep those with chapter-DF >= min_pair_count.
    kept_terms = [t for t, d in term_ch_df.items() if d >= min_pair_count]
    kept_terms.sort()  # stable
    term_id = {t: i for i, t in enumerate(kept_terms)}
    n_terms = len(kept_terms)
    print(f"[V4] chapters={total_chapters} eff={effective_chapters} "
          f"unique_terms={unique_terms} filtered_terms(DF>={min_pair_count})={n_terms} "
          f"truncations={truncations}")

    # Build sparse chapter x term binary matrix over kept terms.
    rows: list[int] = []
    cols: list[int] = []
    for ch_idx, tokset in enumerate(chapter_token_sets):
        if len(tokset) < 2:
            continue
        for tok in tokset:
            j = term_id.get(tok)
            if j is not None:
                rows.append(ch_idx)
                cols.append(j)
    data = np.ones(len(rows), dtype=np.int32)
    M = csr_matrix(
        (data, (np.asarray(rows, dtype=np.int32), np.asarray(cols, dtype=np.int32))),
        shape=(total_chapters, n_terms),
    )
    del rows, cols, data
    Mt = M.T.tocsr()
    print(f"[V4] sparse matrix built: nnz={M.nnz}")

    # Blockwise co-occurrence: C_block = Mt[block] @ M (n_block x n_terms) => count
    # of shared chapters. Threshold >= min_pair_count. Total edges are counted
    # (upper-triangular), but only seed-endpoint edges are materialized in adj
    # to keep peak Python memory bounded.
    seeds_set = set(seeds)
    seed_ids = {term_id[s] for s in seeds if s in term_id}
    adj: dict[str, list[tuple[str, float]]] = {}
    total_edges = 0
    BLOCK = 512  # tune to fit 2GB
    for start in range(0, n_terms, BLOCK):
        end = min(start + BLOCK, n_terms)
        block = Mt[start:end].dot(M)  # (block, n_terms) sparse
        block = block.tocoo()
        # Iterate nonzero entries in this block
        r_arr = block.row
        c_arr = block.col
        v_arr = block.data
        for k in range(v_arr.shape[0]):
            i_local = r_arr[k]
            j = c_arr[k]
            i = start + i_local
            if j <= i:  # keep upper triangular (i < j), skip diagonal
                continue
            v = int(v_arr[k])
            if v < min_pair_count:
                continue
            total_edges += 1
            if i not in seed_ids and j not in seed_ids:
                continue
            a = kept_terms[i]
            b = kept_terms[j]
            fw = float(v)
            adj.setdefault(a, []).append((b, fw))
            adj.setdefault(b, []).append((a, fw))
        del block
        if (start // BLOCK) % 10 == 0:
            print(f"[V4] block {start}/{n_terms}, edges so far={total_edges}")

    build_time = round(time.time() - t0, 2)
    result = {
        "variant": variant,
        "config": {
            "tokenizer": "jieba+user_dict",
            "stopword_source": "cn_stopwords",
            "stopword_count": len(stopwords),
            "df_adaptive": False,
            "df_auto_drop_top_n": None,
            "top_k_per_line": None,
            "min_pair_count": min_pair_count,
            "npmi_threshold": None,
            "window": "chapter",
        },
        "corpus_stats": {
            "total_lines": len(lines_raw),
            "effective_lines": sum(1 for t in lines_tokens if len(t) >= 1),
            "total_chapters": total_chapters,
            "effective_chapters": effective_chapters,
            "unique_terms": unique_terms,
            "total_edges": total_edges,
            "build_time_seconds": build_time,
        },
        "auto_dropped_terms": [],
        "notes": {
            "chapter_truncations": truncations,
            "graph_sparse_warning": (
                "章节共现太稀疏，需要放宽 min_pair_count 或做分级窗口"
                if total_edges < 100 else None
            ),
        },
        "_adj": adj,
    }
    return result


# ---------- main ----------

def main():
    only = sys.argv[1:] or ["V2_retok", "V4"]
    print(f"[build] tokenizing corpus (cache exists: {(DATA/'line_tokens.pkl').exists()})")
    lines_tokens = tokenize_corpus()
    lines_raw = raw_lines()
    assert len(lines_tokens) == len(lines_raw), "token/raw line count mismatch"
    print(f"[build] lines={len(lines_tokens)}")

    seeds_data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    seeds = [s["seed"] for s in seeds_data["seeds"]]

    if "V2_retok" in only:
        print("\n=== V2_retok (line window, DF-adaptive on) ===")
        r = build_line_variant("V2_retok", lines_tokens, STOPWORDS_GENERAL,
                                df_adaptive=True, min_pair_count=2)
        r["queries"] = run_queries(r["_adj"], seeds)
        r.pop("_adj")
        (ROOT / "results_V2_retok.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        _report(r, seeds)

    if "V4" in only:
        print("\n=== V4 (chapter window, DF-adaptive off, min_pair_count=3) ===")
        # V4 sanity: 韩立 must be tokenizable (core test point)
        if "韩立" not in {t for toks in lines_tokens for t in toks}:
            raise RuntimeError("韩立 不在词表：tokenizer 失败，本实验核心测试点 miss，abort")
        r = build_chapter_variant("V4", lines_tokens, lines_raw, STOPWORDS_GENERAL,
                                   min_pair_count=3, seeds=seeds)
        # boundary: total_edges < 100 handled via notes;韩立 must be in graph
        r["queries"] = run_queries(r["_adj"], seeds)
        r.pop("_adj")
        (ROOT / "results_V4.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        _report(r, seeds)


def _report(result, seeds):
    stats = result["corpus_stats"]
    extra = ""
    if "total_chapters" in stats:
        extra = f" chapters={stats['total_chapters']} eff_ch={stats['effective_chapters']}"
    print(f"[{result['variant']}] eff_lines={stats['effective_lines']} "
          f"unique_terms={stats['unique_terms']} edges={stats['total_edges']} "
          f"build_time={stats['build_time_seconds']}s{extra}")
    for q in result["queries"]:
        hit = "y" if q["seed_in_graph"] else "N"
        top5 = ",".join(n["term"] for n in q["returned_neighbors"][:5]) if q["seed_in_graph"] else "-"
        print(f"    {hit} {q['seed']:8s} -> {top5}")


if __name__ == "__main__":
    main()
