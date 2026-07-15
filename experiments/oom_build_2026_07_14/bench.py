"""Single-process build memory profiler for oom_build_2026_07_14.

Runs one ``build_graph_index`` invocation under three-way memory
measurement:

* ``tracemalloc``: Python allocator peak per phase (reset at each phase
  boundary via :func:`tracemalloc.reset_peak`).
* ``resource.getrusage(RUSAGE_SELF).ru_maxrss``: process high-water RSS
  in KB (Linux). Delta between hooks tells how much RSS grew during
  that segment (all allocations, Python + C ext).
* Sampler thread with ``psutil``: 100ms polling of ``memory_info().rss``
  and ``.vms`` for a full timeline; the currently-active phase name is
  attached via a shared variable so ``analyze.py`` can bucket samples.

Invocation is designed to be forked from ``run_all.py`` so ``ru_maxrss``
starts fresh and OOMs stay isolated. Direct CLI use::

    python bench.py --corpus /path/to/dir --out result.json \\
        --ngram-iters 2 --refresh-ngram replace

Writes a single JSON per :mod:`SPEC` §4.1 to ``--out``.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import psutil  # noqa: E402

from grga.graph.build import build_graph_index  # noqa: E402


PROBE_PHASES = {
    # Phase *boundary* markers only. The ``*_advance`` events fire once
    # per _PROGRESS_TICK chunks (thousands of times) and would flood the
    # phase log — the sampler timeline already captures intra-phase RSS,
    # so we only snapshot at start/end transitions here.
    "scan_start", "scan_end",
    "ngram_iter_start", "ngram_iter_end",
    "ngram_finalize_start", "ngram_finalize_end",
    "stopword_derived",
    "materialise_start",
    "pairs_start", "pairs_scan_start", "pairs_scan_end", "pairs_done",
    "done",
}


class Sampler:
    """psutil RSS/VMS sampler on a daemon thread.

    Tags every sample with :attr:`current_phase` (a shared string mutated
    by the main thread on each ``on_event`` callback). Not perfectly
    synchronised — a sample landing mid-phase-transition might carry the
    previous phase label — but at 100ms cadence the error window is
    small enough for the phase-bucket analysis in ``analyze.py``.
    """

    def __init__(self, interval: float = 0.1) -> None:
        self.interval = interval
        self.proc = psutil.Process(os.getpid())
        self.samples: List[dict] = []
        self.current_phase: str = "startup"
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None
        self._t0 = 0.0

    def start(self) -> None:
        self._t0 = time.perf_counter()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t is not None:
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
                "vms_mb": round(mi.vms / (1024 * 1024), 2),
                "phase_at": self.current_phase,
            })
            self._stop.wait(self.interval)


def _ru_maxrss_mb() -> float:
    """Return current process RSS high-water mark in MB (Linux ru_maxrss is KB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _corpus_stats(corpus: Path) -> dict:
    if corpus.is_file():
        files = [corpus]
    else:
        files = [p for p in corpus.rglob("*")
                 if p.is_file() and p.suffix.lower() == ".txt"]
    total_bytes = sum(p.stat().st_size for p in files)
    # Read chars is expensive; approximate via bytes / 3 for CJK-dominant text.
    return {
        "corpus_files": len(files),
        "corpus_bytes": total_bytes,
        "corpus_mb": round(total_bytes / (1024 * 1024), 2),
    }


def run_once(
    corpus: Path,
    *,
    out_path: Path,
    corpus_label: str,
    run_id: int,
    ngram_iters: int,
    refresh_ngram: str,
    max_ngram_n: int,
    chunk_mode: str,
    sampler_interval: float,
    use_tracemalloc: bool,
) -> None:
    corpus_stats = _corpus_stats(corpus)
    sampler = Sampler(interval=sampler_interval)
    phase_log: List[dict] = []

    gc.disable()  # keep GC pauses out of the peak measurement noise
    gc.collect()

    # tracemalloc is OPT-IN: it adds ~4x wall-clock overhead on this
    # tokenisation-heavy workload even at frame depth=1 (every small str /
    # list allocation is traced). See FINDINGS F0. The main scaling matrix
    # runs sampler + ru_maxrss only (near-zero overhead); tracemalloc is
    # enabled only on the small corpus to get the Python-heap vs C-ext
    # (jieba / sqlite) attribution ratio once.
    if use_tracemalloc:
        tracemalloc.start(1)
    sampler.start()
    t_start = time.perf_counter()

    def _on_event(payload: dict) -> None:
        phase = str(payload.get("phase", ""))
        if phase not in PROBE_PHASES:
            return
        if use_tracemalloc:
            current, peak = tracemalloc.get_traced_memory()
            tm_current_mb = round(current / (1024 * 1024), 2)
            tm_peak_mb = round(peak / (1024 * 1024), 2)
        else:
            tm_current_mb = None
            tm_peak_mb = None
        entry = {
            "phase": phase,
            "t_wall_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
            "tracemalloc_current_mb": tm_current_mb,
            "tracemalloc_peak_mb": tm_peak_mb,
            "ru_maxrss_mb": round(_ru_maxrss_mb(), 2),
            "psutil_rss_mb": round(
                sampler.proc.memory_info().rss / (1024 * 1024), 2
            ),
        }
        # Copy anything else from the payload for phase disambiguation
        # (iter number etc.) — but strip the phase name.
        for k, v in payload.items():
            if k == "phase":
                continue
            # Filter to primitive-only fields
            if isinstance(v, (int, float, str, bool)) or v is None:
                entry[k] = v
        phase_log.append(entry)
        sampler.current_phase = phase
        if use_tracemalloc:
            tracemalloc.reset_peak()

    with tempfile.NamedTemporaryFile(
        suffix=".db", prefix="oom_bench_", delete=False
    ) as tf:
        db_path = Path(tf.name)

    error: Optional[str] = None
    stats_dict: Optional[dict] = None
    try:
        stats = build_graph_index(
            scope="oom_bench",
            directory=corpus,
            db_path=db_path,
            chunk_mode=chunk_mode,
            mode="rebuild",
            refresh_ngram=refresh_ngram,
            ngram_iters=ngram_iters,
            max_ngram_n=max_ngram_n,
            on_event=_on_event,
        )
        stats_dict = stats.to_dict()
    except MemoryError as exc:
        error = f"MemoryError: {exc}"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    wall_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
    sampler.stop()
    if use_tracemalloc:
        tm_current, tm_peak_final = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        tm_current_end_mb = round(tm_current / (1024 * 1024), 2)
    else:
        tm_current_end_mb = None
    ru_maxrss_final_mb = _ru_maxrss_mb()

    # Compute peak_rss over the sampler timeline (most reliable).
    peak_rss_mb = 0.0
    peak_rss_phase = ""
    peak_rss_t_ms = 0.0
    for s in sampler.samples:
        if s["rss_mb"] > peak_rss_mb:
            peak_rss_mb = s["rss_mb"]
            peak_rss_phase = s["phase_at"]
            peak_rss_t_ms = s["t_ms"]

    result = {
        "corpus_label": corpus_label,
        "corpus_path": str(corpus),
        **corpus_stats,
        "config": {
            "ngram_iters": ngram_iters,
            "refresh_ngram": refresh_ngram,
            "max_ngram_n": max_ngram_n,
            "chunk_mode": chunk_mode,
        },
        "run_id": run_id,
        "wall_ms": wall_ms,
        "error": error,
        "stats": stats_dict,
        "phases": phase_log,
        "peak": {
            "sampler_peak_rss_mb": peak_rss_mb,
            "sampler_peak_phase": peak_rss_phase,
            "sampler_peak_t_ms": peak_rss_t_ms,
            "ru_maxrss_mb": round(ru_maxrss_final_mb, 2),
            "tracemalloc_current_mb_at_end": tm_current_end_mb,
            "tracemalloc_enabled": use_tracemalloc,
        },
        "sampler_timeline": sampler.samples,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # Cleanup temp DB (harness owns lifecycle; run_all.py will not reuse it).
    try:
        db_path.unlink()
    except OSError:
        pass


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", required=True, type=Path)
    p.add_argument("--corpus-label", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--run-id", type=int, default=0)
    p.add_argument("--ngram-iters", type=int, default=2)
    p.add_argument("--refresh-ngram", default="replace",
                   choices=["replace", "append", "never"])
    p.add_argument("--max-ngram-n", type=int, default=4)
    p.add_argument("--chunk-mode", default="chars", choices=["chars", "line"])
    p.add_argument("--sampler-interval", type=float, default=0.1)
    p.add_argument("--tracemalloc", action="store_true",
                   help="Enable Python-heap tracing (adds ~4x overhead; use only "
                   "on the S corpus to get Python-vs-C attribution once)")
    args = p.parse_args()

    run_once(
        args.corpus,
        out_path=args.out,
        corpus_label=args.corpus_label,
        run_id=args.run_id,
        ngram_iters=args.ngram_iters,
        refresh_ngram=args.refresh_ngram,
        max_ngram_n=args.max_ngram_n,
        chunk_mode=args.chunk_mode,
        sampler_interval=args.sampler_interval,
        use_tracemalloc=args.tracemalloc,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
