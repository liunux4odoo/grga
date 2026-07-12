# Tiered reflection loop (decision tables)

Referenced from "Tiered reflection workflow" in `SKILL.md`. The SKILL.md
body keeps the T0→T1→T2→T3 skeleton and each tier's one-line purpose;
this file holds the per-tier decision tables and metadata idioms that
the caller consults *when actually executing that tier*.

The cost order is **probe ≪ search ≪ expand ≪ related ≪ suggest**.
Exhaust the cheaper tier before spending on the next.

## T0 — probe: size the query

Run `grga probe` with the exact same query-semantic flags you would pass
to `search`. Read:

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

Guardrail: cap re-probes at ~2-3 rounds. If the query will not fall into
band, pick the closest tier and proceed — do not spin.

Skip T0 only when the corpus is tiny and well-scoped (a single small
directory of hand-written notes, for example) so that a full `search` is
already cheap.

## T1 — search: get real evidence

Run `grga search` with the params validated in T0. Read from `matches[]`
/ envelope:

- length of `matches` (non-context), `truncated`
- distribution of `path` values (how many distinct files)
- `heading_path`, `occurrence_index`, `nearby_hits`, `line_ratio`
- `implicit_notice` (was your query interpreted as AND when you expected OR?)

Decide:

| Observation | Action |
|---|---|
| `matches` empty | Back to **T0** to loosen; if already loose, jump to **T3** `intent=reformulate` |
| `truncated: true` | Tighten (back to T0) or raise `--max-total`; do not act on a biased slice |
| Hits concentrated in 1-2 files, but topic likely covered elsewhere | **T2-escalate** `related --mode dir` or `--mode heading` for breadth |
| < ~5 hits and unsatisfactory | **T3** `intent=expand` for more angles, then loop to T1 |
| Hits sufficient and well-labelled | Proceed toward answer — but the **pre-answer T2-bookend `related` still runs** before you commit (SKILL.md "T2 bookends"); optionally **T2-escalate** `expand` first if you need quotable prose |
| Individual hit text already answers the question | Quote `matches[i].text`; you may skip **T2-escalate expand**, but the **pre-answer T2-bookend `related` is still mandatory** |

> The two T2-bookends (session-open, pre-answer) are **not** on this
> table because they are not T1-observation-driven — they run
> unconditionally per SKILL.md "T2 bookends". This table only governs
> **T2-escalate** (reactive widening). Never read "skip T2" here as
> "skip the pre-answer bookend."

Key metadata idioms:

- Missing `heading_path` on Markdown hits means the sidecar was not
  built; the answer still holds, just no chapter context.
- `line_ratio` near 0 or 1 hints at boilerplate (imports, footer) —
  down-weight when reranking manually.
- High `nearby_hits` (e.g. ≥3) means the hit sits in a hotspot:
  `expand --level paragraph` usually beats `--level lines`.

## T2 — expand and related: widen the evidence

Two independent capabilities, choose based on what is missing:

- **Need more text around a hit** → `grga expand`. Chapter for Markdown
  (falls back to paragraph on non-md), lines with `--size 10+` for
  source code, document only for small files. Always read the returned
  `level` field — it reflects what was actually achieved.
- **Need other documents on the same topic** → `grga related`.
  `mode=graph` (needs `index build`) for conceptual neighbours,
  `mode=dir` for repository proximity, `mode=heading` for the same
  section across Markdown files.

Compose both: after `expand --level chapter`, feed the chapter's
headings back into `related --mode heading` to find sibling chapters.

Decide:

- Coverage now sufficient → proceed toward answer, **then run the
  pre-answer T2-bookend `related` on the final seed set** (SKILL.md
  "T2 bookends") before committing.
- Corpus genuinely lacks the material → **T3** `intent=reformulate`
  (maybe the concept is named differently).
- Coverage okay but need prioritisation → **T3** `intent=rerank` on the
  current hits.

### T2 is `related`, not `graph inspect`

Both verbs read the co-occurrence graph, but answer different questions
and are **not interchangeable** for T2 exploration:

| Verb | Answers | Output | When |
|---|---|---|---|
| `related <seed> --mode graph` | "What **documents** are conceptually near this seed?" | Ranked doc paths (PPR) + `expansion.expanded_keywords[].edges_to_seeds` (npmi-weighted) | **T2-bookend** (session-open + pre-answer, mandatory) and **T2-escalate** (mid-session widening) |
| `graph inspect <keyword>` | "What is this **keyword** like — scope distribution, top co-occurring words, polysemy?" | Metadata on the *keyword*, no documents | Diagnostic only: seed-quality check, cross-scope pollution audit, "why is my `related` empty?" |

**Failure mode observed in practice**: caller runs `graph inspect` on a
hub word (protagonist name, framework name, top-level concept) early,
sees the co-occurring column dominated by stopwords / discourse markers
("不过", "什么", "the", "would"), concludes *"the graph has no signal
here"*, and skips T2 entirely for the whole session. This is a misread:
the empty signal was a **seed-quality problem** (see the seed table
below), not a graph-quality problem, and `graph inspect` on the wrong
seed is not a substitute for `related` on a right seed.

Rule of thumb: **catching oneself running `graph inspect` and then *not*
running `related` immediately after usually means `related` was wanted
all along.** If a first `related` call returns `[]`, the correct next
move is a **seed change**, not a switch to `graph inspect`.

### Choosing seeds for `related`

`related` faithfully amplifies whatever seed it is handed. A well-chosen
seed returns a compact on-topic neighbourhood; a poorly-chosen seed
returns `[]`, an irrelevant list, or noise. Seed choice is the single
biggest lever at T2 — **grga will not reject bad seeds**, it computes PPR
on whatever is provided.

**Prefer as seeds**: low-DF specific nouns (proper names of techniques,
entities, APIs, chapter titles — check `expansion.seeds[].chunk_df`,
single-digit to low-hundreds is the sweet spot); names surfaced by T1
`search` (harvest 2-3 low-DF specifics from `matches[i].text`);
multi-seed passes (2-4 diverse specifics beat one hub; PPR fuses across
seeds).

**Avoid as sole seed**: hub words (protagonist, framework name,
top-level concept — high DF means co-occurs with everything, PPR
degenerates); generic descriptors ("main technique", "the API"); terms
not in the index (`results: []` with `expansion.seeds[].chunk_df` missing means the
seed isn't a keyword — confirm via T1 `search`, pick a nearby low-DF
surrogate).

**Meta-rule**: seed selection is a **caller judgment**, not a grga
policy. The verb surfaces the numbers (`chunk_df`, `ppr_score`, `npmi`); the
caller decides whether to keep the seed or swap.

### Seed-quality decision table for `related --mode graph`

Read the `expansion` block off the `related` JSON (v2.1: seeds and
expanded_keywords carry all keyword-level signals in one place), then
decide:

| Signal | Reading | Action |
|---|---|---|
| `expansion.seeds[i].chunk_df` missing or 0 | Seed is not a keyword in the index | Run T1 `search` to confirm the term exists in corpus text; if yes, the tokenizer skipped it — pick a co-appearing indexed term as surrogate |
| `expansion.seeds[i].chunk_df` 1-few | Rare seed; usually strong signal | Proceed; check `expanded_keywords[].edges_to_seeds[].npmi` > 0.5 |
| `expansion.seeds[i].chunk_df` tens - low hundreds | **Sweet spot on a large corpus** | Proceed; expect a tight neighbourhood |
| `expansion.seeds[i].chunk_df` thousands+ | Hub word — co-occurs with everything | Swap seed; use `graph inspect <seed>` to *confirm* stopword-dominated `top_cooccurring_keywords`, then pick a low-DF specific surrogate |
| `results: []` with `expansion.seeds[i].chunk_df` > 0 | Seed indexed but isolated (no chunk-cooccur edges above threshold) | Add another co-appearing seed; or fall back to `--mode dir` / `--mode heading` |
| `expanded_keywords[].edges_to_seeds[].npmi > 0.5` | Strong semantic link | Trust; consider adding the neighbour as a follow-up seed |
| `expanded_keywords[].edges_to_seeds[].npmi` 0.2-0.5 | Weak-to-moderate | Cross-check the neighbour with T1 `search` before trusting |
| `expanded_keywords[].edges_to_seeds[].npmi < 0.2` | Noise-adjacent | Ignore unless corroborated by other signals |
| Neighbours dominated by discourse markers / stopwords | Seed is a hub (even if DF looks modest) | Swap seed for a low-DF specific noun; multi-seed passes help |

**Multi-seed rule of thumb**: 2–4 diverse specifics fused into one call
concentrate PPR on the shared neighbourhood better than any single hub.
When in doubt about which specific to pick, run a T1 `search` on the
task topic first and harvest low-DF nouns from `matches[i].text`.

**Self-diagnosis loop when T2 returns nothing useful**:

1. `expansion.seeds[i].chunk_df` missing → seed not indexed → switch seed
2. `chunk_df` high, `expanded_keywords[].kw` = stopword-adjacent → hub →
   switch to a specific noun
3. `chunk_df` reasonable, `results: []` → isolated seed → try
   multi-seed or `--mode dir`/`heading`
4. Everything looks fine but neighbours are off-topic → run one T3
   `suggest --intent expand` to break the vocabulary frame

## T3 — suggest: LLM reflection

Only tier that costs tokens. Never the first move; every intent expects
the caller to already have hits or a stalled query.

| Situation | Intent | Feed back into |
|---|---|---|
| Vague query, T0/T1 empty | `reformulate` | `queries[]` → back to **T0** (re-probe each) then **T1** |
| Many hits, need ordering | `rerank` | reorder with `ranking[]`, then **T2** on the top few |
| Some hits, need breadth | `expand` | `keywords[]` → new `search` or `related` |

Guardrails:

- **Always re-probe an LLM-reformulated query before searching it** — it
  is unvalidated and can be either empty or explosive.
- Cap the overall T3 loop at a small number of escalations (typically
  1-2). If it still stalls, report evidence gaps rather than looping
  further.
- `--no-votes` during dry runs / evaluation so `llm_extension_votes` is
  not polluted.

## Refeed (task boundary)

Periodically — at task completion or on a schedule — promote well-voted
pairs into the permanent graph so future queries hit them without LLM
cost:

```bash
grga index promote --dry-run --db <path> --json   # inspect first
grga index promote            --db <path> --json   # commit
```

After a promote, `related --mode graph` picks up the new edges
automatically on the next call.

## Stuck signals & escalation (detail)

SKILL.md keeps the short list of four signals + the escalation table.
This section is the **binding definition** — thresholds, symptoms,
multi-signal interactions, and inline-index-build rationale.

**None of the thresholds are hard rules.** Tighten on small / familiar
corpora where mistakes should trigger fast; loosen on large / unfamiliar
corpora where a few misfires are expected before the terminology
settles. The caller self-calibrates.

### The four stuck signals

- **Vocabulary-mismatch** — consecutive `probe` calls return
  `total_lines == 0`.
  - Range: **2-4 consecutive zero-hits** before treating the term family
    as wrong.
  - Low end (~2): small corpus / familiar domain, fail fast.
  - High end (~4): large or unfamiliar corpus, allow a few misses first.

- **Literal-fixation** — same concept tried in several literal wordings,
  all failing.
  - Range: **3-6 wordings** for the same concept before concluding the
    concept is not expressed literally at all.
  - Symptom: producing minor variants (`八位` → `八名` → `八 X`) rather
    than genuinely new phrasings.

- **Wandering** — many probes inside the same family without the signal
  converging.
  - Range: **≥ 5 same-family probes with no convergence trend**.
    Convergence means `total_lines` moving monotonically toward a
    workable range (tens-hundreds) or hitting an informative zero.
  - Symptom: `total_lines` bouncing without direction.

- **Hit-paradox** — `search` returns near-zero hits or far too many but
  all irrelevant.
  - Qualitative call, read from `line_ratio`, `heading_path`,
    `occurrence_index`, and a scan of `matches[i].text`.
  - Symptom: hits cluster in an unrelated section, or `heading_path` is
    uniformly wrong.

**Self-calibration**: quick-terminating task or well-known corpus → pull
ranges toward the low end. Exploratory task or unknown corpus → allow
the high end. When in doubt, escalate **earlier** — a wasted T2/T3 call
costs less than five wasted T0/T1 variants.

### Multi-signal interaction

The escalation table (in SKILL.md) is first-choice, not exclusive. If
vocabulary-mismatch and literal-fixation fire together (common — several
literal wordings all zero-hit), prefer the **cheaper** action first
(T2-escalate `related` before T3 `suggest`). Escalate to the more
expensive tier only if the cheaper one also fails to unblock.

### `index build` is part of the reflection path, not just infra

`grga index build -s <scope> --db <path> --json` is documented as
"offline / infra" because it is offline work, but that framing
undersells its role. **When a stuck signal points to T2 and the corpus
is not yet indexed, building the index inline is the correct next
action** — typically < 60s on a small-to-medium corpus, often faster
than another round of T0/T1 variants. Read "offline / infra" as
"one-off cost, amortised across every subsequent T2 call in the
session", not "prerequisite the caller must have handled before the
session."

Pattern when T2 is chosen but the index is absent:

```bash
grga index build -s <dir> --db <path> --json      # < 60s typical
grga related <seed> --mode graph --db <path> --json
```

This applies equally to the mandatory T2-bookends: a missing index is a
build-inline trigger, never a waiver.

### `suggest` guardrails: production vs. exploration

The verb-level guardrail "never the first verb, capped at 1-2
escalations per task" is written for the **production caller context** —
a real end-user query on a well-understood corpus where LLM cost and
graph-vote pollution both matter.

Relax in two other contexts:

- **Cold-start on an unfamiliar corpus.** The corpus's terminology is
  not yet known; a `suggest --intent expand` early in the session is
  legitimate and often cheaper than five wrong-terminology probes.
- **Skill / workflow testing, exploratory analysis.** Deliberately
  exercising the loop; `suggest` is what is being tested. Pass
  `--no-votes` to prevent test queries from polluting
  `llm_extension_votes`.

The 1-2-escalation cap still applies **per user task**, not per session
in exploratory contexts.
