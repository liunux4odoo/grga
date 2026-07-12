"""Regression bench for ``related --mode graph`` under the perf-plan optimizations.

Runs a synthesised 3-scope corpus, indexes it, and benchmarks a small
sweep of ``related --mode graph`` configurations to expose any regression
in the P1/P2/P3 path (docs/design/plan-2026-07-12-related-perf.md,
experiments/related_perf_2026_07_12/).

Not gated in CI — meant for humans to spot large regressions and for
future performance work to have a reproducible starting point. Prints:

* cold / warm timing per config (median of ``WARM_RUNS`` warm runs)
* PPR backend used (auto)
* doc-agg path (id-native default vs legacy via ``GRGA_DOC_AGG_LEGACY=1``)

Usage::

    python bench/related_perf.py                 # default sweep
    BENCH_WARM_RUNS=5 python bench/related_perf.py
"""
from __future__ import annotations

import os
import random
import shutil
import statistics
import tempfile
import time
from pathlib import Path
from typing import Sequence

random.seed(42)

WARM_RUNS = int(os.environ.get("BENCH_WARM_RUNS", "3"))
SEEDS = ["Kw001", "Install", "Kw042"]  # hub / cross-scope / rare-ish


def _make_corpus(root: Path, n_docs_per_scope: int = 200) -> None:
    """3 scopes × N docs, cross-scope bridges via COMMON pool."""
    KEYWORDS_POOL = [f"Kw{i:03d}" for i in range(300)]
    COMMON = ["Install", "Config", "Api", "Model", "Attention", "Client"]

    def make_doc(scope_prefix: str, doc_idx: int) -> str:
        local_pool = random.sample(KEYWORDS_POOL, 30) + COMMON
        picks = [random.choice(local_pool) for _ in range(12)]
        body = [f"# {scope_prefix} doc {doc_idx}"]
        for h in random.sample(picks[:6], 3):
            body.append(f"\n## {h}\n\n" + " ".join(random.choices(picks, k=50)))
        return "\n".join(body)

    for scope_name in ["alpha", "beta", "gamma"]:
        scope_dir = root / scope_name
        scope_dir.mkdir()
        for i in range(n_docs_per_scope):
            (scope_dir / f"doc_{i:03d}.md").write_text(
                make_doc(scope_name, i), encoding="utf-8"
            )


def _time(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000.0


def _bench_config(
    label: str,
    *,
    seeds: Sequence[str],
    db_path: Path,
    scope: Path,
    env_overrides: dict | None = None,
) -> None:
    """Run one config: 1 cold + N warm; print row."""
    from grga.graph.adjacency_cache import clear_adjacency_cache
    from grga.related import related

    old_env = {}
    if env_overrides:
        for k, v in env_overrides.items():
            old_env[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    try:
        clear_adjacency_cache()
        cold = _time(lambda: related(
            list(seeds), mode="graph", db_path=db_path, directory=scope, limit=20,
        ))
        warms = [
            _time(lambda: related(
                list(seeds), mode="graph", db_path=db_path, directory=scope, limit=20,
            ))
            for _ in range(WARM_RUNS)
        ]
        warm_p50 = statistics.median(warms)
        print(f"  {label:<32s}  cold={cold:8.1f}ms  warm_p50={warm_p50:8.1f}ms")
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="grga_related_perf_bench_"))
    print(f"[setup] workdir={tmp}")
    try:
        _make_corpus(tmp)
        t0 = time.perf_counter()
        from grga.graph.build import build_graph_index

        db = tmp / "bench.db"
        for scope_name in ["alpha", "beta", "gamma"]:
            build_graph_index(tmp / scope_name, db_path=db, min_pair_count=1)
        print(f"[setup] built 3 scopes in {(time.perf_counter() - t0):.2f}s")

        scope_alpha = tmp / "alpha"
        print(f"\nseeds={SEEDS}")
        print("Config                            cold          warm_p50")
        print("-" * 62)

        _bench_config(
            "VF (default: id-native+numpy)",
            seeds=SEEDS, db_path=db, scope=scope_alpha,
            env_overrides={
                "GRGA_DOC_AGG_LEGACY": None,
                "GRGA_PPR_BACKEND": "auto",
            },
        )
        _bench_config(
            "P2.3 off (legacy text IN)",
            seeds=SEEDS, db_path=db, scope=scope_alpha,
            env_overrides={
                "GRGA_DOC_AGG_LEGACY": "1",
                "GRGA_PPR_BACKEND": "auto",
            },
        )
        _bench_config(
            "P3 off (forced py backend)",
            seeds=SEEDS, db_path=db, scope=scope_alpha,
            env_overrides={
                "GRGA_DOC_AGG_LEGACY": None,
                "GRGA_PPR_BACKEND": "py",
            },
        )
        _bench_config(
            "min_npmi=0 (no threshold)",
            seeds=SEEDS, db_path=db, scope=scope_alpha,
            env_overrides={
                "GRGA_DOC_AGG_LEGACY": None,
                "GRGA_PPR_BACKEND": "auto",
                "GRGA_PPR_MIN_NPMI": "0",
            },
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
