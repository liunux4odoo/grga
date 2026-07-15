"""Peak-RSS + wall bench for one build_graph_index invocation.

Used by PR-4 to compare baseline (git HEAD) vs PR-4 patch on the same
corpus + params. sampler thread polls psutil at 200ms; no tracemalloc
(recall PR-2 F0: tracemalloc adds ~4x overhead here).

Usage:
    python bench.py <label> <db_out> <corpus_txt> [--ngram-iters N]
"""
from __future__ import annotations
import argparse, os, sys, threading, time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import psutil
from grga.graph.build import build_graph_index


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("db_out")
    ap.add_argument("corpus")
    ap.add_argument("--ngram-iters", type=int, default=2)
    ap.add_argument("--scope", default="pr4-bench")
    args = ap.parse_args()

    proc = psutil.Process(os.getpid())
    peak = [0.0]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            r = proc.memory_info().rss / (1024 * 1024)
            if r > peak[0]:
                peak[0] = r
            stop.wait(0.2)

    threading.Thread(target=sample, daemon=True).start()
    t0 = time.perf_counter()
    stats = build_graph_index(
        scope=args.scope, directory=Path(args.corpus),
        db_path=Path(args.db_out), chunk_mode="chars", mode="rebuild",
        refresh_ngram="replace", ngram_iters=args.ngram_iters,
    )
    stop.set()
    wall = time.perf_counter() - t0
    # psutil peak is already MB and cross-platform correct.
    # ``resource.getrusage.ru_maxrss`` unit differs between Linux (KB) and
    # macOS (bytes) — drop it here rather than special-case per platform.
    print(f"[{args.label}] wall={wall:.0f}s peak_rss={peak[0]:.0f}MB "
          f"pairs={stats.pairs_written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
