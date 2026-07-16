# SPEC — batched_build_2026_07_16

> **状态**：drafting，实施前的设计冻结版。
> **前置讨论**：2026-07-15/16 与用户关于"共现对生成瓶颈是计算还是数据读取"
> 的对话；用户挑战成功四点全局统计不成立、明确接受"少许偏移是可以的"、
> 明确暂不做多进程。
> **前置数据**：
> - `oom_build_fix8_2026_07_15` 证实 per-doc 两级比单条 SQL **慢 1.6×**
> - `oom_build_fix9_2026_07_15` 证实 numpy 聚合快 3-4× 但内存超预算
> **前置代码**：`b6db151` (PR-1..3) → `76780fe` (PR-8b, pending revert)。

## 0. TL;DR

用户 300 万 chunks 规模下 `生成共现对` 慢 + peak 逼近极限。设计问题
不是"如何压榨单次全量的常数因子"（已榨到 SQL 极限），而是**"把 build
拆成 RAM-有界的 batch,每 batch 独立完整、结果 merge 累积"**。

**用户已冻结方向为 Variant B (分批派)作为主路径**（QUESTIONS.md §Q5）。
本实验**在不动主干代码的前提下**,用独立脚本原型化 Variant B 的 batched
pipeline,量化三件事:

1. Batch size 与 wall / peak RSS 的关系（找工作点）
2. 与全局 build 的**语义偏移程度**（phrase 集合 Jaccard、pair count delta、
   npmi 相关系数）— 用户接受 recall ≥ 0.80 的偏移
3. Batch 内 numpy 聚合相对 SQLite GROUP BY 的实际加速

产出:默认 batch_size 值 + 是否需要保留旧全局路径作 fallback。

---

## 1. 问题定义

现状（PR-8b 之前 = `c4fc6be` PR-8）:build 单次处理全语料,内存靠 SQLite
`cache_size` + `temp_store=FILE` 外部排序 spill 硬撑。用户 3M chunks 时
peak 逼近 4GB 上限,pair 自连接是分钟-数十分钟级、无预测性进度。

用户观点(2026-07-16 讨论,已认可):

> 对于一个 100M chunks 的语料,一次性学完,和每次学 10M 分 10 次学完,
> 得到的结果应该是差不多的,即便有少许偏移,也是可以接受的。

具体到 grga 的知识资产:

| 统计量 | 全局 vs 分批的语义差异 | 用户立场 |
|---|---|---|
| min_count 阈值 | 固定阈值在跨规模语料下**尺度不一致** | 分批反而**更一致**(每批固定大小) |
| ngram frequency count | 分批各自 count,merge 时 UPSERT-add 累加 | ✅ 已被 keyword_pairs 增量模式采纳 |
| npmi | 用全局累加 count 在末尾重算 → bit-exact | 或每批 detect 时用本批 npmi → **稀疏跨批 phrase 会漏检**,可接受 |
| branch entropy | 同 npmi | 同 |
| stopword-df | 每批 stopword 集合可能不同 | ✅ **正常特性**,反映内容偏向 |

---

## 2. 变体（B-only）

用户 Q5 冻结:只实现 Variant B (分批派)。全局 baseline 作为对照真值,
不再实现 Variant A（bit-exact 那条路已被"用户不需要 bit-exact"的立场
移除,且实施成本 1 天可省）。

### Variant B (final, 2026-07-16 experiment-informed)

Batch 循环做 **phrase detection**, pair 计数依赖 baseline SQL 全局 GROUP BY.
用户 Q6-Q10 冻结决策整合. Q1 阶段的"per-batch numpy 聚合"设计已被数据推翻
(见 QUESTIONS.md 中期 pivot 章节).

```
# 全局: 每 chunk cap N=64 (Q7 排序 B: 多字短语 > 稀有度 > id)
# 全局: 全 batch 共享 _build_chunk_kws TEMP TABLE (PR-1.5 db-temp 隔离)

for batch in file_aligned_greedy_pack(corpus, target_batch_size):
    line_tokens = tokenize(batch)
    ngrams = detect_ngram_phrases(line_tokens, conn)     # 就地检测, dict 累加, 无 numpy
    merge_auto_ngram_dict(ngrams, on_conflict="add_count")  # Q3 count-only add

    line_tokens2 = re_tokenize(line_tokens, updated_dict)   # PR-4 pattern
    stopwords = derive_stopwords(line_tokens2)              # 全局阈值 0.15 (Q7 上半)

    for chunk in batch:
        kws = extract_kws_3b_filtered(chunk_tokens, stopwords)
        # Q7 下半: top-N cap 每 chunk 只留 N=64 个最重要 kw
        top_kws = rank_by_B(kws, phrase_set, global_chunk_df)[:64]
        for kw in top_kws:
            insert_triple(_build_chunk_kws, doc_id, chunk_id, kw_id)  # Q9 核心

    upsert_keyword_stats(...)                               # 全局累加
    emit("batch_done", i, total_batches)

# 全部 batch 走完后, 复用 baseline 原代码 (未改一字)
# Q9: SQL 外部排序保证内存有界; Q1: 所有 pair-instance 全局参与聚合
_finalize_pairs_from_temp(conn, scope_id, ...):    # PR-1 代码原样
    # step 1: CREATE INDEX on _build_chunk_kws (doc_id, chunk_id)
    # step 2: INSERT INTO keyword_pairs SELECT ... FROM _build_chunk_kws a JOIN b
    #         GROUP BY a.kw_id, b.kw_id HAVING COUNT(*) >= min_pair_count
    # step 3: DELETE below global floor
    # step 4: _step4_sql — 单条 SQL UPDATE 重算 npmi (Q3 cheap path)
```

### 内存有界的真实机制 (Q10 冻结)

**不是** "每批 pair 空间 <= RAM" (Q1 阶段假设已被实验推翻).
**而是**:
1. `_build_chunk_kws` 累积**每 chunk × N=64** 行 (top-N 前置压缩)
2. 末尾 SQL self-join 走 SQLite 外部排序 (通过 `PRAGMA temp_store=FILE`
   + PR-1.5 的 db-temp 目录 spill 到磁盘)
3. Peak RAM 主要由 `cache_size` (256MB) + 每 batch chunk_tokens dict 决定

### 语义偏移唯一来源 (Q11)

**Top-N cap 丢弃 chunk 内 rank>N 的 kw 参与的 pair**. 丢的 kw 是
高频/单字/非短语的 (npmi 通常最低的一批). 实测 recall 决定 N.

---

## 3. 判据（Acceptance Criteria）

所有判据在 S / M / L(合成大语料)三档语料上评估。**Variant B 的判据以
Variant A 或 baseline 为参考真值**。

### C1 — 内存有界（两 Variant 都必须过）

- **peak RSS < 2 GB**（用户预算 4GB,留 2GB 余量给其他）
- **且不随语料大小线性增长**(斜率明显小于全局 build)

### C2 — Wall 不劣于 baseline

- **M 档 wall ≤ 1.2× baseline**(不追求提速,但不能显著慢)
- **L 档 wall ≤ 0.8× baseline**(应该更快,因为 numpy per-batch + 无大 SQL spill)

如果 M 档 batched 显著慢于 baseline,说明 batch 边界开销高于收益,方向不成立。

### C3 — Variant A 结果与 baseline bit-exact

- keyword_pairs / keyword_stats / auto_ngram_dict / scope_stats 逐行严等
- 若不严等,查明差异根因(浮点顺序、UPSERT 幂等等)

### C4 — Variant B 与 baseline 的偏移量化

- **phrase 集合 Jaccard ≥ 0.90**
- **phrase precision(batched ∩ baseline / batched)≥ 0.90**
  (batched 不能大量"发明"新 phrase — 若太多,说明每批阈值不稳定)
- **phrase recall(batched ∩ baseline / baseline)≥ 0.80**
  (漏检 ≤20% — 用户 Q2 冻结:能解决八成问题即可)
- **共同 phrase 的 count / npmi 相关系数(Pearson)≥ 0.98**
- **pair 集合 Jaccard ≥ 0.90**
- **共同 pair 的 cooccur_count 相对误差 median ≤ 5%, p95 ≤ 20%**

**关键**:这些数字都是"起始假设",实验后如果偏移比预期大且用户仍接受,
可以放宽。**先量化再谈判**。

### C5 — Batch size 敏感性

- 至少 3 个 batch size 各测一次(比如 10k / 50k / 200k chunks)
- 观察 peak RSS + wall + 语义偏移随 batch size 变化
- 目标: 找到"peak 不超 2GB / wall 最小 / 偏移可控"的工作点

---

## 4. 实验维度

### 4.1 语料

| 档 | 来源 | 规模 | 用途 |
|---|---|---|---|
| S | 三体 (2.6MB) | 12k chunks | 快速迭代 + baseline 校对 |
| M | 凡人修仙传 (22MB) | 145k chunks | 主判据,与所有 baseline 对齐 |
| L | 合成:M 复制 10 份、改内容差异 | ~250k chunks | 验证扩展性 |
| XL | (可选)合成:重复 40 份 → ~1M chunks | Q4 冻结:先看 M/L 结果再决定 |

**合成方法**：不是简单复制(会让 pair 计数线性膨胀但 vocab 不变,不真实)。
而是 **shuffle line 顺序** + **随机替换 10-20% token 为 vocab 外新词**,
产生"内容多样但风格相似"的多文档效果,更接近用户 1400 个真实文件的
异构性。合成后**拆成多个独立文件**（比如 M 复制 10 份 = 10 个文件）,
以便测试 file-aligned batching。

### 4.1a File-aligned batch packing (Q1 冻结)

`split_corpus_into_batches(files, target_chunks)` 算法:

```
files_sorted = sorted(files, key=path)  # 同目录文件天然相邻
batches = []
current = []
current_size = 0
for f in files_sorted:
    f_chunks = count_chunks(f)
    # 若本 batch 已有内容 且 加进来会超阈值 → 封口
    if current_size > 0 and current_size + f_chunks > target_chunks:
        batches.append(current)
        current = []
        current_size = 0
    # 单文件超大也接受(不切文件)
    current.append(f)
    current_size += f_chunks
if current:
    batches.append(current)
return batches
```

**保证**:同一文件内容永不跨 batch;同目录文件尽量同批;`target_chunks`
是**软上限**（单文件超阈值时该 batch 允许超）。

### 4.2 Batch size 扫描

Batch target: `{10k, 50k, 200k, 500k chunks}` — 4 档,直接影响 numpy
工作集 + 偏移量,是主判据。

### 4.3 npmi 策略 (Q3 冻结)

**Phrase**: `INSERT ... ON CONFLICT DO NOTHING`（除 count 累加外的字段
保留首次 batch 的值）—— 最简单、避免重扫 events。

**Pair**: cooccur_count UPSERT-add,末尾一次 SQL UPDATE 用累积的
`keyword_stats.chunk_df` + `scope_stats.n_chunks` 重算 npmi（复用现有
`_finalize_pairs_from_temp` §4 SQL 代码）。这不是"大负荷全局重算" —
是 O(surviving pairs) 的存量行 UPDATE,不重扫 events。

### 4.4 Baseline

`main` 分支代码（暂时锁定在 `c4fc6be` PR-8,回退掉 PR-8b,因为 PR-8b
已被证实更慢）作为**baseline global build**。所有 batched 实验的结果
diff 都以它为真值。

**实验前必须先落一件事**：在实验开始时把 PR-8b (`76780fe`) 回退,
让 baseline 是清洁的 PR-8。commit 消息 `revert: PR-8b per-doc 两级 —
实验证实更慢`。

---

## 5. 度量收集

每个 (variant, batch_size, corpus, npmi_strategy) 组合产生一条记录:

| 字段 | 类型 | 收集方式 |
|---|---|---|
| variant | str | A / B-final / B-batch / baseline |
| batch_size | int | chunks per batch |
| corpus | str | S/M/L/XL 标签 |
| wall_s | float | perf_counter 全流程 |
| peak_rss_mb | float | psutil 200ms 采样最大值 |
| n_chunks_total | int | scan 汇总 |
| n_batches | int | 分批数量 |
| npmi_recompute_wall_s | float | 最后重算阶段耗时(仅 B-final) |
| phrase_count | int | detect 输出 phrase 数 |
| phrase_jaccard_vs_baseline | float | vs 相同语料的 baseline |
| phrase_precision / recall | float | vs baseline |
| phrase_count_pearson | float | 共同 phrase 的 count 相关性 |
| phrase_npmi_pearson | float | 共同 phrase 的 npmi 相关性 |
| pair_count | int | keyword_pairs 行数 |
| pair_jaccard_vs_baseline | float | |
| pair_cooccur_rel_err_median | float | 共同 pair 的相对误差 |
| pair_cooccur_rel_err_p95 | float | |
| pair_npmi_pearson | float | |

存 JSONL,`experiments/batched_build_2026_07_16/data/results/`。

---

## 6. 决策路径

### Pass 情景

- **C1/C2 都过 + C4 都过**:Variant B (推荐 B-final) 成为主路径。
  实验产出成为 PR-10 的设计依据。旧 baseline path 保留一段时间作
  `--strict` 精确模式。
- **C1/C2 都过 + C4 全部或部分不过**:偏移超预期,和用户谈判 —
  要么放宽判据接受偏移,要么退回 Variant A(bit-exact 但内存不改善)。
- **C1/C2 过 + Variant A wall 明显好于 Variant B**:上 Variant A 作为
  主路径,B 归档(内存节省小、偏移大、并没有更快)。

### Fail 情景

- **C1 都不过**：batch 内的 pair 工作集本身就超 RAM → 分批需要更细粒度
  (行级?)。返回讨论。
- **C2 严重不过**（batched 慢 2× 以上）:批边界开销太大。查是 tokenize 重复
  开销还是 SQL 每批 CREATE/DROP。可能需要合并连接、减少边界。
- **C4 大幅不达标**（Jaccard < 0.7）:说明每批阈值机制本身有 bug,不是
  语义妥协就能过关的。返回设计。

---

## 7. 落地实施步骤

**Step 0 — 环境准备**(0.5 天):
- Revert PR-8b (`git revert 76780fe`),让 baseline 是 clean PR-8
- 确认 baseline 能在 M 档跑通,记录 baseline 的 wall / peak / 全部 6 表
- 生成 L 档合成语料并存下来

**Step 1 — 原型脚本骨架**(1 天):
`experiments/batched_build_2026_07_16/batched_bench.py`:
- 参数化: variant / batch_size / corpus / npmi_strategy
- 复用 `src/grga/graph/build.py` 的 `_tokenise_chunk` / `detect_ngram_phrases`
  等公共部件(不改主干)
- 生成一个独立 tmp DB,写 keyword_pairs / auto_ngram_dict / keyword_stats
- 输出 JSONL 记录

**Step 2 — Variant B 实现**(1-2 天):
- File-aligned greedy packing (Q1 §4.1a)
- 每 batch 独立跑完整 detect_ngram(单批 line_tokens,PR-6/7 的 SQL
  版本可直接调用,只是输入变短)
- 用 numpy 做 batch 内 pair 聚合(前一轮实验 approach B 代码可复用)
- Merge:auto_ngram_dict `INSERT OR IGNORE`+ count += 单独 UPDATE;
  keyword_pairs UPSERT-add(现有增量模式代码可复用)
- 末尾一条 SQL UPDATE 重算 pair npmi(Q3)

**Step 3 — Baseline 对比脚本 + 判据**(0.5 天):
- Diff 脚本:两个 DB 逐表算 Jaccard / precision / recall /
  Pearson 相关系数,输出 JSON
- 跑完整矩阵 {S,M,L} × batch_size{10k,50k,200k,500k} = 12 组,填 JSONL

**Step 4 — 报告**(0.5 天):
- `FINDINGS.md` 呈现全部数据 + 决策建议
- 与用户讨论:默认 batch_size 是多少、需不需要保留 `--strict` fallback

预计总时间: **3-4 天**(含实验运行时间)。

---

## 8. 冻结决策 → QUESTIONS.md

所有 Q1-Q5 已由用户 2026-07-16 冻结,详见 `QUESTIONS.md`:
- Q1 batch 单位:chunks + file-aligned greedy packing
- Q2 recall 阈值:0.80(接受偏移)
- Q3 npmi 策略:phrase 首批值(不重扫)+ pair 末尾 SQL UPDATE(廉价)
- Q4 XL:先看 M/L,再决定
- Q5 落地:Variant B 为主路径,fallback 与否看 FINDINGS


---

## 后续可选增强 (2026-07-16 实验结果指导)

### 增强 B: 短语频次跨批累积 (未实施, 待触发)

**问题**: 当前架构 recall 天花板 0.73 (M10 3-batch 实测), 主要来自
per-batch 短语检测 —— 跨批稀疏短语(单批 <min_count, 全局累积 >=min_count)
永久漏检.

**设计**:
- 新增跨批持久 TEMP 表 `_ngram_freq_accum(scope_id, phrase_key, n, count)`
- 每批枚举 n-gram 后 UPSERT-add 到累积表
- `detect_ngram_phrases` 加参数 `persistent_freq_table`, freq 门槛判断
  用**累积计数**而非本批
- 邻居分布(entropy)仍局部计算(存储爆炸不划算)

**成本**:
- 工程: 3-6 小时原型 / 8-16 小时生产集成
- 存储: 500MB-1.5GB per 300万 chunks (db-temp, build 结束清)
- 运行时: 每批 +2-5s UPSERT
- 预期收益: recall 0.73 → 0.88-0.93 (估算)

**启用判据** (以下任一满足即触发实施):
1. 用户实测反馈 "漏检的短语确实影响检索质量"
2. 生产环境统计显示 batched 与 baseline 短语 Jaccard < 0.6
3. 有明确的应用场景需要高频次跨批稀疏短语 (如学术论文语料的专业术语)

**不启用理由**: 用户 goal 1(速度)+ goal 3(增量)已达成; recall 0.73
在 caller 多轮逼近哲学下可接受; B 是纯增量增强, 不需要推翻现有架构.

### 增强 C: 熵跨批累积 (未实施, 极不推荐)

**问题**: B 修完后仍有熵漂移(某批邻居样本不足致 entropy 假低).

**代价**: 存储邻居分布 2-5GB per 300万 chunks. 不推荐, 除非 B 之后
熵漂移经实测被证实是主要 recall 障碍.

