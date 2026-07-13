# related 信号契约 v3

> **版本**：v3.1（2026-07-13 · resolved_seeds 探针）
> **状态**：已实现（`src/grga/related.py::related_keywords` + `related --json`）
> **替代**：`docs/design/related-signals-v2.md`（v2.2, 2026-07-12）
> **前置**：`docs/design/plan-2026-07-12-keyword-primary.md`；AGENTS.md §信号暴露原则

---

## 0. TL;DR

v9 keyword-primary 架构（Phase 3b-2）拆除了 PPR + `keyword_chunks` 表 + doc
聚合层。`related --mode graph` 的语义从"返回相关文档 + 关键字级 PPR 元
数据"改成"返回候选关键字 + 关联证据"——把"从关键字找原文片段"的责任
显式地交给 caller 走 ripgrep。

v3.1 加了唯一一个 envelope 级探针 **`resolved_seeds`**：把"未知 seed"
（不在 `keywords` 词表）和"已知但无候选"（hub / isolated / min_npmi 过
紧）两种空结果区分开。见 §1.7。

### v2 → v3 破变清单

| 位置 | v2.2 | v3.0 |
|---|---|---|
| `related --json --mode graph` envelope | `{results, crossed_scopes_preview, expansion}` | `{results}` |
| `results[]` in graph mode | `Related` doc entries with `keyword_contributions` | `RelatedKeyword` entries (`{kw, npmi, cooccur_count, via_seed, chunk_df, source}`) |
| `expansion.seeds / expanded_keywords / total_chunks_in_scope` | 顶层 envelope 字段 | ❌ 删除（PPR 已移除） |
| `crossed_scopes_preview` | `null` 或 object | ❌ 删除（`--cross-scope` flag 已废） |
| `keyword_contributions` | 每个 `Related.` 内嵌 | ❌ 删除（doc 聚合层已移除） |
| `--mode dir` | 支持，返回目录聚合 | ❌ 移除（CLI 拒绝，指向 `grga search --json`） |
| `--alpha` / `--iters` / `--cross-scope` / `--preview` / `--edge-quantile` CLI flags | 支持 | ❌ 移除 |
| `--mode heading` results | `Related` with `keyword_contributions=null` | `Related` — 该字段整个删除 |
| `Related.keyword_contributions` field | `Optional[List[KeywordContribution]]` | ❌ 删除（Phase 3b-3 review F9） |
| **新增**: `--min-npmi` on graph mode | 支持 | 支持（唯一保留的 graph flag，语义收紧写入期 npmi 门槛） |
| **新增 v3.1**: `resolved_seeds` on graph envelope | — | 每个 seed 一个 `{seed, in_index}` bit；探针性质，非完备 |

---

## 1. graph 模式：候选关键字 envelope

```jsonc
{
  "results": [
    {
      "kw": "面壁者",
      "npmi": 0.6746,
      "cooccur_count": 28.0,
      "via_seed": "面壁计划",
      "chunk_df": 153,
      "source": "chunk_cooccur"
    },
    {
      "kw": "听证会",
      "npmi": 0.6712,
      "cooccur_count": 6.0,
      "via_seed": "面壁计划",
      "chunk_df": 12,
      "source": "chunk_cooccur"
    }
  ],
  "resolved_seeds": [
    {"seed": "面壁计划", "in_index": true},
    {"seed": "苍坤",     "in_index": false}
  ]
}
```

### 1.1 排序

`(via_seed, -npmi, kw)`。稳定排序，同参数二次调用字节一致。

### 1.2 多 seed 查询：不融合

多 seed 查询（例 `[面壁计划, 罗辑]`）返回**每个 seed 独立展开的结果**，
不做 cross-seed score fusion。同一个候选关键字 `pdc` 若同时被两个 seed
命中，会出现两行 `RelatedKeyword`：

```jsonc
{"kw": "pdc", "npmi": 0.55, "cooccur_count": 7, "via_seed": "面壁计划", ...},
{"kw": "pdc", "npmi": 0.42, "cooccur_count": 5, "via_seed": "罗辑", ...}
```

caller 决定如何聚合（sum / max / weighted / drop-if-only-one）。设计
决策 D4（plan-2026-07-12-keyword-primary.md §7）——"把 score 交给
caller 判断"。

### 1.3 空 / hub 语义

| 场景 | envelope | `resolved_seeds[i].in_index` | 说明 |
|---|---|---|---|
| 未知 seed（不在 `keywords` 表） | `{"results": []}` | `false` | 静默返回空；`in_index=false` 提示 caller 换词（`suggest reformulate`）|
| Hub seed（`chunk_df` 极高，如 `韩立` df=44811） | `{"results": []}` | `true` | 所有 pair 在写入期被 npmi 门槛过滤，符合设计（决策 D3：不替 caller 判断）；`in_index=true` 提示 caller 换更具体的 seed |
| 稀有 seed（`total_occurrences=1` 或全部与另一词完全绑定） | `{"results": []}` | `true` | 所有 pair 被写入期 **Gate 1 独立性判据** (`df_a == df_b == co`，完全绑定) 视为 npmi=1.0 artefact 过滤；`in_index=true` 提示 caller 稀有词应走 T1 `search`，不适合图扩展。见 `docs/design/decisions.md §2026-07-13 build 期写入门槛自适应` |
| `min_npmi` 收紧过高 | `{"results": []}` | `true` | 读侧过滤后无候选；提示 caller 松开 `--min-npmi` |
| 空 scope | `{"results": []}` | `true`/`false`（词表级，与 scope 无关） | scope 未 index，但 seed 是否在全局词表仍如实报告 |

### 1.4 `resolved_seeds` 探针（v3.1 新增）

多 seed related 唯一暴露的 envelope 级信号。对**每个去重后的输入 seed**
（casefold + 保留首现顺序）报告一个 bit：

```jsonc
"resolved_seeds": [
  {"seed": "面壁计划", "in_index": true},
  {"seed": "苍坤",     "in_index": false}
]
```

- `in_index`：seed 是否命中 `keywords` 词表。**与 scope 无关**——一个
  seed 可以 `in_index=true` 却在某个 `--directory` scope 下无候选。
- **为什么这是探针而不是完备 schema**：空 `results` 对单个 seed 是二义
  的——"没被索引过（拼错 / 从未 index）"还是"索引了但孤立（所有边被
  写入期或读侧 npmi 门槛滤掉）"。这两种情况 caller 拿 `results` 派生不
  出来（信息在结果集里天然重合），下一步动作却完全不同。`in_index`
  正好补上这一 bit。
- **caller 能派生的部分不放这里**（AGENTS.md §信号暴露原则第 1 条）：
  产出过候选的 seed 集 = `{r["via_seed"] for r in results}`；一个
  `in_index=true` 但不在该集合里的 seed 就是"孤立 / 过滤空"的情况：

```python
produced = {r["via_seed"] for r in payload["results"]}
for s in payload["resolved_seeds"]:
    if not s["in_index"]:
        # 未知词 → 换词 / suggest reformulate
        ...
    elif s["seed"] not in produced:
        # 已知但孤立 → 换更具体的 seed，或松开 --min-npmi
        ...
```

- 非 JSON（人类）输出下，dead seed 以 stderr 两行提示呈现
  （`unknown seeds ...` / `indexed but no candidates ...`），不污染
  stdout 的候选流。

### 1.5 `source` 字段

- `chunk_cooccur`（默认）：build 阶从 chunk 级共现派生的边，`npmi` 有实值。
- `llm_promoted`：`grga index promote` 写入的边。数据库中 `npmi IS NULL`，
  在 JSON 输出里被规范化为 `0.0` 保证类型一致。caller 若要区分只需判断
  `source`。

### 1.6 `chunk_df` 语义

- 有 scope 过滤（`--directory`）：`keyword_stats.chunk_df WHERE dir_id = ?`。
- 无 scope 过滤：`SUM(chunk_df)` 跨所有 scope。
- caller 用它做 hub 词剪枝：`if r["chunk_df"] > total_chunks * threshold`。

### 1.7 caller 用法示例

拿到候选关键字后走 ripgrep 找原文：

```python
result = json.loads(subprocess.check_output([
    "grga", "related", "面壁计划", "--mode", "graph",
    "-d", scope, "--json",
]))
seeds = ["面壁计划"] + [r["kw"] for r in result["results"][:5]]
# 拼成 ripgrep 的 -e 参数
subprocess.run(["rga", "-i"] + [f"-e{s}" for s in seeds] + [scope])
```

或者过滤掉 hub 词后再用：

```python
non_hub = [r for r in result["results"] if r["chunk_df"] < 1000]
```

---

## 2. heading 模式：文档级 envelope

```jsonc
{
  "results": [
    {
      "doc_path": "usage.md",
      "score": 1.5,
      "mode": "heading",
      "reason": "shared-heading",
      "keywords": ["Install > macOS"],
      "directory": "/path/to/scope",
      "qualified_path": "/path/to/scope/usage.md"
    }
  ]
}
```

`keywords` 在 heading 模式里存的是**匹配到的 heading path 集合**（例
`"Install > macOS"`），不是候选关键字。字段名保持向前兼容。

`Related.keyword_contributions` 在 v3 里**已从 dataclass 里删除**——不再
是可选 None，而是完全不存在。JSON schema 用 `additionalProperties: false`
强制这个不变量。

---

## 3. dir 模式：已移除

`grga related --mode dir` 在 CLI 层拒绝，抛 `ValueError` 并给出迁移提示：

```
mode must be one of: graph, heading
```

从 Python API 调 `related(mode="dir")` 也直接 raise：

```python
raise ValueError(
    "mode='dir' was removed in v8 keyword-primary (Phase 3b-2). "
    "Use `grga search --json` to enumerate seed-containing docs, "
    "then aggregate by directory client-side (Unix pipeline)."
)
```

替代做法：

```bash
grga search "面壁计划" --json | jq '[.matches[].path] | group_by(.)'
```

---

## 4. 保留的语义

以下**没变**，跨 v2/v3 一致：

- `related --mode heading` 的 `--same-scope` flag 语义。
- `grga inspect <keyword> --json` 输出结构（inspect 独立契约，不属 signals）。
- hot-path 动词（`search` / `probe` / `expand` / `deep-search`）
  `--json` 输出零变化。
- LLM 派生动词（`suggest --json`）无变化。

---

## 5. 未来演化

- **`chunk_df` 派生字段**（如 `chunk_df_ratio`）：若 caller 侧真的出现
  多处重复 `df / total` 计算的证据，additive MINOR bump 可加。当前不加
  是因为 caller 一行就能派生（AGENTS.md §信号暴露原则）。
- **多 seed 融合信号**（如 `dedup_key`）：若 caller 反复实现"同一 kw
  在多 seed 下的合并规则"，可以考虑加。但用户明确要求"不替 caller 判
  断融合"，此路径谨慎。
- **跨 scope 显式标记**：`--cross-scope` flag 移除后，`related_keywords`
  在 `directory=None` 时的行为就是"跨所有 scope 的 SUM"。若未来 caller
  需要区分"scope A 的证据"和"scope B 的证据"，可加 `provenance:
  List[{dir, cooccur, npmi}]` 字段。

---

## 6. 一句话给未来读者

**v3 契约把"文档 × 关键字命中矩阵"折成"关键字 + 证据"扁平表：graph
模式直接吐候选词与它们的 npmi/df 元数据，从关键字到片段是 caller +
ripgrep 的活。当你想扩展信号时，先问"caller 能不能一行派生"，能则不
加。这是 v2 就定下的原则，v3 只是把它落实到了返回体形态本身。**
