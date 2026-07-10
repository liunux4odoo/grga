#!/usr/bin/env bash
# §6 acceptance script — must exit 0 to be considered done.
set -e
export EXP_DIR="${EXP_DIR:-$(cd "$(dirname "$0")" && pwd)}"
echo "EXP_DIR=$EXP_DIR"

# S1
test -f "$EXP_DIR/results_V0.json" && \
test -f "$EXP_DIR/results_V1.json" && \
test -f "$EXP_DIR/results_V2.json" && \
test -f "$EXP_DIR/results_V3.json"
echo "S1 pass"

# S2
test -f "$EXP_DIR/golden.json" && \
jq '.seeds | length' "$EXP_DIR/golden.json" | grep -q '^8$'
echo "S2 pass"

# S3
test -f "$EXP_DIR/eval_report.json" && \
test -f "$EXP_DIR/eval_report.md"
echo "S3 pass"

# S4
for v in V0 V1 V2 V3; do
  jq ".queries | length" "$EXP_DIR/results_$v.json" | grep -q '^8$' || { echo "S4 FAIL: $v"; exit 1; }
done
echo "S4 pass"

# S5
for v in V0 V1 V2 V3; do
  jq -r '.queries[] | . as $q | .returned_neighbors[] | select(.term == $q.seed)' "$EXP_DIR/results_$v.json" | \
    grep -q . && { echo "S5 FAIL: $v contains seed in neighbors"; exit 1; } || true
done
echo "S5 pass"

# S6
grep -q '^| \*\*Mean\*\*' "$EXP_DIR/eval_report.md"
echo "S6 pass"

# S7
test -f "$EXP_DIR/README.md" && \
grep -q "stopword" "$EXP_DIR/README.md"
echo "S7 pass"

# S8
jq '.corpus_stats.build_time_seconds < 900' "$EXP_DIR/results_V3.json" | grep -q true
echo "S8 pass"

# S9
jq '.auto_dropped_terms | length > 0' "$EXP_DIR/results_V2.json" | grep -q true
jq '.auto_dropped_terms | length > 0' "$EXP_DIR/results_V3.json" | grep -q true
echo "S9 pass"

# S10
for v in V0 V1 V2 V3; do
  jq ".variants.$v.mean_p_at_5" "$EXP_DIR/eval_report.json" | grep -qE '^[0-9]+(\.[0-9]+)?$' || { echo "S10 FAIL: $v"; exit 1; }
done
echo "S10 pass"

echo "All acceptance checks passed"
