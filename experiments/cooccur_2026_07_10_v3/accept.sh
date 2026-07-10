#!/usr/bin/env bash
# v3 spec §7 acceptance assertions S1-S10
set -e
export EXP_DIR="${EXP_DIR:-$(dirname "$(readlink -f "$0")")}"
PY="${PY:-/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3}"
echo "EXP_DIR=$EXP_DIR"

# S1. results_V5_npmi.json 存在且合法 JSON
test -f "$EXP_DIR/results_V5_npmi.json"
jq empty "$EXP_DIR/results_V5_npmi.json"
echo "S1 ok"

# S2. 包含 9 个 queries
jq '.queries | length' "$EXP_DIR/results_V5_npmi.json" | grep -q '^9$'
echo "S2 ok"

# S3. 韩立 seed_in_graph = true (核心变量)
jq '.queries[] | select(.seed == "韩立") | .seed_in_graph' "$EXP_DIR/results_V5_npmi.json" | grep -q '^true$'
echo "S3 ok"

# S4. 每个 seed 的 returned_neighbors 长度 in [30, 100] 且按 npmi 严格降序
"$PY" - <<PY
import json, sys
d=json.load(open("$EXP_DIR/results_V5_npmi.json"))
for q in d["queries"]:
    n=len(q["returned_neighbors"])
    assert 30 <= n <= 100, f"{q['seed']}: n={n} not in [30,100]"
    vals=[x["npmi"] for x in q["returned_neighbors"]]
    for i in range(len(vals)-1):
        assert vals[i] >= vals[i+1], f"{q['seed']}: npmi not desc at rank {i+1}: {vals[i]} < {vals[i+1]}"
print("S4 ok")
PY

# S5. 每个邻居有 (term, npmi, w, df_target) 4 字段
"$PY" - <<PY
import json
d=json.load(open("$EXP_DIR/results_V5_npmi.json"))
req={"term","npmi","w","df_target"}
for q in d["queries"]:
    for n in q["returned_neighbors"]:
        missing=req - set(n.keys())
        assert not missing, f"{q['seed']} rank {n.get('rank')} missing {missing}"
print("S5 ok")
PY

# S6. hub_diff_v3.md 存在，含 "韩立 top-30 by npmi 里 entity 命中数 = N"
test -f "$EXP_DIR/hub_diff_v3.md"
grep -qE '韩立 top-30 by npmi 里 entity 命中数 = [0-9]+' "$EXP_DIR/hub_diff_v3.md"
echo "S6 ok"

# S7. hub_diff_v3.md 含 by w vs by npmi 两个 top-30 对比表 + top-31..100 尾部观察
grep -qE '^## V5_npmi · 按 raw w 排序' "$EXP_DIR/hub_diff_v3.md"
grep -qE '^## V5_npmi · 按 npmi 排序' "$EXP_DIR/hub_diff_v3.md"
grep -qE 'top-31 至 top-100 尾部 entity 命中 = [0-9]+' "$EXP_DIR/hub_diff_v3.md"
echo "S7 ok"

# S8. topk_usability_v3.md 存在，8 seed top-30 分类 + Top-5/10/30 汇总
test -f "$EXP_DIR/topk_usability_v3.md"
for s in 墨大夫 李化元 极阴祖师 黄枫谷 筑基丹 曲魂 大衍神君 银月; do
    grep -qE "^## ${s}" "$EXP_DIR/topk_usability_v3.md" || {
        echo "FAIL: seed section missing: $s"; exit 1; }
done
grep -qE '8-seed 均值' "$EXP_DIR/topk_usability_v3.md"
grep -qE 'Top-5 Useful%' "$EXP_DIR/topk_usability_v3.md"
grep -qE 'Top-10 Useful%' "$EXP_DIR/topk_usability_v3.md"
grep -qE 'Top-30 Useful%' "$EXP_DIR/topk_usability_v3.md"
echo "S8 ok"

# S9. build 时间 < 60 秒
jq '.corpus_stats.build_time_sec < 60' "$EXP_DIR/results_V5_npmi.json" | grep -q true
echo "S9 ok"

# S10. hub_diff_v3.md 末尾含明确判定 "位置约束假设 + npmi ranking 成立/不成立"
grep -qE '位置约束假设 \+ npmi ranking (成立|不成立)' "$EXP_DIR/hub_diff_v3.md"
echo "S10 ok"

# 邻居里不包含 seed 本身（自反过滤 sanity，非编号 S）
"$PY" - <<PY
import json
d=json.load(open("$EXP_DIR/results_V5_npmi.json"))
for q in d["queries"]:
    for n in q["returned_neighbors"]:
        assert n["term"] != q["seed"], f"self loop in {q['seed']}"
print("sanity: no self-loop ok")
PY

echo "All acceptance checks passed"
