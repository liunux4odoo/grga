#!/usr/bin/env bash
# v2 spec §5 acceptance shell assertions
set -e
export EXP_DIR="${EXP_DIR:-$(dirname "$(readlink -f "$0")")}"
echo "EXP_DIR=$EXP_DIR"

# S1. 关键产物存在
test -f $EXP_DIR/golden_v2.json && \
test -f $EXP_DIR/data/user_dict.txt && \
test -f $EXP_DIR/results_V2_retok.json && \
test -f $EXP_DIR/results_V4.json && \
test -f $EXP_DIR/eval_report_v2.json && \
test -f $EXP_DIR/eval_report_v2.md && \
test -f $EXP_DIR/hub_diff.md
echo "S1 ok"

# S2. golden_v2 里包含韩立
jq '.seeds[] | select(.seed == "韩立") | .expected | length' $EXP_DIR/golden_v2.json | grep -q '^5$'
echo "S2 ok"

# S3. V4 章节切分成功
jq '.corpus_stats.total_chapters >= 100' $EXP_DIR/results_V4.json | grep -q true
echo "S3 ok"

# S4. V4 里韩立在图中
jq '.queries[] | select(.seed == "韩立") | .seed_in_graph' $EXP_DIR/results_V4.json | grep -q true
echo "S4 ok"

# S5. tokenizer 修正生效
python3 -c "
import json
d = json.load(open('$EXP_DIR/results_V2_retok.json'))
miss4 = ['墨大夫','极阴祖师','筑基丹','大衍神君']
in_graph = sum(1 for q in d['queries'] if q['seed'] in miss4 and q['seed_in_graph'])
assert in_graph >= 3, f'only {in_graph}/4 seed in graph'
print('S5 ok')
"

# S6. hub_diff.md 包含 V0 / V2_retok / V4 三个 section
grep -qE '^## V0' $EXP_DIR/hub_diff.md && \
grep -qE '^## V2_retok' $EXP_DIR/hub_diff.md && \
grep -qE '^## V4' $EXP_DIR/hub_diff.md && \
grep -qE '^## (判定|Verdict)' $EXP_DIR/hub_diff.md
echo "S6 ok"

# S7. 邻居里不包含 seed 本身（自反过滤）
for v in V2_retok V4; do
  hits=$(jq -r '.queries[] | . as $q | .returned_neighbors[] | select(.term == $q.seed) | $q.seed' $EXP_DIR/results_$v.json)
  if [ -n "$hits" ]; then
    echo "FAIL: $v contains seed in neighbors: $hits"
    exit 1
  fi
done
echo "S7 ok"

# S8. hub_diff.md 显式给出实体命中数 N
grep -qE 'V4 top-30 里有 [0-9]+ 个实体' $EXP_DIR/hub_diff.md
echo "S8 ok"

# S9. build 时间 sanity check
jq '.corpus_stats.build_time_seconds < 300' $EXP_DIR/results_V4.json | grep -q true
echo "S9 ok"

echo "All acceptance checks passed"
