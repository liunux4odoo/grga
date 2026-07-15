# SPEC — oom_build_fix4_2026_07_15 (PR-6: solidity gate 流式化)

> **状态**：drafting。**触发**：用户 1400 文件 / 170 万 chunks 实测在
> ngram "过实心门"（solidity gate, Stage 3）再次 OOM。
> **设计**：`docs/design/plan-2026-07-15-build-memory-oom.md`
> **前置**：PR-1/1.5/2/4 已 landed（M 档 peak 2474→517MB）

## 0. TL;DR

M 档只有 12k chunks；用户是 **170 万 chunks（~140×）**。PR-2 的
"post-freq stays small" 假设在此规模崩塌：freq-passing n-gram 达数百万条，
Stage 3 有 3 个 O(freq-passing) Python 结构同时驻留：

| 结构 | 规模（5M freq-passing 估算） | 位置 |
|---|---|---|
| `freq_rows`（fetchall list） | ~750 MB | build.py:1002 |
| `ngram_count`（dict[tuple,int]） | ~1.15 GB | build.py:1013 |
| `solid_pass`（list） | ~500 MB | build.py:1050 |
| `left_dist`/`right_dist`（Stage 4） | O(passing 邻居分布) | build.py:1073 |

合计 ~2.5 GB+ → OOM。

## 1. 方案

把 Stage 2-4 全部 SQL/流式化，Python heap 从 O(freq-passing) 降到 O(batch)：

- **Stage 2**：`freq_rows` fetchall → 落 `_ng_freq(phrase_key PK, n, c, mr)`
  TEMP TABLE + 索引（mr = MIN(rowid)，唯一，供 keyset 分页 + tiebreak 序）
- **Stage 3**：keyset 分页（`mr > ? LIMIT BATCH`）流式扫 `_ng_freq`；每批
  收集需要的 split phrase_key，一次 `WHERE phrase_key IN (...)` 批量取
  count（取代 `ngram_count` 全量 dict）；solidity 过关者直接 INSERT
  `_ng_passed(phrase_key, c, sol, mr)`（取代 `solid_pass` list）
- **Stage 4**：邻居熵改流式——UNION left/right 邻居计数，`ORDER BY
  phrase_key, side`，Python 逐 phrase 累积算熵，写 `_ng_ent(phrase_key, be)`
  （取代 `left_dist`/`right_dist` 全量 dict）
- **Stage 5**：从 `_ng_passed LEFT JOIN _ng_ent ORDER BY mr` 流式读，
  entropy gate（保持 mr 序 → 最终 sort tiebreak 不变）

Stage 6/7（salvage + sort）保持 Python——输入是 post-solidity 子集
（bounded，且 results 是返回值本身，不可避免）。

## 2. 判据

| C | 内容 | 目标 |
|---|---|---|
| C1 | Stage 3 Python heap | O(freq-passing) → O(batch)，不随语料膨胀 |
| C4 | S/M 档 6 表 bit-exact vs baseline | 严等（entropy round(4) 吸收浮点顺序） |
| C4b | pytest 149 项 | 全绿 |
| C6 | wall | ≤ 1.3× baseline（分页 + split 批查开销） |

## 3. 等价性风险

- **split count 查询**：`_npmi_pair_local` 用批查 dict 替代 `ngram_count`，
  值相同（同 `_log_c` / `_log_uni`）→ solidity bit-exact
- **entropy 顺序**：UNION 流 `ORDER BY phrase_key, side` 内部 left_tok 顺序
  impl-defined，与旧 `GROUP BY` 隐式序可能差最后 ULP → `round(be, 4)` 吸收
  （PR-2 已采同一立场）
- **tiebreak**：`_ng_passed.mr` 保 freq MIN(rowid) 序，Stage 5 `ORDER BY mr`
  → 最终 `results.sort` 稳定 tiebreak 与旧一致
- **mr 唯一性**：MIN(rowid) 每组取最小 rowid，各组不重叠 → 唯一，keyset 安全

## 4. 验证路径

1. S 档 build bit-exact（6 表严等 vs baseline）
2. pytest 149 项
3. 大合成语料（tile 多个不同源 → 制造多样 n-gram）测 Stage 3 peak 不爆
