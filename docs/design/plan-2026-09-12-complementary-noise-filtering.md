# grga 互补对噪声过滤开发方案

> **状态**：✅ 已实现（未 commit）。实现 PR 覆盖 `src/grga/graph/build.py`、`src/grga/cli.py`、测试与文档。
> **CLI 开关**：`--no-filter-comp-segregation` 已同时加到 `grga index build` 和 `grga index rebuild`，默认关闭（即过滤默认开启）。
> **Iteration 2**：✅ 已实现（未 commit）。c/E 强度门槛（`COMPLEMENTARY_MAX_C_OVER_E = 0.25`）+ 互补对专用停用词表 `_COMPLEMENTARY_STOPWORDS`，两后端共用 `_passes_strength_gate`，见文末 §迭代 2 实现状态。

**背景**

当前 `_extract_complementary_sql` 只检查 `npmi < 0` + p-value + glued pairs，没做停用词过滤，也没区分“语义互补”和“文件类型隔离”。在混合语料（网文 + 科技文献 + 行政文档等）里，异质语体的高频词会互相形成统计负相关，导致 `probe`/`related` 的 complementary 输出被噪声淹没。

本方案分两个阶段落地：阶段 1 快速降噪，阶段 2 治本（语体隔离检测）。两阶段均默认开启。

---

## 阶段 1：build 阶段停用词过滤

### 目标
阻止全局停用词进入 `complementary_pairs`。

### 要求
- 在 `_extract_complementary_sql`（以及 batch fallback 路径的等价位置）对候选 pair 做入口过滤。
- 若 pair 中任一关键词属于 `_INDEX_STOPWORDS`，直接丢弃。
- 默认开启，无独立 CLI 开关；与阶段 2 共用 `--no-filter-comp-segregation` 可一并关闭。
- 过滤行为必须是纯减法：不改变任何非停用词对的 npmi / p-value / 排序。

### 验收标准
- 对现有混合语料 scope 执行 `rebuild` 后，complementary 输出里不应再出现 `第`、`年`、`他`、`来`、`用`、`及`、`中` 等 `_INDEX_STOPWORDS` 成员。
- 现有测试全部通过；新增一个单元测试：构造一对停用词 vs 非停用词的负 npmi pair，确认停用词侧被过滤。

---

## 阶段 2：build 阶段语体隔离检测

### 目标
过滤掉那些负 npmi 主要由“文件类型隔离”驱动的 pair，保留真正语义层面的互补信号。

### 核心判据：B + A 的弱化版

- **判据 A（弱化版）：文件重叠度下限**
  - 要求候选 pair 至少在少量文件里真实共同出现过。
  - 下限与两词中较小的 `doc_df` 成比例，避免对低频词过严。
  - 示例规则（可调整）：`co_doc >= min(3, max(2, floor(sqrt(min(doc_df_a, doc_df_b)) * 0.1)))`。
  - 仅作为“不是纯文件隔离”的低保真门槛，不单独决定生死。

- **判据 B：chunk-vs-file npmi 偏离度**
  - 计算每个候选 pair 的**文件级 npmi**（`npmi_file`）。
  - 若 `npmi_file` 显著低于 `npmi_chunk`（例如 `npmi_file < npmi_chunk - 0.1`），说明负相关主要由“两个词落在不同文件里”驱动，判为语体隔离并丢弃。
  - 阈值默认 0.1，作为内部常量先不暴露 CLI；后续根据 corpus 反馈再决定是否需要可调。

### 要求
- 两判据同时生效，默认开启。
- 提供 CLI 开关 `--[no-]filter-comp-segregation`，默认开启。
- 过滤行为纯减法，不改变留下来的 pair 的任何统计量。
- 阶段 2 不应回写或修改 `keyword_pairs` 表，只在 `_extract_complementary_sql` 决定是否把 pair 写入 `complementary_pairs`。

### 验收标准
- 对现有混合语料 scope 执行 `rebuild` 后：
  - `韩立` 的 complementary 输出里不应再出现 `设计`、`联盟`、`规定`、`app`、`id` 等明显来自异质语体的词。
  - 若语料里存在真实语义互补对（可通过人工构造或已知测试语料验证），应被保留。
- CLI 开关可正常启用/关闭；关闭时行为回退到阶段 1 之后的状态。

---

## 文件级共现的 3 种计算时机

阶段 2 需要文件级共现（`co_doc` 以及基于 doc 的 npmi）。有三种实现时机，差异如下：

### 方案 A：build 时全量物化

- **做法**：在 Pass 2 / pair finalization 阶段，对所有候选 pair 同时计算 chunk 级和文件级共现，写入临时表，供 `_extract_complementary_sql` 直接读取。
- **优点**：
  - 实现最直接，数据一致性最好。
  - `_extract_complementary_sql` 只做过滤，不访问证据层，I/O 模式干净。
- **缺点**：
  - 内存/临时表峰值增加，因为文件级共现矩阵通常比 chunk 级更稀疏但 pair 基数相同。
  - 增量 build 也会重复计算全量文件级共现。

### 方案 B：按需惰性计算

- **做法**：`_extract_complementary_sql` 只处理 chunk 级 npmi < 0 的候选 pair；对每一批候选，临时查询证据表（`_build_chunk_kws` 或等价表）计算这些 pair 的文件级共现。
- **优点**：
  - 只计算真正有需要的 pair，避免为大量正 npmi pair 浪费资源。
  - 不引入新的持久化结构。
- **缺点**：
  - `_extract_complementary_sql` 需要访问证据层，可能产生大量随机查询或复杂 JOIN。
  - 实现路径与 batched / single-pass 两种 pipeline 的临时表生命周期耦合，容易踩坑。

### 方案 C：持久化增量维护

- **做法**：新增一张持久表（如 `keyword_pair_doc_stats`），记录每个 scope 内 pair 的文件级共现。`rebuild` 时清空重算，`build` 增量时只更新新增/删除/变更 doc 涉及的 pair。
- **优点**：
  - 增量 build 成本最低。
  - 未来其他功能（如文档级相关分析）可复用该表。
- **缺点**：
  - 复杂度最高：需要处理 doc 删除、pair 从正变负/从负变正的状态迁移。
  - 增加 schema 表和 migration。

### 推荐

**先按方案 B 实现。**

理由：
- 阶段 2 的过滤对象只是“chunk 级 npmi < 0 的 pair”，数量远小于全部 pair；惰性计算足够快。
- 不引入新持久表，避免 schema migration 和增量维护的复杂度。
- 若后续 profiling 证明方案 B 是瓶颈，再升级到方案 A 或 C。

---

## 默认行为与兼容性

- 阶段 1 和阶段 2 均默认开启。
- 过滤在每次 pair finalization 阶段生效：`rebuild` 一定触发；`build` 在存在
  文件增删改（`index_layer_changed`）时也会触发，并重建该 scope 的
  `complementary_pairs`。若 scope 本次没有任何变更，`build` 不跑 finalization，
  需要一次 `rebuild` 才能把过滤追溯应用到存量互补对。
- 本方案不修改 `keyword_pairs` 结构，不改变 `related`/`probe` 读侧语义，只减少
  `complementary_pairs` 写入的噪声。

---

## 待 code agent 实现时确认的细节

以下细节由实现者根据代码现状选择最优方案，本方案不做强制：

1. 文件级 npmi 的精确浮点计算是否复用现有 `_npmi` 函数，还是写一个接受 doc_df / co_doc / n_docs 的等价函数。
2. 判据 A 的具体参数形式（sqrt 比例、硬下限等）可在实现后通过单元测试 + 一个真实 mixed corpus 的 spot check 微调。
3. 方案 B 的惰性查询是否用 batch JOIN 还是逐 pair 查询，以实现简单和性能为优先权衡。

---

## 实现状态（2026-09-12）

**已实现，两阶段默认开启。**

- 阶段 1/2 过滤逻辑落在 `graph/build.py`：`_extract_complementary_sql`（SQL 后端）与
  `_step4_batch`（fallback 后端）共享同一套判据；`build_graph_index(..., filter_comp_segregation=True)`
  为内部开关，CLI 暴露 `--no-filter-comp-segregation`。
- 判据实现：`_is_segregation_pair`（A+B 两门槛）、`_npmi_doc`（文件级 npmi）、
  `_compute_co_doc_batch`（方案 B 惰性 batch JOIN `keyword_docs`）、`_kw_ids_for` /
  `_get_complementary_stopword_kw_ids`（阶段 1，迭代 2 起并入互补对专用停用词）。
- 参数取值：`COMP_SEGREGATION_NPMI_GAP = 0.1`、
  `COMP_SEGREGATION_MIN_CO_DOC_RATIO = 0.1`、`COMP_SEGREGATION_MIN_CO_DOC_FLOOR = 2`；
  判据 A 形如 `co_doc >= min(3, max(2, floor(sqrt(min_df) * 0.1)))`，且 `n_docs <= 2`
  时短路为"非隔离"（小语料下隔离检测无统计意义）。
- 测试：`tests/unit/test_complementary_pairs.py` 新增停用词门槛、判据单元、
  隔离开关、纯减法、batch fallback 覆盖。

### 关于验收标准里停用词示例的说明

本方案 §阶段 1 验收标准列出的 `第`、`年`、`他`、`来`、`用`、`及`、`中`
**目前并不在 `_INDEX_STOPWORDS` 中**（`_INDEX_STOPWORDS` 只含 26 个结构虚词
单字，如 的/了/和/在/是/也/就/但/这 等）。主 build 管线本就在 tokenisation
阶段过滤该集合，因此阶段 1 门槛对现有语料基本是防御性冗余。

若要让 `第/年/他/来/用/及/中` 这类高频单字也进入过滤，需要扩展
`_INDEX_STOPWORDS`——但这与项目既有的"单字 CJK 不放入全局静态表、交由
scope-adaptive 3a/3b 处理"设计相冲突（见 `graph/build.py` 中 `_INDEX_STOPWORDS`
上方注释）。本实现严格按方案原文只过滤 `_INDEX_STOPWORDS`，未擅自扩展该集合。

---

## 迭代 2：c/E 强度门槛 + 互补对专用停用词表

### 背景

阶段 1/2 实现后，scope `work` 仍残留 11,221 条互补对。按 c/E（`cooccur_count / expected_count`）分布：

```
c/E < 0.05    3
c/E < 0.10    9
c/E < 0.20    259
c/E < 0.30    1003
c/E < 0.50    6611
c/E >= 0.50   4610
```

即 **97% 的 pair 是 c/E ≥ 0.2 的弱负相关**，被大样本量推到了显著 p-value。这些 pair 并不表示语义互补，只是“共现略低于随机期望”。

同时，阶段 1 依赖的 `_INDEX_STOPWORDS` 没收叙事高频词：
- 人称代词：`他`、`他们`、`我`、`你们`、`她`、`她们`、`它`、`它们`
- 章节/时间标记：`章`、`回`、`节`、`卷`、`篇`
- 叙事虚词/副词：`顿时`、`忽然`、`突然`、`然后`、`接着`、`之后`、`后来`
- 言说动词：`说道`、`问道`、`回答`、`说`、`道`
- 语气词：`吗`、`呢`、`吧`、`啊`、`哦`、`嗯`、`呀`

它们和已有 `_INDEX_STOPWORDS` 中的 `我们`、`你`、`这个`、`那个` 本质同类，只是当初词表偏代码/通用语料，没覆盖网文叙事高频词。

### 目标

把互补对输出从“统计显著弱负相关”收紧到“强度足够的负关联信号”，并剔除叙事高频虚词。

### 要求

#### 2.1 c/E 强度门槛

在 p-value 门槛之前增加一条**纯减法**过滤：候选 pair 必须满足 `cooccur_count / expected_count < COMPLEMENTARY_MAX_C_OVER_E`。

- 默认值：`0.25`。
- 仅用于 `complementary_pairs` 写入决策，不回写 `keyword_pairs`，不改已有统计量。
- 与阶段 2 的语体隔离检测独立生效：先过阶段 1 停用词，再过 c/E 强度，再过阶段 2 隔离检测。
- CLI 开关 `--no-filter-comp-segregation` **不应影响 c/E 门槛**；如需关闭 c/E 过滤，需新增独立开关（建议先不暴露，保持决策面最小；后续若强烈需要再补 `--no-filter-comp-strength`）。

#### 2.2 互补对专用停用词表

新增 `_COMPLEMENTARY_STOPWORDS` 集合，**仅在互补对提取阶段使用**，不污染全局 `_INDEX_STOPWORDS`，因此不影响主索引的 tokenisation / pair 统计。

最小集应覆盖：
- 人称代词：他、他们、她、她们、它、它们、我、你、你们、我们、咱们、自己、本人
- 疑问/语气助词：吗、呢、吧、啊、哦、嗯、呀
- 章节/量词标记：第、章、回、节、卷、篇
- 叙事高频副词：顿时、忽然、突然、然后、接着、之后、后来、这时、此刻、那时、当时
- 叙事高频动词：说道、问道、回答、说、道、看、见、只见、看来

允许实现者根据语料反馈少量增删，但新增词必须是**跨小说语料普遍高频、语义信息含量极低**的词。

#### 2.3（可选）收紧语体隔离参数

若迭代 2 落地后仍残留明显跨语体噪声（如 `联盟|说道`），再评估是否：
- 把 `COMP_SEGREGATION_NPMI_GAP` 从 0.1 提到 0.15；或
- 把 `COMP_SEGREGATION_MIN_CO_DOC_RATIO` 从 0.1 降到 0.05。

本次迭代先不动这些参数，避免多个变量同时变化导致效果归因困难。

### 验收标准

对 scope `work` 执行 `rebuild` 后：

1. `complementary_pairs` 总量从 ~11,000 降到 **300 条以下**（c/E < 0.25 的目标效果）。
2. `韩立` 的 complementary 输出中不再出现 `他`、`他们`、`你`、`你们`、`第`、`章`、`顿时`、`吗`、`说道`。
3. `probe` / `related` 的 complementary 列表顶部不再被代词/叙事虚词占据。
4. 现有互补对单元测试全部通过；新增测试覆盖：
   - c/E = 0.30 的 pair 被过滤；
   - c/E = 0.20 的 pair 被保留；
   - `_COMPLEMENTARY_STOPWORDS` 成员作为 pair 一侧时被过滤；
   - 过滤不改变 `keyword_pairs`。

### 兼容性

- `_COMPLEMENTARY_STOPWORDS` 是新增内部集合，不暴露 CLI；默认开启，无开关。
- c/E 门槛默认开启；开关策略与阶段 2 一致，但独立控制（建议本次先不暴露开关，减少决策面）。
- 不修改 schema，不修改 `related`/`probe` 输出字段结构。

### 实现原则

- **复用不重写**：c/E 已在 `complementary_pairs` 候选计算过程中可得，无需新增 pass。
- **纯减法**：只决定哪些候选 pair 不写表，不改 npmi、p-value、排序键等已有字段。
- **additive 演进**：新增 `_COMPLEMENTARY_STOPWORDS` 常量，不改动 `_INDEX_STOPWORDS`。

### 迭代 2 实现状态（2026-09-12）

**已实现，默认开启，无新增 CLI 开关。**

- 新增常量 `COMPLEMENTARY_MAX_C_OVER_E = 0.25` 与集合
  `_COMPLEMENTARY_STOPWORDS`（位于 `graph/build.py` 阶段 1/2 常量之后）。
- 新增共享判据 `_passes_strength_gate(cooccur, expected)`，并进一步抽出
  `_evaluate_complementary_candidate`（glued/负 npmi/c/E/显著性四道门槛，返回
  `(npmi, p_value)` 或 `None`）与 `_complementary_rows`（写表 tuple 组装）；SQL 后端
  `_extract_complementary_sql` 与 fallback 后端 `_step4_batch` 共用，避免两后端漂移。
  c/E 判据在 p-value 计算之前生效，失败则不跑 hypergeometric。
- 停用词入口改为 `_get_complementary_stopword_kw_ids`（`_INDEX_STOPWORDS ∪
  _COMPLEMENTARY_STOPWORDS`）；`_INDEX_STOPWORDS` 本身未改动，主索引的
  tokenisation / pair 统计不受影响。查询统一走 `_kw_ids_for`。
- 语体隔离的 `doc_df` 映射在两后端各加载一次（不再每个 pagination batch 重扫
  `keyword_stats`），通过 `_filter_segregation_candidates(..., doc_df=...)` 传入。
- 过滤顺序：停用词 → c/E 强度 →（p-value）→ 语体隔离，均为纯减法。
- `--no-filter-comp-segregation` 只关闭语体隔离，不影响 c/E 强度与停用词门槛。
- 测试：`tests/unit/test_complementary_pairs.py` 新增 §9，覆盖
  `_passes_strength_gate` 边界、c/E=0.30 过滤 / c/E=0.20 保留、
  纯减法（`keyword_pairs` 不受影响）、互补停用词拦截，以及 batch fallback 覆盖。
- 未做：真实 mixed corpus 的 spot check 与 §2.3 参数收紧留待语料反馈（本次
  刻意不动 `COMP_SEGREGATION_*`，避免多变量同时变化）。
