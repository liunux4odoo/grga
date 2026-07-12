"""One-shot runner — execute every variant × every seed group, dump JSON.

Usage:
    python3 run_all.py                    # everything
    python3 run_all.py V0_baseline VF_full  # specific variants only

Each variant is run for every seed group with:
    - 1 cold run (fresh SQLite connection + fresh in-process state)
    - 3 warm runs (reuse connection; simulates adjacency_cache hit)

Results dumped to data/results/<variant>.json.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench import BenchConfig, RunResult, connect_ro, run_related_graph
from variants import VARIANTS

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RESULTS = DATA / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

import os as _os
WARM_RUNS = int(_os.environ.get("BENCH_WARM_RUNS", "2"))


def _load_seeds() -> dict:
    with (DATA / "seeds.json").open(encoding="utf-8") as f:
        return json.load(f)


def _run_variant(name: str, cfg: BenchConfig, seeds_data: dict) -> dict:
    variant_out = {
        "variant": name,
        "config": asdict(cfg),
        "groups": {},
    }
    for group_name, group_meta in seeds_data.items():
        seeds = group_meta["seeds"]
        runs = []

        # cold: fresh connection, fresh state (nothing to clear — bench is stateless)
        conn = connect_ro()
        try:
            t0 = time.perf_counter()
            r_cold = run_related_graph(conn, seeds, cfg)
            wall = (time.perf_counter() - t0) * 1000.0
            runs.append(_serialize_run(r_cold, wall, "cold", 0))
            # warm: reuse connection
            for i in range(WARM_RUNS):
                t0 = time.perf_counter()
                r = run_related_graph(conn, seeds, cfg)
                wall = (time.perf_counter() - t0) * 1000.0
                runs.append(_serialize_run(r, wall, "warm", i + 1))
        finally:
            conn.close()

        # Real median of warm runs (statistics.median averages the two
        # middle values on even counts; naive `sorted[n//2]` would return
        # the MAX under WARM_RUNS=2, biasing every downstream number).
        warm_totals = [r["total_ms"] for r in runs if r["kind"] == "warm"]
        warm_p50 = statistics.median(warm_totals) if warm_totals else None

        variant_out["groups"][group_name] = {
            "seeds": seeds,
            "cold_total_ms": runs[0]["total_ms"],
            "warm_p50_ms": warm_p50,
            "runs": runs,
            # semantic snapshots taken from the cold run (deterministic given cfg)
            "top_keywords": r_cold.top_keywords,
            "top_docs": r_cold.top_docs,
            "n_edges": r_cold.n_edges,
            "n_nodes": r_cold.n_nodes,
            "ppr_backend_used": r_cold.ppr_backend_used,
            "quantile_cutoff": r_cold.quantile_cutoff,
        }
    return variant_out


def _serialize_run(r: RunResult, wall_ms: float, kind: str, idx: int) -> dict:
    return {
        "kind": kind,
        "idx": idx,
        "total_ms": round(r.total_ms, 2),
        "wall_ms": round(wall_ms, 2),
        "step_ms": {k: round(v, 2) for k, v in r.step_ms.items()},
        "n_edges": r.n_edges,
        "n_nodes": r.n_nodes,
        "ppr_backend_used": r.ppr_backend_used,
    }


def main(argv: list) -> int:
    seeds = _load_seeds()
    names = argv[1:] if len(argv) > 1 else list(VARIANTS.keys())
    unknown = [n for n in names if n not in VARIANTS]
    if unknown:
        print(f"unknown variants: {unknown}", file=sys.stderr)
        return 2

    for name in names:
        cfg = VARIANTS[name]
        print(f"\n=== {name}  cfg={cfg}", flush=True)
        try:
            out = _run_variant(name, cfg, seeds)
        except Exception as e:
            print(f"  ! FAILED: {type(e).__name__}: {e}", flush=True)
            out = {"variant": name, "config": {}, "error": f"{type(e).__name__}: {e}"}
        target = RESULTS / f"{name}.json"
        with target.open("w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"  → wrote {target.name}", flush=True)
        if "groups" in out:
            for g, gd in out["groups"].items():
                print(f"     {g:6s}  cold={gd['cold_total_ms']:>9.1f} ms  "
                      f"warm_p50={gd['warm_p50_ms']:>7.1f} ms  "
                      f"edges={gd['n_edges']:>8,}  "
                      f"backend={gd['ppr_backend_used']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
