"""Cross-check ``detect_ngram_phrases_sql`` against the mainline
``detect_ngram_phrases`` on all inputs used by the pytest unit tests +
a couple of stress inputs. Reports any drift in phrase set or attributes.

Run: python equivalence_smoke.py
Exit 0 = all equivalent, 1 = any mismatch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "patch"))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from grga.graph.build import detect_ngram_phrases as detect_baseline  # noqa: E402
from detect_ngram_v2 import detect_ngram_phrases_sql  # noqa: E402


def _phrase_map(entries):
    return {e["phrase"]: e for e in entries}


def _same(a: dict, b: dict) -> tuple[bool, str]:
    keys = ("phrase", "n", "count", "npmi", "branch_ent")
    for k in keys:
        va = a.get(k)
        vb = b.get(k)
        if isinstance(va, float) or isinstance(vb, float):
            if abs((va or 0.0) - (vb or 0.0)) > 1e-9:
                return False, f"{k}: {va!r} vs {vb!r}"
        else:
            if va != vb:
                return False, f"{k}: {va!r} vs {vb!r}"
    return True, ""


def check(name: str, lines: list, **kwargs) -> bool:
    b = detect_baseline(lines, **kwargs)
    v = detect_ngram_phrases_sql(lines, **kwargs)
    bmap = _phrase_map(b)
    vmap = _phrase_map(v)
    if set(bmap) != set(vmap):
        only_b = set(bmap) - set(vmap)
        only_v = set(vmap) - set(bmap)
        print(f"[FAIL] {name}: phrase set differs; only_baseline={sorted(only_b)!r}"
              f"  only_sql={sorted(only_v)!r}")
        return False
    for ph in bmap:
        ok, why = _same(bmap[ph], vmap[ph])
        if not ok:
            print(f"[FAIL] {name}: phrase {ph!r}: {why}")
            return False
    # Also check order-sensitive: since final sort key is (-npmi, -count)
    # with Python stable sort, tie-breaks depend on upstream insertion order.
    if [e["phrase"] for e in b] != [e["phrase"] for e in v]:
        # Order differs — this may or may not be a real bug. Report but
        # don't fail: caller of detect_ngram_phrases treats result as set
        # for phrase_set + as list for auto_dict[phrase] = entry (order
        # doesn't affect dict semantics).
        print(f"[WARN] {name}: order differs but content bit-exact")
    print(f"[ OK ] {name}: {len(b)} phrases, bit-exact")
    return True


def main() -> int:
    ok = True
    # 1. finds_repeated_compounds
    ok &= check(
        "finds_repeated_compounds",
        [
            ["指挥官", "章", "北海", "驾驶", "飞船"],
            ["士兵", "章", "北海", "看到", "地球"],
            ["船员", "章", "北海", "登上", "舰桥"],
            ["船长", "章", "北海", "开始", "训练"],
            ["少校", "章", "北海", "命令", "开火"],
        ],
        max_n=3, ent_threshold=0.5,
    )
    # 2. bounded_npmi
    ok &= check(
        "bounded_npmi",
        [["甲", "乙", "丙"] for _ in range(5)],
        max_n=3, min_count=3,
        npmi_threshold=-1.0, ent_threshold=-1.0,
    )
    # 3. salvages_substring
    def line3(prefix, tail):
        return list(prefix) + list("苍坤上人") + list(tail)
    ok &= check(
        "salvages_substring",
        [
            line3("师父", "开示"), line3("拜访", "讲经"),
            line3("寺中", "闭关"), line3("徒弟", "接引"),
            line3("山门", "亲迎"),
            list("苍坤") + list("早年游学"),
        ],
        max_n=4, min_count=3,
    )
    # 4. salvages_infix_not_only_edges
    ok &= check(
        "salvages_infix",
        [
            list("张甲乙丙丁一"), list("李甲乙丙丁二"),
            list("王甲乙丙丁三"), list("赵甲乙丙丁四"),
            list("钱甲乙丙丁五"),
            list("独乙丙出"), list("孤乙丙立"),
        ],
        max_n=4, min_count=3, ent_threshold=0.5,
    )
    # 5. drops_zero_margin_boundary_fragments
    ok &= check(
        "drops_boundary_fragments",
        [
            line3("师父", "开示"), line3("拜访", "讲经"),
            line3("寺中", "闭关"), line3("徒弟", "接引"),
            line3("山门", "亲迎"),
            list("苍坤") + list("早年游学"),
        ],
        max_n=4, min_count=3,
    )
    # 6. salvages_via_multiple_containers
    def with_container(prefix, tail, container):
        return list(prefix) + list(container) + list(tail)
    ok &= check(
        "salvages_multi_containers",
        [
            with_container("师父", "开示", "苍坤上人"),
            with_container("拜访", "讲经", "苍坤上人"),
            with_container("寺中", "闭关", "苍坤上人"),
            with_container("门徒", "参礼", "苍坤真人"),
            with_container("客卿", "拜见", "苍坤真人"),
            with_container("弟子", "叩首", "苍坤真人"),
        ],
        max_n=4, min_count=3,
    )
    # 7. slightly larger stress — repeated triples with varied context
    stress = []
    for i in range(200):
        stress.append(["prefix"+str(i%20), "alpha", "beta", "suffix"+str(i%15)])
        stress.append(["ctx"+str(i%17), "beta", "gamma", "tail"+str(i%13)])
    ok &= check("stress_200_lines", stress, max_n=3, min_count=5)

    print()
    print("ALL PASS" if ok else "MISMATCH FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
