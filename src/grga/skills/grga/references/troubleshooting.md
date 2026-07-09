# Troubleshooting / FAQ

Referenced from `SKILL.md`. These are "consult when something goes
wrong" notes, not needed at trigger time — hence they live here rather
than in the SKILL.md body.

## `grga related` returns `[]`

The graph index is either missing or does not cover the seed keywords.

- Run `grga index build --scope <dir> --db <db>` first. `related --mode graph` needs `keyword_docs` + `keyword_pairs`; `--mode heading` needs `heading_docs` (Markdown only).
- Most likely cause: the seed keyword never entered `keyword_docs`, or the scope name passed to `-d` is misspelled. Confirm with `grga graph inspect <keyword> --json` — if `total_docs` is 0 the term was never indexed.
- Confirm the DB path: `related`, `index build`, and `suggest` share the **same** `--db` value; the default is `~/.config/grga/deep_keywords.db`.
- If the seed word is a stopword or shorter than 2 characters it never enters `keyword_docs`. Use a longer/more concrete term or try `--mode dir` (filesystem proximity).
- To broaden scope, see `references/storage-model.md` for the `-d` / `--cross-scope` / `--mode heading` decision.

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
