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
- Building or refreshing the offline **L2 graph index** (`chunks` / `keyword_chunks` / `keyword_pairs` / `heading_docs`) that powers `related` graph / heading modes.
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
expression, or a phrase). The first **evidence-fetching** verb — but
note that for open-ended queries a **T2-bookend `related` sweep on the
raw seed keywords precedes T1** (see the workflow section below); T1 is
"first to fetch text", not "first thing that runs."

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
intrinsically vague. **Never** the first verb. See
`## Stuck signals & escalation` for the concrete signals that should
trigger `suggest`, and for the relaxed guardrails that apply in
cold-start / testing contexts.

### Supporting commands (offline / infra)

- `grga index build -s <scope> --db <path> --json` — index a scope's segments (`chunks` / `keyword_chunks` / `keyword_pairs[chunk_cooccur]`) and `heading_docs` into the shared graph. Prerequisite for `related`. **Also valid inline** during a stuck-signal escalation — see `## Stuck signals & escalation`; do not treat "offline / infra" as "must be pre-built before the session."
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

**T2 has two distinct roles**, and conflating them is the single most
common way callers defeat the loop:

- **T2-bookend `related`** — a mandatory cheap sweep at *session open*
  and *pre-answer*. Not conditional on any signal. See "T2 bookends"
  below for the exact rules.
- **T2-escalate expand/related** — reactive widening triggered from
  inside T1 by a stuck signal or a specific caller judgment.

```
[session open]
    T2-bookend related  ← MANDATORY, from raw seed keywords
         │
         ▼
    T0 probe  → (evaluate size)   → tighten/loosen  or  proceed
    T1 search → (evaluate hits)   → T2-escalate or T3  or  candidate answer
    T2-escalate expand/related    → (as needed, signal-driven)
    T3 suggest                    → (evaluate LLM output) → loop back to T0/T1
         │
         ▼
[pre-answer]
    T2-bookend related  ← MANDATORY, from final seed set
         │
         ▼
    return answer
```

Cost order: **probe ≪ search ≪ expand ≪ related ≪ suggest**. Exhaust the
cheaper *escalation* tiers before spending on the next; the two
**T2-bookends are not part of that cost trade-off** — they are the
scaffolding around the whole loop, and a T2-bookend `related` is still
cheaper than one wrong-vocabulary T1 or a false-completeness answer.

Per-tier purpose:

- **T0 probe** — size the query cheaply; decide tighten / loosen /
  proceed before paying for `search`.
- **T1 search** — get real evidence with position + structure metadata;
  T1 alone is not a terminal state — the pre-answer T2-bookend still
  runs before any answer is committed.
- **T2-escalate expand + related** — widen mid-session: more text around
  a hit (`expand`) or other documents on the same topic (`related`).
  Signal-driven; see the escalation table.
- **T2-bookend `related`** — mandatory cheap sweeps at session-open (raw
  seeds) and pre-answer (final seeds). Not signal-driven; runs on every
  open-ended task. See "T2 bookends" below.
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

### T2 bookends: two mandatory sweeps + one reactive touchpoint

The tier ladder above shows *where* the two T2-bookends sit. This section
is their **binding definition**. The two bookends are **not optional and
not signal-gated** — they run on every open-ended search task regardless
of how good T1 looked.

1. **Session-open bookend (MANDATORY).** Before the first T1 `search`,
   run one T2 `related` from the caller's raw seed keywords to expand
   into a working seed set. This is the cheapest place to catch alias /
   vocabulary gaps before they compound through every downstream probe.
   Skip **only** when the corpus is tiny and hand-familiar (a handful of
   your own notes) — and say so explicitly when you skip.

2. **Mid-session touchpoint (reactive, this is T2-escalate).** T1-driven,
   not mandatory:
   - On stuck signal V (Vocabulary-mismatch, see below) → escalate to T2.
   - Caller may invoke T2 anytime by judgment (a suspected alias in hits,
     a topic shift widening the seed set).

3. **Pre-answer bookend (MANDATORY).** Before committing *any* answer,
   run one T2 `related` from the final seed set to catch missed aliases
   or adjacent evidence. This is the last cheap defense against
   false-completeness. **A T1 that "looks sufficient" does not waive this
   sweep** — that exact feeling is the failure mode this bookend exists
   to catch. See the pre-answer checklist at the end of this section.

Each T2 call is one hop from the current seeds; whether to chain further
calls is the caller's judgment, not a workflow rule.

#### Pre-answer checklist (do not return an answer until all pass)

Before emitting a final answer to the caller, confirm:

- [ ] The **session-open** T2-bookend `related` ran (or was explicitly
      waived as tiny/familiar corpus, with the reason stated).
- [ ] The **pre-answer** T2-bookend `related` just ran on the final seed
      set, and its `results` / `neighbor_edges` were read — not merely
      invoked.
- [ ] Any new document or alias surfaced by that pre-answer sweep was
      either folded into the answer or explicitly dismissed as
      off-topic.

If you cannot tick all three, you are not done — run the missing sweep
before answering. "T1 already gave me enough" is **not** a valid reason
to skip; it is the precise trigger for the pre-answer bookend.

### T2 is `related`, not `graph inspect`

Both verbs read the co-occurrence graph, but they answer different
questions and are **not interchangeable** for T2 exploration:

| Verb | Answers | Output | When to reach for it |
|---|---|---|---|
| `related <seed> --mode graph` | "What **documents** are conceptually near this seed?" | Ranked doc paths (PPR) + `neighbor_edges` (npmi-weighted) | **T2-bookend**: session-open + pre-answer (mandatory). **T2-escalate**: mid-session widening. |
| `graph inspect <keyword>` | "What is this **keyword** like in the graph — its scope distribution, top co-occurring words, polysemy signal?" | Metadata on the *keyword*, no documents | Diagnostic only: seed-quality check, cross-scope pollution audit, "why is my `related` empty?" |

**Failure mode observed in practice**: caller runs `graph inspect` on a
hub word (protagonist name, framework name, top-level concept) early,
sees the co-occurring column dominated by stopwords / discourse markers
("不过", "什么", "the", "would"), concludes *"the graph has no signal
here"*, and skips T2 entirely for the whole session. This is a misread:
the empty signal was a **seed-quality problem** (see next subsection),
not a graph-quality problem, and `graph inspect` on the wrong seed is
not a substitute for `related` on a right seed.

Rule of thumb: **if you catch yourself running `graph inspect` and then
*not* running `related` immediately after, you probably wanted `related`
all along.**

### Choosing seeds for `related`

`related` faithfully amplifies whatever seed you hand it. A well-chosen
seed returns a compact on-topic neighbourhood; a poorly-chosen seed
returns `[]`, an irrelevant list, or noise. Seed choice is the single
biggest lever the caller controls at T2 — **grga will not reject bad
seeds**, it will compute PPR on whatever you provide.

**Prefer as seeds**:

- **Low-DF specific nouns** — proper names of techniques, entities, APIs,
  chapter titles. Check `graph_signals.seed_chunk_df` in the returned
  JSON: single-digit to low-hundreds DF on a large corpus is the sweet
  spot.
- **Names surfaced by T1 `search`** — T1 hits carry the corpus's actual
  vocabulary; harvest 2–3 low-DF specifics from `matches[i].text` and
  feed them back as T2 seeds.
- **Multi-seed passes** — 2–4 diverse specifics beat one hub. PPR fuses
  across seeds, concentrating signal on the shared neighbourhood.

**Avoid as sole seed**:

- **Hub words** — the protagonist, the framework name, the top-level
  concept. High DF (thousands, or `graph inspect` shows stopword-like
  top co-occurring keywords) means the seed co-occurs with *everything*
  and PPR degenerates.
- **Generic descriptors** — "main technique", "the API", "the config".
  Too many contexts, no coherent neighbourhood.
- **Terms not in the index** — `results: []` with empty `seed_chunk_df`
  means the seed isn't a keyword at all. Confirm via T1 `search` that
  the term appears in the corpus, then pick a nearby low-DF surrogate.

**Self-check signals** (read from the JSON, not gut feel):

- `graph_signals.seed_chunk_df[seed] == 0` → seed not indexed
- `graph_signals.seed_chunk_df[seed]` in the thousands → hub, expect noise
- `neighbor_edges[].npmi > 0.5` → strong semantic link
- `neighbor_edges[].npmi < 0.2` → weak, treat with suspicion
- Empty `results` with non-zero `seed_chunk_df` → seed indexed but
  isolated; swap for a co-appearing term

**Meta-rule**: seed selection is a **caller judgment**, not a grga
policy. The verb surfaces the numbers (`seed_chunk_df`, `npmi`); the
caller decides whether the current seed is worth another T2 round or
whether to swap. Extended DF / npmi decision table lives in
`references/reflection-loop.md` under the T2 section.

### Stuck signals & escalation

The tier ladder above describes the *happy path*. What follows is the
dual — *how to recognise you are stuck, and which tier to escalate to*.
This section exists because the most common failure of the reflection
loop is not "wrong tier for the task" but "stayed on the current tier
too long, cycling through minor query variants."

**None of the thresholds below are hard rules.** They are starting
suggestions. Tighten them on small / familiar corpora where mistakes
should trigger fast; loosen them on large / unfamiliar corpora where a
few misfires are expected before the shape of the terminology settles.
The caller is expected to self-calibrate.

#### Stuck signals

Four signals that suggest the current tier is exhausted. Watch for them
while inside T0/T1; when one fires, follow the escalation table below.

- **Vocabulary-mismatch signal** — consecutive `probe` calls return
  `total_lines == 0`.
  - Suggested range: **2–4 consecutive zero-hits** before treating the
    whole term family as wrong.
  - Low end (~2): small corpus / familiar domain, fail fast.
  - High end (~4): large or unfamiliar corpus, allow a few misses first.

- **Literal-fixation signal** — the same underlying concept has been
  tried in several literal wordings, all failing.
  - Suggested range: **3–6 wordings** for the same concept before
    concluding the concept is not expressed literally at all.
  - Symptom: you notice yourself producing minor variants (`八位` → `八名`
    → `八 X`) rather than genuinely new phrasings.

- **Wandering signal** — many probes inside the *same family* without
  the signal converging (neither reaching a workable count nor hitting
  a hard zero).
  - Suggested range: **≥ 5 same-family probes with no convergence
    trend**. "Convergence" means `total_lines` is monotonically moving
    toward a workable range (say tens–hundreds), or you have hit an
    informative zero.
  - Symptom: `total_lines` is bouncing around without directional signal.

- **Hit-paradox signal** — a `search` returns either far too few
  (near-zero hits) or far too many *but all irrelevant* (noise).
  - No numeric threshold — this is a **qualitative** call, read from
    `line_ratio`, `heading_path`, `occurrence_index`, and a quick
    scan of `matches[i].text`.
  - Symptom: hits cluster in an unrelated section of the corpus, or
    `heading_path` is uniformly wrong.

**Self-calibration rule of thumb**: on a task that must terminate quickly
or on a corpus you already understand, pull all ranges toward the low
end. On an exploratory task or an unknown corpus, allow the high end.
When in doubt, err toward escalating **earlier** rather than later —
a wasted T2/T3 call costs less than five wasted T0/T1 variants.

**When multiple signals fire together**: the escalation table below is
first-choice, not exclusive. If vocabulary-mismatch and literal-fixation
fire simultaneously (a common combo — you tried several literal wordings
and they all zero-hit), prefer the **cheaper** action first (T2
`related` before T3 `suggest`). Escalate to the more expensive tier only
if the cheaper one also fails to unblock.

#### Escalation table (signal → preferred next tier)

| Signal | First choice | Rationale |
|---|---|---|
| Vocabulary-mismatch | **T2-escalate `related`** (build index inline if absent) | The corpus uses a term you have not guessed yet. Structural neighbours (co-occurrence graph) route around the vocabulary you got wrong. |
| Literal-fixation | **T3 `suggest --intent reformulate`** | You are stuck in a literal spelling of the concept. The LLM's job is precisely to break the literal frame. |
| Wandering | **T3 `suggest --intent expand`** | The keyword space is under-expanded; more probing inside the same family will not help. |
| Hit-paradox | **T3 `suggest --intent rerank`** | Hits exist but the ordering is wrong; ask the LLM to re-rank the already-fetched matches. |

If T2-escalate `related` requires an index that is not yet built,
**build it inline** — see the next subsection. Do not treat a missing
index as a reason to skip T2 (this applies equally to the mandatory
T2-bookends: a missing index is a build-inline trigger, never a waiver).

#### `index build` is part of the reflection path, not just infra

`grga index build -s <scope> --db <path> --json` is documented in the
"Supporting commands (offline / infra)" section because it is offline
work, but that framing under-sells its role. **When a stuck signal points
to T2 and the corpus is not yet indexed, building the index inline is
the correct next action** — typically < 60s on a small-to-medium corpus,
often faster than one more round of T0/T1 variants. Do not read "offline
/ infra" as "prerequisite the caller must have handled before the
session" — read it as "one-off cost, amortised across every subsequent
T2 call in the session."

Concrete pattern when T2 is chosen but the index is absent:

```bash
grga index build -s <dir> --db <path> --json      # < 60s typical
grga related <seed> --mode graph --db <path> --json
```

#### `suggest` guardrails: production vs. exploration

The `suggest` verb section above ("never the first verb", "capped at 1–2
escalations per task") is written for the **production caller context**
— a real end-user query on a well-understood corpus where LLM cost and
graph-vote pollution both matter.

In two other contexts the guardrails should relax:

- **Cold-start on an unfamiliar corpus.** You do not yet know the
  corpus's terminology; a `suggest --intent expand` early in the session
  is legitimate and often cheaper than five wrong-terminology probes.
- **Skill / workflow testing, exploratory analysis.** You are
  deliberately exercising the loop; `suggest` is what you are here to
  test. Pass `--no-votes` to prevent test queries from polluting
  `llm_extension_votes`.

The 1–2-escalation cap still applies **per user task**, not per session
in exploratory contexts.

## Caller anti-patterns

Mental-model traps that defeat the reflection loop even when every verb
behaves correctly. The first is a *query-design* trap; the rest are all
variants of the same *tool-substitution* trap — reaching for a low-level
primitive (`grep`, `sqlite3`, `cp`, `find`, …) mid-workflow instead of
staying inside the skill's public verbs.

**Meta-rule for the tool-substitution family** (rules 2–4 below):
whenever you feel the urge to drop out of grga into a lower-level tool,
name the specific grga verb you are bypassing and why it is genuinely
insufficient. If you cannot name one, the urge is muscle-memory, not
necessity. Dropping out silently gives up the skill's stability
guarantees, position metadata, and partition semantics.

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

### Do not open the shared DB with `sqlite3`

`~/.config/grga/deep_keywords.db` is grga's **internal storage**, not a
caller-facing interface. Reaching for `sqlite3 <db>` — even for a
harmless-looking `.tables` or `SELECT DISTINCT directory FROM keyword_chunks` —
couples your workflow to a schema that is free to change between grga
releases (columns get renamed, tables get split, indices get replaced).

Two failure modes it hides:

- **Wrong verb was chosen.** If your task does not use `related`, the
  L2 graph is irrelevant; peeking at the DB is pure ceremony. Ask
  *"does this task actually need the co-occurrence graph?"* first. If
  no, you never needed to look at the DB at all.
- **Right question, wrong tool.** If you *do* need to know a keyword's
  scope distribution, top co-occurring keywords, or polysemy signal,
  the stable public verb is `grga graph inspect <keyword> --db <path>
  --json`. Its output is contract; the underlying SQL is not.

Legitimate exceptions (few and explicit): you are actively developing
grga itself, or debugging a suspected corruption / migration bug after
`grga graph inspect` has already returned inconsistent output. General
"let me just peek at what's in there" curiosity does not qualify.

### Do not copy source data to a "clean" path

`grga -d <dir>` accepts **any** directory. Non-ASCII paths, full-width
punctuation, spaces, colons in filenames — quote the argument once
(`-d "/path/with 全角标点：foo.txt"`) and grga (via `rga`) handles them
fine. Copying a source file into `/tmp/clean/` "to make life easier"
looks harmless but has three real costs:

- **Disk redundancy** — you now maintain two copies of the same content.
- **Silent staleness** — the copy diverges when the original is edited,
  and nothing warns you.
- **Broken partition keys for the L2 graph.** The `directory` column in
  `keyword_chunks` / `heading_docs` is a **partition key**. If you later
  run `grga index build -s /tmp/clean --db ...`, the index is bound to
  a path that vanishes on the next `/tmp` sweep — orphan rows that
  cannot be found through the original directory. Even if you skip
  indexing this session, the copy pattern trains a habit that *will*
  poison the graph eventually.

If a directory contains noise you actually need to exclude, do it at
query time: `-g '*needle*'` (glob filter, passed through to rga),
`--type <lang>`, or a narrower `-d` subpath. Do not copy.

Legitimate exceptions: the source is on read-only or network-latency
media that genuinely slows queries, or you need a frozen snapshot for
reproducibility that survives edits to the original. Both should be
named explicitly, not defaulted to.

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

- **`related` returns `[]`** — index missing, seed not in `keyword_chunks`,
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

## References

Detailed material lives under `references/`, loaded on demand so the
SKILL.md body stays lean. Read the relevant file when its trigger fires:

| File | Read it when |
|---|---|
| `references/verbs.md` | Need a verb's full conceptual signature or per-field semantics (this body has only purpose + "Use when"). |
| `references/storage-model.md` | A scoped query is thin and you are weighing `--cross-scope`; or deciding when to override `--db`; or indexing multiple corpora. |
| `references/reflection-loop.md` | Executing a tier (T0-T3) and need the concrete "if observation X then action Y" decision table, or metadata idioms. |
| `references/troubleshooting.md` | A verb misbehaves — empty `related`, `expand` degraded, `suggest` hangs, promoted pairs not showing up. |

Companion workflows (opt-in, not needed for basic search) live under
`workflows/` — see "Optional workflows" above.


