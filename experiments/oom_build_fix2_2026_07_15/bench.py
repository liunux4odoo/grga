"""End-to-end equivalence + peak comparison on real corpora.

Approach: tokenise the corpus once (jieba, mimicking Phase A iter 1
starting from empty auto_dict), then run baseline vs sql detector on
the *same* token list, in separate subprocesses so peak RSS is clean.

Runs 3 cases:
  * baseline detect_ngram_phrases (in-Python)
  * sql detector, conn=:memory: (no db-temp — control run)
  * sql detector, conn=file DB with db-temp (production path)

Emits ``data/results/<case>_<corpus>.json`` with:
  * wall_ms
  * peak_rss_mb (sampler)
  * ru_maxrss_mb
  * phrase count + full phrase list (for equivalence.py diff)
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "patch"))
sys.path.insert(0, str(HERE.parents[1] / "src"))

import psutil  # noqa: E402

from grga.graph.build import (  # noqa: E402
    _new_jieba_tokenizer, tokenize_hybrid_with_pos, merge_auto_phrases,
    detect_ngram_phrases as detect_baseline,
    DEFAULT_MAX_NGRAM_N,
)
from detect_ngram_v2 import detect_ngram_phrases_sql  # noqa: E402


SANTI = Path("/media/lqw/DATA/sync/temp/三体.txt")
FANREN = Path("/media/lqw/DATA/sync/temp/《凡人修仙传》（精校全本）作者：忘语.txt")


class Sampler:
    def __init__(self, interval=0.1):
        self.interval = interval
        self.proc = psutil.Process(os.getpid())
        self.peak = 0.0
        self._stop = threading.Event()
        self._t = None

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
                rss = self.proc.memory_info().rss / (1024 * 1024)
                if rss > self.peak:
                    self.peak = rss
            except psutil.Error:
                return
            self._stop.wait(self.interval)


def _tokenize_corpus(corpus: Path) -> List[List[str]]:
    """Mimic Phase A iter 1: jieba with empty auto_dict, per-line tokens."""
    text = corpus.read_text(encoding="utf-8", errors="replace")
    jieba_tok = _new_jieba_tokenizer([])  # empty dict = iter 1
    phrase_set: set = set()
    lines: List[List[str]] = []
    for line in text.splitlines():
        toks_pos = tokenize_hybrid_with_pos(line, jieba_tok)
        toks = merge_auto_phrases(toks_pos, phrase_set)
        if toks:
            lines.append(toks)
    return lines


def _ru_maxrss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def run(case: str, corpus_path: Path, out_path: Path) -> None:
    print(f"[{case}] tokenising {corpus_path.name}...", flush=True)
    t0 = time.perf_counter()
    line_tokens = _tokenize_corpus(corpus_path)
    tok_ms = round((time.perf_counter() - t0) * 1000, 1)
    print(f"[{case}] tokenised {len(line_tokens)} lines in {tok_ms}ms", flush=True)

    sampler = Sampler()
    gc.disable(); gc.collect()
    sampler.start()
    t0 = time.perf_counter()

    if case == "baseline":
        results = detect_baseline(line_tokens, max_n=DEFAULT_MAX_NGRAM_N)
    elif case == "sql_mem":
        results = detect_ngram_phrases_sql(line_tokens, max_n=DEFAULT_MAX_NGRAM_N)
    elif case == "sql_file":
        # Simulate the production path: file-backed DB with temp_store on disk.
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name
        temp_dir = tempfile.mkdtemp(prefix="ng_temp_")
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA temp_store = FILE")
            escaped = temp_dir.replace("'", "''")
            conn.execute(f"PRAGMA temp_store_directory = '{escaped}'")
            results = detect_ngram_phrases_sql(line_tokens, conn=conn,
                                                max_n=DEFAULT_MAX_NGRAM_N)
            conn.close()
        finally:
            import shutil
            os.unlink(db_path)
            shutil.rmtree(temp_dir, ignore_errors=True)
    else:
        raise ValueError(case)

    wall_ms = round((time.perf_counter() - t0) * 1000, 1)
    sampler.stop()
    peak_rss = round(sampler.peak, 2)
    ru = round(_ru_maxrss_mb(), 2)

    print(f"[{case}] done: wall={wall_ms}ms peak_rss={peak_rss}MB "
          f"ru_maxrss={ru}MB phrases={len(results)}", flush=True)

    out = {
        "case": case,
        "corpus": corpus_path.name,
        "n_lines": len(line_tokens),
        "tok_ms": tok_ms,
        "wall_ms": wall_ms,
        "peak_rss_mb": peak_rss,
        "ru_maxrss_mb": ru,
        "n_phrases": len(results),
        "phrases": [
            {"phrase": r["phrase"], "n": r["n"], "count": r["count"],
             "npmi": r["npmi"], "branch_ent": r["branch_ent"]}
            for r in results
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--case", required=True, choices=["baseline", "sql_mem", "sql_file"])
    p.add_argument("--corpus", required=True, choices=["S", "M"])
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()
    corpus = {"S": SANTI, "M": FANREN}[args.corpus]
    run(args.case, corpus, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
