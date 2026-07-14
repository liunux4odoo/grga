# Troubleshooting / FAQ

Referenced from `SKILL.md`. These are "consult when something goes
wrong" notes, not needed at trigger time — hence they live here rather
than in the SKILL.md body.

## `grga related` returns `{"results": []}`

The offline graph index does not cover the seed keywords in the current
scope. **This skill is retrieval-only** — the correct response is to
treat empty results as "no signal available" and move on (try a
different seed, `--mode heading`, T3 `suggest`, or continue with the T1
evidence). Do **not** trigger `grga index build` from inside the loop;
building or maintaining the offline index is a user-side task.

Diagnostic notes for understanding *why* results are empty (informational
only — none of these prescribe a build action inside the skill):

- Confirm with `grga graph inspect <keyword> --json`; `total_docs == 0`
  means the term is not in the offline index at all.
- `related --mode graph` returns **candidate keywords** (not documents).
  Empty `{"results": []}` is common on hub seeds: a very high `chunk_df`
  means all outgoing pairs were filtered by the build-time `npmi` gate.
  Prefer a low-DF specific noun as seed.
- Stopwords or 1-character terms never enter the index. Use a
  longer/more concrete term, or run `grga search --json` and aggregate
  hits by directory in the caller.
- On small / synthetic corpora the build-time `npmi` gate may have
  filtered every bridge edge. That is an offline-build choice, outside
  the scope of this skill.
- To turn candidate keywords into passages: `grga related <seed> --mode
  graph --json` → take `results[].kw` → `rga -e kw1 -e kw2 ...`.
- To broaden scope, see `references/storage-model.md` for the `-d` /
  cross-scope union / `--mode heading` decision.

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

## Deeper internals

For the full JSON schema of every command, read `docs/api.md` in the
grga repo.
