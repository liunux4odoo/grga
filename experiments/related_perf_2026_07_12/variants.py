"""Variant registry — the optimisation matrix (SPEC §3.2).

Each variant is a named BenchConfig. Variants accumulate optimisations so the
per-variant delta vs the previous one isolates a single change.
"""
from __future__ import annotations

from bench import BenchConfig

VARIANTS = {
    # V0: pure main-branch behaviour (baseline)
    "V0_baseline": BenchConfig(),

    # V1: + P1.1 stopword union (simulated at load time)
    "V1_stopword": BenchConfig(apply_stopword_union=True),

    # V2: + P1.2 npmi threshold (default 0.1)
    "V2_npmi_0.1": BenchConfig(apply_stopword_union=True, min_npmi=0.1),

    # V2b: more aggressive npmi threshold
    "V2b_npmi_0.2": BenchConfig(apply_stopword_union=True, min_npmi=0.2),

    # V3: + P1.4a quantile entry (top 10% edges). Mutually excl w/ min_npmi,
    # so this branch drops min_npmi and derives cutoff from quantile.
    "V3_quantile_0.1": BenchConfig(apply_stopword_union=True, edge_quantile=0.10),

    # V4: + P2.1 skip the never-used build_adjacency
    "V4_no_adj": BenchConfig(
        apply_stopword_union=True, min_npmi=0.1, skip_build_adjacency=True),

    # V5: + P2.2 id-native load
    "V5_id_native": BenchConfig(
        apply_stopword_union=True, min_npmi=0.1,
        skip_build_adjacency=True, id_native=True),

    # V6: + P3 numpy backend (forced)
    "V6_numpy": BenchConfig(
        apply_stopword_union=True, min_npmi=0.1,
        skip_build_adjacency=True, id_native=True, ppr_backend="numpy"),

    # VF: full recommended default (auto backend)
    "VF_full": BenchConfig(
        apply_stopword_union=True, min_npmi=0.1,
        skip_build_adjacency=True, id_native=True, ppr_backend="auto"),

    # VF2: full stack but aggressive npmi=0.2 — tests whether the higher
    # threshold clears the F1 doc-aggregation wall (fewer hub keywords in
    # the PPR top-k).
    "VF_npmi_0.2": BenchConfig(
        apply_stopword_union=True, min_npmi=0.2,
        skip_build_adjacency=True, id_native=True, ppr_backend="auto"),

    # VF3: full stack + P2.3 doc-agg id-native. This is the variant that
    # should actually clear the <3s target (F1 wall removed).
    "VF_p23": BenchConfig(
        apply_stopword_union=True, min_npmi=0.2,
        skip_build_adjacency=True, id_native=True,
        doc_agg_id_native=True, ppr_backend="auto"),
}

# Pairs used for the single-variable attribution table (SPEC §6 C2).
ATTRIBUTION = [
    ("P1.1 stopword", "V0_baseline", "V1_stopword"),
    ("P1.2 npmi=0.1", "V1_stopword", "V2_npmi_0.1"),
    ("P2.1 no_adj", "V2_npmi_0.1", "V4_no_adj"),
    ("P2.2 id_native", "V4_no_adj", "V5_id_native"),
    ("P3 numpy", "V5_id_native", "V6_numpy"),
    ("P2.3 doc-agg id", "VF_npmi_0.2", "VF_p23"),
]
