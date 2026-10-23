---
name: grga
description: This skill should be used when a caller agent needs to search a local repository, expand a hit into wider context, find related documents through a keyword co-occurrence / heading graph, or ask an LLM to reformulate a stalled query. It exposes the five grga read verbs (probe / search / expand / related / suggest) and a tiered probe → search → expand/related → suggest reflection loop so the caller can size a query cheaply, act on evidence, and escalate to LLM only when necessary. The skill is **retrieval-only** — it assumes the offline graph index was built by the user; empty `related` / `graph inspect` means "no signal available", not a build trigger. Entity / identity / relationship questions (who is X, are A and B the same entity) route to a `related`-first workflow; literal location questions route to `probe → search → expand`. It may consume **entity assets** (wiki files under `entities/<scope>/`, produced by the caller's LLM from `index export-candidates` material) via an **explicit pre-search disambiguation query**: before the first `search`, deliberately read the relevant entity files, fold aliases into the query (e.g. `大史` pulls in `史强` via `--implicit-or`) and verify relations against the raw text — **not** implicit in the search itself. Entity assets are hint, not truth — always verify against the raw text. Producing entity material is an **explicit** workflow (`export-candidates` → organize into wiki), never part of the retrieval loop. It does not fetch URLs, does not modify source files, and calls an LLM only inside `suggest`.
---

# grga skill

grga is a **local, structure-aware search toolbox** built on top of
`ripgrep-all`. The skill wraps five independent, idempotent verbs so a
caller agent can compose them into a reflection loop without hidden
state and without paying LLM cost until absolutely necessary.

## !!**MANDATORY RULES**!!

- **Always use `grga` for keyword search.** While this skill is active,
  `grga` covers every search need. Unless the user explicitly requests
  otherwise or a task genuinely cannot be served by any `grga` verb, do
  **not** reach for `grep`, `rg`, `sqlite3`, ad-hoc `find -exec grep`,
  or similar tools to hunt for keywords, patterns, or DB rows. Route
  the search through the appropriate verb (`probe` / `search` /
  `expand` / `related` / `suggest`) instead.
- **Open-ended task? Run `related` before the first `search`** — the
  session-open T2-bookend is mandatory (see "Tiered reflection
  workflow"). The quickstart below is **cost order, not execution
  order**; "Start here" picks the entry verb by question type.
- **Post-processing is fine.** `sed`, `awk`, `wc`, `sort`, `uniq`,
  `jq`, `cut`, and friends remain the right tools for slicing,
  counting, aggregating, or reformatting `grga` output. Pipe `grga
  --json` (or plain) into them freely.
- **Naming the bypass.** If a task truly needs a non-`grga` search
  tool, first name the specific `grga` verb being bypassed and the
  concrete reason it is insufficient (see the anti-patterns section).
  Absent that justification, the urge is muscle memory, not necessity.
- **Tool hints are not a build trigger.** Read-side commands (`grga
  graph inspect`, `grga scope list`) may print hints that mention `grga
  index build` when the offline index is empty — those hints are written
  for a human maintaining the index. **Ignore the build suggestion
  inside the skill loop**: an empty / missing index is "no signal", and
  the fallthrough stays `probe` sweep → different seed → `related
  --mode heading` → T3 `suggest`.
- **Entity assets have uncertain credibility.** Wiki files under
  `entities/<scope>/` are LLM-produced hints — the `confidence` value is
  the producing LLM's own self-assessment, which can be wrong, stale, or
  biased. **Never treat them as ground truth**: any alias / relation read
  from them must be verified against the raw text (`search` / `expand`)
  before it enters an answer.

## Start here: pick the entry verb

Read this before the quickstart — the **question type** decides the entry
verb, not the cost order below:

- **Entity / identity / relationship** — "X 是谁？", "A 和 B 是同一人 /
  同一个概念吗？", "和 X 相关的人物还有谁？" → **start with `related`**,
  not `search`. `related` returns candidate keywords — **clues, not
  answers**: harvest the names / entities in the list and test each with
  `search` (recipe: "Consuming related output"). For identity questions
  run independent evidence chains per side (anti-pattern #1).
- **Literal / location** — "X 出现在哪？", "包含 X 的段落 / 章节？" →
  `probe` → `search` → `expand`.
- **Vague / open-ended** → `probe` first; escalate via stuck signals.
  For **discovery** use OR: `grga search "A B C" --implicit-or` (or `A || B || C`)
  — reserve implicit AND for verifying a hypothesis you already have.

**Friction-free basics (read once):**

- **Multi-word is implicit AND** — `"A B C"` matches lines with all
  three. Discovery → use OR (`--implicit-or`, or `||`); verification →
  AND.
- **`-d / --directory` is a directory, not a file** — for a single file
  pass its parent directory. Pin it to one file / one type with
  `-g '*name.txt'` (case-insensitive iglob, repeatable) so PDFs and
  other books in the same tree don't pollute results.
- **`related` takes `--scope`, not `-d`** — omit `--scope` for cross-scope
  union, or pick one from `grga scope list --json` (never `sqlite3`).

## 30-second quickstart

Cheapest-first cost order — see **Start here** for entry-verb routing
(entity / identity / relationship questions enter at step 4, not 1).

```bash
grga probe "your query" --directory ./docs --json                            # 1) cheap size check before a full search
grga search "your query" --directory ./docs --json                           # 2) baseline hits + metadata; pin to one file with -g '*name.txt'
grga expand "path/to/file.md:42" --directory ./docs --level chapter --json   # 3) widen one hit into chapter/paragraph/lines
grga related "keyword" --json  # 4) candidate keywords via co-occurrence — the ENTRY verb for entity/relationship questions (clues, not answers). Needs a pre-built offline index.
```

`suggest` is the LLM fallback and runs only on demand — see
`examples/search-suggest.md`.

## Storage model at a glance

grga reads a **shared per-user knowledge graph** at
`~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`), built
**offline by the user beforehand**. This skill never writes to it.

The graph is organised around **logical scope names** (portable across
machines; e.g. `physics-cn`, `/小说/科幻`). `probe` / `search` /
`expand` use `--directory` (physical dir, unrelated to scope);
`related` uses `--scope` / `--tag`, not `-d`.

Full scope/tag semantics, `--tag-logic`, knowledge-only scopes, and
DB-path override in `references/storage-model.md`.

## When to use this skill

Do **not** use for: web/HTTP fetches, editing files, autonomous "agent"
loops (grga verbs are stateless — the caller owns the loop), the
separate `grga deep-search` LLM-planned QA command, or as a replacement
for raw `rg` / `rga` (grga adds structure and graph on top; for raw
byte-level search prefer `rg` directly).

## The five verbs (contract summary)

Every verb accepts `--json`. All are **idempotent** and **read-only**,
except `suggest` (which calls an LLM). Full CLI usage and per-field
semantics in `references/verbs.md`; authoritative JSON schemas in
`docs/api.md`.

| Verb | One-line purpose | Use when |
|---|---|---|
| `grga probe <q> -d <dir>` | `rga --count-matches` — `total_lines` / `file_count`, no bodies. Makes no decision. | About to `search` an unfamiliar corpus / broad keyword / huge file — cheap size signal first. |
| `grga search <q> -d <dir>` | Hits + position metadata (`occurrence_index`, `line_ratio`, `nearby_hits`) + `heading_path` for Markdown. Space-separated terms are **implicit AND**; `--implicit-or` restores OR. `--max-total N` (default 1000) caps hits; overflow sets `truncated`. | Concrete query string. First **evidence-fetching** verb — for open-ended queries a T2-bookend `related` precedes it (see workflow). |
| `grga expand <path>:<line> --level <lines\|paragraph\|chapter\|document\|toc>` | Turn one hit into a `Context` block; degrades `chapter → paragraph → lines → lines(±0)`; `toc` = the file's outline. Always read the returned `level`. Never invokes an LLM. | Surrounding text is needed; want a whole Markdown chapter; need the file's outline / where the hit sits (`toc`). |
| `grga related <kw…> --mode <graph\|heading> [--scope <name> …] [--tag <label> …]` | `graph` → **candidate keywords + `npmi` + `scopes[]`** (not documents; caller routes `scopes[]` to scope-bound dirs, then ripgrep); `heading` → documents sharing a heading path. **Returns clues, not answers** — combine with `search` (recipe: "Consuming related output"). Empty `{"results": []}` = "no signal, move on" (needs a pre-built offline index). | Broaden, not deepen; **entry verb for entity / identity / relationship questions**. |
| `grga suggest <q> --intent <expand\|rerank\|reformulate> --hits <hits.json>` | **Only** verb that calls an LLM. Returns reformulated queries / expanded keywords / rerankings. | Too few / low-quality results, or query intrinsically vague. **Never** the first verb (see "Stuck signals"). |

### Supporting commands (read-side only)

- `grga graph inspect <keyword>` — read-only diagnostic on a keyword's
  scope distribution, `top_docs` (locate-me shortcut), and polysemy
  signal. **Not a substitute for `related`** (T2 is `related`, not
  `graph inspect` — see `references/reflection-loop.md`). Empty output
  means the keyword is not in the offline index — treat as "no signal".
  Full CLI, filters, and field semantics in `references/verbs.md`.
- `grga index export-candidates --seeds "…" --scope <name> [--cross-scope] [--out f.jsonl]`
  — **entity-asset raw-material export** (read-only, no LLM, no DB writes).
  Emits one JSONL candidate pair per line (`{seed, candidate, npmi,
  cooccur_count, source_kws, scope, context}`). Feed it to your LLM to
  organize entities/relations into `entities/<scope>/` — see
  `references/dream.md`. **Not** a build/write command: it only moves
  material out.

> Offline index construction (`grga index build` / `index rebuild` /
> `index promote`) is **out of scope for this skill**. The user is
> expected to have built the graph beforehand. Do not run those
> commands from within the skill loop. (`index export-candidates` is
> read-only and safe inside the loop when producing entity-asset material.)

## Tiered reflection workflow

The five verbs form a **four-tier reflection loop**, cheapest to most
expensive. Each tier ends with a caller-owned **evaluation checkpoint**:
read a few numbers off the JSON, decide *proceed / adjust / escalate*,
and only pay for the next tier when the current one is genuinely
insufficient. grga verbs never loop on their own — the caller owns
every decision.

**T2 has two distinct roles**, and conflating them is the single most
common way callers defeat the loop:

- **T2-bookend `related`** — a mandatory cheap sweep at *session open*
  and *pre-answer*. Not conditional on any signal.
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

Cost order: **probe ≪ search ≪ expand ≪ related ≪ suggest**. Exhaust
cheaper *escalation* tiers before spending on the next. **The two
T2-bookends are not part of that cost trade-off** — they are the
scaffolding around the whole loop, and a T2-bookend `related` is still
cheaper than one wrong-vocabulary T1 or a false-completeness answer.

Per-tier decision tables (what to read on the JSON, what to do next),
metadata idioms (`heading_path`, `line_ratio`, `nearby_hits`), seed
selection for `related`, and the `related` vs `graph inspect` rule live
in `references/reflection-loop.md`. Consult it when executing a tier.

Cross-tier guardrail: **always re-probe an LLM-reformulated query before
searching it** — it is unvalidated and can be either empty or explosive.

### T2 bookends: two mandatory sweeps + one reactive touchpoint

The tier ladder above shows *where* the two T2-bookends sit. This
section is their **binding definition**. The two bookends are **not
optional and not signal-gated** — they run on every open-ended search
task regardless of how good T1 looked.

1. **Session-open bookend (MANDATORY).** Before the first T1 `search`,
   run one T2 `related` from the caller's raw seed keywords to expand
   into a working seed set. Cheapest place to catch alias / vocabulary
   gaps before they compound. Skip **only** on a tiny hand-familiar
   corpus, and say so explicitly.

2. **Mid-session touchpoint (reactive, this is T2-escalate).** T1-driven,
   not mandatory: on stuck signal V (Vocabulary-mismatch) → escalate;
   or invoke by judgment (suspected alias in hits, topic shift widening
   the seed set).

3. **Pre-answer bookend (MANDATORY).** Before committing *any* answer,
   run one T2 `related` from the final seed set to catch missed aliases
   or adjacent evidence. **A T1 that "looks sufficient" does not waive
   this sweep** — that exact feeling is the failure mode this bookend
   exists to catch.

Each T2 call is one hop from the current seeds; chaining further calls
is caller judgment, not a workflow rule.

#### Pre-answer checklist (do not return an answer until all pass)

- [ ] The **session-open** T2-bookend `related` ran (or was explicitly
      waived as tiny/familiar corpus, with the reason stated).
- [ ] The **pre-answer** T2-bookend `related` just ran on the final seed
      set, and its `results` (graph 模式为候选关键字) were **read**, not
      merely invoked.
- [ ] Any new document or alias surfaced by that pre-answer sweep was
      either folded into the answer or explicitly dismissed as
      off-topic.

If any box cannot be ticked, run the missing sweep before answering.
"T1 already gave me enough" is **not** a valid reason to skip — it is
the precise trigger for the pre-answer bookend.

### Consuming `related` output (the recipe)

`related` returns **candidate keywords, never an answer** — reading the
list is caller work:

1. **Filter for entities.** Keep proper nouns / names / concrete terms;
   drop stopwords & discourse markers (`不过`, `什么`, `the`, `would`)
   and very-high-`chunk_df` hub words. Sort survivors by `npmi`.
2. **Feed them back into `search`.** `grga search "<c1> <c2> ..." --implicit-or -d <dir> --json` (or `rga -e c1 -e c2`) to locate & verify passages, then `expand` the best hits.
3. **Hop once more.** Promote a strong candidate (high `npmi`, moderate
   `chunk_df`) to the next `related` seed; 1-2 hops is usually enough.

**Identity questions:** person-name candidates ARE the suspect list —
`related "国字脸"` surfacing `方长 / 狂风 / 老白 / 夜十` means "these
people co-occur with the seed"; test each, do not discard as noise.

**Why is this candidate associated?** `related` keeps candidates light
(`kw` + `npmi` + `scopes[]`), no per-candidate document list. To see
*where* a candidate comes from — which files drive the association — run
`grga graph inspect <candidate> --docs-limit N`; its `top_docs` is the
locate-me shortcut. Call for the candidates you care about, not for all
of them — the mirror is query-on-demand.

### Entity assets (实体资产): explicit pre-search disambiguation query

**Consumption is an explicit step before search, not implicit in it.** Entity
assets (produced by your LLM from `index export-candidates` material, wiki
files under `entities/<scope>/`) are a **distinct source you consult
deliberately** — the caller can tell entity-asset clues apart from search
results, because reading them is its own action. When `entities/<scope>/`
exists for the scope you are about to search:

1. **Query the assets explicitly** — read the relevant entity files (e.g.
   `entities/work/史强.md`) and collect their `members` / relations.
2. **Decide & verify** — fold aliases into your query where useful (knowing
   `史强 = 大史`, searching `大史` becomes `大史 || 史强`) and verify
   relations against the raw text (`search` / `expand`).
3. **Then search** — `probe` / `search` run as usual.

**No entity assets → T0/T1 run exactly as written** — zero extra steps.

- Entity assets have **uncertain credibility** — they are hint, not truth.
  The `confidence` field is the producing LLM's own self-assessment and may
  be wrong, stale, or biased (e.g. a confident-but-wrong alias merge). Any
  alias / relation read from them must be verified against the raw text
  (`search` / `expand`) before it enters an answer. Never blind-trust the
  wiki; treat every entity-asset clue as a lead to check, not a fact.
- **Production is explicit** (never part of the retrieval loop): `grga index
  export-candidates` → your LLM → wiki files, run as a deliberate,
  separate workflow — see `references/dream.md`.

### Stuck signals & escalation

Watch these while inside T0/T1; when one fires, follow the table. Full
thresholds, symptoms, multi-signal interaction, and inline-index-build
rationale in `references/reflection-loop.md` § stuck signals.

- **Vocabulary-mismatch** — 2-4 consecutive `probe` zero-hits.
- **Literal-fixation** — 3-6 wordings of the same concept, all failing.
- **Wandering** — ≥ 5 same-family probes with no convergence.
- **Hit-paradox** — `search` returns near-zero hits, or many but all
  irrelevant (qualitative, read from `line_ratio` / `heading_path` /
  `matches[i].text`).

| Signal | First choice | Rationale |
|---|---|---|
| Vocabulary-mismatch | **T2-escalate `related`** (if the offline index has it; empty → sweep neighbours with T0 `probe` before moving on) | Corpus uses a term not guessed yet; structural neighbours route around wrong vocabulary. |
| Literal-fixation | **T3 `suggest --intent reformulate`** | Stuck in a literal spelling; LLM's job is to break the frame. |
| Wandering | **T3 `suggest --intent expand`** | Keyword space under-expanded; more probing in-family won't help. |
| Hit-paradox | **T3 `suggest --intent rerank`** | Hits exist but ordering is wrong. |

Empty `related` from a missing / partial offline index is **not** a
trigger to build one inside the skill — treat it as "no graph signal
for this seed"; fall through order and the T0 `probe`-sweep recipe live
in `references/reflection-loop.md` § "A missing offline index is not a
build trigger".

## Caller anti-patterns

Mental-model traps that defeat the loop even when every verb behaves
correctly. Full case-by-case walk-through, symptoms, and legitimate
exceptions in `references/anti-patterns.md`.

**Meta-rule** (for 2-4 below): before dropping into a lower-level tool,
name the specific grga verb being bypassed and why it is genuinely
insufficient — if none can be named, the urge is muscle memory, not
necessity.

1. **Do not pre-disambiguate identity questions** — when the question
   is *"are A and B the same entity?"*, run **two independent evidence
   chains** rather than baking the hypothesis into one query.
2. **Do not fall back to `grep` mid-workflow** — `grga search -B 0 -A 0
   --max-total N` is the loop-preserving equivalent.
3. **Do not open the shared DB with `sqlite3`** — the DB is internal
   storage, not a caller contract; use `grga graph inspect` for keyword
   introspection.
4. **Do not copy source data to a "clean" path** — `--directory` accepts
   any path with any Unicode / punctuation; copying invalidates the
   scope's directory binding and creates silent staleness.

## Typical composition patterns

Four worked examples ship with this skill:

- `examples/probe-search-reflect.md` — **default composition for
  open-ended search**: probe → tighten/loosen → search → expand, with a
  T3 fallback loop and both T2 bookends.
- `examples/search-expand-chapter.md` — "give me the full chapter that
  mentions X" (search → expand chapter, degrades to paragraph on non-md).
- `examples/search-related-graph.md` — "find other files discussing the
  same concept" (search → collect keywords → related graph).
- `examples/search-suggest.md` — cold-start / vague-query loop
  with in-session LLM reflection.

## References

Detailed material loaded on demand:

| File | Read when |
|---|---|
| `references/verbs.md` | Need a verb's full CLI usage or per-field semantics. |
| `references/dream.md` | Producing entity material (**explicit**: `index export-candidates` → organize into `entities/<scope>/` wiki) or the consumption model (explicit pre-search disambiguation query). |
| `references/storage-model.md` | Scoped query is thin and weighing `-d` vs cross-scope union; deciding `--db` override; indexing multiple corpora. |
| `references/reflection-loop.md` | Executing a tier (T0-T3); need per-tier decision tables, seed selection, stuck-signal thresholds, `graph inspect` disambiguation, or `suggest` context relaxation. |
| `references/anti-patterns.md` | Considering a `grep` / `sqlite3` / copy-to-clean-path shortcut, or an identity-disambiguation query. |
| `references/troubleshooting.md` | A verb misbehaves — empty `related`, `expand` degraded, `suggest` hangs. |
