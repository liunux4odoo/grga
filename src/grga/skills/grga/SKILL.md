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

## Storage model at a glance

grga writes every `index build` into **one shared per-user knowledge graph**
at `~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`). The
`directory` column is a partition key, and every query verb defaults to
filtering by `--directory` — so **scope is a query-time filter, not a
partition boundary**. Give `-d ./project_a` to narrow to that corpus,
omit `-d` to search the whole per-user graph.

Two consequences worth knowing before you compose queries:

- **Isolation is the default** on `related --mode graph`. Cross-corpus
  reach is an *escalation* opened by `--cross-scope`, gated by the opt-in
  `--preview` observability signal.
- **`related --mode heading` is cross-scope by default** (heading paths
  are structural filters that resist same-name-different-meaning noise).
  Add `--same-scope` to restrict.

Full escalation sequence, `--cross-scope` decision criteria, `--db`
override reasons, and multi-corpus indexing example live in
`references/storage-model.md`. Read that when a query returns thin
results and you are about to reach for cross-scope.

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

Every verb has a stable CLI form with `--json` output. All verbs are
**idempotent** and **read-only** by default, except `suggest` (votes into
`llm_extension_votes`) and `index build` / `index promote` (write the DB).
Conceptual signatures and per-field semantics live in
`references/verbs.md`; authoritative JSON schemas in `docs/api.md`.

### 0. `grga probe <query> -d <dir> --json`

Cheapest verb: run `rga --count-matches` once and return **how big the
result would be** (`total_lines`, `file_count`) without fetching bodies or
context. Makes no decision — returns numbers, the caller owns thresholds.
Query-semantic flags are identical to `search`.

Use when: about to run `search` on an unfamiliar corpus, a broad keyword,
or a huge single file, and want a cheap size signal first. Skip only for
tiny, well-scoped corpora where a full `search` is already cheap.

### 1. `grga search <query> -d <dir> --json`

Return ripgrep-all hits enriched with Layer 2 position metadata
(`occurrence_index`, `line_ratio`, `nearby_hits`) and, for Markdown, Layer 1
structure (`heading_path`). Space-separated terms are **implicit AND** (v2
breaking change; `--implicit-or` restores OR). `--max-total N` (default
1000) caps total hits and sets `truncated`.

Use when: the caller has a concrete query string (keywords, boolean
expression, or a phrase). The always-first verb in any grga workflow.

### 2. `grga expand <path>:<line> --level <lines|paragraph|chapter|document> --json`

Turn one hit into a `Context` block. Levels degrade predictably:
`chapter → paragraph → lines(±3) → lines(±0)`. Always read the returned
`level` — it reflects what was **actually** achieved. Never invokes an LLM.

Use when:
- A hit is interesting and the caller needs surrounding text to reason about it.
- The caller wants a **whole chapter** of a Markdown doc (needs a structure sidecar; falls back to `paragraph` for non-md).
- The caller wants to quote a block back into another prompt.

### 3. `grga related <keyword...> --mode <graph|dir|heading> --db <path> --json`

Return documents adjacent to the seed keywords under one of three
definitions: `graph` (Personalized PageRank over `keyword_pairs`, best for
conceptual neighbours), `dir` (same directory, best for repo navigation),
`heading` (shared Markdown heading path). Scope / cross-scope behaviour and
`crossed_scopes_preview` are covered in `references/storage-model.md`.

**Precondition**: `grga index build -s <dir> --db <path>` must have run
against the corpus (or any scope in the shared DB) first, else `related`
returns empty.

Use when: the caller has keywords and needs to broaden — not deepen — the
exploration.

### 4. `grga suggest <query> --intent <expand|rerank|reformulate> --hits <hits.json> --json`

The **only** verb that calls an LLM; never runs autonomously. Intents:
`expand` (more keywords / queries), `rerank` (reorder supplied `hits`),
`reformulate` (rewrite a vague query into concrete expressions). Emitted
`keyword_pairs` are voted into `llm_extension_votes` unless `--no-votes`;
run `grga index promote` to graduate them into the permanent graph.

Use when: search returns too few / low-quality results, or the query is
intrinsically vague. **Never** the first verb.

### Supporting commands (offline / infra)

- `grga index build -s <scope> --db <path> --json` — index a scope's `keyword_docs` / `heading_docs` into the shared graph. Prerequisite for `related`.
- `grga index promote --threshold N --db <path> [--dry-run] --json` — merge high-vote pairs from `llm_extension_votes` into `keyword_pairs`. Idempotent.
- `grga graph inspect <keyword> --db <path> --json` — report a keyword's `docs_by_scope`, `top_cooccurring_keywords`, and `suspected_polysemy`.

Full signatures in `references/verbs.md`; full JSON schemas in `docs/api.md`.

## Tiered reflection workflow

The five verbs form a **four-tier reflection loop**, ordered from cheapest
to most expensive. Each tier ends with a caller-owned **evaluation
checkpoint**: read a few numbers/fields off the JSON, decide *proceed /
adjust / escalate*, and only pay for the next tier when the current one
is genuinely insufficient. grga verbs never loop on their own — the
caller owns every decision.

```
T0 probe  → (evaluate size)   → tighten/loosen  or  proceed
T1 search → (evaluate hits)   → T2 or T3        or  answer
T2 expand + related → (evaluate coverage) → answer  or  T3
T3 suggest → (evaluate LLM output) → loop back to T0/T1
```

Cost order: **probe ≪ search ≪ expand ≪ related ≪ suggest**. Exhaust the
cheaper tiers before spending on the next.

Per-tier purpose:

- **T0 probe** — size the query cheaply; decide tighten / loosen /
  proceed before paying for `search`.
- **T1 search** — get real evidence with position + structure metadata;
  most tasks end here.
- **T2 expand + related** — widen: more text around a hit (`expand`) or
  other documents on the same topic (`related`).
- **T3 suggest** — LLM reflection; only tier that costs tokens, capped
  at 1-2 escalations per task.
- **Refeed** — at task boundary, `grga index promote` graduates
  well-voted `suggest` pairs into the permanent graph.

Per-tier decision tables — *what to read on the JSON, what to do next* —
and metadata idioms (`heading_path`, `line_ratio`, `nearby_hits`
interpretation) live in `references/reflection-loop.md`. Consult it when
you are executing a tier and need the concrete "if X then Y" rules.

Guardrail across tiers: **always re-probe an LLM-reformulated query
before searching it** — it is unvalidated and can be either empty or
explosive.

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

## Practical CLI invariants

- Every verb accepts `--json` and prints a single JSON document to stdout on success; use `jq` or `json.loads` to parse.
- On failure, the process exits non-zero and either prints an object with `"ok": false` (probe / search / deep-search / suggest) or a plain error string on stderr (expand / related / index). `probe` failures stay schema-homogeneous with success (all fields present, zeroed) plus `ok:false` + `error`.
- Paths returned by `search` are **relative to the search directory** unless the caller passed absolute inputs. `expand` accepts either form and resolves against `--directory`.
- The shared SQLite DB defaults to `~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`); pass `--db <path>` to override — see "Storage model (user-scoped)" for when and why.
- The Markdown structure sidecar is cached under `.grga/structure/` in the search directory root (or `$GRGA_SIDECAR_ROOT` if set). It is rebuilt automatically on `mtime` change.

## What this skill is not

- Not a QA agent. It gives evidence; the caller composes the answer.
- Not a web / codebase-modification tool. `grga` never edits files.
- Not a replacement for `rg` / `rga`. For raw byte-level search prefer `rg` directly; grga adds structure and graph on top.

## Optional workflows

Companion tools that live under `workflows/` in this skill. They are opt-in:
you never need them for basic search, but they help when a specific job comes up.

- **`workflows/trace-viewer/`** — Turn a multi-turn grga session into a
  self-contained HTML page that visualises how the keyword co-occurrence graph
  expanded over successive `related` / `suggest` calls. Useful for (a) grga
  developers inspecting graph-expansion behaviour and (b) end users who want
  a "what did the agent actually search" recap. Start at
  `workflows/trace-viewer/README.md`.

## Troubleshooting / FAQ

Common failure modes and their fixes live in
`references/troubleshooting.md`:

- **`related` returns `[]`** — index missing, seed not in `keyword_docs`,
  wrong `--db`, stopword seed, or scope too narrow.
- **`expand` gave `level="lines"` instead of `chapter`** — the documented
  degrade chain; sidecar missing or paragraph too short.
- **`suggest` hangs / empty fields** — LLM credentials, oversized `--hits`,
  or non-JSON model output.
- **`suggest` pairs never reach `related`** — they stage in
  `llm_extension_votes`; run `grga index promote` (mind `--directory`
  matching).

For deeper internals (three-layer expansion model, graph schema, refeed
loop) read `docs/design/v2-design.md`; for every JSON field read
`docs/api.md`.

