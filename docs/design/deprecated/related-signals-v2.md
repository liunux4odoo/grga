# related 信号契约 v2

> **⚠️ 已归档（2026-07-13）**：v2 契约随 v9 keyword-primary 架构被 v3 取代。
> `related --mode graph` 不再返回文档 + PPR 元数据，改为返回候选关键字 +
> 关联证据；`--mode dir` 移除；PPR / `keyword_chunks` / `expansion` /
> `keyword_contributions` 全部拆除。现行契约见
> `docs/design/related-signals-v3.md` + `schemas/related-signals-v3.json`。
> 本文件仅作历史留痕。

> **版本**：v2.2（2026-07-12 · 同日第二次 refactor）
> **状态**：已实现（`src/grga/related.py` + `related --json --mode graph`
> 的 `expansion` 顶层块 / 每条 `results[]` 内嵌 `keyword_contributions`）
> **替代**：`docs/design/deprecated/trace-signals.md`（v0.2, 2026-07-09）
> **前置**：`docs/design/v2-design.md` §4.2 / §7.3；AGENTS.md §信号暴露原则
>
> **变更史**（未发版，同日三次收敛）：
> * **v2.0**：撤 trace-signals v1（`graph_actions` op log）。
> * **v2.1**：把 `Related.graph_signals` 整块并入顶层 `expansion`，
>   seeds/expanded_keywords 各带 `chunk_df`；`neighbor_edges` 数组
>   （每条冗余带 `a=seed`）折叠为 `expanded_keywords[].edges_to_seeds`；
>   删除 `--graph-signals-limit` CLI 参数。
> * **v2.2**：把 `edges_to_seeds` 列表拆封——hop-1 邻居的**最强直接边**
>   的 `npmi` / `cooccur_count` 直接内联到 `expanded_keywords[]` 条目，
>   `source` 只在 ≠ `chunk_cooccur` 时出现（零值省略），`via_seeds`
>   只在多 seed 查询中的多边邻居里出现。见 §2.7。

---

## 0. TL;DR

- **v2.0 移除**：`trace_signals_version` + `graph_actions` 顶层字段。
- **v2.1 移除**：`Related.graph_signals` 块与 `--graph-signals-limit` CLI 参数。
- **v2.2 移除**：`edges_to_seeds` 列表包装（内联到 entry 自身）。
- **v2.2 新增**：`expanded_keywords[i]` 直接携带最强边的 `npmi` /
  `cooccur_count`；`source` 与 `via_seeds` 走零值省略策略。
- **一处收信号**：seed 的 `chunk_df` 在 `expansion.seeds[i]`；每个 PPR
  邻居的所有信号（`ppr_score`, `hops_from_seed`, `chunk_df`, `npmi`,
  `cooccur_count`, `source`, `via_seeds`）扁平在 `expansion.expanded_keywords[i]`。
- **文档级证据**：每条 `results[]` 内嵌 `keyword_contributions`
  （文档级 per-kw 命中）。
- **不动**：hot-path 动词（`probe` / `search` / `expand` /
  `deep-search`）的 `--json` 输出。
- **caller 收益**：拿到返回体后 ~5 行 Python 就能过滤"仅由 PPR 扩展
  通用词命中"的假阳性文档（三体噪声一类），无需自建 DB 查询。

---

## 1. 为什么切（问题证据）

生产 case：《坠魔谷》一节的稀有关键字 `苍坤` 作 seed，`related --mode
graph --json` 返回结果包含《三体》，`score = 0.03931`，
`keywords = [之门, 上古, 方法, 当年, ...]`。事实：三体全文不含 `苍坤`
相关内容，被拉入是因为 PPR 从 `苍坤` 扩展的邻居里混入了 `上古 / 方法 /
当年` 这类通用词，恰好命中三体。

**caller 用旧契约无法识别**这条噪声：
- `keywords[]` 只列名字不列强度，看不出 `苍坤` 缺席；
- `graph_actions` 里 `kw:上古 → file:三体` 的边权 = `kw:上古` 的 PPR 分
  = `0.00234`，与 `kw:上古 → file:凡人修仙传` 完全相同（关键字级权重，
  非文档级），完全不导向"哪条更可信"的判断；
- `file:` 级 `score_node` 只重复了 `related[].score`，本就派生可得。

**结论**：旧契约瞄准的是"可视化图游走轨迹"，与 caller 判断关联性的实际
需要错位。

---

## 2. 新契约结构

### 2.1 `related --json --mode graph`（envelope）

**单 seed 查询**（大部分调用长这样）：

```jsonc
{
  "results": [ /* Related[]，见 §2.2 */ ],
  "crossed_scopes_preview": null,
  "expansion": {
    "seeds": [
      { "kw": "苍坤", "chunk_df": 3 }
    ],
    "expanded_keywords": [
      {
        "kw": "上古",
        "ppr_score": 0.00234,
        "hops_from_seed": 1,
        "chunk_df": 45312,
        "npmi": 0.42,                                 // 内联最强直接边
        "cooccur_count": 3
        // source 省略：默认 "chunk_cooccur"
        // via_seeds 省略：单 seed 查询不需要
      },
      {
        "kw": "方法",
        "ppr_score": 0.001894,
        "hops_from_seed": 2,
        "chunk_df": 128940
        // npmi / cooccur_count 省略：与该 seed 无直接 keyword_pairs 边
      }
    ],
    "total_chunks_in_scope": 100000
  }
}
```

**多 seed 查询**（例：`[brew, ripgrep]`），只在**同一邻居桥接多个 seed**
时才出现 `via_seeds`：

```jsonc
{
  "kw": "pandoc",
  "ppr_score": 0.166,
  "hops_from_seed": 1,
  "chunk_df": 2,
  "npmi": 0.999,                                     // 最强边 npmi
  "cooccur_count": 2,
  "via_seeds": ["ripgrep", "brew"]                   // 按 npmi desc 排序
}
```

**`expansion` 字段规则**：
- `seeds[]`：`{kw, chunk_df?}` 数组，`kw` 按 caller 传入顺序保留
  （case-folded）。`chunk_df` 缺席条件：seed 不在 `keyword_chunks`
  （拼错或未 index）。
- `expanded_keywords[]`：所有参与本次 PPR 打分的**非-seed** 关键字，
  排序 `(-ppr_score, kw)`。每项字段：
  * **必有**：`kw`, `ppr_score`。
  * **hops_from_seed**（可选）：BFS 距离；缺失表示不与任何 seed 连通。
  * **chunk_df**（可选）：同 seeds 规则。
  * **npmi + cooccur_count**（一起出现或一起缺席）：**最强**的直接
    seed 边（npmi desc, cooccur desc, seed asc 排序取第一条）。存在
    条件：`keyword_pairs` 表里该 neighbour 与至少一个 seed 有直接边。
    注意 `_edges_by_neighbor` 查询**不受 `min_npmi` 过滤**，所以有可能
    出现"hop >= 2 但仍带 npmi"的情况——含义是"存在直接边，但因 npmi
    低于阈值被 PPR 绕开"，是一种"弱直接连接"信号。`npmi` 可为
    `null`（`llm_promoted` 边）。
  * **source**（可选）：**仅在 ≠ "chunk_cooccur" 时 emit**（即
    `llm_promoted`）。缺席 = `chunk_cooccur`（零值省略）。
  * **via_seeds**（可选）：**仅在多 seed 查询里且邻居有 ≥ 2 条直接边**
    时出现，列出这些 seed 名（按 npmi desc）。单 seed 查询与单边邻居
    都省略（无歧义）。
- `total_chunks_in_scope`：scope 内 chunk 总数，`chunk_df` 的分母。
- 空邻居时：`expanded_keywords = []`，`seeds` 与
  `total_chunks_in_scope` 仍在。

### 2.2 `results[]` — `Related`（graph 模式增字段）

```jsonc
{
  "doc_path": "…三体.txt",
  "score": 0.03931,
  "mode": "graph",
  "reason": "ppr",            // 或 "direct-seed"（PPR 无邻居时的 fallback）
  "keywords": ["上古","方法","之门"],    // sorted(list) — 命中该文档的关键字
  "directory": "…",
  "qualified_path": "…",
  "keyword_contributions": [
    { "kw": "方法", "occurrences": 5 },
    { "kw": "上古", "occurrences": 3 },
    { "kw": "之门", "occurrences": 2 }
    /* 排序：occurrences desc, kw asc */
  ]
}
```

**v2.0 里的 `graph_signals` 块已删除**：它承载的是查询级常量（同一次
`related` 调用里所有 `Related` 引用同一个 dict），塞进 per-doc 层是无
杠杆的复制。所有关键字级图信号现在只在顶层 `expansion` 一处出现。

**`keyword_contributions` 字段规则**：
- 只在 `mode=graph` 下出现。`dir` / `heading` 模式不 emit。
- 每条 = 该 kw 在该 doc（该 scope 内）的
  `SUM(keyword_chunks.occurrence_count)`。
- 排序稳定：`(-occurrences, kw)`。
- `set([c.kw for c in keyword_contributions]) == set(keywords)`（不变量）。
- 空文档不出现在 `results[]`；一条 `Related` 的
  `keyword_contributions` 至少 1 项。

### 2.3 `related --json --mode dir|heading`

```jsonc
{
  "results": [
    {
      "doc_path": "…",
      "score": 0.5,
      "mode": "dir",           // 或 "heading"
      "reason": "dir:install",  // 或 "shared-heading"
      "keywords": ["…"],
      "directory": "…",
      "qualified_path": "…"
      /* 不携带 graph_signals / keyword_contributions / expansion */
    }
  ]
}
```

`dir` / `heading` 模式不消费 PPR，"per-kw occurrence"没有语义意义，
`expansion` 亦然。返回体是**纯 results 数组的对象**，不再包裹
trace-signals envelope。

### 2.4 `suggest --json`

```jsonc
{
  "intent": "expand",
  "keywords": ["deep learning","backprop"],
  "queries": [ /* … */ ],
  "keyword_pairs": [ /* … */ ],
  "ranking": [], "scores": {}, "reason": "…",
  "llm_generated": true,
  "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0,
  "model": "…",
  "votes": { /* … */ }
  /* 不再有 trace_signals_version / graph_actions */
}
```

`Suggestion.keywords` 就是 LLM 提出的 `kw:*` 列表；`llm_propose` action
是**纯派生**（AGENTS.md §信号暴露原则第 1 条），撤除。

### 2.5 hot-path 动词

`probe` / `search` / `expand` / `deep-search` 的 `--json` 输出**一字节
未变**。改这些动词的返回体前先看 `docs/api.md` 相应节点。

---

## 3. caller 用法：过滤三体假阳性

```python
seeds = {s["kw"] for s in payload["expansion"]["seeds"]}
kept = [
    r for r in payload["results"]
    if any(c["kw"] in seeds for c in r["keyword_contributions"])
]
# 三体这类"全靠 PPR 扩展词命中"的文档在这里被过滤
```

再叠一层 hub 剪枝（`chunk_df` / 总 chunk 数比例）：

```python
total = payload["expansion"]["total_chunks_in_scope"]
hub_kws = {e["kw"] for e in payload["expansion"]["expanded_keywords"]
           if e.get("chunk_df", 0) / max(total, 1) > 0.1}  # 阈值 caller 定
```

按直接边强度二次过滤——v2.2 里 `npmi` 已经是顶层字段，一行搞定：

```python
strong = [e for e in payload["expansion"]["expanded_keywords"]
          if (e.get("npmi") or 0) > 0.5]
```

**关键**：策略是 caller 的决定，grga 只暴露信号。`ppr_score` /
`chunk_df` / `npmi` / `hops_from_seed` 四路信号一处收，caller 拼装。

---

## 4. 契约影响

| 位置 | 影响 | 处理 |
|---|---|---|
| `related --json --mode graph` 顶层 | 去 `trace_signals_version` / `graph_actions`，加 `expansion` | **BREAKING** |
| `related --json --mode dir|heading` 顶层 | 去 envelope，直接 `{"results": []}` | **BREAKING** |
| `results[].keyword_contributions` | 新增（仅 graph 模式） | **BREAKING**（字段新增可视为 additive，但配合上面属于同一次 major） |
| **v2.1** `results[].graph_signals` | 删除；查询级信号折叠到顶层 `expansion` | **BREAKING** |
| **v2.1** `expansion.seeds` | `List[str]` → `List[{kw, chunk_df?}]` | **BREAKING** |
| **v2.1** `expansion.expanded_keywords[i]` | 增 `chunk_df` + `edges_to_seeds` | additive（在同一次 major 内） |
| **v2.1** `expansion.total_chunks_in_scope` | 新增（原在 `graph_signals` 内） | additive |
| **v2.1** `--graph-signals-limit` CLI 参数 | 删除 | **BREAKING**（无 caller 已知使用） |
| **v2.2** `edges_to_seeds` 数组 | 删除；最强边的 `npmi`/`cooccur_count` 内联到 entry | **BREAKING**（同版本内演进，未发版）|
| **v2.2** `source` 字段 | 只在 != `chunk_cooccur` 时 emit（零值省略）| additive-compatible（新增缺席语义）|
| **v2.2** `via_seeds` 字段 | 新增；仅多 seed + 多边邻居出现 | additive |
| `suggest --json` 顶层 | 去 `trace_signals_version` / `graph_actions` | **BREAKING** |
| `search` / `probe` / `expand` / `deep-search --json` | 无变化 | — |
| `Suggestion.graph_actions` 数据类字段 | 删除 | 内部 API，无外部消费者 |
| `grga.trace` 模块 | 删除 | 内部 API |
| `schemas/trace-signals-v1.json` | 删除 | 契约层归档 |
| `schemas/trace-viz-session-v1.json` | 删除（trace-viewer skill 一并归档） | 见 §5 |

版本号：因是 pre-1.0 项目且现有 caller 尚在少量，直接在下一次发版
CHANGELOG 里声明为 breaking，不设兼容路径。

---

## 5. trace-viewer 相关工件的处置

`src/grga/skills/grga/workflows/trace-viewer/` 与
`src/grga/skills/grga-trace-viz/` 完全依赖旧 `graph_actions`。因新契约
不再 emit 该字段，这两个 skill 事实上失效。

**处理方式**：将两者从内置 skills 树中移除（不留 broken 副本），并在
`skills/grga/SKILL.md` 相应引用节段声明"trace-viewer 已随 trace-signals
v1 一并归档，参见 `docs/design/deprecated/trace-signals.md`；若未来重新
引入图游走可视化，建议先落地 `docs/design/related-signals-v2.md` 未覆盖
的新契约再实现 viewer"。

---

## 6. 边界与不变量

| 场景 | 期望 |
|---|---|
| 孤立 seed（图上无邻居） | `expansion.expanded_keywords = []`；`results[]` 走 `direct-seed` 分支，每条仍带 `keyword_contributions`（只含 seed）|
| seed 全为空串 | `expansion = {"seeds": [], "expanded_keywords": [], "total_chunks_in_scope": 0}`；`results = []` |
| cross-scope 模式 | `expansion` 反映跨 scope 后的 PPR；`keyword_contributions` 在每条 result 的 `directory` 视角内统计 |
| 无 PPR 邻居但 seed 命中多文档 | `direct-seed` 分支：`expansion.expanded_keywords = []`，`expansion.seeds` 仍带 `chunk_df`；每条 result 的 `keyword_contributions` 只含 seed |
| 幂等 | 同参二次调用，`expansion` 与所有 `keyword_contributions` 字节一致（浮点四舍五入 6 位后） |

### 6.1 已知细节（不影响契约，v2.1 落地时记录）

来源：2026-07-12 v2.1 落地后的代码审阅。这些点**符合当前契约与设计意
图**，不算 bug，但对未来读者有价值——避免重复思考同一个问题。

1. **多 seed 时 seed↔seed 边被静默丢弃**。`_edges_by_neighbor` 的 SQL
   WHERE 会匹配到 `keyword_pairs` 中两端都在 seeds 集合的行；orient 规
   则把其中一端塞进 `by_neighbor[seed_B]`。而 `_build_expansion` 在生
   成 `expanded_keywords` 时按 `kw not in seed_set` 过滤，所以这类条
   目永远不进最终返回体。**结果对外一致**，仅有几行浪费的 DB 结果处
   理。若未来 caller 需要多 seed 语义分析（"这两个 seed 有多相关"），
   可以加 `expansion.seed_edges: List[{a, b, npmi, cooccur_count,
   source}]` 字段（additive MINOR），无需回退当前设计。

2. **cross-scope 模式下 `chunk_df` 与 `total_chunks_in_scope` 是全库
   统计**。cross-scope 时 `signals_scope_key=None`，二者反映的是"当前
   DB 中所有已 index scope 的合集"，而非任何单个 scope。这与"结果跨
   多个 scope"的语义一致；`crossed_scopes_preview` 已单独说明其他
   scope 的分布，不需要重复。caller 若要**分 scope** 的 `chunk_df`，
   需要为每个 scope 单独调一次 `related`（这是设计上的选择而非限
   制——保持 grga 的 stateless 与单次调用可预测性）。

3. **`--graph-signals-limit` CLI 参数移除是真 breaking**。任何脚本
   显式传该 flag 会被 typer 拒绝并 exit 2（unknown option）。v2.0 → v2.1
   同版本内落地，CHANGELOG 与 decisions.md 均记录；无 fallback。

4. **只暴露"直接边"**。多跳路径的强度已融进 `ppr_score`，不重新
   emit——避免走回 trace-signals v1 的 linear op log 老路。若 caller 真
   要看"这个远邻怎么和 seed 连上的"，应查 `hops_from_seed` + 用
   `graph inspect` 反向探针。

5. **v2.2 · `hops_from_seed > 1` 也可能带 `npmi`**。`_edges_by_neighbor`
   查 `keyword_pairs` 时**不受 `min_npmi` 过滤**（跟 v2.1 一样，保留
   完整边信息），而 PPR / BFS 用的是过滤后的图。因此存在"直接边低于
   `min_npmi`、PPR 绕开走长路径" 的邻居——此时 `hops_from_seed >= 2`
   但仍带内联 `npmi`（数值必然 < `min_npmi`）。含义是"这条边真实存在
   但被判定为噪声连接"，是一种给 caller 的**弱连接信号**，允许 caller
   在需要时反过来相信这条边（例如放宽 `--min-npmi` 重试）。

---

## 7. 未来演化

- **新加字段**（如 `keyword_contributions[].chunk_df`）：走 additive
  MINOR bump，但**只在 caller 侧派生代码明显重复的证据触发**（AGENTS.md
  §信号暴露原则）。
- **多 seed 多边的完整 npmi 明细**：v2.2 保留 `via_seeds` 名字列表但丢
  弃每条边的独立 npmi。若真的出现多 caller 需要"这个邻居到 seedA 是
  0.8，到 seedB 是 0.3"的用例，加 `via_seed_edges: List[{seed, npmi}]`
  additive 字段（不复活旧 `edges_to_seeds` 列表）。
- **重新引入图游走可视化**：如果真的成为需求，应先定义"消费者拿不到的
  内部图状态是什么"再讨论；不要复活旧的 `traverse_edge`/`score_node` op
  log 契约，因为它包含大量派生冗余。

---

## 8. 一句话给未来读者

**v2 契约把"可视化图游走"重构为"文档 × 关键字命中矩阵 + 查询级 PPR
扩展表"。当我们向 caller 暴露信号时，先问"消费者能否自己派生"，能则
不加，不能才加——`ppr_score` / `hops_from_seed` / per-doc-per-kw
`occurrences` 是仅有的三样 caller 真的拿不到的东西。**
