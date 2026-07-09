# grga trace 信号扩展规范

> **版本**：v0.2（2026-07-09，极简版）
> **状态**：已实现（`src/grga/trace.py` + `related`/`suggest` 的 `--json` 输出；
> 契约 schema 见 `schemas/trace-signals-v1.json`，测试见 `tests/test_trace_signals.py`）
> **前置**：`docs/design/v2-design.md`（v2 五动词原语）
> **背景**：为支持 caller 端"agentic search 过程可视化"，grga 需要暴露一部分
> **caller 无法从现有返回值派生的内部图动作**。grga 本体依然 stateless，且
> **只暴露 caller 拿不到的信息**，不做已有信息的重述。

---

## 0. TL;DR

- 只有两个动词受影响：**`related`** 和 **`suggest`**。
- 只新增 3 个动作：`traverse_edge` / `score_node` / `llm_propose`。
- 其他动词（`probe` / `search` / `expand` / `deep-search`）**一字节都不变**。
- 顶层字段 `trace_signals_version` + `graph_actions` 只在受影响的两个动词的
  `--json` 输出里出现。
- caller 可视化所需的其他信号（命中文件、命中章节、关键字激活）由 caller
  **自行从 `matches` 派生**，成本约 10 行 groupby。

---

## 1. 决策依据（为什么这么切）

### 1.1 只暴露 caller 拿不到的信息

对 v2 原本设想的 10 个 op 做过一次"caller 能否自己派生"的筛查：

| op | caller 能派生？ | 靠什么派生 |
|---|---|---|
| `activate_keyword` | ✅ 能 | 从 query 解析 |
| `hit_file` | ✅ 能 | `groupby matches.path` |
| `hit_heading` | ✅ 能 | `groupby matches.heading_path` |
| `co_activate` | ✅ 能 | `hit_file × matched_terms` 交叉 |
| `expand_context` | ✅ 能 | expand 返回值已含 level / range |
| `seed_keyword` | ✅ 能 | 就是 `--seed` 参数本身 |
| **`traverse_edge`** | ❌ 不能 | related 内部图游走，外部不可见 |
| **`score_node`** | ❌ 不能 | related 内部 PPR 分数，外部不可见 |
| **`llm_propose`** | ⚠️ 半能 | suggest 只返新 query，缺 "为什么" |

**只有 3 个 op 是 caller 真正拿不到的**。其余 7 个把它们放到 grga 里做，
不是"帮 caller 省事"，而是**把 caller 侧派生工作转嫁到 grga 侧永久维护**。
grga 是被 N 个 caller 共用的公共基础设施，往它上面加负担的杠杆是反向的。

### 1.2 保护热路径

`search` / `probe` 是最高频的动词，也是 golden test 面积最敏感的地方。
让它们**完全不 emit**，意味着改内部算法时 golden test 不会被 trace 契约锁死。

### 1.3 可视化不因此损失

图扩展的核心可视化价值就在 **`traverse_edge`**（关键字→关键字、关键字→文件
的图游走轨迹）。这部分保留了，viewer 依然能画出完整的搜索演化过程；命中
高亮层由 viewer 直接消费 `matches` 派生。

---

## 2. 使用示例

### 2.1 `related --seed chunking --seed rerank --json`

```jsonc
{
  "ok": true,
  "related": [ /* 原样 */ ],
  "trace_signals_version": "1.0",
  "graph_actions": [
    {
      "op": "traverse_edge",
      "nodes": ["kw:chunking", "kw:embedding"],
      "meta": {"edge_kind": "co_occurrence", "weight": 0.42, "hop": 1}
    },
    {
      "op": "traverse_edge",
      "nodes": ["kw:embedding", "file:docs/rag/embed.md"],
      "meta": {"edge_kind": "keyword_doc", "weight": 0.31, "hop": 2}
    },
    {
      "op": "score_node",
      "nodes": ["file:docs/rag/embed.md"],
      "meta": {"algo": "ppr", "score": 0.31, "hops_from_seed": 2}
    }
  ]
}
```

### 2.2 `suggest --json`

```jsonc
{
  "ok": true,
  "suggested_query": "chunking strategy rerank",
  "trace_signals_version": "1.0",
  "graph_actions": [
    {
      "op": "llm_propose",
      "nodes": ["kw:strategy"],
      "meta": {
        "origin_query": "chunking rerank",
        "rationale": "扩展 chunking 的实现维度"
      }
    }
  ]
}
```

### 2.3 `probe` / `search` / `expand` / `deep-search`

**无变化**。返回体不新增 `trace_signals_version`，不新增 `graph_actions`。

---

## 3. 节点 id 命名空间（v1 冻结）

只允许 3 个前缀出现在 v1 的 `graph_actions.nodes` 里：

| 前缀 | 含义 | id 主体 |
|---|---|---|
| `kw:` | 关键字节点 | 归一化后的关键字字面量（与 `keyword_docs.term` 一致） |
| `file:` | 文件节点 | 相对 scope 根的 POSIX 路径 |
| `head:` | 章节节点 | `<file 相对路径>#<slug>`（仅在 related 命中章节节点时使用） |

**约束**：

- id 主体不含空白与控制字符；出现时用 URL-encode。
- 保留 `chunk:` / `scope:` / `sym:` 等前缀**作为未来扩展词汇表**，v1 实现里
  不允许出现。任何 caller 若观察到未列前缀应视为 grga 侧 bug 或未来版本。

**已知 v1 限制（cross-scope 节点冲突）**：

`file:` / `head:` 的 id 主体是**相对 scope 根**的路径，因此隐含假设"一次
trace 只覆盖单一 scope"。在 `related --mode graph --cross-scope`（默认关闭
的 escalation）下，结果集可能同时含有多个 scope 里**相对路径相同**的文档
（如两个 `README.md`）。它们会塌缩成同一个 `file:` 节点 id，导致：

- 同一 `file:` 节点出现多条 `score_node`（违反 §5.1"每节点一条"的隐含
  假设）；
- 来自不同 scope 的 `traverse_edge(kw→file)` 融合到同一目标节点。

注意这**只影响 trace 层的节点身份**；搜索结果本身（`Related.qualified_path`）
已正确按 `(directory, doc_path)` 去冲突，不受影响。消费者若要跨 scope 可视化
或去重，应在采集时避免 `--cross-scope`，或把 cross-scope trace 视为近似。

彻底修复需要一个**可移植的 scope 身份方案**（不能用绝对路径，否则 trace 不
再跨机器确定）。该方案是 MINOR bump（见 §10 的 `meta.scope` 占位），**待真实
caller 证据触发**再定，不在 v1 预判。

---

## 4. 动作类型（v1 冻结）

```
{
  "op": <string>,
  "nodes": [<node_id>, ...],   // 见下表
  "meta": {<自由 kv>}
}
```

| op | nodes 语义 | 必需 meta key | 产生自 |
|---|---|---|---|
| `traverse_edge` | 恰好 2 个，顺序 `[from, to]` | `edge_kind`, `weight`, `hop` | related |
| `score_node` | 恰好 1 个 | `algo`, `score`, `hops_from_seed` | related |
| `llm_propose` | ≥ 1 个 `kw:*` | `origin_query`, `rationale` | suggest |

**未来扩展词汇表**（v1 不实现，caller 不应期望看到）：

`activate_keyword` / `seed_keyword` / `hit_file` / `hit_heading` /
`expand_context` / `co_activate` / `llm_replace`

这些词汇进入实现前，需先积累"caller 派生代码在多个仓库重复且明显笨拙"
的证据，再讨论是否上收。

---

## 5. 契约细节

### 5.1 `related`

- 遍历过程中，**每一条被走过的边** emit 一个 `traverse_edge`；hop 从 1 起。
- **每一个被算分的节点** emit 一个 `score_node`（含 0 分节点，方便 viewer
  显示"考察过但没入选"）。
- 结果集里被采纳的节点不额外 emit "adopted" 类动作；caller 靠 `related`
  数组和 `score_node` 交叉判断即可。

### 5.2 `suggest`

- 当 LLM 返回非空建议时，**必须为每个建议 emit 一个** `llm_propose`，`nodes`
  为 LLM 提出的新 `kw:*`。
- 当 LLM 返回空建议（无新 kw）时，`graph_actions = []`，按 §6 边界规则处理，
  **不视为错误**。这包括 `intent=rerank`——它按设计不产出新关键字。
- `meta.rationale` 允许为空字符串但字段必须存在。
- **不 emit `llm_replace`**——是否用新 query 替换旧 query 是 caller 的
  反思决定，grga 不越界记录。

### 5.3 其他动词

**明确不动**。任何在 `probe` / `search` / `expand` / `deep-search` 里出现
`graph_actions` 字段的实现都视为违反本规范。

---

## 6. 边界行为

| 维度 | 场景 | 期望行为 |
|---|---|---|
| 空集 | related 从孤立种子出发，无邻居 | `graph_actions = []`（空数组，非缺字段） |
| 空集 | suggest LLM 返回空建议 | `ok: true` 且 `graph_actions = []`；不视为错误 |
| 单元素 | related 只有 1 跳 1 边 | 正常 emit 1 个 `traverse_edge` + 若干 `score_node` |
| 自反 | related 结果含 seed 本身 | grga **不过滤**；`score_node` 可能对 seed 出现 |
| 降级 | related 图索引缺失 | `graph_actions = []`；`related` 数组本身按现有逻辑降级 |
| 失败 | verb 返回 `ok: false` | `graph_actions` 字段可缺省，caller 应容忍缺失 |
| 幂等 | 相同参数连续两次调用 | `graph_actions` 序列逐字节一致（时间字段除外，本 schema 无时间字段） |

---

## 7. 版本与兼容

- 顶层 `trace_signals_version: "MAJOR.MINOR"`。
- **MINOR bump**：新增 op / 新增前缀 / 新增 meta key。caller 应忽略未知字段。
- **MAJOR bump**：删/改现有 op 语义、删/改前缀、改变 nodes 元数语义。
- 每次发版：CHANGELOG 显式列出 `trace_signals_version` 变化。
- 从 v0.1 到 v0.2 的收窄（10 op → 3 op）不视为破坏性变更，因为 v0.1 从未
  实现发布。

---

## 8. 验收标准

1. `grga related --json` 和 `grga suggest --json` 输出**均包含**
   `trace_signals_version` + `graph_actions`（后者可为空数组）。
2. `grga probe --json` / `grga search --json` / `grga expand --json` /
   `grga deep-search --json` **不含**这两个字段（回归测试要验证不新增）。
3. 提供 `schemas/trace-signals-v1.json`，related/suggest 的集成测试用它做
   契约校验。
4. §6 的每一行都有一条 pytest 用例覆盖。
5. Golden 快照：给定固定 fixture 仓库 + 固定命令序列，`graph_actions`
   逐字节一致（related 内部若换算法，需同步更新 golden 并在 PR 里说明）。
6. README / SKILL.md 增加"trace 信号"小节，指向本规范；并说明**热路径动词
   的返回体格式保持不变**。

---

## 9. 交付顺序

1. 冻结本规范（进入 `docs/design/`）
2. 起草 `schemas/trace-signals-v1.json`（3 个 op、3 个前缀）
3. 在 `models.py` 或新 `trace.py` 定义 `GraphAction` 数据类和 op / 前缀常量
4. `related.py` 改造：在图游走关键点收集 action，返回时拼进 JSON
5. `suggest.py` 改造：LLM 结果生成后拼 `llm_propose`
6. 三份测试：schema 校验、边界表用例、golden 快照
7. 观察一段时间，若发现 caller 派生代码在多仓库重复且笨拙，**基于证据**
   讨论是否把某个 op 从"未来词汇表"上收到实现层

---

## 10. 未来预留（v1 明确不做）

- 上收任何 §4 的"未来扩展词汇表"到实现层——**需要证据触发**
- `meta.scope`（planned, MINOR bump）：给 `traverse_edge` / `score_node`
  的 meta 加一个**可移植**的 scope 身份 token，解决 §3"已知 v1 限制"里
  cross-scope 下 `file:` / `head:` 节点身份塌缩的问题。token 具体形式
  （scope 别名 / 稳定哈希 / basename）**待 caller 侧派生代码笨拙的证据**
  再定，不预判。占位在此，实现里不允许出现。
- `parent_trace_id` / `handoff`：跨 caller 交接
- 代码符号节点 `sym:` 与 tree-sitter 集成
- 实时流式 emit（SSE / stream）
- grga 侧的 trace 文件写入（永远由 caller 做）

---

## 11. 为什么不做 cheap 层（决策留痕）

设计过程中一度提出过"分层 emit"方案：默认 emit `activate_keyword` /
`hit_file` / `hit_heading` 等 cheap 层，`--trace-signals=full` 才 emit
`traverse_edge` 等。最终**否决**，理由记录如下：

1. **cheap 层的信息 caller 全部能自己派生**（见 §1.1 表格）。放到 grga
   做本质上是"grga 替 caller 做 caller 该做的事"。
2. **grga 是被 N 个 caller 共用的基础设施**。往共用层加负担、给 N 个
   caller 减负，杠杆方向是反的——除非派生代码复杂到值得抽公共库，否则
   应在 caller 侧解决。
3. **golden test 锁定面积** 是真痛点。cheap 层挂在热路径 (`search`) 上，
   任何算法优化都会破 golden；full 层只挂在 `related`，锁定面积可控。
4. **可视化能力不损失**：图扩展的核心视觉信号在 `traverse_edge`；命中
   高亮属于渲染问题，viewer 直接读 `matches` 即可。

**元原则**：设计公共接口时先问"消费者能不能自己做"，再决定要不要暴露。
不能派生 → 必须暴露；能派生 → 除非有明确复用证据，否则让消费者自己做。
