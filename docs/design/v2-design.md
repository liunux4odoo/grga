# grga v2 设计规范

> **版本**：v0.1（2026-07-07 首稿）
> **状态**：设计阶段 · 未开始实现
> **作者**：用户 + Ada 共同讨论沉淀
> **参照现状**：`/media/lqw/DATA/myrepo/grga`（4498 行 Python，`deep_search.py` 1606 行为核心）

---

## 0. TL;DR

grga v2 = **Agent-Native 的结构化知识检索原语库**。

不是搜索应用，是**给 caller agent 用的搜索工具箱**。四条硬约束：

1. **轻依赖**：不用 embedding，不用向量库；文件系统 + 关键字 + 简单图
2. **Agent 优先**：接口为 agent 设计，Skill + CLI 双出口；每个动词幂等、可预测
3. **智能反思在 caller**：grga 只提供原语，反思循环由 caller agent 组织
4. **参考百家，走自己的路**：借鉴各家 RAG 长处，但不做它们的产品

---

## 1. 产品定位

### 1.1 一句话

> **grga = 让 agent 可以"像人类查书一样"探索本地知识库的工具。**

### 1.2 心智模型 A(给未来贡献者):侦探与放大镜 —— 为什么 grga 不做 rerank

> **本节读者是决定 grga 实现的贡献者(人或 agent),不是 grga 的 caller。**
> Caller 打开 skill 文档看到的是 §1.1 那两句操作性框架就够了;贡献者需要
> 更深的心智锚点,防止未来忍不住加 rerank / hub warning / 语义过滤。

> **Caller 是侦探福尔摩斯,grga 是放大镜。**
> 语料是沙堆,目标信息是钻石。
> **grga 的目标不是"把沙子吹开只留下钻石"** —— 是放大侦探视野,让他看清楚每一粒东西的形状,自己判断哪个是钻石。

这是理解 grga 所有设计取舍的**根本心智模型**。之下的每一条铁律都可以从这个比喻推出:

#### 1.2.1 放大镜不做判断

- 不预先判定"这个词是噪声"、"这个 seed 是 hub"
- 不做"help caller 决定"的功能(如"df > X 就 skip L2")
- 判断权在侦探手里,任何 grga 侧的默认判断都会伤到某类 caller

**推论**:接口设计时禁止使用"filter"、"prune"、"exclude by default"这类**替 caller 决策**的动词语言;首选"expand"、"show"、"reveal"、"probe"这类**放大侦探视野**的语言。

#### 1.2.2 放大镜不改变原物

- 不做语义过滤(不扩停用词、不注入语义偏见)
- 不 rerank 假设 caller 想要什么(如"人物名优先")
- 数据的语义完整性 = 放大镜的透明度

**允许的加工:数学恒等式**。npmi、tf-idf、log 归一化等**不注入语义假设**的变换等价于"给放大镜换更精细的镜片" —— 侦探得到更清晰信号,决策权依旧在他。

**禁止的加工:语义/先验判断**。扩停用词表、DF-adaptive 剪枝、hub whitelist 等**注入 grga 对语义的默认判断**,等价于"在放大镜下面预先遮挡某些区域" —— 侦探看不到自己该看的东西。

#### 1.2.3 一个侦探的沙,是另一个侦探的钻石

**关键洞察**:"什么是钻石"是 **caller-任务-相对**的,不是 grga 可以默认定义的。

同一堆沙,四个侦探眼里的钻石完全不同:

| 侦探 | 任务 | 钻石 | 沙 |
|---|---|---|---|
| A | 找人物关系 | 墨大夫/李化元 | 眉梢/心中 |
| B | 找叙事风格 | 眉梢/微微一笑/心念一动 | 墨大夫 |
| C | 找术语体系 | 修士/结丹/元婴 | 韩立 |
| D | 找情节推进节点 | 韩立(高频恰恰是主线信号) | 修士(通用背景词) |

**任何 grga 侧对"钻石"的默认定义,都会剥夺至少一类侦探的能力**。这是所有"帮 caller 判 hub"的方案在产品层的失败根因(不只是数学问题)。

#### 1.2.4 放大镜可以磨得更精细,但决策权不变

grga 可以做:

- **提供多种镜片**(不同的 ranking / 归一化方式,如 raw count vs npmi)
- **提供多种观察角度**(不同粒度的扩展:行/段/章节/文档/图关联)
- **提供更清晰的原始信号**(位置元数据、共现权重、结构标签)

grga 不做:

- **替侦探选镜片**("这个 seed 你应该用 npmi 而不是 raw")
- **替侦探决定看哪里**("这个 seed 是 hub,别看 L2")
- **替侦探解读**("这个词是噪声不用看")

#### 1.2.5 侦探自己迭代,放大镜不迭代

多轮反思循环由 caller 组织:

- **侦探侧**:看线索 → 推理 → 决定下一步找什么 → 用放大镜的下一次
- **放大镜侧**:每次调用是原子的、幂等的、可预测的;不做"内部智能反思"

**推论**:grga 不做黑箱 agent loop;所有的"多轮"发生在 caller 侧;grga 只保证每一次原子调用的稳定性和信号完整性。

### 1.3 心智模型 B(给贡献者+ sophisticated caller):K 线图 —— 输出如何被消费

> **本节读者是决定接口契约的贡献者,以及愿意深度使用 grga 的 caller。**
> 侦探比喻回答"grga 该做什么", K 线图比喻回答"grga 的输出应该长什么样、
> 应该如何被消费"。两者互补,一致。

**比喻**:

> **grga 的返回结果 = 一张 K 线图。**
> 单个邻居的 (name, df, co-count, npmi) 元组 = 一根 K 线,携带若干个原始
> 数字,信息量有限。
> **top-K 邻居的整个分布 = K 线图的形态**(头肩顶、旗形、断崖、平原…),
> 形态**不在任何单根 K 线里**,是 caller 用眼睛扫描一整组数据涌现出来的。

映射关系:

| K 线图世界 | grga 世界 |
|---|---|
| 交易所提供 K 线数据 | grga 返回邻居 + 元数据 |
| 交易所不打"买/卖"标签 | grga 不给"这是 hub / 这是好结果"评价 |
| 交易员自己识形态 | Caller 自己判断形态含义 |
| 头肩顶 / 双底 / 旗形 | 平原分布 / 断崖分布 / 双峰分布 |
| 同一张 K 线,短线派 vs 长线派读出不同东西 | 一个 caller 的沙,是另一个 caller 的钻石 |

**这个比喻对 grga 接口设计的两条硬约束**:

1. **K 数量要足够看出形态** —— top-5 看不出 K 线图,`related` 默认返回
   足够多的邻居(现设 top-100),让 caller 能看清分布
2. **每根 K 线的原始参数必须完整** —— 单根 K 线 4 个数字(开/收/高/低)
   不能少;grga 每个邻居的 (name, df, co-count, npmi) 元组也不能少。
   **少给一个字段等于抹掉一个维度的 K 线**

**这个比喻推出的更深洞察**:

- **Caller 必须能识形** —— K 线图对不会看图的读者是无用信息。这解释了
  为什么 grga 定位 caller 是 agent(有推理/模式识别能力),不是 automation
  script(硬编码规则)
- **不同任务的 caller 读同样的 K 线图,看出不同东西** —— 找人物关系的
  看到"平原分布,没实体钻石";找叙事风格的看到"独占叙述词密集,值得挖"。
  这是"一个侦探的沙,是另一个侦探的钻石"的更硬的说法

**比喻的边界**(2026-07-11 主动澄清,避免未来误导):

- **K 线图是精确数字**(开盘价就是开盘价,4 个数字)
- **grga 关键字信号是离散噪声**(用词有偏好/同义/省略/歧义)

这个差异**削弱但不推翻**比喻。削弱的是:K 线图有大量成熟指标(RSI/MACD)
可供交易员调用,grga 的"形态识别"没有对应的成熟工具集,caller 每次要靠
推理临场识别。**不推翻**的是核心洞察"暴露完整维度让形态自现,不替 caller
打标签"。

**这个差异反过来给 grga 一条额外约束**:因为 caller 没有 MACD 这类熟工具
可调,只能靠 grga 给的**原始信号**自己识形,所以 **grga 的接口必须给尽
可能完整的元数据**,包括:
- 每个邻居的完整 (name, df, co-count, npmi, ...) 元组
- 结果整体的形态描述性统计(如 `top_k_dropoff_ratio`,见 §1.5)

### 1.4 关键定位选择

| 维度 | 选择 | 为什么 |
|---|---|---|
| 目标用户 | AI Agent（非人类） | Skill + CLI 决定 |
| 检索原理 | 关键字 + 简单图 | 轻依赖信念 |
| 图粒度 | **关键字级**（不是 token、不是 entity） | 太细噪声大、太粗依赖 NER |
| 反思循环 | Caller 组织 | grga 只做原语 |
| 图更新 | 离线定期整理 | 简单、可控、避免在线消耗 |
| 语言/格式 | 结构化文档优先（MD/代码），其他退化到行段 | 鲁棒性优先 |

### 1.5 API 设计的两条命名铁律(2026-07-11 沉淀)

从心智模型 A + B 推出的**具体接口设计约束**,任何新字段/新返回值必守:

#### 1.5.1 命名规则:事实字段安全,评价化字段陷阱

**Banner 化的真正机制**(经外部旁观者压测精修):不是"关于输入 vs 关于输出",
而是"**单维度处方性总结 vs 多维度描述性原始信号**"。

**命名决定 banner 命运**:

| 字段类型 | 例 | 会 banner 化? |
|---|---|---|
| 客观事实数字 | `df: 0.324` / `count: 30000` | ❌ 不会,永远安全 |
| 描述性统计 | `top_k_dropoff_ratio: 0.12` | ❌ 不会,数字要读要对照 |
| 命名的形状(极度克制) | `neighbor_df_median: 0.28` | ⚠️ 边缘,可用但需谨慎 |
| 分类判断 | `shape_type: "plateau"` | ✅ 会 banner 化(平原是评价) |
| 自评价 | `l2_output_quality: low` | ✅ 会 banner 化 |
| 处方 | `warning: escalate_to_L3` | ✅ 会 banner 化 |

**禁用词表**(字段名中出现即警告):
`confidence` / `quality` / `score` / `warning` / `flag` / `status` /
`is_*`(布尔评价) / 任何形容词

**首选词表**:
`count` / `ratio` / `median` / `df` / `distribution` / `series` / 任何
纯数量单位

#### 1.5.2 最严边界:工具不评价任何东西,包括不评价自己的输出

即使意图正确("给 caller 数据自己判断"),半步差错就滑到评价姿态。**"L2
遇到麻烦了" / "L2 输出可能不好用"** 这类听起来技术客观的自省表达,依然
是 grga 在评价自己的输出 —— **"麻烦"是形容,不是数字**。

**允许的最"高级"表达是命名的形状统计**(如 `top_k_dropoff_ratio: 0.12`),
再往上就是评价,不允许。

**可机械验证的判据**:审视任何字段/汇报,只问一句 —— **"这是数字还是
形容/分类?"** —— 答案立即分出安全与陷阱。

这条铁律为什么必要:抗 banner 是**结果导向**的,不是**姿态导向**的。技术
上做到"看似不评价"("我们只是好心提醒 caller L2 可能失败")在 banner 陷阱
面前依然会失败,因为 caller 大脑的模式匹配是**基于字段形状**的,不是基于
grga 意图的。

#### 1.5.3 跨场景稳健性判据:只有数学恒等式进主干(2026-07-11 沉淀)

命名铁律回答"字段**怎么命名**才安全",本节回答"**哪些加工**才配进 grga
主干"。来源:2026-07-11 深夜的 PPR 塌陷复盘 + 场景稳健性矩阵(见
`docs/design/plan-2026-07-11.md` §P0/§P1)。

**核心判据**:一项加工能否进 grga 主干,取决于它**跨语料是否恒真**:

| 加工类型 | 例 | 跨场景? | 归属 |
|---|---|---|---|
| 数学恒等式 | `df` / `npmi` / `count` 归一化、`log1p(occ)` 阻尼 | ✅ 跨语料恒真 | **grga 主干** |
| 特定语料的媒介过滤 | 过滤 `.min.js` / 编程语言停用词 | ❌ 场景绑定 | **caller skill 分场景配置** |
| 语言级停用词表 | 中文加"心中/一声/看到" | ❌ 中英文错位 | **caller skill 分场景配置** |

**为什么 `log1p(occ)` 属于数学恒等式一侧**:它只是把"出现越多权重越大"
的单调信号压缩长尾,不含任何"哪种词是噪声"的语义先验。同一个变换在代码
仓库(压制 `cytoscape.min.js` 里 980 次 `if`)和中文小说(压制 filler 章节
的 hub 词)都成立 —— **跨场景矩阵两格全绿**,才落地。

**反面教训(曾被 2026-07-11 讨论否决)**:往 grga 主干加"`.min.js` 文件
类型过滤"看似能立刻让代码仓库结果变干净,但它是**场景绑定战术**:换成
中文语料这条规则毫无意义,换成前端项目它甚至会误伤真要找的 vendor 文件。
媒介层过滤的杠杆方向是反的 —— 该由**最懂当前场景的 caller** 在 skill 里
配置,不是压给 N 个 caller 共用的 grga 主干。

**可机械验证的判据**:审视任何"要不要把 X 加进 grga"的提案,先问 ——
**"X 在另一个完全不同的语料上还成立吗?能画出至少 2 格全绿的场景矩阵
吗?"** —— 答不出第二格,就先降级到 caller skill,不进主干。

### 1.6 反定位（明确不做的）

- ❌ Web 搜索前端 / RAG 应用框架（如 LangChain）
- ❌ 面向人类的可视化搜索工具（GUI 保留但不迭代）
- ❌ 强 KG 系统（不做三元组抽取、不做 OpenIE）
- ❌ 内嵌 embedding / 向量索引
- ❌ 内部黑箱反思循环（不做 grga 自主的多轮）
- ❌ **"帮 caller 判钻石/沙"的功能**(2026-07-10 新增,见 §1.2.3)
- ❌ **对 seed 的诊断字段**(如 `seed_is_hub`)—— banner 陷阱(2026-07-11)
- ❌ **对自身输出的评价字段**(如 `l2_confidence`)—— 抗 banner 最严边界(2026-07-11)

---

## 2. 用户产品哲学（原文）

用户于 2026-07-07 明确表达的 5 条 + 补充：

### 2.1 轻依赖
不要 embedding、不要向量库。文件系统 + 关键字/实体 + 简单图索引就够。
**核心信念**：关键字/实体 + 图扩展是更高效的方法。

### 2.2 方便部署
提供 Skill + CLI，agent 可以直接调用。

### 2.3 智能反思
利用 LLM 理解用户意图，通过多轮迭代、搜索扩展、多跳，不断搜集线索，逼近答案。

### 2.4 参考百家但走自己的路
调研的各类 RAG 框架都可以取长处参考，但它们都不是最终产品。

### 2.5 命中扩展能力
除直接命中外，需要根据命中点做扩展：
- 前后行 / 段落 / 章节 / 文档 / 目录邻近文档
- 位置感知：命中在文件中是第几次出现

---

## 3. 三个已敲定的决策

| 决策项 | 选择 | 说明 |
|---|---|---|
| **Skill 格式** | 用于 agent 的 skill | 先实现功能，稍后建 skill |
| **图更新策略** | 离线定期整理 | 不做在线实时图扩展 |
| **反思循环归属** | Caller 反思 | 不做内部黑箱反思 |

---

## 4. 核心概念：命中扩展的三层模型 ⭐

**这是 v2 相对 v1 最大的能力提升**。

### 4.0 心智模型 C:K 线图厚书本 —— 分层的产品定位(2026-07-11 新增)

> 前作把 L1/L2/L3 描述成"成本从低到高、能力从弱到强的 fallback chain"。
> 这个描述**没错但没解释为什么要分层**。以下心智模型给出更精准的定位。

**比喻**:

> grga 搜索 = 在一本**成千上万页 K 线图的厚书**里,找到具备**关注形态**
> 的那几页。
>
> - **L1** = 根据基础信号直接检索若干 K 线(命中原始位置的邻近)
> - **L2** = 在基础信号的基础上,试图**根据已有的形态经验**,以较低成本
>   **补足缺的几根 K 线参数**,更好地定位所需的 K 线组合
> - **L3** = 前两者无功时,花大代价请**资深专家**去完成 L2 不能完成的
>   补足工作

映射:

| L | 补足的是什么 | 补足依据 | 成本 | 类比 |
|---|---|---|---|---|
| L1 | 命中周围的物理上下文 | **物理邻近**(前后行/段落/章节) | 免费 | 翻同一页 K 线的其他点 |
| L2 | 命中周围的经验关联 | **统计经验**(共现图预建) | 低(离线预建) | 用历史形态经验补参数 |
| L3 | 命中周围的语义关联 | **LLM 语义理解**(现场调用) | 高 | 请资深专家现场判断 |

**核心洞察**:三层不是"能力递进",是**"同一件事(信号补足)在不同代价档
位下的实现"**。三层都在补 K 线参数,只是**补足依据不同**(物理位置 → 统计
经验 → 语义理解),**成本从低到高**。

**这个心智模型解释了几件事**:

1. **为什么 L2 必须离线预建**:L2 用的是**跨查询共享**的统计经验。预建一
   次可以服务无数 caller,这是它相比 L3 的真正优势 —— **不是 L2 更聪明,
   是 L2 的智力已经预先支付过了**。L3 每次都要现付
2. **为什么 hub seed 结构性属于 L3 范畴,不该在 L2 里救**(详见 §4.5)
3. **为什么 L3 该谨慎使用**:请资深专家很贵,不该滥用(详见 §4.6)
4. **为什么 caller 必须是 agent 而非硬编码脚本**:K 线图对不会识形态的读
   者是无用信息;caller 必须能扫描一组 K 线自行识形(详见 §4.7)

三种扩展**分离实现**，用途不同，构成 caller 的 fallback chain：

| 层 | 类型 | 邻近维度 | 成本 | 确定性 | 类比 |
|---|---|---|---|---|---|
| **L1** | 规则扩展 | 物理邻近（文档内） | 极低 | 完全确定 | 人查书时前后翻页 |
| **L2** | 图扩展 | 结构邻近（跨文档） | 低（离线预建） | 弱随机 | 顺着索引/参考文献跳 |
| **L3** | LLM 扩展 | 语义邻近（跨概念） | 高 | 强随机 | 靠背景知识猜相关 |

### 4.1 L1 · 规则扩展（物理邻近）

**输入**：hit（path, line_number, ...）
**输出**：一段连续文本

**粒度选项**（从窄到宽）：

```
lines(±N)          ← 现有 rga -B/-A
paragraph          ← 找前后空行边界
chapter            ← Markdown AST，heading 边界
code_block         ← 代码 fence / 缩进块
function           ← tree-sitter 找函数范围
document           ← 整文件
```

**退化路径**（保证鲁棒性）：
```
chapter 找不到 → paragraph
paragraph 只有一行 → lines(±3)
一切都退化 → lines(±0) 即 hit 本身
```

### 4.2 L2 · 图扩展（结构邻近）

**输入**：hit（或 hit 集合）→ 抽取关键字集合 K
**处理**：从 K 出发在图上跑 PPR / 二跳邻居
**输出**：相关关键字 / 相关文档 / 相关章节

**"相邻文档"三种定义**（分开实现）：
1. **目录层级邻近**：同目录 / 兄弟目录（不用图，文件系统遍历）
2. **共同关键字邻近**：图扩展的核心（`keyword_docs` 表 + `keyword_pairs` 表）
3. **Heading_path 邻近**：多篇文档的 "安装 > macOS" 章节可以互为邻居

### 4.3 L3 · LLM 扩展（语义邻近）

**触发条件**（caller 决定）：
- L1 扩展后 caller 判断信息不足
- L2 图上找不到相关节点（冷启动 / 稀疏关键字）
- Query 有强语义但没显式关键字（如"找一个乐观的例子"）

**调用形态**：
- 生成新查询词（把 hit 语境 → 建议下一步搜什么）
- 生成新关键字（把 query 意图 → 猜可能的相关词）
- 判断"哪些 hit 语义相关但表面不像"（rerank）

**原则**：
1. 只在前两层不足时用
2. 结果打上 `llm_generated` 标记，caller 知道这是猜的
3. **成功的结果反哺 L2**（LLM 猜对的关键字对沉淀到图里）

### 4.4 L3 → L2 反哺闭环 ⭐

```
        ┌─────────────────────────────────┐
        │                                 │
        ▼                                 │
    L1 规则扩展（免费）                    │
        │                                 │
        │ 不够                             │
        ▼                                 │
    L2 图扩展（离线预建，快）              │
        │                                 │
        │ 不够                             │
        ▼                                 │
    L3 LLM 扩展（贵，随机）                │
        │                                 │
        │ 成功                             │
        └────────沉淀到图里 ──────────────┘
                 (离线周期整理)
```

**这就是"积累使用经验"的具体机制**：
- 每次 L3 猜对 → 变成 L2 的固定边
- 下次同样查询走 L2 免费拿到
- 系统随时间自动变便宜、变准

**投票机制（防污染）**：
单次 LLM 输出不直接进图。同一关键字对被 LLM 输出 **N 次**（阈值可配，参考 LightMem 的效用累积）后才写入图。

### 4.5 Hub seed 结构性属于 L3 范畴(2026-07-11 复盘沉淀)

**背景**:2026-07-10 花了一整天做四轮实验(V0/V1-3, V4, V5_npmi)试图改进
L2 对 hub seed(如"韩立",df/N=0.33)的邻居质量。四种变量组合(raw count /
DF-adaptive 剪枝 / 章节窗口 / npmi 归一化)**全部失败** —— hub 邻居总
退化为高频虚词或独占叙述词。

**用 §4.0 心智模型解释**:

- Hub seed = "查一根几乎在每页都出现的 K 线"
- L2 试图基于**统计共现经验**补出关联参数
- 但 hub 的共现经验 = "跟所有 K 线都共现" = **无区分度经验**
- 所以 L2 补出全书最高频的 K 线(眉梢/心中),而非"跟 hub 特殊关联的 K 线"

**结论**:hub 结构性**不属于 L2 能处理的信号补足**。它需要**语义级判断**
("韩立是主角,他的关联应是师傅/对手,而非表情动作") —— 只有 L3(资深专
家/LLM)能给。

**这不是 L2 的失败,是设计上就该走 L3**。在 §4.6 定义的 L3 使用规约下,
hub seed 是最典型的"该请专家"case:L1/L2 便宜但对它无能为力,必须付
L3 的代价。

**完整复盘和证据链**:见 `docs/design/graph-l2-postmortem-2026-07-10.md`。

### 4.6 L3 使用规约:资深专家不该滥用

**问题背景**:LLM 很强,容易被 caller 无脑滥用作为"什么都上 L3"的兜底。
从 §4.0 心智模型看,L3 = 请资深专家,每次现付,**滥用会让成本失控**。

**caller 反思循环的具体规约**:

1. **默认从 L1 开始**:物理邻近的信号补足最便宜且完全确定,先榨干
2. **不够走 L2**:统计经验补足,离线预建过,查询快;此时用 §7 定义的
   形态统计判断"L2 输出是否足够"
3. **仍不够或明显是 hub / rare seed / 冷启动才走 L3**:LLM 现场调用,贵

**判"L2 输出是否足够"的信号**(不是 grga 汇报,是 caller 从 §7 输出的
形态统计自行判断):

- Top-K count 分布是**平原**(dropoff_ratio 低) → L2 输出弥散,可能需
  要 L3
- Top-K 邻居品类**单一**(全叙述配色 / 全通用虚词) → L2 输出低多样性,
  可能需要 L3
- Caller 端**形状预期不匹配**(查专名却拿回虚词) → 只有 caller 知道自己
  预期,grga 无法预知

**这个规约不能落地为 grga 的 warning**(见 §1.5.2 抗 banner 最严边界),
只能作为 skill 文档里的**使用指引**,由 caller 自行遵守。

### 4.7 Caller 隐含要求:必须能识形

**从 §4.0 K 线图心智模型推出的**caller 侧要求:

> K 线图对不会看图的读者是无用信息。
> **grga 的输出对不会识形态的 caller 是无用信息**。

**具体含义**:

- Caller 拿到一组 30 个邻居 + 形态统计后,必须能扫描判断"这次输出是集
  中还是弥散、品类是单一还是多样、我要的形状有没有出现"
- 这需要 caller 有**模式识别能力和推理能力** —— 是 LLM-based agent 能
  做到的,不是硬编码脚本能做到的

**这解释了 grga 定位 caller 是 agent 而非 automation script**:

- Automation script:硬编码规则(如"df > 0.05 就 skip") → 遇 domain 词
  会误伤(修士 df=0.056,但 caller 任务恰好要它)
- LLM-based agent:模式识别 + 任务感知 → 能根据自己任务判"这次形状对我
  是不是有用"

**推论**:如果一个使用场景无法接受"caller 必须能识形"这个前提(如纯规则
引擎调用),那 grga 不适合。**这不是 grga 的短板,是产品定位的自觉边界**。

**场景专属 post-filter 的归属**(2026-07-11 补):caller skill 里可以带
场景专属的 post-filter(如构建产物文件名黑名单 `.min.js` / `dist/**`、
中文虚词表、编程语言级停用词),这一层**不是 grga 主干职责** —— 跨场景
不稳健的加工归 caller,理由见 §1.5.3。grga 只承诺跨场景稳健的数学加工;
识形 + 场景适配是 caller 的一等责任。

---

## 5. 架构分层

```
┌────────────────────────────────────────────────────────┐
│ Layer 3 · Agent API（对外暴露）                        │
│                                                        │
│   grga.search(query)         → List[Hit]（含元数据）    │
│   grga.expand(hit, level)    → Context（L1 规则）       │
│   grga.related(keywords)     → List[Keyword|Doc]（L2）  │
│   grga.suggest(query, hits)  → List[Query]（L3，可选）  │
│                                                        │
│   每个动词独立、幂等、可预测                             │
└────────────────────────────────────────────────────────┘
                        ▲
                        │ caller agent 反思循环组合调用
┌────────────────────────────────────────────────────────┐
│ Layer 2 · Anchor Metadata（新增，命中元数据）           │
│                                                        │
│   occurrence_index / total_occurrences                 │
│   line_ratio                                           │
│   nearby_hits                                          │
│   heading_path / chapter_id                            │
└────────────────────────────────────────────────────────┘
                        ▲
┌────────────────────────────────────────────────────────┐
│ Layer 1 · Structure & Graph（新增，旁路增强）           │
│                                                        │
│   ├─ Structure Layer                                   │
│   │   ├─ Markdown AST（TODO: 选择解析工具）             │
│   │   ├─ 代码结构（tree-sitter，可选）                  │
│   │   ├─ 段落分割                                       │
│   │   └─ 退化兜底                                       │
│   │                                                    │
│   └─ Graph Layer                                       │
│       ├─ keyword_pairs（关键字共现，已有）              │
│       ├─ keyword_docs（关键字 → 文档，新增）            │
│       ├─ heading_docs（heading_path → 文档，新增）      │
│       ├─ PPR 扩展算法（新增）                           │
│       └─ 离线整理任务（新增）                            │
└────────────────────────────────────────────────────────┘
                        ▲
┌────────────────────────────────────────────────────────┐
│ Layer 0 · Retrieval（现有，保留）                       │
│                                                        │
│   ├─ ripgrep-all（原始命中）                            │
│   ├─ 布尔查询 AST（现有 search.py）                     │
│   └─ 分层搜索 + 早停（现有 deep_search.py）             │
└────────────────────────────────────────────────────────┘
```

**关键设计原则**：**Layer 1 是旁路增强层，不阻塞 Layer 0**。
ripgrep-all 保持原样跑，结构和图数据通过**预建索引**摊到离线。在线只做 O(log N) 索引查询。

---

## 6. 数据模型

### 6.1 Hit（搜索命中）

```python
class Hit:
    # 静态定位
    path: str                # 文件路径
    line_number: int         # 行号
    text: str                # 匹配行文本
    char_span: tuple[int, int]  # 匹配字符范围

    # 位置元数据（Layer 2）
    occurrence_index: int    # 该 query 在此文件中第几次出现（1-N）
    total_occurrences: int   # 该 query 在此文件中总共出现次数
    line_ratio: float        # line_number / total_lines，0.0-1.0
    nearby_hits: int         # 前后 K 行内的其他命中数量

    # 结构元数据（Layer 1，可选）
    heading_path: list[str] | None   # ["Chapter 1", "Sub 1.1"]
    chapter_id: str | None            # 稳定 slug
    chapter_range: tuple[int, int] | None  # (start_line, end_line)

    # 来源标记
    matched_terms: list[str]   # 命中的原始 query 词
    is_llm_generated: bool     # 是否来自 L3
```

### 6.2 Graph Schema（SQLite）

```sql
-- 已有：关键字使用计数
CREATE TABLE keyword_counts (
    keyword TEXT PRIMARY KEY,
    count INTEGER,
    last_used INTEGER,  -- unix ts
    decay_weight REAL
);

-- 已有：关键字共现（尚未充分利用）
CREATE TABLE keyword_pairs (
    kw1 TEXT,
    kw2 TEXT,
    cooccur_count INTEGER,
    PRIMARY KEY (kw1, kw2)
);

-- 新增：关键字 → 文档倒排索引
CREATE TABLE keyword_docs (
    keyword TEXT,
    doc_path TEXT,
    occurrence_count INTEGER,
    first_line INTEGER,
    PRIMARY KEY (keyword, doc_path)
);

-- 新增：heading_path → 文档索引（跨文档章节匹配）
CREATE TABLE heading_docs (
    heading_path TEXT,       -- 序列化的 path，如 "安装 > macOS"
    doc_path TEXT,
    chapter_range TEXT,      -- "start_line,end_line"
    PRIMARY KEY (heading_path, doc_path)
);

-- 新增：L3 反哺投票
CREATE TABLE llm_extension_votes (
    kw1 TEXT,
    kw2 TEXT,
    vote_count INTEGER,
    last_voted INTEGER,
    PRIMARY KEY (kw1, kw2)
);
-- 达到阈值（如 3 票）后写入 keyword_pairs
```

### 6.3 Structure Index（离线预建）

```
每个 md 文件对应一个 sidecar：
    docs/foo.md
    .grga/structure/docs__foo.md.json

内容示例：
{
    "path": "docs/foo.md",
    "mtime": 1720000000,
    "chapters": [
        {
            "heading_path": ["Chapter 1"],
            "level": 1,
            "start_line": 1,
            "end_line": 20,
            "chapter_id": "chapter-1"
        },
        ...
    ],
    "paragraphs": [
        {"start_line": 3, "end_line": 5},
        ...
    ]
}
```

**更新时机**：文件 mtime 变化时懒重建，或后台周期批量。

---

## 7. Agent API 契约

### 7.1 search()

```python
def search(
    query: str | QueryExpr,
    scope: str | list[str] = ".",   # 目录或文件
    max_hits: int = 100,
    tier_limit: int | None = None,   # 分层早停
) -> list[Hit]
```

**保证**：
- 幂等（相同输入 → 相同结果）
- 元数据完整（`occurrence_index` 等必填）
- 结构元数据 best-effort（无结构文档返回 None）
- 排序按相关性（分层 tier + 出现次数 + 密度）

### 7.2 expand()

```python
def expand(
    hit: Hit,
    level: str,          # "lines" | "paragraph" | "chapter" | "document"
    size: int = 3,       # lines 用
) -> Context
```

**Context 结构**：
```python
class Context:
    hit: Hit                  # 锚点
    text: str                 # 扩展后的文本
    level: str                # 实际达到的层级（可能因退化不同于请求）
    range: tuple[int, int]    # (start_line, end_line)
    truncated: bool           # 是否被截断
```

**保证**：
- 无 LLM 调用
- 无副作用
- 退化路径可预测（chapter→paragraph→lines）
- 返回 `level` 字段告诉 caller 实际达到的层级

### 7.3 related()

```python
def related(
    keywords: list[str],
    mode: str = "graph",   # "graph" | "dir" | "heading"
    limit: int = 30,       # 2026-07-11: 默认从 20 提高到 30 —— K 线图习惯里 30 周期是长短平衡的数字,足以看清形态且不过量
    stats: str = "basic",  # 2026-07-11: "basic" | "full"
) -> RelatedResult
```

**三种 mode 对应"相邻文档"的三种定义**：
- `graph`：从 `keyword_pairs` 出发 PPR
- `dir`：目录层级邻近
- `heading`：`heading_path` 匹配

**返回结构**(2026-07-11 更新,落地 §1.3 K 线图比喻的两条硬约束):

```
RelatedResult = {
    # 每个邻居携带完整原始参数(§1.3 硬约束:"每根 K 线的参数不能少")
    "neighbors": [
        {
            "term": "墨大夫",
            "co_count": 8000,       # 与 seed 集合的共现次数(原始)
            "df": 576,              # 该 term 全 corpus 的 document frequency(原始)
            "df_ratio": 0.004,      # df / N (纯数字,是事实)
            "npmi": 0.42,           # 归一化点互信息(数学恒等式加工)
        },
        ...
    ],
    # 关于 seed 的客观事实字段(§1.5.1 首选词表)
    "seed_stats": {
        "seed_df": 47007,
        "seed_df_ratio": 0.324,
        "seed_in_graph": true,
    },
    # 关于本次输出的形态统计(§1.5.2 允许的最"高级"表达)
    # 命名铁律:纯数字,不含形容,caller 自行判断形态含义
    "result_shape": {
        "top_k_dropoff_ratio": 0.12,   # top-1 / top-K co_count 比例;低=平原
        "neighbor_df_median": 0.28,    # 邻居 df_ratio 中位数;高=语域词占多数
        "neighbor_count_series": [30000, 28500, 27000, ...],  # 原始序列(stats=full 时)
    },
}
```

**stats 参数控制形态字段丰富度**:

- `stats="basic"`(默认):`top_k_dropoff_ratio` + `neighbor_df_median` 两项
- `stats="full"`:追加 `neighbor_count_series`(完整原始序列) 等更多描述性字段
- **不新增独立 `graph diagnose` 命令** —— 通过 `stats=full` 参数控制字段
  丰富度即可,不需要新命令(2026-07-11 决定)

**形态字段的命名铁律**(§1.5 落地):

1. 每个字段名**纯描述性**(`_ratio` / `_median` / `_series` 等数量单位),
   **无任何形容色彩**(禁用 `_quality` / `_confidence` / `_score`)
2. 字段值**是数字或原始序列**,**不是分类标签**(禁用 `"plateau"` / `"cliff"`)
3. Grga **不对 result_shape 做任何评价性汇报**(禁用 `warning` /
   `escalate_suggestion` 等)

**caller 如何使用**:

- 从 `neighbors[].df_ratio` 分布反推 seed 性质:邻居 df 都高 → seed 可能是
  hub;邻居 df 显著低于 seed → seed 是 domain 展开(见 §1.3 K 线图比喻)
- 从 `top_k_dropoff_ratio` 判断分布形状:低 = 平原/弥散,高 = 断崖/集中
- 具体阈值 caller 自定,grga 不建议、不预判、不 rerank

### 7.4 suggest()（L3，可选）

```python
def suggest(
    query: str,
    hits: list[Hit],
    intent: str = "expand",   # "expand" | "rerank" | "reformulate"
) -> list[Suggestion]
```

**保证**：
- 结果打上 `llm_generated=True`
- 触发投票机制（写入 `llm_extension_votes`）
- 成本可见（返回 token 使用）

---

## 8. 分阶段路线图

按"投入产出比"排序，从小改动开始：

### Phase 0 · 基础扎实
- [ ] 给核心纯函数加 pytest（`search.py` 的查询表达式引擎、`deep_search.py` 的合并去重）
- [ ] 建立 CI 基线（现有 4498 行代码要有测试防线）

### Phase 1 · 命中元数据（一天能做完）
- [ ] `occurrence_index` / `total_occurrences`
- [ ] `line_ratio`
- [ ] `nearby_hits`
- [ ] `matched_terms`

### Phase 2 · 结构感知（Markdown 优先）
- [ ] **选择 Markdown 解析工具**（见 §9 待解决问题）
- [ ] `heading_path` 计算
- [ ] `chapter` 边界识别
- [ ] Structure Index sidecar 建立
- [ ] mtime 触发的懒重建

### Phase 3 · L1 规则扩展
- [ ] `expand(hit, level="lines")` ← 复用 rga -B/-A
- [ ] `expand(hit, level="paragraph")` ← 纯 Python
- [ ] `expand(hit, level="chapter")` ← 依赖 Phase 2
- [ ] `expand(hit, level="document")`
- [ ] 退化路径实现

### Phase 4 · L2 图扩展
- [ ] `keyword_docs` 表 + 离线构建脚本
- [ ] `heading_docs` 表 + 离线构建脚本
- [ ] PPR 实现（简单的 power iteration）
- [ ] `related()` API 三种 mode

### Phase 5 · L3 LLM 扩展 + 反哺闭环
- [ ] `suggest()` API
- [ ] `llm_extension_votes` 表
- [ ] 投票达阈值后写入 `keyword_pairs` 的离线任务

### Phase 6 · Skill 化
- [ ] 把这些动词包装成 QwenPaw Skill
- [ ] 编写 SKILL.md（agent 使用指南）
- [ ] 添加使用示例（caller agent 组合 search/expand/related 的典型模式）

### Phase 7+ · 待完善
- [ ] 评估体系（离散指标 + agent-level 端到端）
- [ ] 代码结构感知（tree-sitter，非 md 场景）
- [x] 其他格式（PDF/Office）的行段兜底优化（2026-07-11：expand / graph.build 统一经
      `grga.file_read.read_file_lines` 读取，非文本 suffix 路由到 `rga --passthru`，
      行号与 search 对齐；PDF/Office 结构解析仍留白，`expand --level chapter` 对非 md 继续走退化链）

---

## 9. 待解决问题（记账）

### 9.1 Markdown 解析工具选型 ⚠️
**问题**：验证过 `markdown-to-data` v2.0.1，**代码块内的 `#` 会被误识别为 heading**（示例：` ```python\n# not a heading\n``` ` 会解析出一个 level 1 header）。**不能作为严肃场景的解析器**。

**候选方向**：
- Rust CLI 工具（如 `pulldown-cmark-cli`、`mdcat`），性能好、独立进程
- Python 库替代：`marko`、`mistune`、`markdown-it-py`（都需要评估代码块处理）
- tree-sitter-markdown（更严谨，但集成成本高）

**要求**：
- 必须正确处理 fenced code block
- 必须能拿到 heading 的**行号**（不只是 AST 结构）
- 输出 JSON 便于 sidecar 存储
- 独立进程/子进程调用最好，不拖累 ripgrep-all

**决策待定**。

### 9.2 评估体系
今天没深入讨论。留待未来：
- 离线离散指标（recall@K、扩展精准度）
- Agent-level 端到端（caller 使用体验）
- 图沉淀质量评估（L3 → L2 反哺后图的准确性）

### 9.3 其他未考虑到的问题
承认设计不可能一次完美，Phase 落地过程中会持续暴露新问题，届时增补。

---

## 10. 与调研过的论文/项目的对照

| 项目 | grga v2 借用的思想 | 主动放弃的部分 |
|---|---|---|
| **HippoRAG** | Synonymy edge 思想（同义词自动建边，未来考虑） | OpenIE 抽取（太贵） |
| **LightRAG** | 双 keyword（low + high level）分层查询思想 | 全 LLM 建图 |
| **LinearRAG** | 分层搜索 + 早停（grga 已有）；矩阵化多跳（未来考虑） | spaCy NER 依赖 |
| **TIGRAG** | 关键字共现图 + PPR | Token 粒度（太细） |
| **LightMem** | SLM 结构化决策；效用累积投票；反思闭环 | 三层记忆（对场景太重） |
| **FTS5 微信文章** | 密度滑窗（nearby_hits）；heading_path；anchor slug | SQLite FTS5（rga 已够用） |

---

## 11. 演进哲学

引用讨论中确认的两句话作为北极星：

> "**参考百家，但走自己的路。**"
>
> "**没有什么项目是一次完美的。**"

设计规范不是终点，是**当下最好的近似**。Phase 落地中遇到的问题，会以增量方式补进本文档。

---

## 附录 A · 讨论过程要点索引

本文档由以下讨论沉淀（时间：2026-07-07）：

1. **grga v1 现状分析** → `knowledge_base/projects/grga-analysis.md`
2. **用户 5 条哲学表达** → `PROFILE.md`
3. **三个决策确认**：Skill 用途 / 图离线整理 / caller 反思 → `PROFILE.md`
4. **命中扩展维度提出**（用户第 5 条哲学补充）
5. **三层扩展模型确认**（L1/L2/L3 分离实现）
6. **`markdown-to-data` 验证失败** → 记账为 §9.1 待解决问题

## 附录 B · 术语表

- **Hit**：ripgrep 返回的单条命中，v2 增加元数据
- **Context**：expand() 返回的上下文包络
- **Anchor**：hit 作为锚点，用于计算 expand 边界
- **Sidecar**：与文档同路径的结构索引 JSON 文件（`.grga/structure/...`）
- **PPR**：Personalized PageRank，从种子节点做图上加权游走
- **反哺**：L3 LLM 扩展的成功结果沉淀到 L2 图的机制
- **投票机制**：反哺的防污染门槛（同一 keyword pair 需被多次输出才写入图）
