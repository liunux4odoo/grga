"""Bench one build with a swappable ``_finalize_pairs_from_temp`` variant.

Reuses the measurement approach of ``oom_build_2026_07_14/bench.py``
(subprocess-hosted, sampler + ru_maxrss, opt-in tracemalloc) but adds a
``--variant`` that monkey-patches ``grga.graph.build._finalize_pairs_from_temp``
before the build runs. Also dumps the resulting ``keyword_pairs`` table
to a canonical .jsonl for equivalence.py.
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
import tracemalloc
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(HERE / "patch"))

import psutil  # noqa: E402

import grga.graph.build as build_mod  # noqa: E402
from grga.graph.build import build_graph_index  # noqa: E402
from grga.graph.schema import CHUNK_COOCCUR_SOURCE  # noqa: E402
import finalize_pairs_v2  # noqa: E402

SANTI = Path("/media/lqw/DATA/sync/temp/三体.txt")
FANREN = Path("/media/lqw/DATA/sync/temp/《凡人修仙传》（精校全本）作者：忘语.txt")

PROBE_PHASES = {
    "scan_start", "scan_end",
    "ngram_iter_start", "ngram_iter_end",
    "ngram_finalize_start",
    "stopword_derived", "materialise_start",
    "pairs_start", "pairs_scan_start", "pairs_scan_end", "pairs_done",
    "done",
}


class Sampler:
    def __init__(self, interval: float = 0.1) -> None:
        self.interval = interval
        self.proc = psutil.Process(os.getpid())
        self.samples: List[dict] = []
        self.current_phase = "startup"
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None
        self._t0 = 0.0

    def start(self) -> None:
        self._t0 = time.perf_counter()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                mi = self.proc.memory_info()
            except psutil.Error:
                return
            self.samples.append({
                "t_ms": round((time.perf_counter() - self._t0) * 1000.0, 2),
                "rss_mb": round(mi.rss / (1024 * 1024), 2),
                "phase_at": self.current_phase,
            })
            self._stop.wait(self.interval)


def _ru_maxrss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _resolve(label: str) -> Path:
    return {"S": SANTI, "M": FANREN}[label]


def _dump_pairs(db_path: Path, out: Path) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi "
        "FROM keyword_pairs ORDER BY scope_id, kw_a_id, kw_b_id, source"
    ).fetchall()
    conn.close()
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({
                "scope_id": r[0], "kw_a_id": r[1], "kw_b_id": r[2],
                "source": r[3], "cooccur_count": r[4], "npmi": r[5],
            }) + "\n")
    return len(rows)


def run_once(
    corpus: Path, *, out_path: Path, dump_path: Path,
    corpus_label: str, variant: str, run_id: int,
    ngram_iters: int, use_tracemalloc: bool, sampler_interval: float,
) -> None:
    # Monkey-patch the finalize function for non-baseline variants.
    original = build_mod._finalize_pairs_from_temp
    if variant != "baseline":
        build_mod._finalize_pairs_from_temp = finalize_pairs_v2.make_finalize(variant)

    sampler = Sampler(interval=sampler_interval)
    phase_log: List[dict] = []
    gc.disable()
    gc.collect()
    if use_tracemalloc:
        tracemalloc.start(1)
    sampler.start()
    t_start = time.perf_counter()

    def _on_event(payload: dict) -> None:
        phase = str(payload.get("phase", ""))
        if phase not in PROBE_PHASES:
            return
        if use_tracemalloc:
            _, peak = tracemalloc.get_traced_memory()
            tm_peak = round(peak / (1024 * 1024), 2)
        else:
            tm_peak = None
        phase_log.append({
            "phase": phase,
            "t_wall_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
            "tracemalloc_peak_mb": tm_peak,
            "ru_maxrss_mb": round(_ru_maxrss_mb(), 2),
            "psutil_rss_mb": round(sampler.proc.memory_info().rss / (1024 * 1024), 2),
            **{k: v for k, v in payload.items()
               if k != "phase" and isinstance(v, (int, float, str, bool))},
        })
        sampler.current_phase = phase
        if use_tracemalloc:
            tracemalloc.reset_peak()

    with tempfile.NamedTemporaryFile(suffix=".db", prefix="oomfix_", delete=False) as tf:
        db_path = Path(tf.name)

    error = None
    stats_dict = None
    n_pairs = 0
    try:
        stats = build_graph_index(
            scope="oom_bench", directory=corpus, db_path=db_path,
            chunk_mode="chars", mode="rebuild", refresh_ngram="replace",
            ngram_iters=ngram_iters, on_event=_on_event,
        )
        stats_dict = stats.to_dict()
        n_pairs = _dump_pairs(db_path, dump_path)
    except MemoryError as exc:
        error = f"MemoryError: {exc}"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        build_mod._finalize_pairs_from_temp = original

    wall_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
    sampler.stop()
    if use_tracemalloc:
        tracemalloc.stop()
    ru_final = _ru_maxrss_mb()

    peak_rss = max((s["rss_mb"] for s in sampler.samples), default=0.0)
    peak_phase = ""
    for s in sampler.samples:
        if s["rss_mb"] == peak_rss:
            peak_phase = s["phase_at"]
            break

    # per-phase ru_maxrss delta so pairs_scan_end contribution is visible
    result = {
        "corpus_label": corpus_label, "variant": variant, "run_id": run_id,
        "corpus_mb": round(corpus.stat().st_size / (1024 * 1024), 2) if corpus.is_file() else None,
        "config": {"ngram_iters": ngram_iters},
        "wall_ms": wall_ms, "error": error, "n_pairs": n_pairs,
        "stats": stats_dict, "phases": phase_log,
        "peak": {
            "sampler_peak_rss_mb": peak_rss,
            "sampler_peak_phase": peak_phase,
            "ru_maxrss_mb": round(ru_final, 2),
            "tracemalloc_enabled": use_tracemalloc,
        },
        "dump_path": str(dump_path),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    try:
        db_path.unlink()
    except OSError:
        pass


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--corpus-label", required=True, choices=["S", "M"])
    p.add_argument("--variant", required=True,
                   choices=["baseline", "method_B_sql", "method_B_batch", "method_B_auto"])
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--dump", required=True, type=Path)
    p.add_argument("--run-id", type=int, default=0)
    p.add_argument("--ngram-iters", type=int, default=1)
    p.add_argument("--sampler-interval", type=float, default=0.1)
    p.add_argument("--tracemalloc", action="store_true")
    args = p.parse_args()
    run_once(
        _resolve(args.corpus_label), out_path=args.out, dump_path=args.dump,
        corpus_label=args.corpus_label, variant=args.variant, run_id=args.run_id,
        ngram_iters=args.ngram_iters, use_tracemalloc=args.tracemalloc,
        sampler_interval=args.sampler_interval,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
