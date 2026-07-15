"""Bit-exact equivalence check between two keyword_pairs dumps.

Compares a baseline dump against a variant dump (produced by bench.py's
``_dump_pairs``). Reports the three C5 sub-criteria:

* C5-b: surviving pair set (by scope_id, kw_a_id, kw_b_id, source) strict equal
* C5-a: npmi values within abs(a-b) < 1e-9
* (Gate 2/count) cooccur_count int-strict equal on common pairs

Usage: python equivalence.py baseline.jsonl variant.jsonl [--tol 1e-9]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path) -> dict:
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            key = (r["scope_id"], r["kw_a_id"], r["kw_b_id"], r["source"])
            out[key] = (r["cooccur_count"], r["npmi"])
    return out


def compare(baseline: Path, variant: Path, tol: float = 1e-9) -> dict:
    b = _load(baseline)
    v = _load(variant)
    b_keys = set(b)
    v_keys = set(v)

    only_baseline = b_keys - v_keys
    only_variant = v_keys - b_keys
    common = b_keys & v_keys

    max_npmi_diff = 0.0
    npmi_null_mismatch = 0
    count_mismatch = 0
    npmi_diff_examples = []
    for k in common:
        bc, bn = b[k]
        vc, vn = v[k]
        if bc != vc:
            count_mismatch += 1
        if (bn is None) != (vn is None):
            npmi_null_mismatch += 1
            continue
        if bn is not None and vn is not None:
            d = abs(bn - vn)
            if d > max_npmi_diff:
                max_npmi_diff = d
            if d >= tol and len(npmi_diff_examples) < 20:
                npmi_diff_examples.append({"key": k, "baseline": bn, "variant": vn, "diff": d})

    jaccard = len(common) / len(b_keys | v_keys) if (b_keys | v_keys) else 1.0

    return {
        "baseline_pairs": len(b),
        "variant_pairs": len(v),
        "common": len(common),
        "only_baseline": len(only_baseline),
        "only_variant": len(only_variant),
        "set_jaccard": jaccard,
        "max_npmi_diff": max_npmi_diff,
        "npmi_null_mismatch": npmi_null_mismatch,
        "count_mismatch": count_mismatch,
        "npmi_diff_examples": npmi_diff_examples,
        # C5 verdict
        "C5_b_set_strict_equal": len(only_baseline) == 0 and len(only_variant) == 0,
        "C5_a_npmi_within_tol": max_npmi_diff < tol and npmi_null_mismatch == 0,
        "C5_c_count_strict_equal": count_mismatch == 0,
        "pass": (
            len(only_baseline) == 0 and len(only_variant) == 0
            and max_npmi_diff < tol and npmi_null_mismatch == 0
            and count_mismatch == 0
        ),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("baseline", type=Path)
    p.add_argument("variant", type=Path)
    p.add_argument("--tol", type=float, default=1e-9)
    args = p.parse_args()
    report = compare(args.baseline, args.variant, tol=args.tol)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
