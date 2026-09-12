# Verb reference (CLI usage and JSON fields)

Referenced from "The five verbs" section of `SKILL.md`. The SKILL.md body
keeps each verb's one-line purpose and "Use when"; this file holds the
full CLI usage and the JSON fields the caller reads. Authoritative JSON
schemas live in `docs/api.md`.

All verbs are **idempotent** and **read-only**, except `suggest` (which
calls an LLM). This skill does not include the offline write-side
commands (`index build` / `index rebuild` / `index promote`); the graph
DB is assumed to have been built by the user beforehand.

## 0. `probe`

```
grga probe <query> -d <dir> [--mode auto|regex|glob] [--case-sensitive]
                            [--multiline] [--iglob GLOB]... [--path-only]
                            [--implicit-and | --implicit-or]
                            [--sample N] [--per-file] [--timeout SEC]
                            [--json]
```

Envelope fields (`--json`):

```
{ok, directory, patterns, terms, pcre2, implicit_operator, implicit_notice,
 total_lines, file_count, truncated, warning, skipped_files,
 per_file?[{path, matches}], sample?{sampled_lines, avg_line_bytes, est_total_bytes}}
```

- `total_lines` — total matching lines across the corpus (the real hit count `search` would return, **before** context expansion and **ignoring** `--max-count` / `--max-total`).
- `file_count` — number of files with at least one hit.
- `--sample N` — additionally sample up to N matched lines to estimate `avg_line_bytes` and `est_total_bytes`; the estimate is order-of-magnitude only, not an exact byte count.
- `--per-file` — attach per-file match counts (descending), useful for spotting a single dominating file.
- Query-semantic flags (`--mode` / `--case-sensitive` / `--iglob` / `--multiline` / `--path-only` / `--implicit-and` / `--implicit-or`) are **identical** to `search`, so the probed query and the executed query are the same. `--timeout` is a global budget covering both the count and the sampling pass.
- On failure the JSON is **homogeneous** with the success shape (same fields, zeroed) plus `ok:false` and `error`.
- `neighbors`（可选，仅内容探测且目录命中已绑定 scope 时出现）：首关键字在 `keyword_pairs` 图里的 top-5 近邻，按 `npmi` 降序。每项仅含 `kw` + `npmi`（float，round 6），作为"换词候选"提示。**键缺席 ≠ 空数组**：缺席 = 该目录没有可用图索引（无命中 scope / DB 不可用 / 查询异常）；`[]` = 有索引但该 seed 在图里无候选。多关键字时只对第一个 seed 输出。
- `complementary`（可选，仅内容探测且目录命中已绑定 scope 时出现）：首关键字的**互补对**——共现显著低于随机期望的词。每项 `{kw, cooccur_count, c_over_e, p_value, via_seed}`，按 `c_over_e` 升序（偏离期望最极端者在前）。**不是别名检测器**：互补对只是统计信号，可能是别名、可能是范畴两极、可能是不同时间线称呼——语义归 caller 判断。键缺席语义同 `neighbors`。
  - 什么时候用：T1 换词阶段就能发现"这个词有个从未同段出现的孪生词"，作为词汇盲区补充线索。
  - 注意：`c_over_e` 是排序键（= cooccur / expected），`p_value` 是显著性门槛，两者独立——显著区内 p 值都极小，排序看偏离强度。

`probe` makes **no** decision itself — it returns numbers, never rewrites the query, never judges "too many / too few". The caller owns the threshold logic.

## 1. `search`

```
grga search <query> -d <dir> [--mode auto|regex|glob] [--case-sensitive]
                             [-B N] [-A N] [--iglob GLOB]... [--path-only]
                             [--max-total N] [--strict-cap]
                             [--implicit-and | --implicit-or]
                             [--json]
```

Envelope: `{ok, patterns, directory, truncated, matches[]}`.

**Multi-term semantics:** space-separated terms are an **implicit AND** —
`grga search "韩立 师傅"` returns only lines containing *both* terms
(Google / ripgrep intuition). Use explicit `||` / `OR` for OR, `&&` /
`AND` for AND. Pass `--implicit-or` to restore OR-on-space behaviour.
When implicit AND fires, grga prints a one-line notice to stderr and
sets `implicit_notice` in the envelope.

**Global cap:** `--max-total N` (default `1000`) bounds the *total* number of
real hits returned, streaming and killing rga once the cap is hit; the result
sets `truncated: true`. This keeps response time predictable on huge single
files (novels, GB-scale logs) where the per-file `--max-count / -n` does not
help. Use `--strict-cap` to fail fast with `error: too_many_matches` instead of
returning a partial slice. `0` disables the cap.

Each `matches[i]` (non-context) carries:

- `path`, `line_number`, `text`, `submatches`, `is_context`
- `occurrence_index` / `total_occurrences` — 1-based rank within the file
  (computed **after** any `--max-total` truncation)
- `line_ratio` — `line_number / total_lines`, clamped to `[0, 1]`
- `nearby_hits` — count of other real hits within ±5 lines
- `matched_terms` — the raw query terms this hit satisfied
- `heading_path` / `chapter_id` / `chapter_range` — best-effort, Markdown only

The envelope also carries `truncated` (bool) and, when adjacency was treated
as implicit AND, `implicit_notice` (string).

## 2. `expand`

```
grga expand <path>:<line> -d <base_dir>
                          --level lines|paragraph|chapter|document|toc
                          [--size N]
                          [--json]
```

Envelope: `{hit, text, level, range: [start, end], truncated}`.

Levels degrade predictably: `chapter → paragraph → lines(±size) → lines(±0)`,
and `toc → chapter` when the file has no parseable heading tree. The returned
`level` field reflects what was **actually** achieved (may be smaller than
requested). Never invokes an LLM. Never modifies anything.

`--level toc` is the single-document outline: `text` is one rendered line per
heading (indented by nesting depth, `[start-end]` line range suffix, the hit's
chapter marked with `> `), **not** body text. Following the `document`
philosophy it returns the whole outline by default and only sets
`truncated: true` — centring the kept window on the hit's chapter — when the
rendered outline would exceed the ~1MB safety cap. A non-truncated `toc`
equals the full-document outline; `range` spans the first-to-last heading
returned. Use it to see a file's structure or locate where a hit sits in it
without paying for full body text.

## 3. `related`

```
grga related <kw>... --mode graph|heading
                     [--scope NAME]...      # 多选：重复即并集；不传 = 跨全部 scope
                     [--tag  LABEL]...      # 按标签选 scope（graph only）
                     [--tag-logic and|or]   # 多个 --tag 的组合（默认 or）
                     [--same-scope]         # heading only；要求恰好一个 --scope
                     [--min-npmi FLOAT]     # graph only；读侧地板（只能收紧）
                     [--limit N]
                     [--db <path>]          # 默认共享 DB
                     [--json]
```

`--mode graph` envelope:

```
{results: [{kw, npmi, cooccur_count, via_seed, chunk_df, source, scopes}],
 resolved_seeds: [{seed, in_index}]}
```

`--mode heading` envelope:

```
{results: [{doc_path, score, mode, reason, keywords[], scope}]}
```

Modes:

- `graph` — 对每个 seed 独立展开 top-M 邻居，返回**候选关键字 + 关联证据**。
  多 seed 不融合（每个候选带 `via_seed`；同一 kw 若被多个 seed 命中会
  出现多行）。caller 拿到候选后自行 ripgrep 找原文。`RelatedKeyword`
  字段：
  - `kw`：候选关键字（casefold）。
  - `npmi`：与 `via_seed` 的关联强度（存储值，或读侧过滤后）。
    LLM 提升的 pair `npmi` 在 JSON 里规范化为 `0.0`。多 scope 命中时取 max。
  - `cooccur_count`：原始共现次数（多 scope 时同上，与 npmi 同行的那份）。
  - `via_seed`：这个候选是从哪个 seed 扩展来的（多 seed 不融合）。
  - `chunk_df`：候选词自身的 chunk 频次（hub 词判别信号；有 `--scope`
    时限定这些 scope 内求和，无 `--scope` 时跨全部 scope 求和）。
  - `source`：`chunk_cooccur` | `llm_promoted`。
  - `scopes`：该候选在本次查询范围内命中的**逻辑 scope 名**列表。单
    scope 查询是 singleton；多 scope 是命中子集；跨 scope（不传
    `--scope`）是全库里所有承载该 pair 的 scope。永远非空，字典序稳定。
    **用途**：让 caller 免于额外的 `graph inspect` 反查就能把候选路由
    到正确的目录（`grga scope list` 查 scope→目录绑定），再 ripgrep 找片段。
  
  排序 `(via_seed, -npmi, kw)`。稳定，同参数字节一致。
  
  **`resolved_seeds` 探针**：每个去重后的输入 seed 一条 `{seed, in_index}`。
  `in_index=false` = 词表里没有（拼错 / 未 index）；`in_index=true` 但
  seed 不出现在任何 `results[].via_seed` 里 = 已知但孤立（hub /
  `--min-npmi` 过紧 / scope 未 index）。前者换词 / `suggest reformulate`，
  后者换更具体 seed 或松开 `--min-npmi`。**避免 caller 为每个 seed 额外
  调一次 `grga graph inspect` 探路。**

- **`complementary`**（仅 `--mode graph`）：互补对——共现**显著低于**
  随机期望的词对（负 npmi + 超几何下尾 p_value < 0.05）。作为共现线索
  的**补充**，与 `results` 并列。**不是别名检测器**：互补对只是统计
  信号，语义归 caller 判断。

  每行字段（最小集，刻意比 `results` 收敛）：
  - `kw`：候选关键词（casefold）。
  - `cooccur_count`：实际共现次数（证据量：c=1 vs c=50 的可信度不同）。
  - `c_over_e`：cooccur / expected — 偏离随机期望的比率（排序键，升序）。
  - `p_value`：超几何下尾显著性（caller 自设门槛）。
  - `via_seed`：从哪个 seed 扩展来的（多 seed 不融合）。

  排序：`(via_seed, c_over_e, kw)`。数量：与 `results` 共享
  `--limit` / `limit_per_seed`。空语义：seed 在词表但无显著互补对 → `[]`。

  什么时候用：T2 bookend 阶段发现"这个概念可能还有另一种叫法"，
  或者排查"为什么两个高度相关的词从未同段出现"。不要把它当别名表用——
  范畴两极、时间线称呼等也会产生互补信号。

- `heading` — documents that share a full or leaf **Markdown heading path**
  with a seed-carrying document. Cross-scope by default (heading paths
  are structural filters that resist same-name-different-meaning noise).
  Add `--same-scope` (要求恰好一个 `--scope`) to keep matches inside that
  scope. 每条结果的 `scope` 字段是它所在的逻辑 scope 名。

Edge filter (`--mode graph` only):

- `--min-npmi FLOAT` — **读侧地板**。写入期资产已固化下界（build 默认
  `--min-npmi-build 0.2`，低于此的 pair 根本没进图谱）。`--min-npmi`
  只能**收紧**到 ≥ 写入期门槛；不能放松。若召回不足，属离线索引构建
  选择，不在本 skill 内解决。

Scope filter (`--scope` + `--tag`，`--mode graph`)：

- `--scope NAME`（可多选）— 精确逻辑 scope 名匹配；多个即并集。
- `--tag LABEL`（可多选）— 按 scope 标签选 scope 集合。
  `--tag-logic or`（默认）= 命中任一标签的 scope；`--tag-logic and` =
  同时带全部标签的 scope。标签在索引构建时写入，或通过
  `grga scope tag add` 添加。
- **`--scope` 与 `--tag` 同时给 = 取交集**（scope 必须既在 name 列表里
  又满足 tag 条件）。任一 filter 非空但解析为空集 → 返回空结果（不
  退化成跨全部 scope）。两者都不给 = cross-scope union。
- `--tag` 仅作用于 `--mode graph`；heading 模式传 `--tag` 会报错。热路径
  动词（`probe` / `search` / `expand`）不接受 `--tag`。

**Precondition**: the offline graph index must already exist for the
target scope. If it does not, `related` returns `{"results": []}` —
treat that as "no signal, move on". Building the index is a user-side
offline task, not a skill action.

## 4. `suggest`

```
grga suggest <query> --intent expand|rerank|reformulate
                     [--hits <hits.json>]   # 可选：给 rerank / 提供上下文时用
                     [--scope NAME]         # 可选：记账进 vote 表；不传静默跳过
                     [--no-votes]           # 只读：完全不写 vote
                     [--max-hits N]
                     [--llm-base-url URL] [--llm-api-key KEY] [--llm-model M]
                     [--db <path>]          # 默认共享 DB
                     [--json]
```

Envelope:

```
{intent, keywords[], queries[], keyword_pairs[[a,b],...],
 ranking[], scores{}, reason, llm_generated: true,
 tokens_used, prompt_tokens, completion_tokens, model}
```

The **only** verb that calls an LLM. It never runs autonomously; the caller
decides when evidence from cheaper tiers is insufficient.

Intents:

- `expand`     — propose more keywords / follow-up queries. Feed `keywords` into the next `search` or `related`.
- `rerank`     — return a `ranking` (list of indices into the supplied `hits`) plus per-index `scores`.
- `reformulate` — rewrite a vague natural-language query into concrete search expressions in `queries[]`.

The suggestions are consumed in-session: feed `keywords[]` / `queries[]`
back into `search` / `related`. Persisting them into the offline graph
(vote accumulation / promotion) is out of scope for this retrieval-only
skill.

## Supporting command (read-side diagnostic)

```
grga graph inspect <keyword> [--db <path>] [--json]
                             [--scope <name> ...] [--tag <label> ...]
                             [--tag-logic and|or] [--docs-limit N]
```

Reports a keyword's `docs_by_scope` spread, `top_docs` (the documents
holding the keyword, sorted by occurrence density — a locate-me shortcut
that saves a `search` round-trip), `top_cooccurring_keywords`, and a
`suspected_polysemy` warning. Run it on a bridge keyword before 扩大 seed
集或跨 scope 查询。Empty (`total_docs == 0`) means the keyword is not in
the offline index — treat as "no signal" and skip.

`--scope` / `--tag` / `--tag-logic` narrow the report to specific corpora
(same filter semantics as `related`: name ∩ tag; both empty = cross-scope
union). An explicit filter that matches no scope returns a
`scope_filter_hint` instead of falling back to cross-scope. `--docs-limit`
caps `top_docs` (default 5, `0` disables the field); `top_docs_truncated`
flags when `total_docs` exceeds the cap.
