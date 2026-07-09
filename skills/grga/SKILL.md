---
name: grga
description: This skill should be used when a caller agent needs to search a local repository, expand a hit into wider context, find related documents through a keyword co-occurrence / heading graph, or ask an LLM to reformulate a stalled query. It exposes the five grga v2 verbs (probe / search / expand / related / suggest) plus the offline index build/promote helpers, and prescribes a tiered probe->search->expand/related->suggest reflection loop so the caller can size a query cheaply, act on evidence, and escalate to LLM only when necessary. Prefer this skill for structure-aware Markdown search, chapter-level context extraction, and cross-document navigation on the local filesystem. It does not fetch URLs, does not modify source files, and only calls an LLM inside `suggest`.
---

# grga skill

grga v2 is a **local, structure-aware search toolbox** built on top of `ripgrep-all`. The skill wraps five independent, idempotent verbs so a caller agent can compose them into a reflection loop without hidden state and without paying LLM cost until absolutely necessary.

## 30-second quickstart

Five commands cover the full basic workflow. Copy-paste them against any local directory:

```bash
grga index build --scope ./docs --db ~/.config/grga/deep_keywords.db --json  # 1) one-time (or on mtime change) offline index
grga probe "your query" --directory ./docs --json                            # 2) cheap size check before committing to a full search
grga search "your query" --directory ./docs --json                           # 3) baseline hits + metadata
grga expand "path/to/file.md:42" --directory ./docs --level chapter --json   # 4) widen one hit into a chapter/paragraph/lines
grga related "keyword" --mode graph --db ~/.config/grga/deep_keywords.db --json  # 5) find neighbouring documents (needs step 1)
```

`suggest` is the LLM fallback and only runs on demand — see the `search → suggest → promote` example.

## Storage model (user-scoped)

grga is designed around a **single per-user knowledge graph**:
`~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`). Every
`index build` writes into that shared DB using the `directory` column as a
partition key, and every query verb defaults to **filtering results by
`--directory`** — scope is a *query-time filter*, not a partition
boundary. Give `-d ./project_a` and you narrow the answer to that corpus;
omit `-d` and the whole per-user graph is in scope.

Why: grga's target caller is an AI agent that hops across corpora (novels,
papers, code, notes) in one session. Keeping the graph unified turns
cross-corpus co-occurrence into a *feature* (recall the concept
regardless of which corpus you were browsing yesterday) while
`--directory` still lets a query stay inside one project when needed.

Three levers control cross-corpus behaviour on `related`:

- **Default (isolated)**: `related -d ./proj_a --mode graph` returns only
  `proj_a` documents. This is the safe default — cross-corpus association
  is an *escalation*, not the default retrieval path.
- **`--cross-scope` (advanced, `mode=graph` only)**: seeds stay in `-d`
  but PPR expansion crosses freely into other scopes. Use when the concept
  might be discussed under different names in different corpora and you
  have first read the observability signals below.
- **`--mode heading` default (cross-scope)**: matches shared Markdown
  heading paths across every corpus. Add `--same-scope` to restrict to
  `-d`. Heading paths are structural filters, so cross-scope is the more
  useful default here.

Observability before you escalate:

- `related --mode graph --json` always includes a
  `crossed_scopes_preview` field with `would_add`, `top_other_scopes`,
  and `top_bridge_keywords`. It answers "if I *did* open `--cross-scope`,
  what would come in?" without paying the full cross-scope cost. `null`
  when `-d` is not given.
- `grga graph inspect <keyword>` reports `docs_by_scope`,
  `top_cooccurring_keywords`, and a `suspected_polysemy` heuristic. Use
  it on suspicious bridge keywords surfaced by the preview.

### When to enable --cross-scope (advanced)

`--cross-scope` is an **upgrade action**, not the day-to-day default.
Recommended sequence:

1. Run `search + expand + related` inside `-d` first (isolated mode).
2. If the result is thin, read `crossed_scopes_preview` in the last
   `related --mode graph --json` output. `would_add` tiny → nothing new.
   `would_add` big **and** `top_bridge_keywords` full of generic words
   (`model`, `agent`, `attention`) → likely polysemy noise.
3. On promising bridge keywords, run `grga graph inspect <keyword>` and
   read `suspected_polysemy` / `polysemy_hint`.
4. Only enable `--cross-scope` when (a) the preview shows real incremental
   docs **and** (b) the bridge keywords are *domain* concepts, not
   ambient jargon.

Cross-corpus association's value is *serendipity*, but serendipity is not
the same thing as noise. The sequence above lets you see the trade before
opening the gate.

When to override with `--db <path>`:

- Multi-user machines where per-user isolation matters (each user still
  gets a separate `~/.config/grga/deep_keywords.db` by default; use
  `--db` only to escape that).
- CI / test fixtures that must not touch your personal DB.
- Sensitive projects that must not co-occur with anything else.

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

## The five verbs (contract summary)

Every verb has a stable CLI form with `--json` output. The JSON schemas are declared in `docs/api.md` (in the grga repo). All verbs are **idempotent** and **read-only** by default, except `suggest` (which may vote into `llm_extension_votes`) and `index build` / `index promote` (which write to the SQLite graph DB).

### 0. `grga probe <query> -d <dir> --json`

The cheapest verb: run `rga --count-matches` (or `rg --files` for `--path-only`) once and return **how big the result would be**, without fetching any match bodies or context. Use it to size a query and decide whether to tighten / loosen it *before* paying for a full `search`.

Signature (conceptual):

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

Use when: the caller is about to run `search` on an unfamiliar corpus, a broad keyword, or a huge single file, and wants a cheap size signal first. Skip it only for tiny, well-scoped corpora where a full `search` is already cheap.

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
        limit=20, db_path, directory,
        cross_scope=False,   # graph only: expand PPR into other scopes
        same_scope=False,    # heading only: stay inside --directory
) ->
  # dir | heading
  [{doc_path, score, mode, reason, keywords[]}]
  # graph
  {results: [...], crossed_scopes_preview: {would_add, top_other_scopes,
                                             top_bridge_keywords} | null}
```

Modes:

- `graph` — Personalized PageRank over `keyword_pairs` seeded on the input, then materialised through `keyword_docs`. Best for "conceptually related" navigation. Default is scope-isolated; add `--cross-scope` to reach other corpora, and read `crossed_scopes_preview` first (see "Storage model" above).
- `dir`   — documents that live in the **same directory** as documents containing the seed keywords. Best for local repository navigation. Cross-scope has no meaning here (directory proximity is intrinsically scope-local).
- `heading` — documents that share a full or leaf **Markdown heading path** with a seed-carrying document. Cross-scope by default (heading paths are structural filters that resist same-name-different-meaning noise). Add `--same-scope` to keep matches inside `-d`.

**Precondition**: `grga index build -s <dir> --db <path>` must have been run against the corpus (or any indexed scope inside the shared DB) first, otherwise `related` returns an empty result.

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

- `grga index build -s <scope> --db <path> --json` — index a directory scope's `keyword_docs` and `heading_docs` into the **shared** user knowledge graph. Prerequisite for `related`. Every scope writes into the same DB; `directory` is the partition column, not a separate database.
- `grga index promote --threshold N --db <path> [--dry-run] --json` — merge high-vote pairs from `llm_extension_votes` into `keyword_pairs`. Idempotent; `--dry-run` reports without writing.
- `grga graph inspect <keyword> --db <path> --json` — report a keyword's `docs_by_scope` spread, `top_cooccurring_keywords`, and a `suspected_polysemy` warning. Run it on a bridge keyword before enabling `related --cross-scope`.

Full JSON schemas for every command are enumerated in `docs/api.md`.

## Tiered reflection workflow

The five verbs form a **four-tier reflection loop**, ordered from cheapest to most expensive. Each tier ends with a caller-owned **evaluation checkpoint**: read a few numbers/fields off the JSON, decide *proceed / adjust / escalate*, and only pay for the next tier when the current one is genuinely insufficient. grga verbs never loop on their own — the caller owns every decision.

```
T0 probe  → (evaluate size)   → tighten/loosen  or  proceed
T1 search → (evaluate hits)   → T2 or T3        or  answer
T2 expand + related → (evaluate coverage) → answer  or  T3
T3 suggest → (evaluate LLM output) → loop back to T0/T1
```

Cost order: **probe ≪ search ≪ expand ≪ related ≪ suggest**. Exhaust the cheaper tiers before spending on the next one.

### T0 — probe: size the query

Run `grga probe` with the exact same query-semantic flags you would pass to `search`. Read:

- `total_lines`, `file_count`
- `per_file[0].matches` (if `--per-file`) — spot single-file domination
- `sample.est_total_bytes` (if `--sample N`) — rough context-budget check

Decide:

| Observation | Action |
|---|---|
| `total_lines == 0` | Loosen: `and → or`, drop a term, drop an `--iglob`, or jump to **T3** `intent=reformulate` |
| `total_lines` huge or `est_total_bytes` blows the budget | Tighten: `or → and`, add a term, add `--iglob`, or plan to use `--max-total` / `--max-count` at T1 |
| One file holds >80% of `total_lines` | Add an `--iglob` excluding it, or plan a per-file cap |
| `total_lines` in a sane band (task-specific, e.g. 5-500) | Proceed to **T1** with the same params |

Guardrail: cap re-probes at ~2-3 rounds. If the query will not fall into band, pick the closest tier and proceed — do not spin.

Skip T0 only when the corpus is tiny and well-scoped (a single small directory of hand-written notes, for example) so that a full `search` is already cheap.

### T1 — search: get real evidence

Run `grga search` with the params validated in T0. Read from `matches[]` / envelope:

- length of `matches` (non-context), `truncated`
- distribution of `path` values (how many distinct files)
- `heading_path`, `occurrence_index`, `nearby_hits`, `line_ratio`
- `implicit_notice` (was your query interpreted as AND when you expected OR?)

Decide:

| Observation | Action |
|---|---|
| `matches` empty | Back to **T0** to loosen; if already loose, jump to **T3** `intent=reformulate` |
| `truncated: true` | Tighten (back to T0) or raise `--max-total`; do not act on a biased slice |
| Hits concentrated in 1-2 files, but topic likely covered elsewhere | **T2** `related --mode dir` or `--mode heading` for breadth |
| < ~5 hits and unsatisfactory | **T3** `intent=expand` for more angles, then loop to T1 |
| Hits sufficient and well-labelled | Answer directly, or **T2** `expand` if you need quotable prose |
| Individual hit text already answers the question | Quote `matches[i].text` and stop — skip T2 |

Key metadata idioms:

- Missing `heading_path` on Markdown hits means the sidecar was not built; the answer still holds, just no chapter context.
- `line_ratio` near 0 or 1 hints at boilerplate (imports, footer) — down-weight when reranking manually.
- High `nearby_hits` (e.g. ≥3) means the hit sits in a hotspot: `expand --level paragraph` usually beats `--level lines`.

### T2 — expand and related: widen the evidence

Two independent capabilities, choose based on what is missing:

- **Need more text around a hit** → `grga expand`. Chapter for Markdown (falls back to paragraph on non-md), lines with `--size 10+` for source code, document only for small files. Always read the returned `level` field — it reflects what was actually achieved.
- **Need other documents on the same topic** → `grga related`. `mode=graph` (needs `index build`) for conceptual neighbours, `mode=dir` for repository proximity, `mode=heading` for the same section across Markdown files.

Compose both: after `expand --level chapter`, feed the chapter's headings back into `related --mode heading` to find sibling chapters.

Decide:

- Coverage now sufficient → answer.
- Corpus genuinely lacks the material → **T3** `intent=reformulate` (maybe the concept is named differently).
- Coverage okay but need prioritisation → **T3** `intent=rerank` on the current hits.

### T3 — suggest: LLM reflection

Only tier that costs tokens. Never the first move; every intent expects the caller to already have hits or a stalled query.

| Situation | Intent | Feed back into |
|---|---|---|
| Vague query, T0/T1 empty | `reformulate` | `queries[]` → back to **T0** (re-probe each) then **T1** |
| Many hits, need ordering | `rerank` | reorder with `ranking[]`, then **T2** on the top few |
| Some hits, need breadth | `expand` | `keywords[]` → new `search` or `related` |

Guardrails:

- **Always re-probe an LLM-reformulated query before searching it** — it is unvalidated and can be either empty or explosive.
- Cap the overall T3 loop at a small number of escalations (typically 1-2). If it still stalls, report evidence gaps rather than looping further.
- `--no-votes` during dry runs / evaluation so `llm_extension_votes` is not polluted.

### Refeed (task boundary)

Periodically — at task completion or on a schedule — promote well-voted pairs into the permanent graph so future queries hit them without LLM cost:

```bash
grga index promote --dry-run --db <path> --json   # inspect first
grga index promote            --db <path> --json   # commit
```

After a promote, `related --mode graph` picks up the new edges automatically on the next call.

## Caller anti-patterns

Two mental-model traps that defeat the reflection loop even when every verb
behaves correctly.

### Do not pre-disambiguate identity questions

When the question is *"are A and B the same entity?"* / *"is X actually
Y?"*, **do not design the search around that binary**. Feeding a hypothesis
into the query re-injects the ambiguity you were trying to resolve — every
match now has to be interpreted through the hypothesis, and you will start
filtering evidence to fit it.

Instead, run **two independent evidence chains** — one for A, one for B —
through `probe → search → expand` on their own terms (first appearance,
characteristic contexts, endings). Let the evidence meet on its own. In
domains where identity is intrinsically fuzzy (fiction with alter-egos,
reincarnation, dubbed aliases; near-homonym symbols across a large
codebase; user IDs vs. session IDs in log data), the "same or not same"
answer is often **emergent**, not decidable up front.

Signal you are in this trap: you catch yourself writing a search plan
that reads *"if we find X then A=B, otherwise A≠B"*. Stop, and split it
into two independent explorations.

### Do not fall back to `grep` mid-workflow

If you are inside a grga session, stay in grga. Reaching for `grep -n |
head` "just to look at raw text" silently collapses the reflection loop:

- `grga search -B 0 -A 0 --max-total N --json` is the direct equivalent
  of `grep -n | head -N`, works on PDF / EPUB / Office where `grep` fails,
  and preserves `occurrence_index` / `line_ratio` / `nearby_hits` /
  `heading_path` — precisely the signals a **multi-round** loop needs.
- If the goal is "read text around a hit", that is exactly `grga expand`
  with `--level lines --size N` (or `paragraph` / `chapter`).

Rule of thumb: **`grep` for a one-shot recon when you know you will not
iterate. `grga` for anything you plan to refine, expand, or navigate.**

## Typical composition patterns

Four worked examples ship with this skill. Read them when the pattern matches the task at hand:

- `examples/probe-search-reflect.md` — **default composition for open-ended search**: probe → tighten/loosen → search → expand, with a T3 fallback loop.
- `examples/search-expand-chapter.md` — "give me the full chapter that mentions X" (search → expand chapter, degrades to paragraph on non-md).
- `examples/search-related-graph.md` — "find other files discussing the same concept" (search → collect keywords → related graph).
- `examples/search-suggest-promote.md` — cold-start / vague-query loop with LLM reflection and refeeding into the graph (search → suggest → search-again → index promote).

Additional composition notes:

- **Probe first when in doubt**: an unfamiliar corpus, a broad single keyword, or a directory with likely-huge files. `probe` is cheaper than a full `search`, and re-probing 2-3 times to tune the query costs almost nothing.
- **Skip expand for tiny hits**: if the hit line already fully answers the question (e.g. a config value), quoting `matches[i].text` is enough.
- **Chain expand + related**: after `expand --level chapter`, feed the chapter's own headings back into `related --mode heading` to find sibling chapters in other files.
- **Rerank before expand**: on a broad `search` that returns 50+ hits, run `suggest --intent rerank` first, then `expand` only the top 3–5.
- **Re-probe LLM output**: always run `probe` on any query returned by `suggest --intent reformulate` before searching it — the model may emit an empty or explosive query.
- **Vote hygiene**: pass `--no-votes` to `suggest` in dry-run / evaluation contexts so the graph is not polluted during experiments.

## Working with multiple corpora

Because every `index build` writes into one shared per-user DB, working
across corpora needs no special setup — just index each directory once.

**Default model (one DB, many scopes).** Index three unrelated corpora into
the same graph:

```bash
grga index build -s ~/novels  --db ~/.config/grga/deep_keywords.db --json
grga index build -s ~/papers  --db ~/.config/grga/deep_keywords.db --json
grga index build -s ~/code    --db ~/.config/grga/deep_keywords.db --json
```

Each query then defaults to isolation: `related "x" -d ~/papers` sees only
papers, while omitting `-d` searches all three.

**Typical cross-corpus flow (escalation).** You are in the novels corpus and
a term ("轮回") is thinly covered. Do *not* jump straight to `--cross-scope`:

```bash
grga related "轮回" -d ~/novels --mode graph --json      # 1) isolated, read crossed_scopes_preview
grga graph inspect "轮回" --json                          # 2) check bridge keyword for polysemy
grga related "轮回" -d ~/novels --mode graph --cross-scope --json  # 3) only if preview + inspect look clean
```

Step 3 keeps the seed in `~/novels` but lets PPR reach a `~/papers`
document discussing the same idea under "reincarnation loop".

**When to isolate with `--db <custom>`.** The shared per-user DB is the
default, but pass an explicit `--db` when:

- On a multi-user machine and per-user separation is not enough
  (`--db /secure/team.db`).
- Running CI or tests that must not touch the personal DB
  (`--db "$TMPDIR/ci.db"`).
- A sensitive project must not co-occur with anything else
  (`--db ~/work/secret/graph.db`).

## Practical CLI invariants

- Every verb accepts `--json` and prints a single JSON document to stdout on success; use `jq` or `json.loads` to parse.
- On failure, the process exits non-zero and either prints an object with `"ok": false` (probe / search / deep-search / suggest) or a plain error string on stderr (expand / related / index). `probe` failures stay schema-homogeneous with success (all fields present, zeroed) plus `ok:false` + `error`.
- Paths returned by `search` are **relative to the search directory** unless the caller passed absolute inputs. `expand` accepts either form and resolves against `--directory`.
- The shared SQLite DB defaults to `~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`) and is a **single per-user knowledge graph**: every `index build` scope writes into it, `--directory` is the query-time partition filter. Pass `--db <path>` only to escape that (multi-user machines, CI, sensitive projects — see "Storage model (user-scoped)" above).
- The Markdown structure sidecar is cached under `.grga/structure/` in the search directory root (or `$GRGA_SIDECAR_ROOT` if set). It is rebuilt automatically on `mtime` change.

## What this skill is not

- Not a QA agent. It gives evidence; the caller composes the answer.
- Not a web / codebase-modification tool. `grga` never edits files.
- Not a replacement for `rg` / `rga`. For raw byte-level search prefer `rg` directly; grga adds structure and graph on top.

## Troubleshooting / FAQ

**`grga related` returns `[]`.**
The graph index is either missing or does not cover the seed keywords.
- Run `grga index build --scope <dir> --db <db>` first. `related --mode graph` needs `keyword_docs` + `keyword_pairs`; `--mode heading` needs `heading_docs` (Markdown only).
- Most likely cause: the seed keyword never entered `keyword_docs`, or the scope name passed to `-d` is misspelled. Confirm with `grga graph inspect <keyword> --json` — if `total_docs` is 0 the term was never indexed.
- `-d <dir>` narrows results to a scope (a query-time filter, not a separate DB). To broaden, drop `-d` (search the whole user graph), add `--cross-scope` on `mode=graph` (seeds stay in `-d`, PPR expands out), or rely on `mode=heading`'s cross-scope default.
- Confirm the DB path: `related`, `index build`, and `suggest` share the **same** `--db` value; the default is `~/.config/grga/deep_keywords.db`.
- If the seed word is a stopword or shorter than 2 characters it never enters `keyword_docs`. Use a longer/more concrete term or try `--mode dir` (filesystem proximity).

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
- Promotion defaults to the whole shared graph: omit `--directory` in both `suggest` and `promote` and the pair graduates globally. It only becomes per-scope when you **explicitly** pass `--directory` — and then `suggest --directory /a` + `promote --directory /b` will not match. Use the same `--directory` (or omit it in both).
- Already-promoted pairs are skipped by default. Pass `--repromote` to add another additive count into `keyword_pairs`.

For deeper internals (three-layer expansion model, graph schema, refeed loop), read `docs/design/v2-design.md` in the grga repo. For every JSON field, read `docs/api.md`.
