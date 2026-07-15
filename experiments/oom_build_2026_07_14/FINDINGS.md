# FINDINGS — oom_build_2026_07_14

**状态**：第一轮 profile 完成（S/M × baseline × 3 次 + S tracemalloc × 1 次）。
判据 C1-C4 全部 **strong pass**；C5 未跑（需要单独探针）。

## TL;DR

**用户直觉正确、猜的位置正确、猜的严重程度不足**。三体（2.6MB）
peak RSS = **570MB（220×）**，凡人修仙传（22MB）peak = **2474MB
（114×）**，两点线性外推 R² = 1.000，斜率 **~100 MB peak / MB 语料**。
按此推算 **100MB 语料 ≈ 10GB peak、500MB 语料 ≈ 50GB peak**——足以
在任何常规机器上 OOM。

**决定性发现**：`ru_maxrss` 只报单峰，实际是**两个独立峰**——
`ngram_iter_1`（**+2368MB** on M）和 `pairs_scan_end`（**+800MB** on M
sampler envelope），中间 RSS 只回落 900MB。**优化必须两处并进**，
只改一处最多减半 peak，仍不够撑 100MB+ 语料。

## 判据裁决

| C | 名称 | 结果 | 证据 |
|---|---|---|---|
| C1 | 观测正交性 | ✅ **strong pass** | sampler / ru_maxrss ratio = 0.998-1.000 on all M runs, 1.000 on S；同 phase 排序一致 |
| C2 | 可复现 | ✅ **strong pass** | M 三次 peak = 2473.7/2473.8/2473.4 MB（方差 <0.1%）；S 三次 = 570.4/570.5/570.8 MB |
| C3 | 归因显著性 | ✅ **strong pass**（比预期更集中） | ngram_iter_1 独占 M 峰值 **96%**（2368/2474）；S 上 ngram+pair 合计 **62%**（345/574） |
| C4 | 外推可信 | ✅ **strong pass** | R² = 1.000（两点必然，但斜率与两个测量法均一致） |
| C5 | sizeof 探针一致 | ⏸ 未跑 | 需要单独 monkey-patch 拿 `chunk_tokens / ngram_count` 的大小 |

## F1 — Peak RSS ≈ 100× corpus_mb（决定性）

```
| corpus_mb | peak_rss_mb | ratio |
|---:|---:|---:|
| 2.6  | 570.6  | 220.3× |
| 21.6 | 2473.7 | 114.3× |
```

线性拟合：`peak_rss_mb = 99.9 × corpus_mb + 311.9`（R² = 1.000）。

**220× 到 114× 的下滑**：小语料上 `_STOPWORDS / jieba baseline / Python
interpreter` 常数占比大；大语料这部分被摊薄，斜率收敛到 100×/MB。

外推（假设线性外推区间可延伸）：
- 50 MB 语料 → **5.3 GB** peak
- 100 MB 语料 → **10.3 GB** peak
- 500 MB 语料 → **50.3 GB** peak

**推论**：任何"大语料能不能 build"的问题，答案在这条曲线上；
不改代码就是把上限硬钉在 100× 内存/语料比。

## F2 — 高水位掩盖了 pair 阶段的真实贡献

`ru_maxrss` 只告诉你**总最高点**，而 M 档 sampler envelope 显示实际是
**两个独立峰**：

```
phase window                RSS envelope (min..max MB)     Δ per window
scan_start                  81..105                        +24
ngram_iter_1                90..2474                       +2384   ← 峰 1
[iter1 结束]                → 1576 (释放 898 MB)
ngram_iter_2                1576..2068                     +492
ngram_finalize              1639..1683                     +44
[detect 后]                 → 1639 (释放 429 MB)
materialise + pairs_start   1639..1732                     +93
pairs_scan (npmi 重算)      1723..2456                     +733    ← 峰 2
```

**关键**：两个峰高度接近，中间**只释放不到一半 RSS**——`chunk_tokens` /
`docs[*].chunks` / detector 中间态没有及时清理，跨了三个 phase 边界。

**优化建议的重要修正**（对 tracer 阶段结论的更新）：

- 只解决 F2（pair 阶段）→ 收 ~800 MB peak，改进 <30%，**没用**
- 只解决 F1（ngram 阶段）→ 收 ~2400 MB peak，但 pair 阶段 800 MB 仍在，
  改进 ~50%，**还是不够 100MB+ 语料**
- **两处并改** → 才能让 peak 从 100×/MB 掉到 ~10×/MB 量级

## F3 — Python-heap vs 总 RSS：C 扩展占 50-70%

S 档 tracemalloc（Python 分配跟踪）vs `ru_maxrss`：

```
phase                    tm_peak MB  ru_maxrss MB  py_share
ngram_iter_1 end            336         694          48%
ngram_iter_2 end            359         746          48%
pairs_scan_end              496        1547          32%
pairs_done                  496        1606          31%
```

**推论**：ngram 阶段 Python heap 与 C ext 各占一半——jieba tokenizer +
combined regex compile + jieba prefix dict 的 C 侧数据结构占相当份额。
pair 阶段 Python heap 占比降到 32%——SQLite temp store 页 + statement
cache 的 C 侧贡献更大。

**这不否定"改 Python 数据结构"的价值**：Python 那一半是我们能动的
杠杆；C 侧 SQLite 有 `PRAGMA cache_size` / `temp_store_directory`
可以调（AGENTS.md 铁律 1，把负担交给 SQLite 的可控 spill 机制）。

**但要注意**：tracemalloc 本身也会大量分配跟踪表——S 档打开 tracemalloc
后 ru_maxrss 从 570→1606（+180%）。所以 F3 表里 py_share 的**绝对值
不可信**，但 phase 之间的**比例趋势**仍有意义。

## F4 — `ngram_iter_1` 是最大单点（M 档 +2368 MB）

Iter 1 的贡献远大于 iter 2（M 上 +2384 vs +492 MB）：
- iter 1 从空字典开始，`ngram_count / left_ctx / right_ctx` 三个
  defaultdict 从零填到 vocab × max_n × 邻居分布的完整规模
- iter 2 auto_dict 已有 iter 1 detected 的 phrases，jieba 分词更粗，
  tokens 更少，n-gram 窗口更少

**优化含义**：iter 1 是必然 O(vocab_words × max_n) 的探测；
即使砍 iter 数（`ngram_iters=1`），iter 1 本身的 +2.4GB 无法回避。
必须动 detector 内部的数据结构（TEMP TABLE + SQL GROUP BY）才有本质改善。

## F5 — `pairs_scan_end` 的 800 MB 来源确认

代码 walk（`src/grga/graph/build.py::_finalize_pairs_from_temp` §4）：

```python
pair_rows = conn.execute(
    "SELECT kw_a_id, kw_b_id, cooccur_count FROM keyword_pairs ..."
).fetchall()          # ← 全表拉进 list：293k 行 × 3 int tuple

updates: List[Tuple[float, int, int, int]] = []
drops: List[Tuple[int, int, int]] = []
for row in pair_rows:
    ...
    updates.append(...)   # ← 再一份
```

M 档 `surviving = 293824`：`pair_rows` + `updates` + `drops` 三重
Python tuple 列表 ~500-800 MB，跟观察到的 +733 MB 峰吻合。

**修复模板**：改成 server-side cursor 遍历 + `executemany` 增量
UPDATE，或者干脆把 npmi 公式表达成 SQL `UPDATE ... FROM (SELECT ...
JOIN keyword_stats ...)` 一次搞定，Python 侧只当 SQL 派遣器。这跟
`db-memory-optimization-lessons.md` §"正确模式"完全对齐。

## F0 — tracemalloc 开销复核（本轮工程发现）

`tracemalloc.start(1)` 在 tokenize/detect 密集 workload 上：

- Wall-clock：51s → 215s（**4.2×**）
- Peak RSS：570 → 1606 MB（**+180%**，tracking table 自己开销）

**决定**：主矩阵默认关 tracemalloc；只在 S 档跑一次拿 Python/C 段间比
（F3）。M/L/XL 档禁开——2GB 峰变 6GB+ 会立刻 OOM。

已固化到 QUESTIONS.md Q2。

## 下一步动作

### 立即（这轮实验结束前）：

- [x] 判据 C1-C4 pass，可以进决策阶段
- [ ] 写 `docs/design/plan-2026-07-XX-build-memory.md` 主干优化设计
      （拟名 `plan-2026-07-15-build-memory-oom.md`）
- [ ] 更新 `docs/navigation.md`：把 `db-memory-optimization-lessons.md`
      从 pair 单例扩到"ngram 检测也走同套方法学"

### 下一个实验（`experiments/oom_build_fix_2026_07_15/`）：

**验证 TEMP TABLE 方案在 detect_ngram_phrases 上的实际收益**。判据：

1. iter 1 peak Python heap 从 ~350MB 降到 ~50MB（M 档）
2. 结果 phrase 集合与主干版本 Jaccard = 1.0（**严等价**——纯数学判据，
   不允许结果差异）
3. wall-clock 不超过主干版本 1.5×（SQL round-trip 可容忍范围内）

**同一实验里也验证** `_finalize_pairs_from_temp` §4 的 SQL 化：

1. pairs_scan_end 段 Python heap 从 ~500MB 降到 ~50MB（S 档）
2. npmi 值 bit-exact（浮点顺序容忍在 `abs(a-b) < 1e-9`）

不 pass 就不进主干。SPEC / QUESTIONS / bench / analyze 沿用本实验模板。

### 不进本实验的（scope 明确排除）：

- ❌ 断点续跑：F1/F2 显示单次 build 内的两个峰是主要问题，不是"跑到
  一半崩掉"；重启不解决内存问题
- ❌ 运行时检测内存 + 自动分批：SQLite 已有 `PRAGMA cache_size` 做同样
  的事；不重造轮子（AGENTS.md 铁律 1）
- ❌ pickle 磁盘缓存：TEMP TABLE 是同一个语义、少一层实现负担
