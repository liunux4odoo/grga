"""Bench grga performance hotspots on a synthesised corpus."""
import cProfile, pstats, io, time, tempfile, shutil, random
from pathlib import Path

random.seed(42)
tmp = Path(tempfile.mkdtemp(prefix="grga_bench_"))
db = tmp / "bench.db"

# Corpus: 3 scopes × 200 docs each, average 500 words per doc
KEYWORDS_POOL = [f"Kw{i:03d}" for i in range(300)]  # 300 candidate keywords
COMMON = ["Install", "Config", "Api", "Model", "Attention", "Client"]  # cross-scope

def make_doc(scope_prefix: str, doc_idx: int) -> str:
    # Pick 8-15 keywords per doc, biased so a few appear more often
    local_pool = random.sample(KEYWORDS_POOL, 30) + COMMON
    picks = [random.choice(local_pool) for _ in range(12)]
    body = [f"# {scope_prefix} doc {doc_idx}"]
    for h in random.sample(picks[:6], 3):
        body.append(f"\n## {h}\n\n" + " ".join(random.choices(picks, k=50)))
    return "\n".join(body)

t0 = time.perf_counter()
for scope_name in ["alpha", "beta", "gamma"]:
    scope_dir = tmp / scope_name
    scope_dir.mkdir()
    for i in range(200):
        (scope_dir / f"doc_{i:03d}.md").write_text(
            make_doc(scope_name, i), encoding="utf-8"
        )
t_gen = time.perf_counter() - t0
print(f"[gen] 600 docs written in {t_gen:.2f}s")

# ---- Time index build ----
from grga.graph.build import build_graph_index
t0 = time.perf_counter()
for scope_name in ["alpha", "beta", "gamma"]:
    build_graph_index(tmp / scope_name, db_path=db, min_pair_count=1)
t_build = time.perf_counter() - t0
print(f"[index] 3 scopes built in {t_build:.2f}s")

import sqlite3
c = sqlite3.connect(db)
print(f"[index] keyword_chunks rows: {c.execute('SELECT COUNT(*) FROM keyword_chunks').fetchone()[0]}")
print(f"[index] heading_docs rows: {c.execute('SELECT COUNT(*) FROM heading_docs').fetchone()[0]}")
c.close()

# ---- Profile PPR-heavy path ----
from grga.related import related

def bench(label, **kwargs):
    t0 = time.perf_counter()
    for _ in range(5):
        res = related(["Install"], mode="graph", db_path=db, limit=20, **kwargs)
    dt = (time.perf_counter() - t0) / 5
    print(f"[{label}] avg {dt*1000:.1f}ms | {len(res)} results")
    return dt

bench("related graph scoped", directory=tmp / "alpha")
bench("related graph global (all 3 scopes)")
bench("related heading scoped", ) # will fall back
bench("related graph cross-scope", directory=tmp / "alpha", cross_scope=True)

# Detailed profile of one hot call
print("\n=== cProfile: related graph global (single call) ===")
pr = cProfile.Profile()
pr.enable()
for _ in range(3):
    related(["Install"], mode="graph", db_path=db, limit=20)
pr.disable()
s = io.StringIO()
pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(20)
print(s.getvalue())

# Preview cost
print("\n=== Preview extra cost ===")
from grga.related import related_graph_with_preview
t0 = time.perf_counter()
for _ in range(5):
    _ = related_graph_with_preview(
        ["Install"], db_path=db, directory=str(tmp / "alpha"),
        limit=20, preview=False,
    )
dt_no = (time.perf_counter() - t0) / 5
t0 = time.perf_counter()
for _ in range(5):
    _ = related_graph_with_preview(
        ["Install"], db_path=db, directory=str(tmp / "alpha"),
        limit=20, preview=True,
    )
dt_yes = (time.perf_counter() - t0) / 5
print(f"[preview=False] avg {dt_no*1000:.1f}ms")
print(f"[preview=True]  avg {dt_yes*1000:.1f}ms  ({dt_yes/dt_no:.1f}x)")

print(f"[bench] db kept at {db}")
# shutil.rmtree(tmp)  # keep for EXPLAIN analysis
