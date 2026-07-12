# Troubleshooting / FAQ

Referenced from `SKILL.md`. These are "consult when something goes
wrong" notes, not needed at trigger time — hence they live here rather
than in the SKILL.md body.

## `grga related` returns `{"results": []}`

The graph index is either missing or does not cover the seed keywords.

- Run `grga index build --scope <dir> --db <db>` first. `related --mode graph` needs `keyword_pairs` + `keyword_stats`; `--mode heading` needs `heading_docs` (Markdown only).
- Most likely cause: the seed keyword never entered the index (`keywords` 表没有它), or the scope name passed to `-d` is misspelled. Confirm with `grga graph inspect <keyword> --json` — if `total_docs` is 0 the term was never indexed.
- v3 起 `related --mode graph` 返回**候选关键字**（不再是文档），空 `{"results": []}` 常见于 hub seed：seed 的 `chunk_df` 极高时，它的所有 pair 在写入期被 npmi 门槛过滤掉了（符合设计，见 `docs/design/related-signals-v3.md` §1.3）。用 `grga graph inspect <seed>` 看 `top_cooccurring_keywords` 是否被 stopword 主导；若是，换一个 low-DF 具体名词做 seed。
- If the seed word is a stopword or shorter than 2 characters it never enters the index. Use a longer/more concrete term, or用 `grga search --json` 拿命中后按目录聚合（`--mode dir` 已在 v3 移除）。
- **Small / synthetic corpora**: 写入期资产已固化下界 `npmi >= 0.2`（build 默认 `--min-npmi-build 0.2`，见 `docs/design/plan-2026-07-12-keyword-primary.md` §3.3）。在极小 fixture 上 npmi 被噪声主导，所有桥边都在门槛以下，pair 表几乎为空。读侧 `--min-npmi` 只能收紧、救不回；需在重建时放宽写入期门槛：`grga index rebuild -s <dir> --min-npmi-build 0 --min-pair-count 1`。
- 想拿到候选关键字后铺开原文：`grga related <seed> --mode graph --json` → 取 `results[].kw` → `rga -e kw1 -e kw2 ...`。
- To broaden scope, see `references/storage-model.md` for the `-d` / cross-scope 求和 / `--mode heading` decision.

## `grga expand` returned `level="lines"` even though I asked for `chapter`

This is the documented degrade chain, not a bug: `chapter → paragraph → lines(±size) → lines(±0)`.

- `chapter` only works on Markdown files with a parseable structure sidecar. Non-md paths silently degrade to `paragraph`.
- A paragraph with fewer than 2 non-empty lines degrades to `lines(±size)`.
- Always read `Context.level` on the return value rather than assuming the request was honoured.
- If `chapter` should work but does not, delete the cached sidecar under `.grga/structure/` (or `$GRGA_SIDECAR_ROOT`) and re-run; it will be rebuilt from mtime.

## `grga suggest` hangs, times out, or returns empty fields

Only `suggest` calls an LLM; every other verb is offline.

- Check `--llm-base-url`, `--llm-api-key`, `--llm-model` (or the `GRGA_OPENAI_*` / `OPENAI_*` env vars). Missing credentials typically raise, not hang.
- Very large `--hits` payloads slow the request; cap with `--max-hits 10` for interactive use.
- The response is parsed as strict JSON. If the model returns markdown or prose, the parser drops it and you get empty `keywords[]` / `queries[]`. Switch to a JSON-mode-capable model, or try a different `--intent`.
- In tests / dry runs use `--no-votes` to avoid writing to `llm_extension_votes` while iterating on prompts.

## I ran `suggest` several times but `related --mode graph` still ignores the new keywords

`suggest` only writes to `llm_extension_votes` (a staging table). Pairs graduate into `keyword_pairs` (which `related` reads) only after `grga index promote`.

- Inspect first: `grga index promote --dry-run --json` shows candidates and their `vote_count`.
- Default threshold is `3`. If your pair has fewer votes, either run `suggest` more times or lower with `--threshold 2`.
- Promotion defaults to the whole shared graph: omit `--directory` in both `suggest` and `promote` and the pair graduates globally. It only becomes per-scope when you **explicitly** pass `--directory` — and then `suggest --directory /a` + `promote --directory /b` will not match. Use the same `--directory` (or omit it in both).
- Already-promoted pairs are skipped by default. Pass `--repromote` to add another additive count into `keyword_pairs`.

## Deeper internals

For the three-layer expansion model, graph schema, and refeed loop, read
`docs/design/v2-design.md` in the grga repo. For every JSON field, read
`docs/api.md`.
