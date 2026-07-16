"""Build a scope with the current global build path, record wall + peak.

Used to produce a reference DB for later diff against batched runs.
"""
import os, sys, time, resource, threading
import psutil
sys.path.insert(0, "src")
from pathlib import Path
from grga.graph.build import build_graph_index

label, corpus, out = sys.argv[1], sys.argv[2], sys.argv[3]
niters = int(sys.argv[4]) if len(sys.argv) > 4 else 2

proc = psutil.Process(os.getpid())
peak = [0.0]
stop = threading.Event()

def sample():
    while not stop.is_set():
        r = proc.memory_info().rss / (1024 * 1024)
        if r > peak[0]: peak[0] = r
        stop.wait(0.2)

threading.Thread(target=sample, daemon=True).start()

t0 = time.perf_counter()
stats = build_graph_index(
    scope=f"bl-{label}", directory=Path(corpus),
    db_path=Path(out), chunk_mode="chars", mode="rebuild",
    refresh_ngram="replace", ngram_iters=niters,
)
wall = time.perf_counter() - t0
stop.set()

print(f"[baseline {label}] wall={wall:.0f}s peak_rss={peak[0]:.0f}MB "
      f"pairs={stats.pairs_written} phrases={stats.ngram_dict_size} "
      f"files={stats.files_indexed}")
