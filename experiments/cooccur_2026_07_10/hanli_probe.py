"""Side-script: query '韩立' as seed against all 4 variants.

Reuses build.py machinery, does not modify golden.json or results_V*.json.
Motivation: user wants to know whether '韩立' (highest-DF token) — which
gets auto-dropped in V2/V3 — has a semantically meaningful neighbor list
in V0/V1, and how that differs from the semantic seeds. This distinguishes
"stopword" (pure noise) from "hub node" (high-connectivity but meaningful).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build import (
    VARIANTS,
    tokenize_corpus,
    build_variant,
    top_neighbors,
)

ROOT = Path(__file__).resolve().parent
HUB_SEED = "韩立"
TOP_K = 30


def main():
    print(f"[hanli-probe] tokenizing (cached expected)...")
    lines_tokens = tokenize_corpus()

    report = {"seed": HUB_SEED, "top_k": TOP_K, "variants": {}}

    for vcfg in VARIANTS:
        vname = vcfg["variant"]
        print(f"\n=== {vname} ===")
        result = build_variant(
            variant=vname,
            lines_tokens=lines_tokens,
            stopwords=vcfg["stopwords"],
            df_adaptive=vcfg["df_adaptive"],
            top_k_per_line=vcfg["top_k_per_line"],
            min_pair_count=vcfg["min_pair_count"],
            npmi_threshold=vcfg["npmi_threshold"],
            stopword_source=vcfg["stopword_source"],
        )
        adj = result["_adj"]
        auto_dropped = result["auto_dropped_terms"]
        in_dropped = HUB_SEED in set(auto_dropped)

        nbrs = top_neighbors(adj, HUB_SEED, k=TOP_K)
        if nbrs is None:
            print(f"[{vname}] seed '{HUB_SEED}' NOT IN GRAPH (dropped? {in_dropped})")
            report["variants"][vname] = {
                "in_graph": False,
                "auto_dropped": in_dropped,
                "neighbors": [],
                "total_edges": result["corpus_stats"]["total_edges"],
            }
        else:
            print(f"[{vname}] seed in graph, {len(nbrs)} neighbors returned "
                  f"(auto_dropped: {in_dropped})")
            for i, (term, w) in enumerate(nbrs[:TOP_K], 1):
                print(f"  {i:2d}. {term:8s}  w={w:.3f}")
            report["variants"][vname] = {
                "in_graph": True,
                "auto_dropped": in_dropped,
                "neighbors": [{"rank": i + 1, "term": t, "weight": round(float(w), 4)}
                              for i, (t, w) in enumerate(nbrs)],
                "total_edges": result["corpus_stats"]["total_edges"],
            }

    out_path = ROOT / "hanli_probe.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[hanli-probe] wrote {out_path}")


if __name__ == "__main__":
    main()
