# grga 互补对噪声过滤开发方案

> **状态**：✅ 已实现（commit `ceda059`）。实现 PR 覆盖 `src/grga/graph/build.py`、`src/grga/cli.py`、测试与文档。
> **CLI 开关**：`--no-filter-comp-segregation` 已同时加到 `grga index build` 和 `grga index rebuild`，默认关闭（即过滤默认开启）。
> **Iteration 2**：✅ 已实现（commit `ceda059`）。c/E 强度门槛（`COMPLEMENTARY_MAX_C_OVER_E = 0.25`）+ 互补对专用停用词表 `_COMPLEMENTARY_STOPWORDS`。scope `work` rebuild 后 `complementary_pairs` 从 12,495 降至 **295 条**。
> **Iteration 2.1**：❌ 废弃。用户决定不继续净化输出层，改为内部静默数据通道（见 §迭代 3）。
> **Iteration 3**：✅ 已实现。互补对降级为内部静默数据通道：写入门槛收紧到 c/E < 0.10 + keyword-quality（两侧 max npmi > 0.70）；`related`/`probe` 的文本与 JSON 输出、`schemas/related-signals-v3.json`、用户文档均不再暴露。见文末 §迭代 3 实现状态。

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

---

## 迭代 2.1：收紧 c/E 门槛并扩展叙事停用词表

### 背景

Iteration 2 落地后，scope `work`（`--include-glob "*.txt"` rebuild）的
`complementary_pairs` 从 12,495 降至 **295 条**，分布如下：

```
c/E < 0.10    3
c/E < 0.15   22
c/E < 0.20   80
c/E < 0.25  190
```

但 `grga related 方长` 顶部仍出现叙事虚词：

```
时      (c/E=0.220)
对方    (c/E=0.227)
其中    (c/E=0.236)
一次    (c/E=0.236)
却      (c/E=0.241)
```

这些词全部落在 `c/E ∈ [0.20, 0.25)`。说明：
1. `COMPLEMENTARY_MAX_C_OVER_E = 0.25` 对 narrative 虚词仍太松；
2. `_COMPLEMENTARY_STOPWORDS` 还没覆盖这类通用虚词。

### 目标

把互补对输出从“显著弱负相关”进一步收紧到“强度足够的负关联信号”，同时清理叙事语料里的高频虚词噪声。

### 要求

#### 2.1.1 c/E 门槛收紧

- `COMPLEMENTARY_MAX_C_OVER_E` 从 `0.25` 调整为 **`0.20`**。
- 仍为纯减法、默认开启、无 CLI 开关、独立于 `--no-filter-comp-segregation`。
- 仅改常量，不改过滤流程。

#### 2.1.2 扩展 `_COMPLEMENTARY_STOPWORDS`

新增以下类别（仅作用于互补对提取，不污染 `_INDEX_STOPWORDS`）：

- **时间与频次**：`时`、`时刻`、`时候`、`一次`、`再次`、`每次`、`有一次`、`一直`、`始终`、`往往`、`常常`、`经常`
- **指代与范围**：`对方`、`彼此`、`其中`、`其间`、`所有`、`一切`、`任何`、`每个`、`有的`、`有些`、`其余`、`其他`
- **转折与连接**：`却`、`但是`、`但`、`而`、`然而`、`而且`、`并且`、`因此`、`于是`、`所以`
- **情态与副词**：`竟`、`竟然`
- **动词补语/体标记**：`一下`、`起来`、`看着`

实现者可按语料反馈少量增删，但新增词必须同时满足：
- 在小说/叙事语料中高频出现；
- 本身语义含量极低，几乎不会形成有意义的语义互补关系。

### 验收标准

对 scope `work` 执行 `rebuild` 后：

1. `complementary_pairs` 总量从 **295 条降至 80 条以下**（c/E < 0.20 的效果）。
2. `grga related 方长 --scope work` 的 complementary 部分不再出现 `时`、`对方`、`其中`、`一次`、`却`。
3. 现有测试全部通过；新增测试覆盖：
   - `c/E = 0.21` 被过滤，`c/E = 0.19` 被保留；
   - 新增 `_COMPLEMENTARY_STOPWORDS` 成员（如 `时`、`对方`、`其中`）作为 pair 一侧时被过滤；
   - `keyword_pairs` 不受任何影响。

### 兼容性

- 不修改 schema，不修改 CLI 接口。
- `_INDEX_STOPWORDS` 不变，主索引 tokenisation / pair 统计不变。
- `--no-filter-comp-segregation` 语义不变（仍只关阶段 2 语体隔离）。

### 实现原则

- 复用不重写：c/E 判据已封装为 `_passes_strength_gate`，只改常量；停用词入口已封装为 `_get_complementary_stopword_kw_ids`，只扩集合。
- 纯减法：不改留下来的 pair 的任何统计量。
- additive 演进：只加常量/集合，不删既有。

---

## 迭代 3：互补对转为内部静默数据通道

### 背景

互补对最初是为**别名检测**引入的：当两个词指代同一实体但几乎不在同一段落共现时（如 `史强` 与 `大史`），它们会呈现负 npmi。但后续验证发现：

1. 在当前 corpus 中，`史强`/`大史` 已经出现在 `keyword_pairs` 正共现网络中，别名检测的主要动机被削弱；
2. "无关联"信号本身比"有关联"信号更容易被噪声淹没（语体隔离、虚词分布、样本量驱动的弱显著性）；
3. 继续迭代过滤（停用词表、c/E 收紧、keyword-quality 门）的 ROI 持续下降，且无法保证用户可见输出的质量。

因此决定将互补对从**用户可见功能**降级为**内部数据通道**：库表保留，build 阶段继续计算并写入，但所有命令、接口、文档、skill 说明中都不再展示该功能，只在库表中静默积累高质量数据，供未来分析使用。

### 目标

- 库表 `complementary_pairs` 结构不变，build 流程不变；
- 大幅提升写入门槛，只保留**高置信度负关联**对；
- 所有用户侧输出默认隐藏，CLI/API/文档/skill 中当作该功能不存在；
- 数据静默积累，未来可做离线分析（如别名挖掘、语体边界研究等）。

### 要求

#### 3.1 收紧写入条件

候选 pair 必须同时满足：

1. **c/E 强度**：`cooccur_count / expected_count < 0.10`
2. **关键词质量**：pair 中每个词都必须在 `keyword_pairs` 中存在至少一个 npmi > 0.70 的共现搭档
3. 保留既有条件：
   - 任一词不在 `_INDEX_STOPWORDS` 或 `_COMPLEMENTARY_STOPWORDS` 中
   - 通过语体隔离检测（`--no-filter-comp-segregation` 仍可控）
   - p-value 显著
   - 非 glued pair

`COMPLEMENTARY_MAX_C_OVER_E` 从 `0.25` 调整为 **`0.10`**。

新增 keyword-quality 门：在 complementary 提取前，预计算每个 keyword 在 `keyword_pairs` 中的 `max_npmi`；候选 pair 两侧 kw 的 `max_npmi` 都必须 > 0.70。

#### 3.2 隐藏所有用户侧输出

- `grga probe` 不再渲染 `complementary:` 区块；
- `grga related` 不再渲染 `complementary:` 区块；
- `--json` 输出中不再包含 `complementary` 字段；
- `src/grga/cli.py` 中与 complementary 相关的输出代码可移除或注释（保留底层数据结构以支持未来分析）；
- `docs/build-tuning.md`、`docs/navigation.md` 中关于 complementary 的用户说明移除；
- `skills/grga/SKILL.md` 及所有 skill 示例中不再提及 complementary；
- `--no-filter-comp-segregation` 开关保留（控制 build 阶段是否写表），但用户文档中不再强调。

#### 3.3 测试调整

- `tests/unit/test_complementary_pairs.py`：保留 build 阶段过滤逻辑的单元测试，但验证条件改为 c/E < 0.10 + max_npmi > 0.70；
- 新增/更新测试：验证 `probe`/`related` CLI 输出中**不包含** complementary 字段；
- `tests/test_graph_build.py`：若涉及 complementary 输出断言，同步移除。

### 验收标准

1. 对 scope `work` 执行 `rebuild` 后，`complementary_pairs` 行数接近 0（当前 corpus 预计 0-3 条）。
2. `grga probe 方长 -d /data/ports/sync/temp` 输出中不出现 `complementary:` 区块。
3. `grga related 方长 --scope work` 输出中不出现 `complementary:` 区块。
4. `grga probe 方长 -d /data/ports/sync/temp --json` 的 JSON 中无 `complementary` 字段。
5. 文档、skill 说明中搜索不到 "complementary" / "互补对" 的用户-facing 描述。
6. 现有测试（调整后的）全部通过。

### 兼容性

- `complementary_pairs` 表结构不变，历史数据不受影响；
- build 流程不变，只是写入门槛提高；
- 不新增 CLI 开关，不修改 schema；
- 对现有 `keyword_pairs` / `related` / `probe` 正侧输出零影响。

### 备注

- 迭代 2.1 中规划的"扩展 `_COMPLEMENTARY_STOPWORDS`"不再执行，因为输出层已隐藏，扩表收益大幅降低；
- 未来若需要重新启用用户侧 complementary 输出，只需恢复 CLI 渲染代码并回调过滤常量，无需改 schema。

---

## 迭代 3 实现状态（2026-09-12）

**已实现。**

### 写入侧

- `COMPLEMENTARY_MAX_C_OVER_E` 收紧为 `0.10`。
- 新增 `COMPLEMENTARY_MIN_KEYWORD_MAX_NPMI = 0.70` 与 keyword-quality 门：在
  `_evaluate_complementary_candidate` 中要求候选 pair 两侧 kw 都在 quality 集合内。
- quality 集合按 scope 预计算：
  - SQL 后端 `_get_complementary_quality_kw_ids(conn, scope_id)` —— 读
    `keyword_pairs.npmi`（在其 npmi `UPDATE` 之后调用），走
    `idx_kp_a_npmi` / `idx_kp_b_npmi` 的 scope-local index-only 查询；
  - no-`ln()` 后端 `_scan_complementary_quality_kw_ids(...)` —— 用 Python 计算
    npmi 扫描 scope 内 pair。**不能复用表查询**：batch 后端边分页边算 npmi，
    新 build 时该列尚为 NULL，若读表会得到空 quality 集合、把所有候选滤掉。
- `--no-filter-comp-segregation` 开关保留，语义不变（只关阶段 2）。

### 读侧

- `related`：`RelatedKeywordsResult.complementary` 字段保留（向后兼容），但查询
  时不再填充，`to_dict()` 省略该字段；CLI 文本渲染移除。
- `probe`：`_attach_complementary` / `_lookup_complementary` 删除，两处调用点移除。
- `schemas/related-signals-v3.json`：移除 `complementary` 字段与
  `complementary_keyword` 定义（v3.3）。
- `docs/build-tuning.md` 移除互补对章节；`docs/navigation.md` 移除 build 行的
  用户说明；`docs/CLI.md` 移除 probe JSON 的 `complementary` 示例与字段说明。

### 测试

- `tests/unit/test_complementary_pairs.py`：build 侧夹具改为满足 c/E < 0.10 +
  quality（`_build_alias_corpus` 用 `行动`/`警官` 作为非 glued 高强度正搭档；
  `_seed_chunk_scope` / `_seed_synthetic_segregation_scope` 自动补非 glued 高质量
  锚点）；新增 quality 门保留/过滤用例、SQL↔batch 在 filter 开启下的 parity；
  读侧改为断言 `complementary` 不出现。
- `tests/test_related_signals.py`：`complementary` 列入禁止字段。
- `schemas/related-signals-v3.json` 同步。

### 已知取舍

- quality 查询按 scope 而非整库计算：npmi 是 per-scope 统计量，跨 scope 取 MAX
  会让别的 scope 的强搭档"洗白"本 scope 的负相关对，且整库扫描在 76M pair
  规模下过慢。
- no-`ln()` fallback 的 quality 扫描是额外一遍 Python pass，仅在缺 `ln()` 的
  SQLite 上触发，属可接受成本。
