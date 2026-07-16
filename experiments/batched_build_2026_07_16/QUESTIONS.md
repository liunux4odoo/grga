# QUESTIONS — batched_build_2026_07_16（决策冻结）

用户 2026-07-16 拍板。以下为**冻结决策**,实施期间不再变动;若实验数据
迫使调整,须在 FINDINGS.md 明标"Q# 变更 + 理由"。

## 总方向

**Variant B（分批派）为唯一主路径。** 不再并行实现 Variant A。
（用户："毫无疑问,按 B 分批派" + "以 B 分批派为主路径"）

理由链:知识是渐进积累的,不必一次学完;grga 核心目标是学常见短语 +
行级共现,没有信息必须从全局语料学;"少许偏移"由 caller 查询期多轮
逼近补齐。

---

## Q1 — batch 单位:chunks,但文件对齐（frozen）

**算法**（file-aligned greedy packing）:
1. 枚举 scope 内所有文件,**按路径排序**（同目录文件自然相邻 → 语义
   最相关的文件被分到一起）
2. 逐文件累加其 chunks 到当前 batch
3. 若"当前 batch 已有内容"且"加入该文件会超过 batch_size" → 先封口
   当前 batch,该文件进入新 batch
4. 若**单个文件的 chunks 本身就 > batch_size** → 该文件独占一个（超大）
   batch（宁可超,也不把单文件内容切散）

**属性**:同一文件的内容永不跨 batch（除非文件自身超限）;同目录文件
尽量同批。batch_size 是"软上限"（可被单个大文件突破）。

---

## Q2 — recall 阈值放宽（frozen）

**接受漏检 ≤ ~20%（recall ≥ 0.80 即可,不强求 0.85）。**

用户："能解决八成以上的问题就足够;误差是 caller 查询期多轮逼近去
解决的 gap。"

→ C4 判据 recall 门槛从 0.85 降到 **0.80**。其余（Jaccard / precision /
npmi 相关）维持,但整体基调是"量化偏移供参考,不是硬门"。

---

## Q3 — 避免大负荷全局计算（frozen,影响最大）

用户:"看计算量,按计算最简单的来,尽量避免大负荷的全局计算。"

**这否决了 SPEC §4.3 原来的 B-final(全局重算 npmi/entropy)方案。** 精确
定案如下,区分两类资产:

### ngram phrase（auto_ngram_dict）—— 纯 per-batch,不做全局重算

- `count`：跨 batch **UPSERT-add 累加**
- `npmi`(solidity) / `branch_ent`：**保留首次 detect 该 phrase 的 batch
  的值**（`INSERT ... ON CONFLICT DO NOTHING` 语义,只更新 count）
  - 理由:solidity 需要 split counts、entropy 需要邻居分布,这些数据
    在 batch 结束就丢弃了,全局重算需要重新扫全语料 events = 正是要
    避免的"大负荷全局计算"
  - 首次值 vs 最新值:**首次最简单**(不用每次比较覆盖),且首次出现
    时该 phrase 在那个语义相近的文件批里的 npmi 通常最有代表性
  - 代价:同一 phrase 在不同批的 npmi 可能不同,只留第一个 → 可接受
    偏移

### keyword_pairs —— cooccur 累加 + npmi 末尾一次性重算（保留现有廉价机制）

- `cooccur_count`：跨 batch UPSERT-add（现增量模式已有）
- `npmi`：**末尾用累积的 keyword_stats(chunk_df) + n_chunks 一次性
  SQL UPDATE 重算**
  - 关键:这**不是**"大负荷全局计算"。pair npmi = f(cooccur_count,
    chunk_df_a, chunk_df_b, n_chunks),全部是**已累积的聚合值**,重算是
    O(surviving pairs) 的单条 SQL UPDATE,不重扫 events
  - 这正是现在 `_finalize_pairs_from_temp` step 4 在做的,**原样保留**

**一句话总结 Q3**:phrase 的 npmi/entropy 就地取首批值（省去全局重扫);
pair 的 npmi 用已累积的 stats 末尾廉价重算（不重扫,只 UPDATE 存量行）。

---

## Q4 — XL 合成推迟（frozen）

先看 S/M/L 结果。M/L 判据都过且扩展性曲线明确 → 省掉 XL。

---

## Q5 — B 为主路径（frozen）

实验通过后,Variant B 成为 `index build` 的**主路径**。是否保留旧的
全局路径作 `--strict` fallback,等 FINDINGS 出来、看偏移实际多大再定
（若偏移很小,可能连 fallback 都不需要,直接替换）。

---

## 对 SPEC 的修订摘要（据以上决策）

1. §2 只保留 Variant B;删除 Variant A 的实现计划（A 的 bit-exact
   对照角色由"baseline global build"直接承担）
2. §4.3 npmi 策略:改为 Q3 定案（phrase 首批值 / pair 末尾廉价重算）,
   删除 B-final vs B-batch 的二选一（不再全局重算 phrase）
3. §4.1 batch 单位:改为 Q1 的 file-aligned greedy packing
4. §3 C4 recall 门槛 0.85 → 0.80
5. §7 Step 2（Variant A 实现）删除,时间省 1 天 → 总 3-4 天

---

## 仍待实验回答（非阻塞,实验中观察）

- best batch_size 默认值（C5 敏感性扫描给出）
- phrase npmi "首批值" 策略的实际偏移量（C4 phrase npmi Pearson 观察;
  若 < 0.9 说明首批值偏移太大,再考虑改"最大 count 批的值"等）
- L 档 wall 是否真的 < baseline（numpy per-batch 加速能否兑现）


---

## 中期 pivot (2026-07-16 实验数据推翻部分假设)

首次 prototype 跑 M10 (12260 chunks) 单批就 OOM (SIGKILL, exit 137)。
Instrument 定位数据 (见 FINDINGS_interim.md):

- 每 chunk **平均 162 个 kw** (post-3b-filter), 非我估计的 30
- M10 单批 pair-instances = **1.62 亿**
- numpy keys 数组 1.3 GB + np.unique 2× 缓冲 = **~4 GB peak**, OOM
- Baseline 单条 SQL `GROUP BY` 用外部排序流式聚合, 不 OOM
- **多批 4.2M 累积不是多批 merge 的锅**, 是**每批 numpy 中间态本身超预算**

用户 2026-07-16 补充三个决策 (Q6-Q8), 加上后续 Q9-Q10:

## Q6 — 后期批次不能硬跳过老 kw 对 (frozen)

用户: "已有的共现对不能跳过, 不然很容易出现后期学不到新连接的情况"

→ **否决 "a+c hard-skip" 方案** (原 a: 老 kw + 老 kw 未在 keyword_pairs 就跳过).
所有 pair-instance 都要参与最终 count. **软阈值** (per-batch min_pair_count)
仍允许后期批次中一个强新连接被检出.

## Q7 — Stopword 分层, top-N 精选每 chunk (frozen)

用户: "全局 stopword 要严格 (保持 0.15 阈值), 批次 stopword 单独调整,
降低每 chunk 的 kw 数, 每段文字只学一定数量最重要的概念, 不追求大而全"

→ 修正 Q4 的 "per-batch stopword 是特性" 立场. 全局 stopword 用于 3b
过滤 (与 baseline 一致), **额外加 top-N per-chunk cap**:
- 排序 B (用户选定): 多字短语优先 > 全局稀有度升序 > kw_id 升序
- 默认 N = 64 (每 chunk 保留信息量最高的 64 个 kw)
- 语义代价: 丢弃 chunk 内 rank>N 的 kw 参与的所有 pair (预期是低信息量的
  "高频单字 kw" pair, npmi 本来就低)

## Q8 — 完全去除 numpy 依赖 (frozen)

用户: "去除对 numpy 的依赖, 它限制了对内存的优化, 带来的效果提升并不明显"

→ 数据支持: numpy 快 3-4× 需要"全部在 RAM", 但 kw 密度 162 时装不下.
Python dict 累加是替代, **但 dict 也会 O(distinct pairs) 增长**,
最终决定采纳 Q9 全局 SQL 方案.

## Q9 — Pair 计数走 baseline 的全局 SQL, 批只做 phrase (frozen)

用户: "字典累加能换成 baseline 的 sql group 吗?"

→ **是**. 最终架构:

- **Phrase detection**: batched (per-batch dict, 无 numpy) —— batch 循环
  的核心价值在这里
- **Pair 聚合**: 每 batch dump (doc, chunk, kw) triples 进**跨批持久的**
  `_build_chunk_kws` TEMP TABLE, 全部 batch 走完后运行 baseline 原代码
  `_finalize_pairs_from_temp` (一条全局 SQL self-join + GROUP BY)
- **理由**: SQL GROUP BY 是外部排序, 天然 disk-spill (通过 PR-1.5 的
  db-temp), 内存有界. dict 累加做不到. Baseline 代码已经在这里,
  bit-exact by construction.

## Q10 — Peak 有界的真实机制 (frozen 认知)

**不是** "每批 pair 空间 <= RAM" (Q1 阶段的假设已被实验推翻).
**而是** "top-N cap 让 pair-instance 总量降 6× + SQL 外部排序处理剩余".

- 无 cap: 162万chunks × 162² = 4.25 亿 pair-instances (baseline 尚可)
- N=64 cap: 162万 × 64² = 6.6 千万 pair-instances (baseline 快 6×)
- 用户 300 万 chunks + N=64: 3M × 64² = 1.2 亿 pair-instances,
  SQL 外部排序 + top-N 直接砍小工作集, peak 主要看 SQLite cache_size

## Q11 — 语义偏移量化 (待实验)

Top-N cap 是唯一新增的偏移来源. Q1 保证不硬丢 pair, Q7 的 N=64 决定
"每 chunk 只学 64 个概念". 丢的 pair 是低信息量的 (高频 + 单字 kw
参与), npmi 本来就低. 需要**实测 recall 数字**决定 N 是否合适.

M10 计划测: N ∈ {∞, 128, 64, 32}, 观察 recall / wall / peak. 若 N=64
recall < 0.80, 增大 N 到达标为止; 若 N=64 recall >> 0.80, 尝试更小 N
看能否进一步压 peak.
