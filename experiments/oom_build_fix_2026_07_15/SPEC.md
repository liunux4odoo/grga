# SPEC — oom_build_fix_2026_07_15

> **状态**：drafting，PR-1（方案 B）实施中。
> **设计文档**：`docs/design/plan-2026-07-15-build-memory-oom.md`
> **前置实验**：`experiments/oom_build_2026_07_14/`（peak profile 判据 C1-C4 pass）
> **母方法学**：`docs/design/db-memory-optimization-lessons.md`

## 0. TL;DR

按设计文档 §8 的分 PR 顺序，本实验先验证 **PR-1 方案 B**（pair npmi 重算 SQL 化）
的三个硬判据（C2/C5/C6），通过后再进主干。

**范围**（本实验只做方案 B，不做方案 A / spill 配置）：

- 替换 `_finalize_pairs_from_temp` §4 的 Python 全量 npmi 循环
- 保持所有其它行为不变（Gate 1 glued、Gate 2 min_pair_count、Gate 3 min_npmi、UPSERT-add、on_event 契约）
- 加 `ln()` 能力探测；无 math functions 环境走分批游标兜底

## 1. 判据（Acceptance Criteria）

**硬判据**（任一失败 = PR-1 否决，回炉重做）：

| C | 内容 | 目标 |
|---|---|---|
| C2 | `pairs_scan_end` phase Δ RSS（M 22MB 档） | 从 ~800 MB → **< 100 MB** |
| C5-a | 每个 surviving pair 的 npmi 值 vs baseline | `abs(a-b) < 1e-9`（浮点顺序容忍） |
| C5-b | surviving pair 集合（by kw_a_id, kw_b_id） | **严等**（Jaccard = 1.0） |
| C5-c | Gate 1 glued 判定 | **int 严等**（不允许浮点化） |
| C6 | wall-clock（M 档整个 build） | **≤ baseline × 1.5** |

**软判据**（记录、不阻断）：

- pairs_scan_end 段 tracemalloc Python heap peak（S 档 opt-in）：观察 Python 侧回落程度
- ln() 能力探测的准确性：手动逼一次 fallback 分支跑通

## 2. 实验方法

### 2.1 语料

- **S**：三体（2.6 MB）—— 快速迭代 + tracemalloc 归因
- **M**：凡人修仙传（22 MB）—— C2 / C6 主证据

（沿用 `oom_build_2026_07_14` 的语料定义，不合成 L/XL——本实验只证 PR-1
方向对；scaling 由主干落地后独立测。）

### 2.2 变体

| Variant | 说明 |
|---|---|
| `baseline` | main 主干代码（未改动） |
| `method_B_sql` | 方案 B 走 SQL UPDATE/DELETE 分支（要求 SQLite ≥ 3.35 + math functions） |
| `method_B_batch` | 方案 B 走分批游标兜底分支（强制走 fallback，模拟 `ln()` 不可用） |

### 2.3 等价性验证流程

对每个 (corpus, variant) 组合：

1. 用**独立临时 DB**（`tempfile`）跑一次完整 `build_graph_index`
   (rebuild + refresh_ngram=replace + ngram_iters=1)
2. 只把 `keyword_pairs` 表（`scope_id`, `kw_a_id`, `kw_b_id`, `source`,
   `cooccur_count`, `npmi`）导出为 canonical 排序的 dump
3. 与 `baseline` 的 dump 逐行对比：
   - pair 集合（三元组 key）**严等** → C5-b
   - `cooccur_count` **int 严等** → 顺便验证 Gate 2/Gate 1
   - `npmi` **`abs(a-b) < 1e-9`** → C5-a
   - **列出所有 baseline drop 但 variant 保留的 pair、反之** → C5-c

任一失败 → 打印 diff 前 20 行 → 实验红灯。

### 2.4 内存/wall 测量

复用 `oom_build_2026_07_14/bench.py`：subprocess-hosted、sampler-only、opt-in tracemalloc。
每 (corpus, variant) 跑 3 次取 median；M 档 baseline 已有历史数据（`../oom_build_2026_07_14/data/results/M_baseline_run*.json`），直接对齐。

## 3. 目录布局

```
experiments/oom_build_fix_2026_07_15/
├── SPEC.md                    # 本文件
├── README.md                  # 一句话结论 + 复现（pass 后填）
├── FINDINGS.md                # C2/C5/C6 判据裁决 + 遗留问题
├── patch/                     # 方案 B 的补丁分支（不进主干，实验局部）
│   └── finalize_pairs_v2.py   # 替换版 _finalize_pairs_from_temp
├── equivalence.py             # 两个 DB 的 canonical dump 比对
├── bench.py                   # symlink 或简 wrapper → oom_build_2026_07_14/bench.py
├── run_all.py                 # 矩阵驱动
└── data/
    ├── dbs/                   # 保留每 run 的 keyword_pairs dump（.jsonl，方便 diff）
    └── results/               # bench json + equivalence report
```

## 4. 实现约束（PR-1 落主干前必满足）

- 探测 `ln()` 用 `try: conn.execute("SELECT ln(1.0)")`，捕 `sqlite3.OperationalError`
- 探测**只跑一次**、结果缓存在 build 进程内（不重复 probe）
- fallback 分批 batch 大小 = 50000（可通过 `GRGA_BUILD_NPMI_BATCH` env 覆盖，
  写入 `docs/env-vars.md`）
- 事件契约不变：`pairs_scan_start` / `pairs_advance(current, total)` / `pairs_scan_end(surviving)` 全部保留
- Gate 1 glued (`df_a == df_b == co`) 在 SQL 路径也用 int 比较（`co = df_a AND co = df_b`）
- fallback 路径的 batch UPDATE 用 `executemany`，Python 侧只保留当前 batch 的 tuple list

## 5. 决策后动作

**Pass (C2/C5/C6 全绿)**：
- 写 PR：把 `patch/finalize_pairs_v2.py` 的实现替换主干 `build.py::_finalize_pairs_from_temp`
- 写 CHANGELOG 一行：`fix(build): drop 800MB Python peak in pair npmi recompute (M-corpus)`
- 更新 `docs/env-vars.md` 加 `GRGA_BUILD_NPMI_BATCH`
- FINDINGS 归档 C5 的 bit-exact 数据

**Fail (任一 C 红)**：
- FINDINGS 记录根因（浮点顺序 / Gate 判定漂移 / etc）
- 保留实验作参考实现；PR-1 推迟或方案调整（可能退回 fallback-only 无 SQL 分支）

**Partial (C2 pass 但 C6 边缘)**：
- 视 wall 具体数值决定是否可接受（1.5× 硬阈）
- 若 SQL 分支慢、fallback 快 → 只落 fallback、砍掉 SQL 分支简化实现
