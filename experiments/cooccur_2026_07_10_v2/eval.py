"""Evaluate results_V2_retok.json + results_V4.json vs golden_v2.json.

Contract: v2 spec §3.2. Reuses prior eval.py P@K semantics.
Only V2_retok and V4 are compared (spec §3.2). V0-V3 not restated.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GOLDEN = ROOT / "golden_v2.json"
VARIANTS = ["V2_retok", "V4"]
KS = [5, 10]


def norm(s: str) -> str:
    return s.strip().lower()


def is_relevant(term: str, expected: list[str]) -> bool:
    t = norm(term)
    for e in expected:
        en = norm(e)
        if not en:
            continue
        if en in t or t in en:
            return True
    return False


def main():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["seeds"]
    exp_by_seed = {g["seed"]: g["expected"] for g in golden}
    seeds = [g["seed"] for g in golden]

    report = {"golden_set_size": len(golden), "k_metrics": KS, "variants": {}}

    for v in VARIANTS:
        res = json.loads((ROOT / f"results_{v}.json").read_text(encoding="utf-8"))
        queries = {q["seed"]: q for q in res["queries"]}
        per_seed = {}
        p5_vals, p10_vals = [], []
        in_graph = 0
        for seed in seeds:
            q = queries[seed]
            expected = exp_by_seed[seed]
            if not q["seed_in_graph"]:
                per_seed[seed] = {"p_at_5": 0.0, "p_at_10": 0.0, "hit_terms": [], "in_graph": False}
                continue
            in_graph += 1
            terms = [n["term"] for n in q["returned_neighbors"]]
            entry = {"in_graph": True}
            hit_terms_all = []
            for k in KS:
                topk = terms[:k]
                hits = [t for t in topk if is_relevant(t, expected)]
                entry[f"p_at_{k}"] = round(len(hits) / k, 4)
                if k == max(KS):
                    hit_terms_all = hits
            seen = set()
            hit_terms = []
            for t in hit_terms_all:
                if t not in seen:
                    seen.add(t)
                    hit_terms.append(t)
            entry["hit_terms"] = hit_terms
            per_seed[seed] = entry
            p5_vals.append(entry["p_at_5"])
            p10_vals.append(entry["p_at_10"])

        mean_p5 = round(sum(p5_vals) / len(p5_vals), 4) if p5_vals else 0.0
        mean_p10 = round(sum(p10_vals) / len(p10_vals), 4) if p10_vals else 0.0
        report["variants"][v] = {
            "per_seed": per_seed,
            "mean_p_at_5": mean_p5,
            "mean_p_at_10": mean_p10,
            "seed_coverage": round(in_graph / len(seeds), 4),
        }

    (ROOT / "eval_report_v2.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- markdown ---
    lines = []
    lines.append("# 章节级共现实验 · 评估报告 (V2_retok vs V4)")
    lines.append("")
    lines.append(f"- golden set 规模: {len(golden)} seeds（含核心测试点『韩立』）")
    lines.append(f"- K metrics: {KS}")
    lines.append("- 只对比 V2_retok（整行窗口 + 新 tokenizer）与 V4（章节级窗口）两个变体")
    lines.append("")
    lines.append("## P@5 per seed")
    lines.append("")
    header = "| Seed | " + " | ".join(f"{v} P@5" for v in VARIANTS) + " |"
    sep = "|------|" + "|".join(["--------"] * len(VARIANTS)) + "|"
    lines.append(header)
    lines.append(sep)
    for seed in seeds:
        row = [seed]
        for v in VARIANTS:
            e = report["variants"][v]["per_seed"][seed]
            row.append("—" if not e["in_graph"] else f"{e['p_at_5']:.2f}")
        lines.append("| " + " | ".join(row) + " |")
    mean_row = ["**Mean**"]
    for v in VARIANTS:
        mean_row.append(f"**{report['variants'][v]['mean_p_at_5']:.2f}**")
    lines.append("| " + " | ".join(mean_row) + " |")
    lines.append("")

    lines.append("## P@10 per seed")
    lines.append("")
    lines.append("| Seed | " + " | ".join(f"{v} P@10" for v in VARIANTS) + " |")
    lines.append(sep)
    for seed in seeds:
        row = [seed]
        for v in VARIANTS:
            e = report["variants"][v]["per_seed"][seed]
            row.append("—" if not e["in_graph"] else f"{e['p_at_10']:.2f}")
        lines.append("| " + " | ".join(row) + " |")
    mean_row = ["**Mean**"]
    for v in VARIANTS:
        mean_row.append(f"**{report['variants'][v]['mean_p_at_10']:.2f}**")
    lines.append("| " + " | ".join(mean_row) + " |")
    lines.append("")

    lines.append("## seed coverage (seed_in_graph=true 比例)")
    lines.append("")
    lines.append("| " + " | ".join(VARIANTS) + " |")
    lines.append("|" + "|".join(["----"] * len(VARIANTS)) + "|")
    lines.append("| " + " | ".join(
        f"{report['variants'][v]['seed_coverage']:.3f}" for v in VARIANTS) + " |")
    lines.append("")

    (ROOT / "eval_report_v2.md").write_text("\n".join(lines), encoding="utf-8")

    print("mean_p_at_5:", {v: report["variants"][v]["mean_p_at_5"] for v in VARIANTS})
    print("mean_p_at_10:", {v: report["variants"][v]["mean_p_at_10"] for v in VARIANTS})
    print("coverage:", {v: report["variants"][v]["seed_coverage"] for v in VARIANTS})


if __name__ == "__main__":
    main()
