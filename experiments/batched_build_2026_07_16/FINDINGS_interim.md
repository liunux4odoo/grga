# FINDINGS (interim) — batched_build_2026_07_16

**状态**: **早期实验数据引出重要问题,需要与用户对齐后再继续**。已 revert
PR-8b (`9251854`),baseline path 干净。

## 已验证

### ✅ 单批 = bit-exact

| Corpus | batch (chunks) | n_batches | pair_recall | phrase_recall | wall vs baseline |
|---|---|---|---|---|---|
| S (1 file, 1360 chunks) | 200000 | 1 | 1.00 | 1.00 | **1.27× 快** (46s vs 59s) |
| S12 (12 files, 1365 chunks) | 5000 | 1 | 1.00 | 1.00 | 相同 (46s) |

单批时 batched = baseline,证明 pipeline 逻辑正确(re-tokenize 修复 phrase
dict 未生效 bug 后 100% 命中)。numpy 单批比 baseline 大 SQL 稍快。

### ⚠️ 多批时 recall 显著下降 (符合 SPEC 预期方向,幅度超预期)

S12(1365 chunks 切成多批)recall vs baseline 曲线:

| batch_chunks | n_batches | 平均每批 | wall | peak | pair_recall | phrase_recall |
|---|---|---|---|---|---|---|
| 100 | 12 | ~114 | 81s | 343MB | **0.19** | 0.36 |
| 300 | 6 | ~228 | 50s | 341MB | 0.23 | 0.46 |
| 1000 | 2 | ~683 | 46s | **664MB** | 0.54 | 0.77 |
| 5000 (=whole) | 1 | 1365 | 46s | 940MB | **1.00** | 1.00 |

**要点**: recall 是**每批绝对 chunks 数**的函数,不是批数的函数。114
chunks 批不够形成有意义的统计,大量 phrase / pair 未过 per-batch
min_count 门槛而丢失。

## ⛔ 意外问题: 多批 M10 peak 高于 baseline

**M10** (10 files, 12260 chars-mode chunks, 22MB total) baseline:
- wall 600s, peak **729 MB**, pairs 296610, phrases 33976

Batched M10 batch=5000 (3 batches ~4000 chunks each):
- wall 425s (0.71× 更快), peak **2784 MB** (3.8× 更高), pairs 157078 (recall 0.53)
- 中间 keyword_pairs 一度达 **4.2M 行** (final 157k)

关键异常: batched 的 keyword_pairs 中间态 4.2M 行,而 baseline HAVING
之后是 ~700k。**batched 在 _step4 之前就有 6× 更多行**。

## 未确定但可能的解释

Per-batch 过滤 `cnts >= resolve_min_pair_count(batch_chunks)` 与全局
`resolve_min_pair_count(n_chunks_total)` 在 M10 都是 4,阈值应相同。
数学上 batched 中间行数应 ≤ baseline (因为 lossy)。为什么反而 6× 更多?

**候选假设**:
1. `chunk_tokens.get()` 用 re-tokenize 后的 tokens,而 baseline Phase B
   实际用不同 tokens (某些 phrase 边界差异)?
2. 3b 过滤(stopword 包含)在 per-batch stopwords 下比 global stopwords 更宽松,
   放行更多 kw 进入 pair,pair 空间膨胀?
3. numpy 编码 `(kw_a << 40) | kw_b` 或 decode 有 bug?
4. UPSERT-add 语义某处双计数?

必须先 debug 这个才能判断 Variant B 是否根本可行。

## 1-batch M10 OOM (可能是并发导致)

尝试单批 M10 (batch=15000, 全部 12260 chunks 一批) 时进程被 SIGKILL,
只跑到 batches_planned emit。可能原因: 之前同时启动 3 个 batched 进程
挤占内存导致 OOM killer。**未复现**,需要单独跑再确认。

## 建议下一步 (需与用户对齐)

在继续投入前,先答三个问题:

### Q1. debug 4.2M 中间行膨胀

这是**卡关问题**。若批多得越多行数越涨,peak 就无法有界化,Variant B
的核心承诺 (RAM-bounded) 破产。需要:
- 定位 4.2M 中哪些是 baseline 也有的 vs batched 新增的
- 检查 3b 过滤 / numpy 编码是否有 bug

### Q2. 重新评估"peak 有界"是否真的成立

原假设: 每 batch RAM 有界 → total peak ≈ 单批 peak + SQLite cache。
观察: 3-batch M10 peak 2.7GB,远超单批预期(每批数百 MB)。要么单批
本身内存需求高于估计,要么 keyword_pairs 累积推高 SQLite 页缓存 spill。
需要 per-batch RSS 采样确认 peak 是**哪个时刻**发生的。

### Q3. 实验语料是否够大能体现 batched 优势

M10 = 22MB / 12260 chunks / baseline peak 729MB (远低于 4GB 限)。这个
规模 baseline 单进程本来就能吃下,batched 分批**没有本质收益**,反而
多付出批边界开销。要证明 batched 的价值,需要 L (250MB / ~140k chunks
/ baseline peak 应 >2GB) 或更大。但 L 生产 baseline 要 1-2 小时。

## 提议

- **A**: 我先花时间 debug 4.2M 中间行膨胀根因(拆解一批看 kw / pair
  分布,与 baseline 逐 pair 对比),再决定继续
- **B**: 承认多批设计有未预料的复杂度,回到讨论层面重新评估
- **C**: 先跑 L baseline(~1h)后台,期间 debug,同时到位

我倾向 A + C 并行:debug 中间行膨胀是必须的(不解决就不能继续 Variant B),
L baseline 后台跑给最终验证用。

