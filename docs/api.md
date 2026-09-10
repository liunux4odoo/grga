# grga JSON API (`--json` output contract)

> Version: v0.1 (Phase 6.2)
> Every grga v2 subcommand accepts `--json` and emits a single JSON document
> to stdout. This document declares every field so a caller agent (or `jq`)
> can rely on a stable schema. Fields not listed here are not part of the
> contract and may change.

## Conventions

- On **success** the process exits `0` and prints one JSON value.
- On **failure**:
  - `search` / `deep-search` / `suggest` print a JSON object with
    `"ok": false` and an `"error"` string, and exit non-zero.
  - `expand` / `related` / `index build` / `index promote` print a plain
    error string to stderr and exit non-zero (their success JSON has no
    top-level `ok`).
- All line numbers are **1-based**.
- `range` / `chapter_range` are `[start_line, end_line]` inclusive arrays.
- Paths in `search` results are **relative to the search `--directory`**
  unless absolute inputs were given.

---

## `grga search` / `grga deep-search`

Both return the same envelope shape (`deep-search` adds a few planning
fields). Shape:

```jsonc
{
  "ok": true,                         // bool — false on error
  "error": "…",                       // string — present only when ok=false
  "patterns": ["安装"],               // string[] — compiled rga patterns
  "directory": "/abs/path",           // string — resolved search root
  "truncated": false,                 // bool — true when --max-total capped the result set
  "total_files_matched": 0,           // int — distinct file paths present in `matches`
  "implicit_notice": "…",             // string? — set when adjacency was treated as implicit AND
  "warning": "…",                     // string? — non-fatal notice
  "skipped_files": [ … ],             // any[]? — records rga could not read
  "answer": { … },                    // object? — deep-search only (see below)
  "matches": [ Match, … ]             // Match[] — see below
}
```

### Query semantics (breaking change vs v1)

- Space-separated terms are parsed as **implicit AND** by default. `"韩立 师傅"`
  compiles to a PCRE2 lookahead that requires both terms on the same line.
- Explicit `||` / `OR` still means OR; explicit `&&` / `AND` still means AND.
- `search_directory(...)` accepts `implicit_operator="and"` (default) or
  `"or"` to restore the v1 behaviour. On the CLI the same knobs are
  `--implicit-and` (default) and `--implicit-or`.
- When implicit AND is actually applied, the result carries an
  `implicit_notice` string and the CLI prints `[grga] multi-term query
  treated as AND: …` to stderr (unless the user set `--implicit-*`
  explicitly).

### Global result cap

- `search_directory(..., max_total=N, strict_cap=False)` streams rga output
  and stops after `N` real matches, kills the rga subprocess, and sets
  `truncated: true` in the returned envelope. Metadata (occurrence /
  ratio / nearby) is computed *after* truncation, so the counts reflect
  the returned slice.
- `strict_cap=True` runs a lightweight `rga --count-matches` precheck and
  returns `{"ok": false, "error": "too_many_matches", "total_hits": …}`
  instead of returning a partial result.
- `total_files_matched` is a descriptive shape counter: the number of
  distinct file paths that appear in `matches`. Under `truncated: true`
  it lets a caller tell "the cap fell on one giant file" (value `1`)
  from "the cap spread across many files" (value `> 1`) without
  re-running the search. It counts only files with visible matches — it
  is **not** a full "files scanned in the corpus" total, which would
  require a second rga pass under truncation. It is a plain integer, not
  a verdict (design §1.5.1).
- On the CLI: `--max-total N` (default `100`; `0` disables), plus
  `--strict-cap` for the fail-fast variant. The per-file `--max-count / -n`
  still applies unchanged.

### `Match` object

Context lines (`is_context: true`) carry only the base fields. Real matches
additionally carry the Layer 2 / Layer 1 metadata (present only when
`--metadata`, the default, is on).

Base fields (always present):

| field | type | meaning |
|---|---|---|
| `path` | string | file path (relative to `directory`) |
| `line_number` | int\|null | 1-based line; null for path-only rows |
| `text` | string | the matched or context line text |
| `is_context` | bool | `true` for `-A`/`-B` context lines |
| `absolute_offset` | int\|null | byte offset from rga, when available |
| `submatches` | object[] | `[{match, start, end}, …]` char spans |

Metadata fields (real matches only, `--metadata` on):

| field | type | meaning |
|---|---|---|
| `occurrence_index` | int | 1-based rank of this hit within its file |
| `total_occurrences` | int | total real hits for the query in this file |
| `line_ratio` | float | `line_number / total_lines`, clamped `[0,1]` |
| `nearby_hits` | int | other real hits within ±5 lines in the file |
| `matched_terms` | string[] | raw query terms this hit satisfied |
| `heading_path` | string[]\|absent | Markdown heading chain, e.g. `["安装","macOS"]` |
| `chapter_id` | string\|absent | stable chapter slug (Markdown) |
| `chapter_range` | [int,int]\|absent | `[start_line, end_line]` of the chapter |

Non-Markdown files (or unparsable Markdown) simply omit the three structure
fields; callers should treat "absent" as `null`.

### Real output sample

```json
{
  "ok": true,
  "patterns": ["attention"],
  "directory": "/abs/papers",
  "truncated": false,
  "total_files_matched": 2,
  "matches": [
    {
      "path": "nlp/transformers.md",
      "line_number": 17,
      "absolute_offset": 612,
      "text": "The attention mechanism is the core of the transformer model.",
      "is_context": false,
      "submatches": [{"match": "attention", "start": 4, "end": 13}],
      "occurrence_index": 1,
      "total_occurrences": 8,
      "line_ratio": 0.15,
      "nearby_hits": 2,
      "matched_terms": ["attention"],
      "heading_path": ["Architecture", "Attention"],
      "chapter_id": "architecture-attention",
      "chapter_range": [12, 45]
    },
    {
      "path": "psych/attention-and-memory.md",
      "line_number": 3,
      "absolute_offset": 98,
      "text": "Attention and memory are closely linked cognitive processes.",
      "is_context": false,
      "submatches": [{"match": "Attention", "start": 0, "end": 9}],
      "occurrence_index": 1,
      "total_occurrences": 5,
      "line_ratio": 0.07,
      "nearby_hits": 0,
      "matched_terms": ["attention"]
    }
  ]
}
```

### `answer` object (deep-search only)

| field | type | meaning |
|---|---|---|
| `text` | string | formatted natural-language answer |
| `fields` | object | extracted structured fields per sub-question |
| `citations` | object[] | supporting hit references |
| `reasoning_chain` | object[]? | multihop only — per-hop reasoning |

Deep-search also adds top-level `query`, `strategy`
(`horizontal`\|`multihop`), `initial_query_expression`, `plan`,
`iterations`, `stop_reason`.

---

## `grga expand`

```jsonc
{
  "hit": Hit,                 // the anchor hit (see Hit below)
  "text": "…",               // string — expanded context text
  "level": "chapter",        // string — level ACTUALLY achieved after degrade
  "range": [1, 31],          // [int,int] — inclusive line range
  "truncated": false         // bool — true if a document-level read was capped
}
```

`level` is one of `lines | paragraph | chapter | document | toc`. It may be
**smaller** than requested because of the degrade chain
`chapter → paragraph → lines(±size) → lines(±0)` (and `toc → chapter` when
the file has no heading tree). Always read it back.

`toc` is a special level: `text` is the single-document outline (one line
per heading, indented by nesting depth, the hit's chapter marked with
`> `), not body text. Like `document` it returns the whole outline by
default and only sets `truncated: true` — centring the window on the hit's
chapter — when the rendered outline would exceed the ~1MB safety cap. A
non-truncated `toc` therefore equals the full-document outline. `range`
spans the first-to-last heading returned.

### `Hit` object (serialised dataclass)

| field | type | meaning |
|---|---|---|
| `path` | string | file path |
| `line_number` | int | 1-based anchor line |
| `text` | string | anchor line text (empty when built from `PATH:LINE`) |
| `char_span` | [int,int] | matched char range |
| `occurrence_index` | int | Layer 2 rank (0 if not annotated) |
| `total_occurrences` | int | Layer 2 total (0 if not annotated) |
| `line_ratio` | float | Layer 2 ratio (0.0 if not annotated) |
| `nearby_hits` | int | Layer 2 density (0 if not annotated) |
| `heading_path` | string[]\|null | Layer 1 heading chain |
| `chapter_id` | string\|null | Layer 1 chapter slug |
| `chapter_range` | [int,int]\|null | Layer 1 chapter range |
| `matched_terms` | string[] | raw query terms |
| `is_llm_generated` | bool | `true` if the hit came from L3 |
| `is_context` | bool | context-line flag |
| `absolute_offset` | int\|null | byte offset |
| `submatches` | object[] | char spans |
| `from_query` | string\|null | which query produced it (deep-search) |
| `from_tier` | int\|null | which tier produced it (deep-search) |

---

## `grga related`

v3 起 graph 模式返回**候选关键字**（不是文档）。heading 模式仍返回文档。
`--mode dir` 已在 v3 移除。完整契约见 `docs/design/related-signals-v3.md`
+ `schemas/related-signals-v3.json`。热路径动词（`search`/`probe`/
`expand`/`deep-search`）的 `--json` 输出不含任何相关字段。

### `--mode graph`（返回候选关键字）

```jsonc
{
  "results": [
    {
      "kw": "linux",                  // string — 候选关键字（casefold）
      "npmi": 0.6746,                 // float — 与 via_seed 的关联强度（存储值）
      "cooccur_count": 28.0,          // float — 原始共现次数
      "via_seed": "brew",             // string — 从哪个 seed 扩展来（D4：不融合）
      "chunk_df": 153,                // int — 候选词自身的 chunk_df（hub 判别信号）
      "source": "chunk_cooccur",      // string — chunk_cooccur | llm_promoted
      "scopes": ["physics-cn"]        // string[] — v3.1：该候选在本次查询范围内命中的 scope 名
    }
  ],
  "resolved_seeds": [                 // v3.1 探针：每个去重 seed 一个 bit
    {"seed": "brew",   "in_index": true},
    {"seed": "苍坤",   "in_index": false}   // 未知 seed
  ]
}
```

排序 `(via_seed, -npmi, kw)`。稳定，同参数字节一致。

**多 seed 不融合**：多个 seed 时每个 seed 独立展开 top-M 邻居；同一个候选
`kw` 若被多个 seed 命中会出现多行（每行不同 `via_seed`）。caller 自决聚合
策略（sum / max / weighted / drop-if-only-one）。

**`resolved_seeds` 探针**（v3.1）：把空 `results` 的两种情形区分开——
`in_index=false` 是未知词（`suggest reformulate` 或换词）；`in_index=true`
但 seed 不出现在任何 `results[].via_seed` 里，则是"已知但孤立"（hub /
过紧的 `--min-npmi` / scope 未 index）。caller 一段 5 行代码即可分派下
一步动作，不需要额外的 `grga graph inspect` round-trip：

```python
produced = {r["via_seed"] for r in payload["results"]}
for s in payload["resolved_seeds"]:
    if not s["in_index"]:
        ...  # 未知词
    elif s["seed"] not in produced:
        ...  # 已知但孤立
```

**`source` 语义**：`chunk_cooccur`（build 从 chunk 共现派生，`npmi` 有实值）
或 `llm_promoted`（`grga index promote` 写入的边，DB 中 `npmi IS NULL`，
JSON 里规范化为 `0.0`）。

**`chunk_df` 语义**：
- 有 `--scope` / `--tag`（单选/多选）：`SUM(keyword_stats.chunk_df) WHERE scope_id IN (...)`，
  其中 scope_id 集合是 `--scope` name 解析 ∩ `--tag` 标签解析。
- 无 `--scope` 且无 `--tag`：`SUM(chunk_df)` 跨所有 scope。
- caller 用它做 hub 词剪枝：`if r["chunk_df"] > threshold: drop`。

**scope 过滤（`--mode graph`）**：

- `--scope <name>`（可多选）— 精确逻辑 scope 名匹配；多个 = 并集。
- `--tag <label>`（可多选，v11）— 按 scope 标签选 scope 集合。
  `--tag-logic or`（默认）= 命中任一标签；`--tag-logic and` = 同时带
  全部标签。标签在 `grga index build --tag ...` 或 `grga scope tag add`
  时写入 `scope_tags` 表（知识层，随 knowledge pack 迁移）。
- `--scope` 与 `--tag` 同时给 = **取交集**。任一 filter 非空但解析为
  空集 → 返回空结果（不退化成跨 scope）。两者都不给 = cross-scope。
- `--tag` 只作用于 `--mode graph`；heading 模式传 `--tag` 会报错。
- 热路径动词 `search` / `probe` / `expand` 不接 `--tag`；用户想按 tag
  缩范围时先跑一次 `grga related --tag ...` 拿到候选 scope names，再
  喂给 `search --scope <name>`。

**caller 用法**：拿到候选关键字后走 ripgrep 找原文片段。

```python
result = json.loads(subprocess.check_output([
    "grga", "related", "brew", "--mode", "graph",
    "--scope", scope_name, "--json",   # 不加 --scope 则跨全部 scope 联查
]))
seeds = ["brew"] + [r["kw"] for r in result["results"][:5]]
# 每条候选带 scopes[]，可据此定位到对应 scope 的绑定目录再 ripgrep
subprocess.run(["rga", "-i"] + [f"-e{s}" for s in seeds] + [corpus_dir])
```

Edge filter (`--mode graph` only)：

- `--min-npmi FLOAT` — **读侧地板**。写入期资产已固化下界（build 默认
  `--min-npmi-build 0.2`），`--min-npmi` 只能**收紧**到 ≥ 写入期门槛；
  不能放松。想让更多噪声边参与召回，需在 `index build` 时用更低的
  `--min-npmi-build`（代价：写入期资产膨胀）。

**Precondition**: run `grga index build` first; otherwise the array is empty.

### `--mode heading`（返回文档）

```jsonc
{
  "results": [
    {
      "doc_path": "usage.md",                    // string — relative to the scope's bound directory
      "score": 1.5,                              // float — rounded to 6 dp
      "mode": "heading",                         // string — "heading"
      "reason": "shared-heading",                // string — provenance tag
      "keywords": ["Install > macOS"],           // string[] — 匹配到的 heading path 集合
      "scope": "physics-cn"                      // string|null — v10：逻辑 scope 名（原 directory/qualified_path 已移除）
    }
  ]
}
```

`keywords[]` 在 heading 模式里存的是**匹配到的 heading path**（例
`"Install > macOS"`），不是候选关键字。字段名保持向前兼容。

**v3 破变**：`Related.keyword_contributions` 字段已从 dataclass 里删除
（不再是 `Optional[List[KeywordContribution]]`，而是完全不存在；JSON
schema 用 `additionalProperties: false` 强制）。

Heading 模式的 flag：

- `--same-scope` — 把 heading 匹配限制在传入的 `--scope`（需恰好一个
  `--scope`）。默认关闭；heading mode 默认跨 scope（heading paths 是
  结构化过滤，抗同名歧义）。

### `--mode dir`：已移除

`grga related --mode dir` 在 CLI 层拒绝并抛错，指向 `grga search --json`：

```
mode must be one of: graph, heading
```

Python API 调 `related(mode="dir")` 也直接 raise `ValueError`。替代做法：

```bash
grga search "brew" --json | jq '[.matches[].path] | group_by(.)'
```

### v3 移除的 flag / 字段（历史，供 caller 迁移参考）

以下 CLI flag 全部在 v3（Phase 3b-2）移除，传入会报未识别参数错误：

- `--cross-scope`、`--preview`、`--alpha`、`--iters`、`--edge-quantile`

以下 JSON 字段已从 `related --json` 移除：

- graph 顶层 envelope：`crossed_scopes_preview`、`expansion`（含
  `seeds` / `expanded_keywords` / `total_chunks_in_scope`）
- graph `results[]`：`doc_path` / `score` / `reason` / `keywords` /
  `directory` / `qualified_path` / `keyword_contributions`（graph 现在
  返回关键字，不再是文档）
- 已停用的 `expanded_keywords[]` 字段：`ppr_score` / `hops_from_seed`
  / `via_seeds`（PPR 已整体移除）
- heading `results[]`：`keyword_contributions`（已从 dataclass 删除）

依赖上述字段的 caller 需按 v3 契约重写；`grga` 无兼容 fallback。

---

## `grga graph inspect`

Reports how a single keyword distributes across scopes in the shared
user-scoped DB, plus a heuristic same-name-different-meaning warning.
Read this to gauge seed quality (hub? polysemy?) 之前决定是否用它做
`related` 的 seed，或跨 scope 查询时是否可信。

**Scope / tag 过滤**（与 `grga related` 同款语义）：`--scope <name>`
（可重复）把 `docs_by_scope` / `top_docs` / `top_cooccurring_keywords` /
`knowledge_scopes` 全部收敛到指定 scope；`--tag <label>`（可重复，schema
v11）先经 `scope_tags` 解析成 scope 集合再套同一过滤链路，`--tag-logic
and|or`（默认 `or`）控制多 tag 组合。`--scope` + `--tag` 同时给时取交集
（name ∩ tag）；两者皆空 = 跨 scope union。显式过滤若命中 0 个 scope，
返回专用的 `scope_filter_hint`（区别于 not-indexed 的 `index_hint`——
关键字本身可能已索引，只是不在该过滤范围内），并清空所有 scope 相关字段。

```jsonc
{
  "keyword": "attention",         // string — echoes request
  "total_docs": 89,               // int — rows in keyword_docs
  "docs_by_scope": {              // {scope: doc_count}, desc by count
    "/abs/papers/nlp":         42,
    "/abs/papers/psychology":  31,
    "/abs/notes":              16
  },
  "top_docs": [                   // 定位捷径：持有该词的文档，按出现密度降序
    {"scope": "/abs/papers/nlp",
     "doc_path": "transformers.md",
     "occurrence_count": 12,
     "chunk_count": 4},
    {"scope": "/abs/papers/psychology",
     "doc_path": "attention-and-memory.md",
     "occurrence_count": 9,
     "chunk_count": 3}
  ],
  "top_docs_truncated": true,      // bool — true when total_docs > --docs-limit
  "top_cooccurring_keywords": [
    {"keyword": "transformer",
     "weight": 0.71,
     "shared_scopes": ["/abs/papers/nlp"]},
    {"keyword": "cognition",
     "weight": 0.58,
     "shared_scopes": ["/abs/papers/psychology"]}
  ],
  "suspected_polysemy": true,      // bool — true when per-scope neighbour
                                   //   heads have min pairwise Jaccard < 0.2
  "polysemy_hint": "This keyword appears in 3 scopes with largely disjoint …"
}
```

`top_docs` 是"哪些文件持有这个词"的定位捷径：直接读 `keyword_docs`
（doc-count 聚合已扫过的同一张表，零额外成本），跨 scope 全局按
`(-occurrence_count, -chunk_count, scope, doc_path)` 排序，返回前
`--docs-limit`（默认 5，`0` 关闭）条。每行带 `scope` 便于 caller 把后续
`grga search` / 文件打开路由回正确的 `scope_directories` 绑定，免去额外的
全文搜索 round-trip。`top_docs_truncated` 在 `total_docs` 超过
`--docs-limit` 时为 `true`。`doc_path` 相对于该 scope 绑定的目录。
`top_docs` / `top_docs_truncated` 只出现在 indexed 分支——not-indexed 与
knowledge-only（`grga index import` 后未 build）分支都无本地
`keyword_docs` 证据，故不带此字段。

`suspected_polysemy` uses the top ``--limit`` co-occurring keywords per
scope and flags cases where different corpora keep entirely different
company around the term (the classic ML-`attention` vs psychology-`attention`
pattern).

**Not-indexed branch**: when the keyword never entered `keyword_docs`
(``total_docs == 0``) the response replaces the polysemy fields with a
dedicated ``index_hint`` and a ``similar_keywords`` vocabulary probe:

```jsonc
{
  "keyword": "苍坤",
  "total_docs": 0,
  "docs_by_scope": {},
  "top_cooccurring_keywords": [],
  "index_hint": "'苍坤' is not indexed in the graph. It may still appear in the corpus text — try `grga search \"苍坤\"` for full-text hits. Re-indexing a scope (`grga index build`) is a user-side offline task and outside any retrieval-only skill loop.",
  "similar_keywords": [
    {"kw": "苍坤上人", "kind": "superstring", "chunk_df": 87},
    {"kw": "苍坤真人", "kind": "superstring", "chunk_df": 12}
  ]
}
```

``suspected_polysemy`` and ``polysemy_hint`` are **absent** on the
not-indexed branch (a term that never entered the graph cannot be
polysemous). Callers key on field *presence* to distinguish the two
branches; the naming rule ``event → factual name`` (design §1.5.1) is
why the indexing-layer message no longer lives under a ``polysemy_*``
field.

``similar_keywords`` reports objective vocabulary facts, not judgment.
Each entry's ``kind`` is either ``"superstring"`` (the indexed keyword
*contains* the folded seed, e.g. seed ``苍坤`` → ``苍坤上人``) or
``"substring"`` (the indexed keyword is *contained in* the folded seed,
e.g. seed ``韩立道友`` → ``韩立``). Sorted by ``(-chunk_df, kw)``. The
list is empty when the seed is shorter than 2 chars (would over-match
the vocab) or when nothing in the ``keywords`` table matches. Cap the
result with ``--similar-limit`` (default 10). The intended use is the
reflection chain ``related <seed>`` empty → ``inspect <seed>`` sees
``similar_keywords`` → retry ``related`` with a corrected seed.

---

## `grga suggest`

```jsonc
{
  "intent": "expand",            // string — echoes request
  "keywords": ["…"],            // string[] — new keywords (expand)
  "queries": ["…"],             // string[] — reformulated queries (reformulate)
  "keyword_pairs": [["a","b"]],  // [str,str][] — pairs voted into the graph
  "ranking": [2, 0, 1],          // int[] — hit indices, best first (rerank)
  "scores": {"0": 0.9},          // {intIdx: float} — per-hit relevance (rerank)
  "reason": "…",                // string — model's short justification
  "llm_generated": true,         // bool — always true for suggest
  "tokens_used": 0,              // int — total tokens
  "prompt_tokens": 0,            // int
  "completion_tokens": 0,        // int
  "model": "…",                 // string — model name (or "fake-llm" in tests)
  "votes": {                     // object|null — present unless --no-votes
    "pairs_seen": 0,
    "pairs_recorded": 0,
    "votes_added": 0.0
  }
}
```

On error: `{"ok": false, "error": "…"}` and non-zero exit.

`ranking` indices refer to positions in the `hits` array the caller supplied
via `--hits`. `scores` keys are those same indices (as JSON string keys),
values clamped to `[0, 1]`.

---

## `grga index build` / `grga index rebuild`

```jsonc
{
  "scope": "physics-cn",          // string — 逻辑 scope 名（v10：可迁移的域名，非绝对路径）
  "files_scanned": 4,             // int
  "files_indexed": 4,             // int — files that produced keyword rows
  "files_skipped": 0,             // int
  "keyword_docs_written": 20,     // int — rows written to keyword_docs
  "heading_docs_written": 13,     // int — rows written to heading_docs
  "md_files_indexed": 3,          // int — Markdown files with structure
  "elapsed_seconds": 0.0658,      // float — rounded to 4 dp
  "reasons": { "no_keywords": 1 } // {reason: count} — skip breakdown
}
```

Writes to the v10 knowledge asset tables: `keyword_pairs`（累积知识资产，
`chunk_cooccur` 边带 `npmi`）、`keyword_stats`（per-scope df）、
`scope_stats`（分母 N），加上机器本地 evidence 层的 `docs` / `keyword_docs`
（doc 级倒排）、`heading_docs`（Markdown 结构）、`scope_directories`
（scope → 本机绑定目录）。`grga index rebuild` 是全清重建（DELETE 该 scope
的 evidence 后等价重跑 build，知识资产仍按 upsert 增量累加）。

Scope 参数（v10 破变，plan-2026-07-14）：

- `--scope <name>`（**必填**）— 逻辑域名，跨机器可迁移的稳定 handle。
  与本机目录解耦：同一台机器上一个 scope 可以绑多个目录（多次 build），
  跨机器可以是完全不同的目录树，只要 scope 名一致，知识资产就能对得上。
  **不接受路径**：`resolve_scope_name` 只做 strip，不 `expanduser` / 不
  `Path.resolve`；路径形字符串会字面存进 `scopes.name`（不推荐但不报错）。
- `--directory <path>`（**默认当前目录**）— 物理语料目录，进 `scope_directories`
  作为 scope 在本机的绑定；build 从这里枚举文件。`--directory` 省略时用
  cwd，与 `grga search` / `probe` / `deep-search` 一致。
- `--seed-ngram-from <other-scope>`（可选）— 冷启动分词字典：借用另一个
  scope 的 `auto_ngram_dict` 作种子（典型场景：`grga index import` 后本
  机 build 时省去重新检测短语）；目标 scope 自身条目在冲突时优先，
  seed scope 不存在或等于目标时静默 no-op。
- `--tag <label>`（v11，可多次）— 给该 scope 附加多值标签（如
  `--tag 科幻 --tag 小说`），UPSERT 进 `scope_tags`（`INSERT OR IGNORE`，
  累积不删）。标签是知识层元数据，随 `grga index export/import` 迁移；
  想删标签用 `grga scope tag rm`；想改名用 `grga scope tag mv`。
  Rebuild 也接受 `--tag`（同样累积语义）。查询侧对应 `grga related
  --tag <label> [--tag-logic and|or]`。

CLI flag（其余，与 v10 无关）：

- `--min-pair-count INT`（默认未设置 = **scope 自适应阶梯**：`≤200 chunks→2`
  / `≤2000→3` / `≤20000→4` / `>20000→5`）— 单 scope 内 `cooccur_count < N`
  的 pair 不记入 `keyword_pairs`。显式传 int 覆盖阶梯（如 `--min-pair-count 5`
  强制回到 pre-v3.2 固定下界）。
- **独立性判据**（无参、恒开、不受 `--min-pair-count` 影响）— 两个词
  完全绑定（`chunk_df_a == chunk_df_b == cooccur_count`，npmi 蜕化为
  1.0 artefact）的 pair 一律不记入。稀有词单向依附（只跟某常见词同现，
  但该常见词有独立出现）是真信号，保留。
- `--min-npmi-build FLOAT`（默认 `0.2`）— `npmi < X` 的 pair 不记入
  `keyword_pairs`。`llm_promoted` 边不受此限。
- 上述门槛固化资产**下界**；读侧 `related --mode graph --min-npmi`
  只能收紧到 ≥ 写入期门槛，不能放松。想让更多噪声边参与召回，需重建
  时用更低值（代价：写入期资产膨胀）。
- `--chunk-mode line`（默认，1 line = 1 chunk）或
  `--chunk-mode chars --chunk-max-chars 600 --chunk-min-chars 300`。
- v8/v9 起 `chunks` / `keyword_chunks` 两张表已删除，chunk 只在 build
  期 RAM 存活。

---

## `grga index promote`

```jsonc
{
  "threshold": 3.0,                    // float — vote gate
  "dry_run": false,                    // bool
  "candidates": 5,                     // int — pairs at/above threshold
  "promoted": 4,                       // int — pairs merged into keyword_pairs
  "skipped_already_promoted": 1,       // int
  "pairs": [
    {
      "scope": "physics-cn",           // string — v10：逻辑 scope 名
      "keyword_a": "brew",             // string
      "keyword_b": "install",          // string
      "vote_count": 4.0,               // float
      "already_promoted": false        // bool
    }
  ]
}
```

Non-destructive: `llm_extension_votes` rows are never deleted; a
`promoted_at` marker prevents double counting unless `--repromote` is set.

---

## `grga index export` / `grga index import`（v10 知识包）

v10 支持把一个 scope 的**知识层**（`scopes` + 引用到的 `keywords` +
`keyword_pairs` + `keyword_stats` + `scope_stats` + `auto_ngram_dict`
+ `llm_extension_votes` + `scope_tags`(v11) + `meta`）打包成便携的
`.grgapkg`（本身是一个只装了知识、evidence 表为空的 SQLite 库），拷到
另一台机器 import 后即可复用 —— 见 plan-2026-07-14 §5。

### `grga index export`

```jsonc
// --scope <name>：写单包
{
  "out_path": "/abs/physics-cn.grgapkg",  // string
  "scopes_included": ["physics-cn"],       // string[]
  "n_keywords": 42, "n_pairs": 130,
  "n_stats": 42, "n_scope_stats": 1,
  "n_auto_ngram": 0, "n_votes": 3,
  "n_tags": 2,                             // v11：随包迁移的标签条数
  "elapsed_seconds": 0.021
}

// --all --out <dir>：为每个 scope 写一个 <scope>.grgapkg 到目录
{"packs": [ {...同上}, {...} ]}
```

- `--scope <name>` 与 `--all` 互斥；恰好一个必须传。
- `--out <path>`：单 scope 时是 `.grgapkg` 文件（同目录 tempfile +
  `os.replace` 原子写，崩溃不留残包）；`--all` 时是目录（每 scope 一
  个文件，文件名把 `/` / 反斜杠等映射成 `_` 以保跨平台文件系统安全）。
- **多 scope pack 从不产出**：`--all` 走目录形式而非单文件，保证
  `1 pack = 1 scope` 不变式，让 `import` 的语义确定。

### `grga index import`

```jsonc
{
  "pack_path": "/abs/physics-cn.grgapkg",
  "into_scope": "physics-en",            // 由 --as 决定
  "n_keywords_new": 12,                   // 新插入 keywords（其余复用现有词表）
  "n_pairs": 130, "n_stats": 42,
  "n_auto_ngram": 0, "n_votes": 3,
  "n_tags": 2,                            // v11：随包导入的标签条数
  "scope_stats_written": true,
  "elapsed_seconds": 0.018
}
```

- `--as <name>`（**必填**）— 在目标库中新建的 scope 名（不能与已有
  scope 冲突；冲突时提前抛 ValueError，不会写脏事务）。
- **不实现 `--merge-into`**（D9）：npmi 合并策略未定，避免"预留即污染"；
  传入直接 `NotImplementedError`。
- import 是原子的：`BEGIN IMMEDIATE` 内一次性写入 scope 行、kw 全量
  remap、pair/stats/vote/ngram 拷贝；任何步骤失败整体 ROLLBACK。
- 只搬**知识**：evidence 表不搬（`docs` / `keyword_docs` / `heading_docs`
  / `scope_directories` 等）。目标机器需要用 `grga index build --scope
  <as-name> --directory <本机语料>` 补上 evidence。可以配 `--seed-ngram-from
  <as-name>` 冷启动分词字典。

---

## `grga scope`（v10 scope 管理 + v11 tag 管理）

```
grga scope list                          # 列出所有 scope + 绑定目录 + 标签 + 规模
grga scope bind <name> <directory>       # 绑 scope → 本机目录（可多次绑多目录）
grga scope unbind <name> [<directory>]   # 解绑一个或全部目录（不动 evidence 数据）
grga scope rename <old> <new>            # 单行 UPDATE，知识资产透明保留
grga scope rm <name> --yes               # 默认只删 evidence（D6：知识永不因证据丢失而销毁）
grga scope rm <name> --knowledge --yes   # 连知识资产 + scopes 行一起清

# tag 管理（v11 — scope 的多值标签，写入 knowledge-layer scope_tags）
grga scope tag add  <scope> <tag>...       # 累积附加（INSERT OR IGNORE），创建 scope on demand
grga scope tag rm   <scope> [<tag>...]     # 删标签；省略 tag = 清该 scope 全部标签
grga scope tag list <scope>                # 列该 scope 全部标签（升序）
grga scope tag mv   <scope> <old> <new>    # 单 scope 内改名（单行 UPDATE，不动其它 scope）
```

`list --json` 返回：

```jsonc
[
  {
    "name": "physics-cn",
    "kind": null,
    "created_at": "2026-07-14T05:28:49Z",
    "directories": [
      {"directory": "/abs/corpus", "role": null, "indexed_at": "2026-07-14T05:28:49Z"}
    ],
    "tags": ["物理", "中文"],            // v11：该 scope 的多值标签（升序）
    "n_pairs": 130, "n_keywords": 42, "n_docs": 4
  }
]
```

`rm --json` 返回 `RemoveStats`：`scope`, `purged_knowledge`, `scope_found`,
`directory_bindings_removed`, `docs_removed`, `keyword_docs_removed`,
`heading_docs_removed`, `pairs_removed`, `stats_rows_removed`, `votes_removed`,
`ngram_rows_removed`, **`tags_removed`** (v11), `scope_row_removed`。CLI 的
"not found"判定看 `scope_found`（正向信号，区分"scope 不存在"与"scope
存在但仅有知识、无 evidence 可删"两种情形）。

`tag mv` 严格拒绝碰撞：目标标签在同一 scope 已存在时抛
`FileExistsError`（退出码 1），错误信息指向 `scope tag rm` 让 caller
显式先删旧的。作用域是**单个 scope**——不动其它 scope 上的同名标签。

`build --tag <label>`（可多次）在 `grga index build` / `rebuild` 时把
标签 UPSERT 到 `scope_tags`（累积不删）。 tag 是知识层元数据，随
`grga index export/import` 迁移；`ExportStats.n_tags` / `ImportStats.n_tags`
报告条数。

---

## `grga check` / `grga tools status`

`check` returns `{ok, paths:{name:path|null}, warnings[], errors[]}`.
`tools status` returns `{platform, tools_dir, tools:{name:{path, source, version}}}`.
`tools check` is a deprecated alias that prints a warning to stderr and delegates
to `tools status`.
These are environment-diagnostic commands, not part of the four-verb search
contract, and are documented here for completeness.
