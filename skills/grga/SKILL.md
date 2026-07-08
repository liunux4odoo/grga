---
name: grga
description: This skill should be used when a caller agent needs to search a local repository, expand a hit into wider context, find related documents through a keyword co-occurrence / heading graph, or ask an LLM to reformulate a stalled query. It exposes the four grga v2 verbs (search / expand / related / suggest) plus the offline index build/promote helpers. Prefer this skill for structure-aware Markdown search, chapter-level context extraction, and cross-document navigation on the local filesystem. It does not fetch URLs, does not modify source files, and only calls an LLM inside `suggest`.
---

# grga skill

grga v2 is a **local, structure-aware search toolbox** built on top of `ripgrep-all`. The skill wraps four independent, idempotent verbs so a caller agent can compose them into a reflection loop without hidden state and without paying LLM cost until absolutely necessary.

## 30-second quickstart

Four commands cover the full basic workflow. Copy-paste them against any local directory:

```bash
grga index build --scope ./docs --db ~/.config/grga/deep_keywords.db --json  # 1) one-time (or on mtime change) offline index
grga search "your query" --directory ./docs --json                            # 2) always start here; get hits + metadata
grga expand "path/to/file.md:42" --directory ./docs --level chapter --json    # 3) widen one hit into a chapter/paragraph/lines
grga related "keyword" --mode graph --db ~/.config/grga/deep_keywords.db --json  # 4) find neighbouring documents (needs step 1)
```

`suggest` is the LLM fallback and only runs on demand — see the `search → suggest → promote` example.

## When to use this skill

Use grga when the task involves any of the following on a local directory:

- Full-text or boolean search across mixed content (Markdown, source code, PDF/Office via `rga` preprocessors) with **position metadata** (`occurrence_index`, `line_ratio`, `nearby_hits`, `heading_path`).
- Turning a single hit into a **wider textual context**: nearby lines, a paragraph, a Markdown chapter, or the whole file.
- Answering "**what else is related to this?**" — either through the keyword co-occurrence graph (PPR), through directory proximity, or through shared Markdown heading paths across files.
- Asking an LLM to **expand keywords, rerank hits, or reformulate a vague query** — but only after cheaper layers (search + expand + related) have been tried and found insufficient.
- Building or refreshing the offline **L2 graph index** (`keyword_docs` / `heading_docs`) that powers `related` graph / heading modes.
- Promoting well-voted LLM keyword pairs (`llm_extension_votes`) into the permanent `keyword_pairs` graph.

Do **not** use this skill for:

- Web search, HTTP fetches, or anything outside the local filesystem.
- Editing source files or committing changes.
- Autonomous "agent" loops — grga verbs are stateless; the caller owns the reflection loop.
- Deep-search style QA with LLM planning — that is the separate `grga deep-search` command, not part of the four-verb Skill contract.

## The four verbs (contract summary)

Every verb has a stable CLI form with `--json` output. The JSON schemas are declared in `docs/api.md` (in the grga repo). All verbs are **idempotent** and **read-only** by default, except `suggest` (which may vote into `llm_extension_votes`) and `index build` / `index promote` (which write to the SQLite graph DB).

### 1. `grga search <query> -d <dir> --json`

Return ripgrep-all hits enriched with Layer 2 position metadata and, for Markdown files, Layer 1 structure metadata.

Signature (conceptual):

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

Use when: the caller has a concrete query string (keywords, boolean expression, or a phrase). This is the always-first verb in any grga workflow.

### 2. `grga expand <path>:<line> --level <lines|paragraph|chapter|document> --json`

Turn one hit into a `Context` block. Levels degrade predictably: `chapter → paragraph → lines(±3) → lines(±0)`.

Signature:

```
expand(hit: {path, line_number}, level, size=3, *, base_dir) ->
  {hit, text, level, range: [start,end], truncated}
```

The returned `level` field reflects what was **actually** achieved (may be smaller than requested). Never invokes an LLM. Never modifies anything.

Use when:
- A hit is interesting and the caller needs more surrounding text to reason about it.
- The caller wants a **whole chapter** of a Markdown doc (only works if a Markdown structure sidecar was built; falls back to `paragraph` for non-md).
- The caller wants to render or feed a quoted block back into another prompt.

### 3. `grga related <keyword...> --mode <graph|dir|heading> --db <path> --json`

Return documents adjacent to the seed keywords under one of three definitions of "adjacent".

Signature:

```
related(keywords: list[str], *, mode="graph"|"dir"|"heading",
        limit=20, db_path, directory) -> [{doc_path, score, mode, reason, keywords[]}]
```

Modes:

- `graph` — Personalized PageRank over `keyword_pairs` seeded on the input, then materialised through `keyword_docs`. Best for "conceptually related" navigation.
- `dir`   — documents that live in the **same directory** as documents containing the seed keywords. Best for local repository navigation.
- `heading` — documents that share a full or leaf **Markdown heading path** with a seed-carrying document. Best for "same chapter across files" (e.g. every `安装 > macOS` section in the repo).

**Precondition**: `grga index build -s <dir> --db <path>` must have been run against the corpus first, otherwise `related` returns an empty list.

Use when: the caller has keywords (from a search, from the user, or from a prior `suggest`) and needs to broaden — not deepen — the exploration.

### 4. `grga suggest <query> --intent <expand|rerank|reformulate> --hits <hits.json> --json`

The **only** verb that calls an LLM. It never runs autonomously; the caller decides when L1/L2 evidence is insufficient.

Signature:

```
suggest(query, hits, *, intent, directory, max_hits=20, no_votes=False,
        llm_base_url, llm_api_key, llm_model, db_path) ->
  {intent, keywords[], queries[], keyword_pairs[[a,b],...],
   ranking[], scores{}, reason, llm_generated: true,
   tokens_used, prompt_tokens, completion_tokens, model, votes: {...}}
```

Intents:

- `expand`     — propose more keywords / follow-up queries. Feed `keywords` into the next `search` or `related`.
- `rerank`     — return a `ranking` (list of indices into the supplied `hits`) plus per-index `scores`.
- `reformulate` — rewrite a vague natural-language query into concrete search expressions in `queries[]`.

By default, emitted `keyword_pairs` are voted into `llm_extension_votes`. After enough votes accumulate, run `grga index promote` to graduate them into `keyword_pairs`, at which point `related --mode graph` picks them up for free.

Use when: search returns too few / low-quality results, or the user query is intrinsically vague. **Never** the first verb.

### Supporting commands (offline / infra)

- `grga index build -s <scope> --db <path> --json` — build `keyword_docs` and `heading_docs` for a directory scope. Prerequisite for `related`.
- `grga index promote --threshold N --db <path> [--dry-run] --json` — merge high-vote pairs from `llm_extension_votes` into `keyword_pairs`. Idempotent; `--dry-run` reports without writing.

Full JSON schemas for every command are enumerated in `docs/api.md`.

## Decision guide — when to call which verb

The caller agent should walk this checklist for any grga task:

1. **Has the caller been given a concrete query?** → start with `search`. Never skip this step; the metadata (`heading_path`, `occurrence_index`, `nearby_hits`) is often enough to answer without any other verb.
2. **Does the caller need *more text* around one hit?** → `expand`. Choose `chapter` for Markdown (most informative), `paragraph` for prose / code comments, `lines` for source code (with `--size 10`+ for a wider window), `document` only for small files.
3. **Does the caller need *other documents* that discuss the same topic?** → `related`.
   - Use `mode=graph` when the goal is conceptual similarity (requires a built index).
   - Use `mode=dir` when the user is browsing a specific area of the repo.
   - Use `mode=heading` when the user is comparing the same **section** across files (uniquely powerful for structured Markdown corpora).
4. **Did `search` return < N useful hits, or is the user query vague?** → `suggest`.
   - Vague query, no hits yet → `intent=reformulate`; feed the returned `queries[]` back into `search`.
   - Hits exist but low quality → `intent=rerank`; use the returned `ranking` to reorder.
   - Hits are okay but the caller wants more angles → `intent=expand`; feed `keywords[]` into a new `search` or into `related`.
5. **Periodically (or at task boundaries)** → `grga index promote --dry-run` to see what the graph would learn from recent LLM output; run without `--dry-run` when the caller is confident.

Rule of thumb: **cost order is search ≪ expand ≪ related ≪ suggest**. Always exhaust the cheaper verbs before the more expensive one. `suggest` is the fallback, not the default.

## Typical composition patterns

Three worked examples ship with this skill. Read them when the pattern matches the task at hand:

- `examples/search-expand-chapter.md` — "give me the full chapter that mentions X" (search → expand chapter, degrades to paragraph on non-md).
- `examples/search-related-graph.md` — "find other files discussing the same concept" (search → collect keywords → related graph).
- `examples/search-suggest-promote.md` — cold-start / vague-query loop with LLM reflection and refeeding into the graph (search → suggest → search-again → index promote).

Additional composition notes:

- **Skip expand for tiny hits**: if the hit line already fully answers the question (e.g. a config value), quoting `matches[i].text` is enough.
- **Chain expand + related**: after `expand --level chapter`, feed the chapter's own headings back into `related --mode heading` to find sibling chapters in other files.
- **Rerank before expand**: on a broad `search` that returns 50+ hits, run `suggest --intent rerank` first, then `expand` only the top 3–5.
- **Vote hygiene**: pass `--no-votes` to `suggest` in dry-run / evaluation contexts so the graph is not polluted during experiments.

## Practical CLI invariants

- Every verb accepts `--json` and prints a single JSON document to stdout on success; use `jq` or `json.loads` to parse.
- On failure, the process exits non-zero and either prints an object with `"ok": false` (search / deep-search / suggest) or a plain error string on stderr (expand / related / index).
- Paths returned by `search` are **relative to the search directory** unless the caller passed absolute inputs. `expand` accepts either form and resolves against `--directory`.
- The shared SQLite DB defaults to `~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`). Pass `--db <path>` to isolate per-project graphs.
- The Markdown structure sidecar is cached under `.grga/structure/` in the search directory root (or `$GRGA_SIDECAR_ROOT` if set). It is rebuilt automatically on `mtime` change.

## What this skill is not

- Not a QA agent. It gives evidence; the caller composes the answer.
- Not a web / codebase-modification tool. `grga` never edits files.
- Not a replacement for `rg` / `rga`. For raw byte-level search prefer `rg` directly; grga adds structure and graph on top.

## Troubleshooting / FAQ

**`grga related` returns `[]`.**
The graph index is either missing or does not cover the seed keywords / directory.
- Run `grga index build --scope <dir> --db <db>` first. `related --mode graph` needs `keyword_docs` + `keyword_pairs`; `--mode heading` needs `heading_docs` (Markdown only).
- Pass `--directory <dir>` to `related` to restrict to a scope, and make sure it matches the resolved path used at `index build` time (both are `Path.resolve()`-ed).
- Confirm the DB path: `related`, `index build`, and `suggest` must share the **same** `--db` value; the default is `~/.config/grga/deep_keywords.db`.
- If the seed word is a stopword or shorter than 2 characters it never enters `keyword_docs`. Use a longer/more concrete term or try `--mode dir` (filesystem proximity, no index required for hits — it still uses `keyword_docs` internally, but a `dir` fallback is easier when the graph is sparse).

**`grga expand` returned `level="lines"` even though I asked for `chapter`.**
This is the documented degrade chain, not a bug: `chapter → paragraph → lines(±size) → lines(±0)`.
- `chapter` only works on Markdown files with a parseable structure sidecar. Non-md paths silently degrade to `paragraph`.
- A paragraph with fewer than 2 non-empty lines degrades to `lines(±size)`.
- Always read `Context.level` on the return value rather than assuming the request was honoured.
- If `chapter` should work but does not, delete the cached sidecar under `.grga/structure/` (or `$GRGA_SIDECAR_ROOT`) and re-run; it will be rebuilt from mtime.

**`grga suggest` hangs, times out, or returns empty fields.**
Only `suggest` calls an LLM; every other verb is offline.
- Check `--llm-base-url`, `--llm-api-key`, `--llm-model` (or the `GRGA_OPENAI_*` / `OPENAI_*` env vars). Missing credentials typically raise, not hang.
- Very large `--hits` payloads slow the request; cap with `--max-hits 10` for interactive use.
- The response is parsed as strict JSON. If the model returns markdown or prose, the parser drops it and you get empty `keywords[]` / `queries[]`. Switch to a JSON-mode-capable model, or try a different `--intent`.
- In tests / dry runs use `--no-votes` to avoid writing to `llm_extension_votes` while iterating on prompts.

**I ran `suggest` several times but `related --mode graph` still ignores the new keywords.**
`suggest` only writes to `llm_extension_votes` (a staging table). Pairs graduate into `keyword_pairs` (which `related` reads) only after `grga index promote`.
- Inspect first: `grga index promote --dry-run --json` shows candidates and their `vote_count`.
- Default threshold is `3`. If your pair has fewer votes, either run `suggest` more times or lower with `--threshold 2`.
- Promotion is per-scope: if `suggest` was invoked with `--directory /a` and `promote` with `--directory /b`, the pair stays put. Use the same `--directory` (or omit it in both) to match.
- Already-promoted pairs are skipped by default. Pass `--repromote` to add another additive count into `keyword_pairs`.

For deeper internals (three-layer expansion model, graph schema, refeed loop), read `docs/design/v2-design.md` in the grga repo. For every JSON field, read `docs/api.md`.
