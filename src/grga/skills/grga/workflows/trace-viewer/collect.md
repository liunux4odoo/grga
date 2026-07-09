# trace 累积协议

**面向 caller agent。** 讲清楚一次 grga 会话中，caller 如何把每次 `--json`
输出的 `graph_actions` 追加到 JSONL 文件里，供 `render.py` 消费。

> **本文档冻结 JSONL 记录 shape 作为契约。**`render.py` 依赖此 shape；
> 未来 shape 演进按 `trace_signals_version` 语义化版本。

---

## 何时该攒 trace

只有两种 grga 调用有 `graph_actions`：

- `grga related ... --json`（graph 模式有内容，dir/heading 是空数组）
- `grga suggest ... --json`（有新建议时有内容，rerank 或空建议是空数组）

其他动词（`probe` / `search` / `expand` / `deep-search` / `index build` /
`graph inspect` 等）**不 emit** `graph_actions`。caller 不应把它们的返回
写进 trace JSONL —— renderer 会忽略但会浪费文件空间。

---

## 会话与文件路径

- **一次"会话"** = caller 完成一个用户任务过程中的所有相关 grga 调用
- **session-id** 由 caller 选定，可以是 UUID、时间戳、任务名，只要在文件系统
  安全且当次会话内稳定
- **文件路径**：约定为 `.grga/trace/<session-id>.jsonl`，相对于 caller 的
  工作目录。caller 可以覆盖，但 renderer 只吃 JSONL 文件路径，路径本身对
  语义无影响

---

## JSONL 记录格式（v1，冻结）

每次目标 grga 调用完成后，追加**一行**到 session JSONL：

```jsonc
{
  "ts": "2026-07-09T21:30:00Z",           // ISO 8601 UTC，caller 生成
  "step": 3,                               // 会话内递增的整数，从 1 开始
  "verb": "related",                       // "related" | "suggest"
  "args": {                                // 复现该调用所需的参数
    "seeds": ["chunking", "rerank"],       // related 的 seed 或 suggest 的 hits 摘要
    "mode": "graph",                       // related 专有
    "intent": null,                        // suggest 专有，其他 null
    "directory": "/path/to/scope",         // 该次调用的 -d
    "extra": {}                            // 未来扩展位，可空
  },
  "trace_signals_version": "1.0",          // 从 grga 返回原样复制
  "graph_actions": [ /* ... */ ]           // 从 grga 返回原样复制
}
```

### 字段约束

- **`ts`**：必需，UTC，带 `Z` 结尾。renderer 按时间排序做时间轴
- **`step`**：必需，整数 ≥ 1。同一 session 内**严格单调递增**（不必连续）
- **`verb`**：必需，只允许 `"related"` 或 `"suggest"`
- **`args`**：必需，键必存在但值可为 null / 空对象。renderer 显示在节点详情
  面板里方便复现
- **`trace_signals_version`**：必需，直接从 grga 输出的顶层字段复制
- **`graph_actions`**：必需，数组（可空）。空数组的记录也应写入——它们让
  时间轴上"这一步没扩展新东西"这件事显式可见

### 什么**不该**写进 JSONL

- grga 返回的 `results` / `matches` / `suggested_query` 等业务字段
  — trace JSONL 只关心图动作，业务数据请 caller 另存
- 敏感信息（LLM API key、用户 query 原文如果涉密）——caller 自行脱敏
- 二进制内容

---

## 追加协议

### 单个 caller 单进程

按 step 顺序追加即可。伪流程：

1. caller 记住 `step` 计数器，初值 1
2. 每次目标 grga 调用完成，读 stdout JSON
3. 构造 JSONL 记录（`ts` 用当前 UTC，`step` 用当前计数器，`graph_actions`
   原样搬）
4. 以文本模式 append 一行，末尾加 `\n`
5. `step += 1`

### 并发 caller / 多进程

不建议对同一 session 文件并发写。**每个进程用独立 session-id**。renderer
不做去重也不做合并。

### 崩溃恢复

JSONL 是行原子的：写到一半崩溃最多丢最后一行。renderer 遇到解析失败的行
应跳过并在 stderr 记录，不应终止整个渲染（见 IMPLEMENTATION §边界）。

---

## 示例：三步搜索会话

```jsonl
{"ts":"2026-07-09T21:30:00Z","step":1,"verb":"related","args":{"seeds":["chunking"],"mode":"graph","intent":null,"directory":".","extra":{}},"trace_signals_version":"1.0","graph_actions":[{"op":"traverse_edge","nodes":["kw:chunking","kw:embedding"],"meta":{"edge_kind":"co_occurrence","weight":0.42,"hop":1}},{"op":"score_node","nodes":["kw:embedding"],"meta":{"algo":"ppr","score":0.31,"hops_from_seed":1}}]}
{"ts":"2026-07-09T21:30:05Z","step":2,"verb":"suggest","args":{"seeds":null,"mode":null,"intent":"expand","directory":".","extra":{}},"trace_signals_version":"1.0","graph_actions":[{"op":"llm_propose","nodes":["kw:reranker"],"meta":{"origin_query":"chunking","rationale":"expand strategy"}}]}
{"ts":"2026-07-09T21:30:12Z","step":3,"verb":"related","args":{"seeds":["chunking","reranker"],"mode":"graph","intent":null,"directory":".","extra":{}},"trace_signals_version":"1.0","graph_actions":[{"op":"traverse_edge","nodes":["kw:reranker","file:docs/rag/pipeline.md"],"meta":{"edge_kind":"keyword_doc","weight":0.5,"hop":1}},{"op":"score_node","nodes":["file:docs/rag/pipeline.md"],"meta":{"algo":"ppr","score":0.5,"hops_from_seed":1}}]}
```

`example/sample.jsonl` 提供了一份更完整的样例，可以直接跑 `render.py` 验证。

---

## 与 grga 侧契约的关系

- `graph_actions` 内部结构由 `docs/design/trace-signals.md` 冻结
- 本文档冻结的是**外层封装**（`ts` / `step` / `verb` / `args`）
- 两者独立版本化：`trace_signals_version` 是内层契约，本文档隐含"JSONL
  外层 v1"是外层契约
- 未来外层扩展（比如加 `parent_session_id`）走 MINOR bump；破坏性改动走
  MAJOR

---

## 边界情况

| 场景 | 期望行为 |
|---|---|
| grga 调用失败（非零退出）| **不** 写入 JSONL；caller 记到别处 |
| grga 返回 `ok: false` 但退出码 0 | **不** 写入（`graph_actions` 可能缺失或不可信） |
| `graph_actions == []` | **要写入**（时间轴需要"这一步没扩展"的信号）|
| `graph_actions` 缺字段 | **不** 写入；grga 侧 bug，报给 grga |
| session 文件不存在 | caller 先 `mkdir -p .grga/trace/` 再 append |
