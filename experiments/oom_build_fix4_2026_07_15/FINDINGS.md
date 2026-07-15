# FINDINGS — oom_build_fix4_2026_07_15 (PR-6 solidity gate 流式化)

**触发**：用户 1400 文件 / 170 万 chunks 在 Stage 3（solidity gate,
"过实心门"）OOM。M 档只有 12k chunks (~140×)，之前 PR-2 的
"post-freq stays small" 假设在此规模崩塌。

## TL;DR

Stage 2-4 全部 SQL/流式化：`freq_rows` fetchall → `_ng_freq` TEMP TABLE；
`ngram_count` 全量 dict → 每批分页查 split counts；`solid_pass` list → 直接
INSERT `_ng_passed`；`left_dist`/`right_dist` 全量 dict → UNION 流式 + 逐
phrase 累积算熵 → `_ng_ent` TEMP TABLE。**Stage 3 Python heap 从
O(freq-passing) 降到 O(batch=4096)**，M 档 6 表 bit-exact，wall 反而快 5%
（SQL 聚合比 Python 循环高效）。

## 判据

| C | 内容 | 结果 |
|---|---|---|
| C4 | S/M 档 6 表 bit-exact vs baseline | ✅ 全等（787k+ 行严等） |
| C4b | pytest 149 项 | ✅ 全绿 |
| C6 | wall M | ✅ 686 → 650s（**0.95×**，反而快） |
| C1 | Stage 3 Python heap | ✅ O(batch)：`_SOLIDITY_BATCH = 4096`, `_ENT_INSERT_BATCH = 4096` |

## 归因（1400 文件 / 170 万 chunks 场景）

PR-2 之前实测 M 12k chunks：post-freq n-gram ~30k 条，`ngram_count` dict
~4 MB — 假设"小"没错。用户 1.7M chunks（140×）+ 内容多样性 → post-freq
可能达数百万条：

| 结构 | 估算规模 (5M freq-passing) | 位置 (before PR-6) |
|---|---|---|
| `freq_rows` (fetchall list) | ~750 MB | build.py:1002 |
| `ngram_count` (dict[tuple,int]) | ~1.15 GB | build.py:1013 |
| `solid_pass` (list) | ~500 MB | build.py:1050 |
| `left_dist`/`right_dist` (dict) | O(passing 邻居) | build.py:1073 |

三者叠加 ~2.5 GB+ 击穿。

## 落地设计

### Stage 2: `_ng_freq` 落表（取代 fetchall）
```sql
CREATE TEMP TABLE _ng_freq(phrase_key TEXT PK, n INT, c INT, mr INT) WITHOUT ROWID
INSERT INTO _ng_freq SELECT phrase_key, n, COUNT(*), MIN(rowid) FROM _ng_ev
GROUP BY phrase_key, n HAVING c >= ?
CREATE INDEX _idx_ng_freq_mr ON _ng_freq(mr)
```
- `mr` = MIN(rowid) 保 mainline 插入序（各组不重叠 → 唯一 → keyset 安全）

### Stage 3: keyset 分页 + 每批 split IN(...) 查询
```
每批 4096 条候选：
  1. 收集本批需要的 split phrase_keys (仅 tuple splits，unigrams 走 uni)
  2. 单次 SELECT ... WHERE phrase_key IN (?, ?, ...) 批量取 count
     （按 SQLite 参数上限 500 分块）
  3. 本地 dict 算 solidity；过关者累加 to_insert
  4. executemany INSERT _ng_passed(phrase_key, c, sol, mr)
  5. keyset 前进 last_mr = 本批最大 mr
```

关键洞察：**所有 freq-passer 的 split 也必然是 freq-passer**（每次容器
出现携带一次 split 出现，count(sub) >= count(super) >= min_count），
所以 need_keys 必然在 `_ng_freq` 中命中，无需二次 fallback。

### Stage 4: UNION 流式熵计算
```sql
SELECT phrase_key, side, tok, COUNT(*) FROM (
  SELECT e.phrase_key, 'L', e.left_tok FROM _ng_ev e JOIN _ng_passed p ...
    WHERE e.left_tok IS NOT NULL
  UNION ALL
  SELECT e.phrase_key, 'R', e.right_tok FROM _ng_ev e JOIN _ng_passed p ...
    WHERE e.right_tok IS NOT NULL
) GROUP BY phrase_key, side, tok ORDER BY phrase_key, side
```
Python 端只保 **当前一个 phrase 的 left/right Counter**，phrase_key 一变
就 `_flush_current_phrase()` 算熵+入 `_ng_ent` 批。

### Stage 5: SQL JOIN 流式
```sql
SELECT p.phrase_key, p.c, p.sol, COALESCE(e.be, 0.0)
FROM _ng_passed p LEFT JOIN _ng_ent e ON p.phrase_key = e.phrase_key
ORDER BY p.mr
```
- LEFT JOIN + COALESCE：零邻居 phrase 未进 `_ng_ent` → be=0.0（与旧
  `Counter().sum()==0 → 0.0` 语义一致）
- `ORDER BY p.mr` 保 tiebreak 顺序

## 等价性论证

- **solidity 数值**：本地 `_npmi_pair` 用 `_log_uni_local` + `_log_c`
  （lru_cache），与旧代码同函数、同参、同顺序 → bit-exact
- **entropy 数值**：`_entropy_from_counter` 逐 phrase 累积后调用一次；
  内部 `sum(counter.values())` 顺序取决于 SQL `GROUP BY tok` 内部序，
  可能与旧 dict 迭代序差最后 ULP → `round(be, 4)` 吸收
- **tiebreak**：Stage 5 `ORDER BY p.mr` = 旧 `enumerate(solid_pass)` 序
  （solid_pass 是 `enumerate(freq_rows)` 过滤，freq_rows `ORDER BY MIN(rowid)`
  = p.mr）→ 严等

## 遗留 / 后续

- Stage 6 (salvage) / Stage 7 (sort) Python 保留 —— 输入是 post-entropy
  子集（bounded），`results` 是返回值本身
- 若用户 170 万 chunks 场景**still OOM**：下一嫌疑是 Stage 5 的
  `entropy_rejects` list（O(solidity-passing)），改流式化到临时表即可
- `uni` Counter O(vocab) 未流式化 —— 大 vocab 场景可能是下一台阶

## 复现

```bash
PY=/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3

# 补丁前后各 build 一次
git stash push -m "PR-6" -- src/grga/graph/build.py
$PY /tmp/kilo/build_defaults.py BASE /tmp/kilo/base.db  # ~70s S
git stash pop
$PY /tmp/kilo/build_defaults.py PR6 /tmp/kilo/pr6.db  # ~70s S

# diff 6 表
$PY experiments/oom_build_fix3_2026_07_15/equivalence_check.py /tmp/kilo/base.db /tmp/kilo/pr6.db
# 期望：全 [ OK ]，PASS
```
