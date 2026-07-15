"""Read data/results/*.json and produce a phase-level memory comparison.

Outputs ``report_memory.md`` with:

* Per-cell peak RSS + wall time (median across runs, min/max as spread)
* Per-phase RSS delta table (which segment is the biggest jump?)
* External scaling: peak_rss_mb vs corpus_mb regression + R²
* Cross-measurement consistency: ru_maxrss vs sampler_peak
* Optional tracemalloc-vs-RSS attribution (S corpus only)
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "data" / "results"
REPORT = HERE / "report_memory.md"


def load_all() -> list[dict]:
    out = []
    for p in sorted(RESULTS_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] {p.name}: {exc}")
            continue
        d["_path"] = p
        out.append(d)
    return out


def phase_deltas(result: dict) -> list[tuple[str, float, dict]]:
    """Return [(phase, ru_maxrss_delta_mb, extras)] in chronological order."""
    phases = result.get("phases") or []
    out = []
    prev = 0.0
    for p in phases:
        rss = p.get("ru_maxrss_mb")
        if rss is None:
            continue
        delta = rss - prev
        extras = {k: v for k, v in p.items()
                  if k not in {"phase", "t_wall_ms",
                                "tracemalloc_current_mb", "tracemalloc_peak_mb",
                                "ru_maxrss_mb", "psutil_rss_mb"}}
        out.append((p["phase"], delta, extras))
        prev = rss
    return out


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else float("nan")


def group_runs(results: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group by (corpus_label, config signature)."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in results:
        if r.get("error"):
            continue
        cl = r.get("corpus_label", "?")
        cfg = r.get("config", {})
        sig = f"i{cfg.get('ngram_iters','?')}_{cfg.get('refresh_ngram','?')[:3]}_n{cfg.get('max_ngram_n','?')}"
        grouped[(cl, sig)].append(r)
    return grouped


def build_report(results: list[dict]) -> str:
    lines: list[str] = []
    L = lines.append
    L("# oom_build_2026_07_14 — memory profile report\n")
    L(f"Generated from {len(results)} runs "
      f"({sum(1 for r in results if not r.get('error'))} clean, "
      f"{sum(1 for r in results if r.get('error'))} errored).\n")

    # --- 1. Per-cell summary ---
    L("## 1. Peak RSS by (corpus, config)\n")
    L("| corpus | config | runs | corpus_mb | wall_s p50 | peak_rss p50 | peak phase | ru_max p50 | tm_traced? |")
    L("|---|---|---:|---:|---:|---:|---|---:|:-:|")
    grouped = group_runs(results)
    rows_for_regression: list[tuple[float, float]] = []
    for (cl, sig), runs in sorted(grouped.items()):
        peaks = [r["peak"]["sampler_peak_rss_mb"] for r in runs
                 if r.get("peak")]
        walls = [r["wall_ms"] / 1000.0 for r in runs if "wall_ms" in r]
        rumax = [r["peak"]["ru_maxrss_mb"] for r in runs if r.get("peak")]
        corpus_mb = runs[0].get("corpus_mb", 0)
        peak_phase = runs[0]["peak"]["sampler_peak_phase"] if runs[0].get("peak") else "?"
        tm = "y" if any(r.get("peak", {}).get("tracemalloc_enabled") for r in runs) else "n"
        L(f"| {cl} | {sig} | {len(runs)} | {corpus_mb:.1f} | {_median(walls):.1f} | "
          f"{_median(peaks):.1f} | {peak_phase} | {_median(rumax):.1f} | {tm} |")
        # For regression: use baseline config on each corpus.
        if sig.startswith("i2_rep_n4"):
            rows_for_regression.append((corpus_mb, _median(peaks)))
    L("")

    # --- 2. Per-phase RSS delta (biggest jumps) ---
    L("## 2. Where RSS grew (per-phase Δru_maxrss, MB)\n")
    L("Rows aggregated across runs of the same (corpus, config); median Δ shown.\n")
    L("| corpus | config | phase | Δ MB p50 | extras (sample) |")
    L("|---|---|---|---:|---|")
    for (cl, sig), runs in sorted(grouped.items()):
        all_phase_deltas: dict[str, list[float]] = defaultdict(list)
        sample_extras: dict[str, dict] = {}
        for r in runs:
            for phase, delta, extras in phase_deltas(r):
                all_phase_deltas[phase].append(delta)
                if phase not in sample_extras:
                    sample_extras[phase] = extras
        # Only print phases whose median delta is >= 1 MB
        for phase, deltas in all_phase_deltas.items():
            med = _median(deltas)
            if abs(med) < 1:
                continue
            extras = sample_extras.get(phase, {})
            extras_s = " ".join(f"{k}={v}" for k, v in extras.items()
                                if isinstance(v, (int, float, str)))
            L(f"| {cl} | {sig} | {phase} | {med:+.1f} | {extras_s[:80]} |")
    L("")

    # --- 3. Scaling: peak_rss vs corpus_mb (baseline config) ---
    L("## 3. Peak RSS vs corpus size (baseline config)\n")
    if len(rows_for_regression) >= 2:
        L("| corpus_mb | peak_rss_mb | ratio |")
        L("|---:|---:|---:|")
        for mb, peak in sorted(rows_for_regression):
            L(f"| {mb:.1f} | {peak:.1f} | {peak/mb:.1f}× |")
        # Simple linear regression on the sorted rows
        xs = [r[0] for r in rows_for_regression]
        ys = [r[1] for r in rows_for_regression]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        den = sum((x - mean_x) ** 2 for x in xs) or 1.0
        slope = num / den
        intercept = mean_y - slope * mean_x
        ss_tot = sum((y - mean_y) ** 2 for y in ys) or 1.0
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        r2 = 1 - ss_res / ss_tot
        L("")
        L(f"Linear fit: `peak_rss_mb = {slope:.1f} × corpus_mb + {intercept:.1f}` "
          f"(R² = {r2:.3f}).")
        L("")
        # Extrapolation
        L(f"Extrapolation at 100 MB corpus → **{slope * 100 + intercept:.0f} MB peak RSS**.")
        L(f"Extrapolation at 500 MB corpus → **{slope * 500 + intercept:.0f} MB peak RSS**.")
    else:
        L("*(Need at least 2 baseline-config corpora to fit — run more sizes.)*")
    L("")

    # --- 4. Cross-measurement consistency ---
    L("## 4. Measurement consistency (sampler vs ru_maxrss)\n")
    L("| corpus | config | sampler_peak | ru_maxrss | delta MB | ratio |")
    L("|---|---|---:|---:|---:|---:|")
    for (cl, sig), runs in sorted(grouped.items()):
        for r in runs:
            peak = r.get("peak")
            if not peak:
                continue
            sp = peak.get("sampler_peak_rss_mb", 0)
            ru = peak.get("ru_maxrss_mb", 0)
            if ru <= 0:
                continue
            L(f"| {cl} | {sig} | {sp:.1f} | {ru:.1f} | {sp-ru:+.1f} | {sp/ru:.3f} |")
    L("")

    # --- 5. Tracemalloc attribution (S corpus only) ---
    tm_runs = [r for r in results
               if r.get("peak", {}).get("tracemalloc_enabled")
               and not r.get("error")
               and r.get("phases")]
    if tm_runs:
        L("## 5. Python-heap vs total RSS (tracemalloc runs)\n")
        L("How much of the peak RSS is Python-allocator-visible objects?\n")
        L("| corpus | config | phase | tm_peak MB | ru_maxrss MB | py_share |")
        L("|---|---|---|---:|---:|---:|")
        for r in tm_runs:
            cl = r["corpus_label"]
            cfg = r["config"]
            sig = f"i{cfg['ngram_iters']}_n{cfg['max_ngram_n']}"
            for p in r["phases"]:
                if p.get("tracemalloc_peak_mb") is None:
                    continue
                if p["tracemalloc_peak_mb"] < 1:
                    continue
                ru = p.get("ru_maxrss_mb", 0)
                tm_p = p["tracemalloc_peak_mb"]
                share = tm_p / ru if ru > 0 else 0
                L(f"| {cl} | {sig} | {p['phase']} | {tm_p:.1f} | {ru:.1f} | {share:.2%} |")
        L("")

    return "\n".join(lines)


def main() -> int:
    results = load_all()
    if not results:
        print("no result files found in", RESULTS_DIR)
        return 1
    report = build_report(results)
    REPORT.write_text(report, encoding="utf-8")
    print(f"wrote {REPORT} ({len(report)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
