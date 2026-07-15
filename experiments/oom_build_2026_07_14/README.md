# oom_build_2026_07_14

`index build` 大语料 OOM 的**内存 profile 实验**。契约 → `SPEC.md`，
决策冻结 → `QUESTIONS.md`。**本轮只测不改。**

## 一句话结论

> **✅ 完整通过 C1-C4**。三体 2.6MB → peak **570 MB（220×）**；凡人修仙传 22MB
> → peak **2474 MB（114×）**；两点线性外推 R²=1.000，斜率 **~100 MB peak/MB
> 语料** ⇒ 100 MB 语料需 10 GB、500 MB 需 50 GB，直接解释 OOM。
>
> **两个独立峰**（sampler envelope 揭示，`ru_maxrss` 单峰读数会掩盖）：
> `ngram_iter_1` **+2368 MB** on M（detector 内 `ngram_count/left_ctx/right_ctx`
> 三 dict 从零填至完整规模）+ `pairs_scan_end` **+800 MB** on M
> （`_finalize_pairs_from_temp` §4 npmi 重算：`fetchall()` 全量 pair +
> `updates/drops` 双 list）。**两处必须并改**——只解决一处最多减半。
>
> 详见 `FINDINGS.md`；决策后动作与下一实验规划在 §"下一步动作"。

## 背景

见 `SPEC.md §1`。触发问题：用户报告大语料 build 易 OOM。历史 v9.1
refactor 解决了 pair **计数** 的 OOM，但 (a) auto n-gram 检测这一段
和 (b) pair **npmi 重算** 这一段没做同等的 SQLite 化改造。

## 关键工程发现

| # | 发现 | 数据 |
|---|---|---|
| F0 | tracemalloc 4.2× wall + +180% RSS 开销 | 主矩阵关 tracemalloc；只 S 档 opt-in |
| F1 | peak_rss ≈ 100 × corpus_mb（R²=1.000） | 100 MB → 10 GB、500 MB → 50 GB |
| F2 | 两个独立峰（sampler envelope，`ru_maxrss` 单峰读数掩盖） | 中间 RSS 只回落 <900 MB |
| F3 | Python heap 占总 RSS 50-70%（ngram 阶段） | 剩余是 jieba / SQLite C 侧 |
| F4 | ngram_iter_1 独占 M 峰值 96%（+2368 MB） | detector 三 dict 从零填满 |
| F5 | pairs_scan_end +800 MB 定位到 `pair_rows = ...fetchall()` | v9.1 未覆盖的 npmi 重算 |

判据裁决与详细论证见 `FINDINGS.md`。

## 文件

| 文件 | 内容 |
|---|---|
| `SPEC.md` | 实验规范：假设热点、测量三路、判据 C1-C5、决策后动作 |
| `QUESTIONS.md` | Q1-Q12 决策冻结 |
| `bench.py` | 单次 run 的 subprocess-hosted profiler（sampler + ru_maxrss + opt-in tracemalloc） |
| `run_all.py` | (corpus × config) 矩阵驱动，隔离子进程 + OOM 不中断 |
| `analyze.py` | 生成 `report_memory.md`：分 phase Δ、外推回归、测量一致性 |
| `data/corpora/` | L/XL 合成语料（按需生成） |
| `data/results/` | 每次 run 的 JSON |

## 复现

```bash
PY=/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3

# 单次快速验证（三体，iters=1，sampler-only ~50s）
$PY bench.py --corpus "/media/lqw/DATA/sync/temp/三体.txt" \
    --corpus-label S --out data/results/S_iters1_run0.json --ngram-iters 1

# 全矩阵（默认 S,M × baseline,iters1,ngram_n3，各 3 次）
$PY run_all.py

# 只跑小档、单次
BENCH_ONLY=S BENCH_RUNS=1 BENCH_CONFIGS=baseline $PY run_all.py

# 加 M（22MB，单次 baseline 预计 3-8 分钟）
BENCH_ONLY=M BENCH_RUNS=1 BENCH_CONFIGS=baseline $PY run_all.py

# S 档带 tracemalloc（拿 Python/C 占比，慢 4x）
BENCH_ONLY=S BENCH_TRACEMALLOC=S BENCH_RUNS=1 $PY run_all.py

# 生成报告
$PY analyze.py
```

**环境变量**（见 `run_all.py` 头部）：`BENCH_RUNS` / `BENCH_ONLY` /
`BENCH_CONFIGS` / `BENCH_TRACEMALLOC` / `BENCH_TIMEOUT_SEC` /
`BENCH_SAMPLER_INTERVAL`。

## 注意

- 每次 run 用 `tempfile` 新建临时 DB，run 结束删除，**不污染生产库**
  `~/.config/grga/deep_keywords.db`。
- 只测 `.txt`（Q12），隔离 rga adapter I/O 变量。
- 本实验**不改** `src/grga/graph/build.py` 生产代码（SPEC §0 边界）；
  profile 结论出来后另开 `oom_build_fix_*` 实验验证具体重构。
