# grga trace 信号扩展规范（已废弃 · 2026-07-12）

> **状态**：已废弃。本规范定义的 `trace_signals_version` + `graph_actions`
> 契约在 2026-07-12 被移除，参见 `docs/design/related-signals-v2.md`。
>
> **为什么撤**：本规范原为"图游走路线可视化"设计，emit 一条 linear op log
> （`traverse_edge` / `score_node` / `llm_propose`）供 viewer 消费。
> 但实际使用中，caller 的核心痛点不是"画出图走了哪条路"，而是"判断这条
> `related[]` 结果是不是噪声"——尤其是稀有种子经 PPR 扩展后拉入的通用词
> 噪声（三体假阳性一类）。
>
> linear action-log 无法有效表达证据：
> - `file:` 级 `score_node` 与 `related[].score` 逐字节重复；
> - `keyword_doc` 边权重是**关键字级** PPR 分，不是文档级信号，跨文档相同
>   权重会误导 caller；
> - 每条 seed×文档路径 emit 数十条 op，量大但对"筛掉三体"这类判断无用。
>
> 新契约把关键信息重组为两块：
> 1. **顶层 `expansion`**：`{seeds, expanded_keywords[{kw, ppr_score,
>    hops_from_seed}]}`——查询级 PPR 扩展证据；
> 2. **每条 `related[]` 内嵌 `keyword_contributions`**：`[{kw, occurrences}]`
>    ——文档级 per-kw 命中明细。
>
> 二者结合 caller 可在 5 行 Python 内完成"过滤仅由通用词命中的假阳性"，
> 而不需要重演任何图游走。
>
> **`suggest` 层的 `llm_propose`** 也一并撤除：`Suggestion.keywords` 已经
> 携带 LLM 提出的关键字列表，`llm_propose` 是纯派生（AGENTS.md §信号
> 暴露原则第 1 条）。
>
> 契约变更类型：**MAJOR / breaking**。`trace_signals_version` / `graph_actions`
> 两个顶层字段消失，不再是收窄。schema `schemas/trace-signals-v1.json` /
> `schemas/trace-viz-session-v1.json` 已随之移除。
>
> **归档理由**：保留原文供未来 caller 若真的需要"图游走可视化"时对照参
> 考——但重新加回这块 emit 之前，必须先积累多个 caller 的重复派生代码
> 证据。目前证据不足，暂不做。
>
> 以下为原文（v0.2, 2026-07-09）保留不动。

---

