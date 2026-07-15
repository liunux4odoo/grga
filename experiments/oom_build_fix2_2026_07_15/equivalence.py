"""Compare two bench outputs for bit-exact phrase equivalence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("a", type=Path)
    p.add_argument("b", type=Path)
    p.add_argument("--tol", type=float, default=1e-9)
    args = p.parse_args()
    a = json.loads(args.a.read_text(encoding="utf-8"))
    b = json.loads(args.b.read_text(encoding="utf-8"))
    ma = {r["phrase"]: r for r in a["phrases"]}
    mb = {r["phrase"]: r for r in b["phrases"]}
    only_a = set(ma) - set(mb)
    only_b = set(mb) - set(ma)
    max_diff = {"npmi": 0.0, "branch_ent": 0.0}
    count_mismatch = 0
    n_mismatch = 0
    for ph, ea in ma.items():
        if ph not in mb: continue
        eb = mb[ph]
        for k in ("npmi", "branch_ent"):
            d = abs(float(ea[k]) - float(eb[k]))
            if d > max_diff[k]:
                max_diff[k] = d
        if int(ea["count"]) != int(eb["count"]):
            count_mismatch += 1
        if int(ea["n"]) != int(eb["n"]):
            n_mismatch += 1
    ok = (not only_a and not only_b
          and max_diff["npmi"] < args.tol and max_diff["branch_ent"] < args.tol
          and count_mismatch == 0 and n_mismatch == 0)
    print(json.dumps({
        "a": a["case"], "b": b["case"], "corpus": a["corpus"],
        "phrases_a": len(ma), "phrases_b": len(mb),
        "only_a": len(only_a), "only_b": len(only_b),
        "max_npmi_diff": max_diff["npmi"],
        "max_branch_ent_diff": max_diff["branch_ent"],
        "count_mismatch": count_mismatch,
        "n_mismatch": n_mismatch,
        "pass": ok,
        "only_a_sample": sorted(only_a)[:10],
        "only_b_sample": sorted(only_b)[:10],
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
