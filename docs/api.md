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
- On the CLI: `--max-total N` (default `1000`; `0` disables), plus
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

`level` is one of `lines | paragraph | chapter | document`. It may be
**smaller** than requested because of the degrade chain
`chapter → paragraph → lines(±size) → lines(±0)`. Always read it back.

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
      "source": "chunk_cooccur"       // string — chunk_cooccur | llm_promoted
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
- 有 `--directory`：`keyword_stats.chunk_df WHERE dir_id = ?`。
- 无 `--directory`：`SUM(chunk_df)` 跨所有 scope。
- caller 用它做 hub 词剪枝：`if r["chunk_df"] > threshold: drop`。

**caller 用法**：拿到候选关键字后走 ripgrep 找原文片段。

```python
result = json.loads(subprocess.check_output([
    "grga", "related", "brew", "--mode", "graph",
    "-d", scope, "--json",
]))
seeds = ["brew"] + [r["kw"] for r in result["results"][:5]]
subprocess.run(["rga", "-i"] + [f"-e{s}" for s in seeds] + [scope])
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
      "doc_path": "usage.md",                    // string — relative to `directory`
      "score": 1.5,                              // float — rounded to 6 dp
      "mode": "heading",                         // string — "heading"
      "reason": "shared-heading",                // string — provenance tag
      "keywords": ["Install > macOS"],           // string[] — 匹配到的 heading path 集合
      "directory": "/abs/path/to/scope",         // string|null — indexed scope root
      "qualified_path": "/abs/path/to/scope/usage.md"
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

- `--same-scope` — restrict heading matches to `--directory`. Off by
  default; heading mode 默认跨 scope（heading paths 是结构化过滤，抗
  同名歧义）。

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

```jsonc
{
  "keyword": "attention",         // string — echoes request
  "total_docs": 89,               // int — rows in keyword_docs
  "docs_by_scope": {              // {scope: doc_count}, desc by count
    "/abs/papers/nlp":         42,
    "/abs/papers/psychology":  31,
    "/abs/notes":              16
  },
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
  "index_hint": "'苍坤' is not indexed in the graph. It may still appear in the corpus text — try `grga search \"苍坤\"` for full-text hits, or `grga index build` to (re)index a scope.",
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

## `grga index build`

```jsonc
{
  "scope": "/abs/dir",            // string — resolved index scope
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

Writes to the v9 keyword-primary tables: `keyword_pairs`（累积知识资产，
`chunk_cooccur` 边带 `npmi`）、`keyword_stats`（per-scope df）、
`scope_stats`（分母 N）、`keyword_docs`（doc 级倒排）、`heading_docs`
（Markdown 结构）。`grga index rebuild` 是全清重建（DELETE 该 scope 后
等价重跑 build）。

CLI flag：

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
      "directory": "/abs/dir",         // string — scope of the vote
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

## `grga check` / `grga tools check`

`check` returns `{ok, paths:{name:path|null}, warnings[], errors[]}`.
`tools check` returns `{platform, tools_dir, tools:{name:{path, source, version}}}`.
These are environment-diagnostic commands, not part of the four-verb search
contract, and are documented here only for completeness.
