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

### 1.2 关键定位选择

| 维度 | 选择 | 为什么 |
|---|---|---|
| 目标用户 | AI Agent（非人类） | Skill + CLI 决定 |
| 检索原理 | 关键字 + 简单图 | 轻依赖信念 |
| 图粒度 | **关键字级**（不是 token、不是 entity） | 太细噪声大、太粗依赖 NER |
| 反思循环 | Caller 组织 | grga 只做原语 |
| 图更新 | 离线定期整理 | 简单、可控、避免在线消耗 |
| 语言/格式 | 结构化文档优先（MD/代码），其他退化到行段 | 鲁棒性优先 |

### 1.3 反定位（明确不做的）

- ❌ Web 搜索前端 / RAG 应用框架（如 LangChain）
- ❌ 面向人类的可视化搜索工具（GUI 保留但不迭代）
- ❌ 强 KG 系统（不做三元组抽取、不做 OpenIE）
- ❌ 内嵌 embedding / 向量索引
- ❌ 内部黑箱反思循环（不做 grga 自主的多轮）

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
    limit: int = 20,
) -> list[Related]
```

**三种 mode 对应"相邻文档"的三种定义**：
- `graph`：从 `keyword_pairs` 出发 PPR
- `dir`：目录层级邻近
- `heading`：`heading_path` 匹配

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
- [ ] 其他格式（PDF/Office）的行段兜底优化

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
