# Verb signatures and field reference

Referenced from "The five verbs" section of `SKILL.md`. The SKILL.md body
keeps each verb's one-line purpose and "Use when"; this file holds the
conceptual signatures and per-field semantics. Authoritative JSON schemas
for every command live in `docs/api.md` in the grga repo.

All verbs are **idempotent** and **read-only**, except `suggest` (which
calls an LLM). This skill does not include the offline write-side
commands (`index build` / `index rebuild` / `index promote`); the graph
DB is assumed to have been built by the user beforehand.

## 0. `probe`

```
probe(query: str, scope: dir, *, mode=auto|regex|glob, case_sensitive=False,
      multiline=False, iglob=[], path_only=False, implicit_operator=and|or,
      sample=0, per_file=False, timeout=60) ->
  {ok, directory, patterns, terms, pcre2, implicit_operator, implicit_notice,
   total_lines, file_count, truncated, warning, skipped_files, elapsed_ms,
   per_file?[{path, matches}], sample?{sampled_lines, avg_line_bytes, est_total_bytes}}
```

- `total_lines` — total matching lines across the corpus (the real hit count `search` would return, **before** context expansion and **ignoring** `--max-count` / `--max-total`).
- `file_count` — number of files with at least one hit.
- `--sample N` — additionally sample up to N matched lines to estimate `avg_line_bytes` and `est_total_bytes`; the estimate is order-of-magnitude only, not an exact byte count.
- `--per-file` — attach per-file match counts (descending), useful for spotting a single dominating file.
- Query-semantic flags (`--mode` / `--case-sensitive` / `--iglob` / `--multiline` / `--path-only` / `--implicit-and` / `--implicit-or`) are **identical** to `search`, so the probed query and the executed query are the same. `--timeout` is a global budget covering both the count and the sampling pass.
- On failure the JSON is **homogeneous** with the success shape (same fields, zeroed) plus `ok:false` and `error`.

`probe` makes **no** decision itself — it returns numbers, never rewrites the query, never judges "too many / too few". The caller owns the threshold logic.

## 1. `search`

```
search(query: str, scope: dir, *, mode=auto|regex|glob, case_sensitive=False,
       before_context=3, after_context=3, iglob=[], path_only=False,
       max_total=1000, strict_cap=False, implicit_operator=and|or,
       metadata=True) -> {ok, patterns, directory, truncated, matches[]}
```

**Multi-term semantics (v2, breaking change):** space-separated terms are an
**implicit AND** — `search("韩立 师傅")` returns only lines containing *both*
terms (Google / ripgrep intuition). Use explicit `||` / `OR` for OR, `&&` /
`AND` for AND. Pass `--implicit-or` to restore the old OR-on-space behaviour.
When implicit AND fires, grga prints a one-line notice to stderr.

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
expand(hit: {path, line_number}, level, size=3, *, base_dir) ->
  {hit, text, level, range: [start,end], truncated}
```

Levels degrade predictably: `chapter → paragraph → lines(±3) → lines(±0)`.
The returned `level` field reflects what was **actually** achieved (may be
smaller than requested). Never invokes an LLM. Never modifies anything.

## 3. `related`

```
related(keywords: list[str], *, mode="graph"|"heading",
        limit=20, db_path, directory,
        same_scope=False,    # heading only: stay inside --directory
        min_npmi=None,       # graph only: 读侧 npmi 地板；默认沿用写入期 0.2，
                             # 可传更严值（如 0.3），不能低于写入期门槛
) ->
  # graph（v3 起返回候选关键字，不再是文档）：
  {results: [
     {kw, npmi, cooccur_count, via_seed, chunk_df, source}
   ],
   resolved_seeds: [          # v3.1 探针：每个去重 seed 一个 bit
     {seed, in_index}
   ]}
  # heading（返回文档，与 v2 兼容）：
  {results: [
     {doc_path, score, mode, reason, keywords[], directory, qualified_path}
  ]}
```

`--mode dir` **已在 v3 移除**。CLI 与 Python API 均会抛错，指向
`grga search "<seed>" --json` 后 caller 侧按 `matches[].path` 分组聚合
（Unix pipeline）。

Modes:

- `graph` — 查 `keyword_pairs`（`source='chunk_cooccur'` 或
  `'llm_promoted'`），对每个 seed 独立展开 top-M 邻居，返回**候选关键字
  + 关联证据**。多 seed 不融合（每个候选带 `via_seed`；同一 kw 若被多
  个 seed 命中会出现多行）。caller 拿到候选后自行 ripgrep 找原文。
  `RelatedKeyword` 字段：
  - `kw`：候选关键字（casefold）。
  - `npmi`：与 `via_seed` 的关联强度（存储值，或读侧过滤后）。`llm_promoted`
    的 pair 在 DB 中 `npmi IS NULL`，JSON 里规范化为 `0.0`。
  - `cooccur_count`：原始共现次数。
  - `via_seed`：这个候选是从哪个 seed 扩展来的（design D4：不融合）。
  - `chunk_df`：候选词自身的 chunk_df（hub 词判别信号；有 `-d` 时来自
    `keyword_stats.chunk_df WHERE dir_id=?`，无 `-d` 时跨 scope 求和）。
  - `source`：`chunk_cooccur` | `llm_promoted`。
  排序 `(via_seed, -npmi, kw)`。稳定，同参数字节一致。
  **`resolved_seeds` 探针**（v3.1）：每个去重后的输入 seed 一条
  `{seed, in_index}`。`in_index=false` = 词表里没有（拼错 / 未 index）；
  `in_index=true` 但 seed 不出现在任何 `results[].via_seed` 里 = 已知但
  孤立（hub / `--min-npmi` 过紧 / scope 未 index）。前者换词 / suggest
  reformulate，后者换更具体 seed 或松开 `--min-npmi`。**避免 caller 为
  每个 seed 额外调一次 `grga graph inspect` 探路。**
  完整契约见 `docs/design/related-signals-v3.md` +
  `schemas/related-signals-v3.json`。
- `heading` — documents that share a full or leaf **Markdown heading path**
  with a seed-carrying document. Cross-scope by default (heading paths
  are structural filters that resist same-name-different-meaning noise).
  Add `--same-scope` to keep matches inside `-d`. `Related` 里的
  `keyword_contributions` 字段 **在 v3 已删除**。

Edge filter (`--mode graph` only):

- `--min-npmi FLOAT` — **读侧地板**。写入期资产已固化下界（build 默认
  `--min-npmi-build 0.2`，低于此的 pair 根本没被记入 `keyword_pairs`）。
  `--min-npmi` 只能**收紧**到 ≥ 写入期门槛；不能放松。若召回不足，属
  离线索引构建选择，不在本 skill 内解决。

**v3 破变清单**（对齐 `docs/design/decisions.md` §"2026-07-13"）：
- `--mode dir` 移除；`--alpha` / `--iters` / `--cross-scope` / `--preview`
  / `--edge-quantile` CLI flag 全部移除。
- PPR 全图算法及 `ppr_score` / `hops_from_seed` / `expansion` /
  `keyword_contributions` / `graph_signals` 字段整体移除。
- `keyword_chunks` 表在 schema v8 已删除，`related --mode graph` 不再
  经 doc 聚合；文档级证据由 caller 用返回的关键字走 ripgrep 自取。

**Precondition**: the offline graph index must already exist for the
target scope. If it does not, `related` returns `{"results": []}` —
treat that as "no signal, move on". Building the index is a user-side
offline task, not a skill action.

## 4. `suggest`

```
suggest(query, hits, *, intent, directory, max_hits=20,
        llm_base_url, llm_api_key, llm_model, db_path) ->
  {intent, keywords[], queries[], keyword_pairs[[a,b],...],
   ranking[], scores{}, reason, llm_generated: true,
   tokens_used, prompt_tokens, completion_tokens, model}
```

The **only** verb that calls an LLM. It never runs autonomously; the caller
decides when L1/L2 evidence is insufficient.

Intents:

- `expand`     — propose more keywords / follow-up queries. Feed `keywords` into the next `search` or `related`.
- `rerank`     — return a `ranking` (list of indices into the supplied `hits`) plus per-index `scores`.
- `reformulate` — rewrite a vague natural-language query into concrete search expressions in `queries[]`.

The suggestions are consumed in-session: feed `keywords[]` / `queries[]`
back into `search` / `related`. Persisting them into the offline graph
(vote accumulation / promotion) is out of scope for this retrieval-only
skill.

## Supporting command (read-side diagnostic)

- `grga graph inspect <keyword> --db <path> --json` — report a keyword's `docs_by_scope` spread, `top_cooccurring_keywords`, and a `suspected_polysemy` warning. Run it on a bridge keyword before扩大 seed 集或跨 scope 查询。Empty (`total_docs == 0`) means the keyword is not in the offline index — treat as "no signal" and skip.
