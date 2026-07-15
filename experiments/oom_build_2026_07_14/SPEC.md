# SPEC — oom_build_2026_07_14

> **状态**：draft，tracer bullet 阶段。
> **目标读者**：未来落地本实验及后续优化 PR 的开发者。
> **前置讨论**：2026-07-14 与用户关于 `index build` OOM 的三段对话；
> 结论指向 `docs/design/db-memory-optimization-lessons.md` §"一句话结论"——
> **"能让 SQLite 用 SQL 完成的聚合，永远不要在 Python 端 Counter/set 里做"**——
> 未在 auto n-gram 检测这一段兑现。
> **决策路径**：先 profile / 再动手。本轮**只测不改**。

## 0. TL;DR

**要回答的问题**（先测后改）：

1. `build_graph_index` 在大语料上 peak RSS 到底停在哪一段？
   `docs[*].chunks.text` / Phase A `line_token_lists` / Phase B `chunk_tokens` /
   `detect_ngram_phrases` 内部 `ngram_count + left_ctx + right_ctx` /
   `_finalize_pairs_from_temp` 已迁 SQLite 之后的残留？
2. 各段的 peak 与语料**哪一个维度**相关（total_chars、n_chunks、vocab、n-gram 窗口数）？
   斜率是线性、超线性、还是常数？
3. **相对贡献**：如果只优化 A 段而不动 B 段，能省下多少 peak RSS？
   即 v9.1 pair 优化那种 "one big drop" 是否可能，还是需要多段并行改造？
4. 目前架构下**理论上界**是多少？一台 16GB 内存机器上，从 22MB 中文小说
   到 200MB / 500MB 语料的 peak RSS 外推曲线长什么样？

**判据方向**（本实验的 pass 条件是**产出可信数据**，不是"跑得快"）：

- **观测正交性**：每段 peak 能独立测出，段与段互不遮蔽
- **可复现**：三次 run 的 peak RSS 差 <5%
- **归因可信**：至少两种独立测量（tracemalloc + `resource.getrusage` RSS
  高水位 + 采样线程 psutil RSS 时间序列）互相印证

**不在本实验范围内**（明确划边界）：

- ❌ 修改 `build.py` 的任何生产代码路径（本轮只加 instrumentation，不改算法）
- ❌ 提议或验证 TEMP TABLE 替换方案的**实现**（等 profile 数据出来再规划）
- ❌ 断点续跑（scope creep；等 profile 显示"确实需要"再单独开实验）
- ❌ 优化 pair 聚合（`_finalize_pairs_from_temp` 已是 v9.1 正例，不动）

---

## 1. 背景

### 1.1 触发问题

用户报告："处理大语料时非常容易 OOM"。凡人修仙传（22MB / 145k 行）在
默认参数下能跑完但接近水位（v9.1 refactor 时代的 benchmark 语料），
更大语料时观察到崩溃。历史 refactor（v8→v9.1，见 `docs/design/decisions.md`
2026-07-13 条目）解决了 pair 聚合的 OOM，但 auto n-gram 检测这一段
**没有做同等改造**，仍然是"全语料 tokens 塞 Python 列表 + Counter 聚合"。

### 1.2 假设的热点排序（待验证）

基于代码阅读（`build.py::detect_ngram_phrases` + Phase A / Phase B），
**预估**的 peak RSS 贡献从大到小：

| 排位 | 数据结构 | 位置 | 上界 |
|---|---|---|---|
| 假设 A | `chunk_tokens: Dict[tuple, List[str]]` | Phase B 之后到 Pass 2 结束 | O(总 token 数) |
| 假设 B | Phase A 每轮的 `line_token_lists` | `build_graph_index` line 1693 | O(总 token 数) |
| 假设 C | `ngram_count + left_ctx + right_ctx` | `detect_ngram_phrases` 内部 | O(vocab × max_n) |
| 假设 D | `docs[*].chunks[*].text` | scan 到 Pass 2 都占着 | O(语料总字符数) |
| 假设 E | pair 聚合残留（stage/temp table） | `_finalize_pairs_from_temp` | 已经受控（SQLite 管） |

**这些是待检验的假设，不是结论**。实验目的就是给这个排序打分数、
测斜率、找出未被 code review 预见的第 6 个热点。

### 1.3 与既有决策的关系

- `docs/design/db-memory-optimization-lessons.md`：本实验产出的 profile
  数据要能引用回其中的"何时用 TEMP TABLE"判断标准
  （raw 事件数 × 单事件字节 > 500 MB 且超线性增长）。
- AGENTS.md §"信号暴露原则"：本实验只在 profile 层加 hook，
  **不改 build 的 `--json` schema、不改 `on_event` 契约**——
  所有 instrumentation 走本实验私有的 event 订阅回调。
- `experiments/related_perf_2026_07_12` 的 SPEC 模式：本 SPEC 沿用它的
  "单变量隔离 + 累积叠加 + 判据 C1-Cn"结构。

---

## 2. 语料与配置矩阵

### 2.1 语料档

| 档 | 文件 | 大小 | 行数 | 定位 |
|---|---|---|---|---|
| S | `三体.txt` | 2.6 MB | 12 k | sanity check + 冷启动最小复现 |
| M | `《凡人修仙传》.txt` | 22 MB | 145 k | 已知临界语料（v9.1 就是拿它调过来的） |
| L | 合并 S+M（22 拷贝也可）| ~24-44 MB | ~157-300k | 外推曲线锚点 |
| XL | 合并再合成到 ~100MB | ~100 MB | 逼近实际 OOM 边界 |

**语料来源**：`/media/lqw/DATA/sync/temp/`，已在本机。
XL 用**内容拷贝**合成（改文件名保持独立文档结构），避免走网络。

### 2.2 build 参数（frozen）

```python
BASELINE = dict(
    scope="oom_bench",
    chunk_mode="chars",              # chars 是主流用法
    chunk_max_chars=600,
    chunk_min_chars=300,
    mode="rebuild",                  # 干净场景，隔离增量的影响
    refresh_ngram="replace",         # 触发完整 Phase A
    ngram_iters=2,                   # 默认值；实验里也测 iters=1 的对照
    max_ngram_n=4,                   # 默认
    min_pair_count=None,             # 走 adaptive
    min_npmi=BUILD_MIN_NPMI,         # 默认
)
```

**变体维度**（本实验**只影响观测**、不影响算法）：

| 维度 | 值 | 目的 |
|---|---|---|
| corpus | S / M / L / XL | 主外推变量 |
| ngram_iters | 1 / 2 | 判断"多轮"是否加剧内存（预期不加，因为每轮 `del line_token_lists`） |
| refresh_ngram | replace / never | 隔离"检测本身"的贡献（never 跳过 Phase A） |
| max_ngram_n | 3 / 4 / 5 | 检验假设 C 的斜率（vocab × n 曲线） |

每格独立跑 3 次取 median 与 p50–p95 区间。

---

## 3. 测量方法

### 3.1 三路独立测量

同时启用三种测量，交叉印证防单一噪声：

1. **`tracemalloc.get_traced_memory()`**：Python 对象堆的 peak。
   在每个 phase 边界 (via `on_event` 回调) 记录 `current / peak` 快照，
   phase 结束时 `tracemalloc.reset_peak()`。**只算 Python 分配**，
   不含 C 扩展（jieba / sqlite3 buffer / mmap）。
2. **`resource.getrusage(RUSAGE_SELF).ru_maxrss`**：进程生命周期 RSS 高水位
   （Linux 单位是 KB）。**含所有分配**（Python + C ext + malloc arena）。
   相邻两次采样的差即"这段代码 RSS 涨了多少"。
3. **采样线程 psutil**：独立线程以 100ms 采样 `Process.memory_info().rss`
   + `memory_info().vms`，与主线程共享的 `phase_id` 标签连接。
   得到完整时间序列，用于绘 peak curve、检出 phase 内部尖峰。

**互相印证的期望**：`ru_maxrss ≈ 采样线程 peak ≥ tracemalloc.peak`。
差距大说明 C 侧（jieba / SQLite 内部）占主导 → 说明单靠改 Python 数据
结构效果有限。

### 3.2 phase 边界 hooks

`build_graph_index` 已经有 `on_event(payload)` 回调，phase 名如
`scan_start / scan_end / ngram_iter_start / ngram_iter_end /
ngram_finalize_start / ngram_finalize_end / pass2_start / pass2_end /
finalize_pairs_start / finalize_pairs_end`。**本实验只订阅、不新增**。

每次收到 event：
- 记录 `(t_wall, phase, tracemalloc.get_traced_memory(), ru_maxrss)`
- 调 `tracemalloc.reset_peak()`（下一段的 peak 独立测）

### 3.3 数据结构大小采样

只在**几个已知悬疑变量**上做一次性 sizeof 采样，不做全递归深测（太慢）。
用 `pympler.asizeof.asizeof` 或手写 `_estimate_size` 在每个 phase 结束
时探针，输出到 event log：

| 变量 | 探针位置 | 单位 |
|---|---|---|
| `docs[*].chunks` 的 text 字节总和 | scan_end | MB |
| `line_token_lists` 元素数 + 平均长度 | ngram_iter_end | count |
| `chunk_tokens` 元素数 + 平均长度 | ngram_finalize_end | count |
| `ngram_count / left_ctx / right_ctx` | detect_ngram_phrases 内部（需临时加 hook） | count |
| `_build_events` TEMP TABLE 行数 | `_finalize_pairs_from_temp` 里 | rows |

**pympler 不必依赖**：sys.getsizeof + 手算 `len(str) * ~50 B` 估算下界；
对 dict/list 用 `getsizeof(obj) + sum(getsizeof(e) for e in obj)` 单层。
估算下界永远小于真实值，但足以判断量级。

### 3.4 隔离进程

每个 (corpus, config) 组合跑**独立 subprocess**（`multiprocessing.Process`
或 fork exec）。理由：
- 消除跨 run 的堆状态污染（jieba 单例、SQLite connection cache）
- 让 `ru_maxrss` 单调有效（跨 run 复用主进程会看到最大值堆积）
- OOM 时不炸整个 benchmark harness

进程间通信：subprocess 把 event log 写 JSON 文件，父进程 collate。

### 3.5 rebuild vs 冷启动

每次跑之前**删除对应 scope 的 DB rows**（`grga scope rm --purge-knowledge`），
保证 `mode="rebuild"` 是真正的冷启动，不复用上次 auto_ngram_dict。

DB 用**临时文件**：`tempfile.NamedTemporaryFile(suffix=".db")`，
run 结束就删除，不污染 `~/.config/grga/deep_keywords.db`。

---

## 4. 输出与判据

### 4.1 结果 schema

`data/results/<corpus>_<config>_run<i>.json`：

```json
{
  "corpus": "M_fanren",
  "corpus_bytes": 22691427,
  "corpus_lines": 145060,
  "corpus_chars": 22600000,
  "config": {
    "ngram_iters": 2,
    "refresh_ngram": "replace",
    "max_ngram_n": 4,
    "chunk_mode": "chars"
  },
  "run_id": 0,
  "wall_ms": 187342.0,
  "phases": [
    {
      "phase": "scan_end",
      "t_wall_ms": 12034.5,
      "tracemalloc_current_mb": 234.1,
      "tracemalloc_peak_mb": 245.8,
      "ru_maxrss_mb": 380.2,
      "psutil_rss_at_hook_mb": 375.5,
      "probes": {
        "docs_total_chunks": 51230,
        "docs_text_bytes_mb": 22.4
      }
    },
    { "phase": "ngram_iter_end", "iter": 1, ... },
    { "phase": "ngram_finalize_end", ... },
    { "phase": "pass2_end", ... },
    { "phase": "finalize_pairs_end", ... }
  ],
  "sampler_timeline": [
    { "t_ms": 100, "rss_mb": 45.2, "vms_mb": 132.8, "phase_at": "scan_start" },
    { "t_ms": 200, "rss_mb": 68.4, "vms_mb": 155.1, "phase_at": "scan_start" },
    ...
  ],
  "peak_rss_mb": 1234.5,
  "peak_rss_phase": "ngram_iter_end@iter=2",
  "notes": []
}
```

### 4.2 判据

**C1 — 观测正交性**：三种测量方法（tracemalloc / ru_maxrss / sampler）
在同一 phase 上的排序一致（相关系数 Spearman ≥ 0.9）。若某测量法给出
和另两者截然不同的 phase 排序 → 该测量法要么错要么在测别的东西，
判据不算过。

**C2 — 可复现**：同一 (corpus, config) 三次 run 的 peak RSS 相对方差
< 5%。若不达标，说明测量本身噪声大或者 GC 时机不稳，需要固定
`PYTHONHASHSEED` + `gc.freeze()` 后重跑。

**C3 — 归因显著性**：至少一个 phase 独占 peak RSS 的 **≥ 40%**。
若五个 phase 各占 20% 左右均匀分布 → 说明没有"一处优化即可"的杠杆点，
后续 refactor 要一次动多处，实验结论方向和"pair 优化那样单点大改"
不同。

**C4 — 外推可信**：`peak_rss_mb` 对 `corpus_chars` 的回归 R² ≥ 0.95，
且斜率显著（p < 0.05）。合格后可以外推到 100MB / 500MB / 1GB 语料。

**C5 — sizeof 探针一致**：`docs_text_bytes_mb / line_token_avg_len /
chunk_tokens_size` 探针值 × 单元素字节估算，与对应 phase 的
tracemalloc peak 差在 2× 以内。差距大说明还有未探针到的隐藏结构。

### 4.3 决策后动作

**Pass (C1–C4 全过)**：
- 起草主干优化设计文档 `docs/design/plan-2026-07-XX-build-memory.md`
- 按 profile 结论决定优化优先级（改 A 段还是 B 段先落）
- **不直接进主干代码**——先写实验 `experiments/oom_build_fix_202X_XX/`
  验证具体重构方案（TEMP TABLE for chunk_tokens 之类）

**Partial pass**（C3 显示分布均匀）：
- FINDINGS 记录"没有单点杠杆"结论
- 优化路径改为"先做易改的"（比如释放 `docs[*].chunks.text`），
  别追求一次大重构

**Fail**（C1/C2 崩了）：
- FINDINGS 记录测量方法问题
- 换 memray（需要额外安装）或 py-spy 重试；本实验归档

---

## 5. 目录布局

```
experiments/oom_build_2026_07_14/
├── SPEC.md                    # 本文件
├── README.md                  # 一句话结论 + 使用说明（跑完后填）
├── FINDINGS.md                # 关键发现（跑完后填）
├── QUESTIONS.md               # 决策冻结（跑之前先冻结）
├── bench.py                   # subprocess-hosted 单次 run
├── run_all.py                 # 矩阵驱动 + 结果收集
├── analyze.py                 # 表格 + 回归 + 判据裁决
├── data/
│   ├── corpora/               # 合成的 L / XL 语料（软链接或临时拷贝）
│   └── results/               # 每次 run 的 JSON
└── report_memory.md           # 详细内存对比（分 phase / 分档）
```

---

## 6. 一句话给未来读者

**这个实验的产物不是"跑得更快的 build"，而是"下次要不要动手改 build 的
证据"**。判据挂在测量正交性、可复现性、归因显著性上；即使结论是
"不用改"，也是有价值的结论——它意味着 v9.1 refactor 之后剩下的
peak 是 C 扩展（jieba / SQLite）主导的、Python 层再改无益。
