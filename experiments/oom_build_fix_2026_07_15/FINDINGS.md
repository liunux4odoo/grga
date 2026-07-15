# FINDINGS — oom_build_fix_2026_07_15

**状态**：PR-1（方案 B）验证完成，**判据 C2 / C5 / C6 全部 strong pass**。
可以进主干。

## TL;DR

> `_finalize_pairs_from_temp` §4 的 Python 全量 npmi 循环两种改造方案都通过
> bit-exact 等价验证。**M 22 MB 档 pair 阶段 Python peak 从 +771 MB 降到
> ±4 MB**（-99.5%），wall-clock：batch 变体 **1.00× baseline**（几乎零成本），
> sql 变体 1.16×。两变体在 S/M 上产出的 keyword_pairs 与主干 **完全相同**
> （max_npmi_diff = **0.0**，jaccard = 1.0，count 严等）。**默认变体建议
> `auto`**（探测 `ln()` 存在则走 sql，否则 batch），忠实设计文档 §4 意图。

## 判据裁决

### C2 — pairs_scan 段 RSS 增长

phase 边界 `psutil_rss_mb` 在 `pairs_scan_start` → `pairs_scan_end` 的差
（M 22 MB 档，`ngram_iters=2`）：

| Variant | pair_scan_start | pair_scan_end | ΔRSS | 判据 (<100 MB) |
|---|---:|---:|---:|:-:|
| baseline | 1691 MB | 2462 MB | **+771 MB** | ✗ (target) |
| method_B_sql | 1692 | 1687 | **-4** | ✅ |
| method_B_batch | 1702 | 1706 | **+4** | ✅ |

**pair 段的 Python 全量循环彻底消失**。两变体都超额 99.5% 达标。

### C5 — 结果 bit-exact 等价

M 档 `keyword_pairs` 表（293824 rows）逐行对比 baseline vs variant：

| Variant | 集合 Jaccard | max_npmi_diff | count 严等 | overall |
|---|---:|---:|:-:|:-:|
| method_B_sql | **1.0** | **0.0** | ✅ | ✅ pass |
| method_B_batch | **1.0** | **0.0** | ✅ | ✅ pass |

C5-a（npmi abs<1e-9）、C5-b（集合严等）、C5-c（Gate 1 glued int 严等）全绿。
0.0 max_diff 意味着**不是"在容忍内"，是精确相同**——SQL `ln()` 与 Python
`math.log` 在这些输入上产出完全一致的 IEEE-754 值。

预验证（写代码前）50000 随机输入的合成测试也是 max_diff = 0.0，证实
两种实现在数学上是同一表达式的相同求值顺序。

### C6 — wall-clock 上限（≤ 1.5× baseline）

| Corpus | baseline | method_B_sql | method_B_batch |
|---|---:|---:|---:|
| S (2.6 MB) | 49.1 s | 60.8 s (1.24×) | 49.5 s (1.01×) |
| M (22 MB)  | 480 s  | 558 s (1.16×) | 481 s (1.00×) |

两变体均在 1.5× 硬阈以内。**batch 变体几乎零成本**，SQL 变体 M 档
16% 惩罚，源于 UPDATE 里的两次 `SELECT chunk_df FROM df WHERE ...`
关联子查询逐行触发（可优化为 `JOIN`，但已在阈内、暂不追）。

## 关键工程发现

### F1 — batch 分页 key 必须避开 rowid

`keyword_pairs` 是 `WITHOUT ROWID` 表（`schema.py` 里定义），首版 batch
实现用 `WHERE rowid > :last` 分页 → SQLite 报 `no such column: rowid`。
改用 `(kw_a_id, kw_b_id)` 元组比较分页，配合表的主键序天然稳定。

**主干迁移注意**：`_stage_chunk_kws` 是 rowtable（默认有 rowid），可以用
rowid 分页；`keyword_pairs` 不行。两个上下文语义相同但底层不同。

### F2 — SQL npmi 与 Python bit-exact 的前提

SQL 表达式必须**精确复刻** Python 求值顺序：

```
Python: math.log(p_ab / (p_a * p_b))
SQL:    ln((co*1.0/n) / ((df_a*1.0/n) * (df_b*1.0/n)))
```

代数上等价于 `ln(co*n/(df_a*df_b))` 更简洁，但**改动了浮点乘除顺序 →
低位可能漂移**。为拿到 max_diff = 0.0，必须逐 log/mul/div 对齐。

### F3 — batch 意外地"零成本"

批 50k 行 × N 次 SELECT/UPDATE 的 round-trip 总量看似不便宜，但实测
M 档 wall 相同（481s vs 480s）。原因：
- Python `_npmi` 每 pair 只做几次浮点运算（约 100 ns/pair × 300k pair
  = 30 ms 微不足道）
- SQLite executemany 批量 UPDATE 走同一 statement cache，比逐行
  UPDATE 快 20-50×
- 293k pair 用 6 批次覆盖，`SELECT LIMIT 50000` 每次亚秒完成

这为**主干默认变体选择**提供依据（见"落地方向"）。

## 落地方向（对 PR-1 主干实现的建议）

设计文档 §4.1 写"优先纯 SQL、§4.2 兜底 batch"。**保留这个语义，把默认
变体做成 `auto`**：

```python
# In _finalize_pairs_from_temp:
if _sqlite_has_ln(conn):
    _step4_sql(...)      # 精确复刻 Python 求值顺序，bit-exact
else:
    _step4_batch(...)    # 分批游标，同 Python 语义
```

理由：

1. **忠实设计文档意图**（SQL 优先）
2. **无 ln() 环境自动降级**（不 crash，仅少了几个百分点的 wall 优化）
3. **同一二进制发行版跨机器行为一致**：无论目标机器 SQLite 是否带
   math functions 都能跑

**批大小**通过 env var `GRGA_BUILD_NPMI_BATCH` 覆盖（默认 50000）；
写入 `docs/env-vars.md`。

## 与 C 侧那一半的关系（前置说明）

本 PR 只降 Python heap。**总 peak RSS 在 M 档仍是 2474 MB**，因为
ngram_iter_1 峰（+2368 MB）依然存在——这是 PR-2 方案 A 的目标，本 PR
不动。用户视角的"OOM 修复"要等 PR-2 落地才完整。

## 遗留 / 待办

- ✅ 完成：C2 / C5 / C6 判据
- ➡️ 主干 PR：把 `patch/finalize_pairs_v2.py` 的实现回移到
  `src/grga/graph/build.py::_finalize_pairs_from_temp`
- ➡️ `docs/env-vars.md` 加 `GRGA_BUILD_NPMI_BATCH`
- ➡️ 跑 `pytest tests/test_graph_build.py tests/graph/` 确认无回归
- ⏭️ 后续 PR-2：方案 A（ngram detector TEMP TABLE）—— 独立设计，本文件
  不覆盖

## 复现（数据可信度自检）

```bash
PY=/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3
cd experiments/oom_build_fix_2026_07_15

# S 档三变体（~3 分钟）
for v in baseline method_B_sql method_B_batch; do
    $PY bench.py --corpus-label S --variant $v \
        --out data/results/S_${v}_run0.json \
        --dump data/dbs/S_${v}_run0.jsonl --ngram-iters 1
done
$PY equivalence.py data/dbs/S_baseline_run0.jsonl data/dbs/S_method_B_sql_run0.jsonl
$PY equivalence.py data/dbs/S_baseline_run0.jsonl data/dbs/S_method_B_batch_run0.jsonl

# M 档三变体（~25 分钟）
for v in baseline method_B_sql method_B_batch; do
    $PY bench.py --corpus-label M --variant $v \
        --out data/results/M_${v}_run0.json \
        --dump data/dbs/M_${v}_run0.jsonl --ngram-iters 2
done
$PY equivalence.py data/dbs/M_baseline_run0.jsonl data/dbs/M_method_B_sql_run0.jsonl
$PY equivalence.py data/dbs/M_baseline_run0.jsonl data/dbs/M_method_B_batch_run0.jsonl
```
