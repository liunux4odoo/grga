"""Variant B (分批派) prototype for batched_build_2026_07_16 — final design.

架构: batch 循环做 phrase detection + 每 chunk top-N kw 采样, pair 计数
交给 baseline SQL 全局 GROUP BY. 具体见 SPEC.md §2 和 QUESTIONS.md
Q6-Q10 冻结决策.

Per-batch (file-aligned greedy packing):
  1. Read chunk texts
  2. First-tokenize with current auto_dict (jieba user dict)
  3. detect_ngram_phrases (per-batch, dict-based) → survivor phrases
  4. UPSERT-add phrases → auto_ngram_dict (Q3 count-only add)
  5. Re-tokenize with updated dict (PR-4 pattern, bit-exact by construction)
  6. Derive per-batch stopwords
  7. Per chunk: 3b filter + top-N cap (Q7 排序 B: 多字短语 > 稀有度 > id)
     → INSERT (doc_id, chunk_id, kw_id) triples into _build_chunk_kws
  8. UPSERT-add keyword_stats (chunk_df / doc_df / total_occ delta)

Final (once, after all batches):
  9. _finalize_pairs_from_temp (baseline PR-1 code, unchanged): SQL
     self-join + GROUP BY + HAVING >= min_pair_count + npmi 重算.
     Q9 关键: SQLite 外部排序处理累积 triples, 内存有界; 全局 count 保证
     Q1 (所有 pair-instance 参与聚合, 后期批次的新连接也能检出).
  10. Update scope_stats (n_chunks / n_docs / vocab_size)

Not implemented (out of scope):
  - keyword_docs (v9 evidence layer, not touched by pair generation)
  - keyword_counts (v1 legacy) / heading_docs / llm_extension_votes
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import resource
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import psutil

from grga.graph.build import (
    _new_jieba_tokenizer,
    _tokenise_chunk,
    _keywords_from_tokens,
    _derive_scope_stopword_candidates,
    _filter_chunk_kws_for_pairs,
    _setup_build_temp_dir,
    _cleanup_build_temp_dir,
    detect_ngram_phrases,
    _finalize_pairs_from_temp,
    iter_text_files,
    split_chunks,
    DEFAULT_MAX_NGRAM_N,
    DEFAULT_CHUNK_MAX_CHARS,
    DEFAULT_CHUNK_MIN_CHARS,
    BUILD_MIN_NPMI,
    resolve_min_pair_count,
)
from grga.graph.ids import IdInterner
from grga.graph.schema import CHUNK_COOCCUR_SOURCE, connect, ensure_schema


# Q7/方向3: 每 chunk 保留的最重要 kw 数. 默认 64 (最快学习: 少而精,
# 保留高 npmi 连接). 可通过 GRGA_BATCH_TOP_N 覆盖 —— 觉得学习不够全面
# 时调大 (N=256 → recall ~0.73 但慢 3×). 见 FINDINGS.md 权衡表.
try:
    TOP_N_KW_PER_CHUNK = int(os.environ.get("GRGA_BATCH_TOP_N", "64"))
    if TOP_N_KW_PER_CHUNK <= 0:
        raise ValueError
except (TypeError, ValueError):
    TOP_N_KW_PER_CHUNK = 64
UNKNOWN_KW_PRIOR_SCORE = 0.25  # 无先验证据的 kw 默认分 (略高于 min_npmi=0.2)


def _rank_and_cap_kws(
    kw_ids: List[int],
    phrase_id_set: set,
    global_chunk_df: Dict[int, int],
    prior_kw_npmi: Dict[int, float],  # 方向 3: kw 参与过的最高 npmi
    prior_kw_seen: set,                # 前批已见过的 kw_ids
    n: int,
) -> List[int]:
    """方向 3: 按 kw 参与的最高 npmi pair 分数排序.

    排序键 (全 ASC 排序, 靠 (bool, negated_score) 实现 DESC 语义):
      1. is_new (0=new, 1=seen) — Q1: 新 kw 永远优先, 保证新连接可发现
      2. neg_npmi (负值) — DESC by npmi: 参与过高 npmi pair 的 kw 优先
                          无先验的 kw 用 UNKNOWN_KW_PRIOR_SCORE 兜底
      3. is_phrase (0=phrase, 1=not) — 多字短语优先(tiebreaker)
      4. chunk_df ASC — 稀有度(tiebreaker)
      5. kw_id ASC — 确定性 tiebreaker
    """
    if len(kw_ids) <= n:
        return kw_ids
    def _key(k):
        is_new = 0 if k not in prior_kw_seen else 1
        neg_score = -prior_kw_npmi.get(k, UNKNOWN_KW_PRIOR_SCORE)
        is_phrase = 0 if k in phrase_id_set else 1
        df = global_chunk_df.get(k, 0)
        return (is_new, neg_score, is_phrase, df, k)
    return sorted(kw_ids, key=_key)[:n]


def _compute_prior_kw_scores(
    conn, scope_id: int, n_chunks_prior: int,
) -> Dict[int, float]:
    """Compute {kw_id -> max npmi across pairs it participates in} using
    accumulated keyword_pairs.cooccur_count + keyword_stats.chunk_df from
    PRIOR batches. Returns {} for cold-start (batch 1).

    npmi is computed here on-the-fly (baseline's _step4 hasn't run yet,
    so keyword_pairs.npmi is NULL during batch loop). Uses n_chunks_prior
    which excludes this batch's contribution.
    """
    if n_chunks_prior <= 0:
        return {}
    import math
    kw_score: Dict[int, float] = {}
    for kw_a, kw_b, cc, df_a, df_b in conn.execute("""
        SELECT p.kw_a_id, p.kw_b_id, p.cooccur_count, sa.chunk_df, sb.chunk_df
        FROM keyword_pairs p
        JOIN keyword_stats sa ON sa.kw_id=p.kw_a_id AND sa.scope_id=?
        JOIN keyword_stats sb ON sb.kw_id=p.kw_b_id AND sb.scope_id=?
        WHERE p.scope_id=? AND p.source='chunk_cooccur'
    """, (scope_id, scope_id, scope_id)):
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
        if npmi > kw_score.get(kw_a, -1.0):
            kw_score[int(kw_a)] = npmi
        if npmi > kw_score.get(kw_b, -1.0):
            kw_score[int(kw_b)] = npmi
    return kw_score


class Sampler:
    def __init__(self, interval=0.2):
        self.interval = interval
        self.proc = psutil.Process(os.getpid())
        self.peak = 0.0
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None

    def start(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=1.0)

    def _run(self):
        while not self._stop.is_set():
            try:
                mi = self.proc.memory_info().rss / (1024 * 1024)
                if mi > self.peak:
                    self.peak = mi
            except psutil.Error:
                return
            self._stop.wait(self.interval)


# ---------------------------------------------------------------------------
# File-aligned greedy packing (Q1).
# ---------------------------------------------------------------------------

def pack_batches(
    files: List[Path],
    target_chunks: int,
    *,
    chunk_mode: str = "chars",
    chunk_max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
    chunk_min_chars: int = DEFAULT_CHUNK_MIN_CHARS,
) -> List[Tuple[List[Path], int]]:
    """Group files into batches, each targeting ~target_chunks chunks.

    Whole files never split across batches (except when a single file
    exceeds target_chunks by itself, in which case it becomes an
    over-sized single-file batch).

    Returns list of (files, chunk_count) tuples.
    """
    # Sort by path so same-directory files stay together.
    files = sorted(files, key=lambda p: str(p))
    # Pre-count chunks per file (cheap: split_chunks is O(text length)).
    per_file: List[Tuple[Path, int]] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        chunks = split_chunks(
            text, mode=chunk_mode,
            max_chars=chunk_max_chars, min_chars=chunk_min_chars,
            is_markdown=f.suffix.lower() in (".md", ".markdown"),
        )
        n = sum(1 for c in chunks if c.text.strip())
        if n > 0:
            per_file.append((f, n))

    batches: List[Tuple[List[Path], int]] = []
    cur_files: List[Path] = []
    cur_chunks = 0
    for f, n in per_file:
        # Close current batch if adding this file would exceed target
        # AND we already have something (allow first file even if oversized).
        if cur_chunks > 0 and cur_chunks + n > target_chunks:
            batches.append((cur_files, cur_chunks))
            cur_files = []
            cur_chunks = 0
        cur_files.append(f)
        cur_chunks += n
    if cur_files:
        batches.append((cur_files, cur_chunks))
    return batches


# ---------------------------------------------------------------------------
# Numpy pair enumeration (fix9 approach B, adapted to kw_id space).
# ---------------------------------------------------------------------------

def merge_phrases(
    conn, scope_id: int, phrases: List[dict], now: str,
) -> None:
    """UPSERT-add phrases: only count += new_count on conflict, other
    fields (n, npmi, branch_ent) keep the first-seen batch's value."""
    if not phrases:
        return
    rows = [
        (scope_id, e["phrase"], int(e["n"]), int(e["count"]),
         float(e["npmi"]), float(e["branch_ent"]), now)
        for e in phrases
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
        rows,
    )


def merge_keyword_stats(
    conn, scope_id: int,
    kw_chunk_df: Dict[int, int],  # kw_id -> +chunk_df this batch
    kw_doc_df: Dict[int, int],    # kw_id -> +doc_df this batch
    kw_total_occ: Dict[int, int], # kw_id -> +occurrences this batch
    now: str,
) -> None:
    if not kw_chunk_df:
        return
    kw_ids = list(kw_chunk_df.keys())
    rows = [
        (scope_id, k, kw_chunk_df[k], kw_doc_df.get(k, 0),
         kw_total_occ.get(k, 0), now)
        for k in kw_ids
    ]
    CHUNK = 50_000
    for i in range(0, len(rows), CHUNK):
        conn.executemany(
            """
            INSERT INTO keyword_stats
                (scope_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_id, kw_id) DO UPDATE SET
                chunk_df = chunk_df + excluded.chunk_df,
                doc_df = doc_df + excluded.doc_df,
                total_occurrences = total_occurrences + excluded.total_occurrences,
                updated_at = excluded.updated_at
            """,
            rows[i:i+CHUNK],
        )


# ---------------------------------------------------------------------------
# Main pipeline.
# ---------------------------------------------------------------------------

def batched_build(
    scope: str,
    corpus_dir: Path,
    db_path: Path,
    *,
    batch_target_chunks: int,
    chunk_mode: str = "chars",
    chunk_max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
    chunk_min_chars: int = DEFAULT_CHUNK_MIN_CHARS,
    max_ngram_n: int = DEFAULT_MAX_NGRAM_N,
    min_npmi: float = BUILD_MIN_NPMI,
    on_event=None,
) -> dict:
    def _emit(phase, **info):
        if on_event is not None:
            try:
                on_event({"phase": phase, **info})
            except Exception:
                pass

    conn = connect(db_path)
    build_temp_dir = None
    try:
        build_temp_dir = _setup_build_temp_dir(conn, db_path)
        ensure_schema(conn)
        interner = IdInterner(conn)
        scope_id = interner.scope_id(scope)

        # Wipe scope's previous knowledge (rebuild semantics for the bench).
        conn.execute("DELETE FROM keyword_pairs WHERE scope_id = ?", (scope_id,))
        conn.execute("DELETE FROM keyword_stats WHERE scope_id = ?", (scope_id,))
        conn.execute("DELETE FROM auto_ngram_dict WHERE scope_id = ?", (scope_id,))
        conn.execute("DELETE FROM scope_stats WHERE scope_id = ?", (scope_id,))
        # Cross-batch persistent TEMP TABLE for pair aggregation (Q9).
        conn.execute("DROP TABLE IF EXISTS _build_chunk_kws")
        conn.execute(
            "CREATE TEMP TABLE _build_chunk_kws ("
            "  doc_id INTEGER NOT NULL, "
            "  chunk_id INTEGER NOT NULL, "
            "  kw_id INTEGER NOT NULL"
            ")"
        )
        conn.commit()

        # Scan + plan batches.
        files = list(iter_text_files(corpus_dir, max_file_bytes=0, ignore_dirs=(".git", ".grga", ".venv", "node_modules", "__pycache__")))
        _emit("scan_end", total_docs=len(files))
        batches = pack_batches(
            files, batch_target_chunks,
            chunk_mode=chunk_mode,
            chunk_max_chars=chunk_max_chars, chunk_min_chars=chunk_min_chars,
        )
        n_batches = len(batches)
        total_chunks = sum(nc for _, nc in batches)
        _emit("batches_planned", n_batches=n_batches, total_chunks=total_chunks)

        # State accumulated across batches.
        auto_dict: Dict[str, dict] = {}
        n_chunks_total = 0
        n_docs_total = 0
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        import psutil as _psutil, os as _os, gc as _gc
        _proc = _psutil.Process(_os.getpid())
        def _rss():
            return _proc.memory_info().rss / (1024*1024)
        def _mp(label):
            _gc.collect()
            print(f"    [mem {label}] rss={_rss():.0f}MB", flush=True)
        for bi, (batch_files, batch_chunk_est) in enumerate(batches):
            batch_t0 = time.perf_counter()

            # 1. Read this batch: keep chunk texts (needed for
            #    the RE-tokenization pass after detect_ngram_phrases
            #    grows the dictionary).
            #
            #    NOTE: batch-local memory scales with batch_target_chunks
            #    × avg chunk text length (~600 chars). At 200k chunks
            #    ~120 MB text, bounded.
            batch_chunks: List[Tuple[int, int, str]] = []  # (doc_id, chunk_id, text)
            batch_docs: List[int] = []
            for f in batch_files:
                text = f.read_text(encoding="utf-8", errors="replace")
                is_md = f.suffix.lower() in (".md", ".markdown")
                chunks = split_chunks(
                    text, mode=chunk_mode,
                    max_chars=chunk_max_chars, min_chars=chunk_min_chars,
                    is_markdown=is_md,
                )
                doc_path = (str(f.relative_to(corpus_dir))
                            if corpus_dir.is_dir() else str(f.name))
                doc_id = interner.doc_id(scope_id, doc_path)
                batch_docs.append(doc_id)
                for chunk in chunks:
                    if chunk.text.strip():
                        batch_chunks.append((doc_id, chunk.chunk_id, chunk.text))

            n_docs_total += len(batch_docs)
            n_chunks_total += len(batch_chunks)

            # 2. First-pass tokenize with the CURRENT accumulated dict
            #    (== Phase A iter 1's tokenize in mainline).
            jieba_tok = _new_jieba_tokenizer(auto_dict.keys())
            phrase_set = set(auto_dict.keys())
            line_tokens_for_detect: List[List[str]] = []
            for doc_id, chunk_id, text in batch_chunks:
                toks = _tokenise_chunk(text, jieba_tok, phrase_set, max_ngram_n)
                if toks:
                    line_tokens_for_detect.append(toks)

            # 3. Detect ngram phrases (survivor only for this batch).
            new_phrases = detect_ngram_phrases(
                line_tokens_for_detect, max_n=max_ngram_n, conn=conn,
            )
            del line_tokens_for_detect
            # UPSERT into auto_dict in-memory + DB.
            new_phrase_texts_added: List[str] = []
            for pdata in new_phrases:
                ph = pdata["phrase"]
                if ph not in auto_dict:
                    auto_dict[ph] = dict(pdata)
                    new_phrase_texts_added.append(ph)
                else:
                    auto_dict[ph]["count"] += int(pdata["count"])
            merge_phrases(conn, scope_id, new_phrases, now)

            # 3b. RE-tokenize with the UPDATED dict (== Phase B in mainline,
            #     bit-exact-by-construction since jieba is deterministic).
            #     This is what lets pair enumeration see the merged phrases
            #     (e.g. 木星轨道 as one token instead of 木/星/轨/道).
            #     Skip if no new phrases were added (identity re-tokenize).
            if new_phrase_texts_added:
                jieba_tok = _new_jieba_tokenizer(auto_dict.keys())
                phrase_set = set(auto_dict.keys())
            chunk_tokens: Dict[Tuple[int, int], List[str]] = {}
            for doc_id, chunk_id, text in batch_chunks:
                chunk_tokens[(doc_id, chunk_id)] = _tokenise_chunk(
                    text, jieba_tok, phrase_set, max_ngram_n,
                )
            del batch_chunks

            # 4. Derive stopwords for this batch (Q4: per-batch is a feature).
            stopwords = _derive_scope_stopword_candidates(chunk_tokens)

            # 5. Aggregate per-chunk kw counts + top-N rank + dump triples.
            #    Q9: pair 计数交给 baseline SQL 全局 GROUP BY, 这里只负责
            #    把 (doc_id, chunk_id, kw_id) triples 塞进 _build_chunk_kws.
            #    Q7 top-N: 每 chunk 只保留 rank <= N 的 kw (排序 B).
            kw_chunk_df_delta: Counter = Counter()
            kw_total_occ_delta: Counter = Counter()
            doc_seen_kws: Dict[int, set] = defaultdict(set)

            # Intern kw ids in one shot for efficiency.
            all_kws: set = set()
            for toks in chunk_tokens.values():
                if not toks:
                    continue
                all_kws.update(_keywords_from_tokens(toks).keys())
            kw_id_map = interner.kw_ids(sorted(all_kws)) if all_kws else {}

            # Load current global chunk_df (accumulated from PRIOR batches).
            global_chunk_df: Dict[int, int] = {}
            prior_kw_seen: set = set()
            for r in conn.execute(
                "SELECT kw_id, chunk_df FROM keyword_stats WHERE scope_id = ?",
                (scope_id,),
            ):
                global_chunk_df[int(r[0])] = int(r[1])
                prior_kw_seen.add(int(r[0]))
            # 方向 3: kw 先验 npmi 分数 (基于 PRIOR 批 keyword_pairs +
            # keyword_stats). n = 本批之前累积的 chunks 数.
            n_chunks_prior = n_chunks_total - len(chunk_tokens)
            prior_kw_npmi = _compute_prior_kw_scores(
                conn, scope_id, n_chunks_prior,
            )
            # Phrase-id set: multi-char learned phrases (tiebreaker).
            phrase_id_set: set = set()
            for ph in auto_dict.keys():
                if ph in kw_id_map and len(ph) > 1:
                    phrase_id_set.add(kw_id_map[ph])

            # Dump triples in batches to bound the executemany buffer size.
            triples_buf: List[Tuple[int, int, int]] = []
            TRIPLES_FLUSH = 100_000
            capped_kw_count = 0
            total_kw_count = 0
            for (doc_id, chunk_id), toks in chunk_tokens.items():
                if not toks:
                    continue
                kw_counts = _keywords_from_tokens(toks)
                if not kw_counts:
                    continue
                # 3b filter (stopword containment) — using per-batch stopwords.
                pair_kws = _filter_chunk_kws_for_pairs(
                    list(kw_counts.keys()), stopword_candidates=stopwords,
                )
                # keyword_stats increments (over full kw_counts, not pair-filtered)
                for kw, occ in kw_counts.items():
                    kid = kw_id_map[kw]
                    kw_chunk_df_delta[kid] += 1
                    kw_total_occ_delta[kid] += int(occ)
                    doc_seen_kws[doc_id].add(kid)
                # Top-N cap on the pair-eligible set (Q7 排序 B).
                if pair_kws:
                    pids = list({kw_id_map[k] for k in pair_kws})
                    total_kw_count += len(pids)
                    capped = _rank_and_cap_kws(
                        pids, phrase_id_set, global_chunk_df,
                        prior_kw_npmi, prior_kw_seen,
                        TOP_N_KW_PER_CHUNK,
                    )
                    capped_kw_count += len(capped)
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

            # Per-doc doc_df: each kw seen in a doc contributes +1 to doc_df.
            kw_doc_df_delta: Counter = Counter()
            for kids in doc_seen_kws.values():
                for k in kids:
                    kw_doc_df_delta[k] += 1

            merge_keyword_stats(
                conn, scope_id,
                dict(kw_chunk_df_delta), dict(kw_doc_df_delta),
                dict(kw_total_occ_delta), now,
            )
            conn.commit()

            batch_wall = time.perf_counter() - batch_t0
            triples_total = int(conn.execute(
                "SELECT COUNT(*) FROM _build_chunk_kws"
            ).fetchone()[0])
            _emit(
                "batch_done", batch=bi + 1, total=n_batches,
                batch_files=len(batch_files),
                batch_chunks=len(chunk_tokens),
                new_phrases_this_batch=len(new_phrases),
                dict_size=len(auto_dict),
                triples_total=triples_total,
                kw_capped_ratio=(capped_kw_count / total_kw_count
                                 if total_kw_count else 1.0),
                wall_s=round(batch_wall, 1),
            )

            del chunk_tokens, kw_id_map, global_chunk_df, phrase_id_set
            del prior_kw_npmi, prior_kw_seen
            del kw_chunk_df_delta, kw_doc_df_delta, kw_total_occ_delta, doc_seen_kws
            gc.collect()

        # 6. Baseline SQL pair aggregation over accumulated _build_chunk_kws.
        #    Q9: 用 baseline 原代码, 一条 SQL self-join + GROUP BY + HAVING
        #    + step4 npmi 重算. 外部排序保证内存有界; 全局 count 保证 Q1
        #    (所有 pair-instance 都参与, 没有 hard skip).
        _emit("pair_aggregation_start", triples=int(conn.execute(
            "SELECT COUNT(*) FROM _build_chunk_kws").fetchone()[0]))
        now_final = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        min_pair_count = resolve_min_pair_count(n_chunks_total)
        _finalize_pairs_from_temp(
            conn,
            scope_id=scope_id,
            min_pair_count=int(min_pair_count),
            min_npmi=min_npmi,
            n_chunks=n_chunks_total,
            now=now_final,
            on_event=None,
        )
        _emit("pair_aggregation_end")

        # 8. Write scope_stats.
        vocab_size = int(conn.execute(
            "SELECT COUNT(*) FROM keyword_stats WHERE scope_id = ?",
            (scope_id,),
        ).fetchone()[0])
        conn.execute(
            "INSERT INTO scope_stats (scope_id, n_chunks, n_docs, vocab_size, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_id) DO UPDATE SET "
            "n_chunks = excluded.n_chunks, n_docs = excluded.n_docs, "
            "vocab_size = excluded.vocab_size, updated_at = excluded.updated_at",
            (scope_id, n_chunks_total, n_docs_total, vocab_size, now),
        )
        conn.commit()

        n_pairs_final = int(conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs WHERE scope_id = ?",
            (scope_id,),
        ).fetchone()[0])
        _emit(
            "done", pairs=n_pairs_final, phrases=len(auto_dict),
            n_chunks=n_chunks_total, n_docs=n_docs_total,
            n_batches=n_batches,
        )
        return {
            "n_pairs": n_pairs_final, "n_phrases": len(auto_dict),
            "n_chunks": n_chunks_total, "n_docs": n_docs_total,
            "n_batches": n_batches, "min_pair_count": min_pair_count,
        }
    finally:
        conn.close()
        _cleanup_build_temp_dir(build_temp_dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", required=True)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--batch-chunks", type=int, default=200_000)
    ap.add_argument("--result-json", type=Path,
                    help="Write JSON result to this path")
    args = ap.parse_args()

    sampler = Sampler()
    sampler.start()
    t0 = time.perf_counter()

    events = []
    def on_event(payload):
        # Print concise line for tail-follow
        p = payload["phase"]
        if p == "batch_done":
            print(f"[batch {payload['batch']}/{payload['total']}] "
                  f"files={payload['batch_files']} chunks={payload['batch_chunks']} "
                  f"+phrases={payload['new_phrases_this_batch']} "
                  f"dict={payload['dict_size']} triples={payload['triples_total']} cap={payload.get('kw_capped_ratio', 1.0):.2f} "
                  f"wall={payload['wall_s']}s", flush=True)
        elif p in ("scan_end", "batches_planned", "done"):
            print(f"[{p}] {payload}", flush=True)
        events.append(payload)

    result = batched_build(
        scope=args.scope, corpus_dir=args.corpus, db_path=args.db,
        batch_target_chunks=args.batch_chunks,
        on_event=on_event,
    )
    wall = time.perf_counter() - t0
    sampler.stop()

    result["wall_s"] = round(wall, 1)
    result["peak_rss_mb"] = round(sampler.peak, 1)
    result["batch_chunks_target"] = args.batch_chunks

    print(f"\n[SUMMARY] wall={result['wall_s']}s peak={result['peak_rss_mb']}MB "
          f"pairs={result['n_pairs']} phrases={result['n_phrases']} "
          f"batches={result['n_batches']}")

    if args.result_json:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        args.result_json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

if __name__ == "__main__":
    main()
