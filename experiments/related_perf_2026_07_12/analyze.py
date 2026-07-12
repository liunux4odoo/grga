"""Analyzer — read data/results/*.json, emit comparison tables into report_perf.md
and a condensed FINDINGS-ready summary to stdout.

Usage:
    python3 analyze.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from variants import ATTRIBUTION, VARIANTS

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "data" / "results"
REPORT = ROOT / "report_perf.md"
GROUPS = ["hub", "mid", "rare"]


def _load(name: str) -> dict | None:
    p = RESULTS / f"{name}.json"
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def _jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _doc_paths(group_data: dict) -> list:
    return [d["doc_path"] for d in group_data.get("top_docs", [])]


def main() -> int:
    loaded = {name: _load(name) for name in VARIANTS}
    present = {k: v for k, v in loaded.items() if v and "groups" in v}
    if not present:
        print("no results found — run run_all.py first", file=sys.stderr)
        return 1

    lines: list = []
    W = lines.append

    W("# report_perf — related_perf_2026_07_12\n")
    W("> 自动生成 by analyze.py。原始数据 data/results/*.json。\n")

    # -- 1. 总性能表 (cold / warm p50, 各档均值) --
    W("## 1. 总性能(3 档 seed 均值)\n")
    W("| Variant | cold 均值 (ms) | warm_p50 均值 (ms) | n_edges | n_nodes | backend |")
    W("|---|---|---|---|---|---|")
    for name in VARIANTS:
        v = present.get(name)
        if not v:
            W(f"| {name} | SKIPPED | | | | |")
            continue
        colds = [v["groups"][g]["cold_total_ms"] for g in GROUPS if g in v["groups"]]
        warms = [v["groups"][g]["warm_p50_ms"] for g in GROUPS
                 if g in v["groups"] and v["groups"][g]["warm_p50_ms"] is not None]
        any_g = next(iter(v["groups"].values()))
        cold_avg = sum(colds) / len(colds) if colds else float("nan")
        warm_avg = sum(warms) / len(warms) if warms else float("nan")
        W(f"| {name} | {cold_avg:,.1f} | {warm_avg:,.1f} | "
          f"{any_g['n_edges']:,} | {any_g['n_nodes']:,} | "
          f"{any_g['ppr_backend_used']} |")
    W("")

    # -- 2. 单项收益归因 --
    W("## 2. 单项收益归因(相邻变体差值,hub 档 warm_p50)\n")
    W("| 优化 | before → after | warm_p50 (ms) | Δ | 说明 |")
    W("|---|---|---|---|---|")
    for label, before, after in ATTRIBUTION:
        vb, va = present.get(before), present.get(after)
        if not vb or not va:
            W(f"| {label} | {before}→{after} | 缺数据 | | |")
            continue
        b = vb["groups"]["hub"]["warm_p50_ms"]
        a = va["groups"]["hub"]["warm_p50_ms"]
        if b and a:
            delta = f"{(b - a):+,.1f} ({(a/b - 1)*100:+.0f}%)"
        else:
            delta = "?"
        W(f"| {label} | {before}→{after} | {b:,.1f} → {a:,.1f} | {delta} | |")
    W("")

    # -- 3. 子步骤耗时 (VF vs V0, hub 档 cold) --
    W("## 3. 子步骤耗时对比(hub 档 cold run)\n")
    v0, vf = present.get("V0_baseline"), present.get("VF_full")
    if v0 and vf:
        s0 = v0["groups"]["hub"]["runs"][0]["step_ms"]
        sf = vf["groups"]["hub"]["runs"][0]["step_ms"]
        allk = list(dict.fromkeys(list(s0.keys()) + list(sf.keys())))
        W("| step | V0 (ms) | VF (ms) |")
        W("|---|---|---|")
        for k in allk:
            W(f"| {k} | {s0.get(k, 0):,.1f} | {sf.get(k, 0):,.1f} |")
    W("")

    # -- 4. 正确性: top-20 doc Jaccard (VF vs V0) --
    W("## 4. 正确性 top-20 doc Jaccard (VF vs V0)\n")
    W("| 档 | Jaccard | 判据 | 结论 |")
    W("|---|---|---|---|")
    thresholds = {"hub": 0.60, "mid": 0.80, "rare": 0.85}
    if v0 and vf:
        for g in GROUPS:
            if g in v0["groups"] and g in vf["groups"]:
                j = _jaccard(_doc_paths(v0["groups"][g]), _doc_paths(vf["groups"][g]))
                thr = thresholds[g]
                verdict = "✅ pass" if j >= thr else "❌ FAIL"
                W(f"| {g} | {j:.3f} | ≥{thr} | {verdict} |")
    W("")

    # -- 5. backend 等价性 (V6 numpy vs V5 py) --
    W("## 5. backend 等价性 top-20 keyword Jaccard (V6 numpy vs V5 py)\n")
    v5, v6 = present.get("V5_id_native"), present.get("V6_numpy")
    W("| 档 | keyword Jaccard | 判据 |")
    W("|---|---|---|")
    if v5 and v6:
        for g in GROUPS:
            if g in v5["groups"] and g in v6["groups"]:
                j = _jaccard(v5["groups"][g]["top_keywords"],
                             v6["groups"][g]["top_keywords"])
                verdict = "✅" if j >= 0.95 else "⚠️"
                W(f"| {g} | {j:.3f} | =1.0 {verdict} |")
    W("")

    # -- 6. quantile 便捷入口成本 --
    W("## 6. quantile 采样成本(P1.4a, C5)\n")
    v3 = present.get("V3_quantile_0.1")
    if v3:
        for g in GROUPS:
            if g in v3["groups"]:
                gd = v3["groups"][g]
                qms = gd["runs"][0]["step_ms"].get("quantile_cutoff", 0.0)
                W(f"- {g}: cutoff={gd.get('quantile_cutoff')}, "
                  f"采样耗时={qms:.1f} ms (判据 <200ms: "
                  f"{'✅' if qms < 200 else '❌'})")
    W("")

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[analyze] wrote {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
