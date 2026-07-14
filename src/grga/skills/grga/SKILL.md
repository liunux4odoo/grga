---
name: grga
description: This skill should be used when a caller agent needs to search a local repository, expand a hit into wider context, find related documents through a keyword co-occurrence / heading graph, or ask an LLM to reformulate a stalled query. It exposes the five grga v2 read verbs (probe / search / expand / related / suggest) and prescribes a tiered probe->search->expand/related->suggest reflection loop so the caller can size a query cheaply, act on evidence, and escalate to LLM only when necessary. The skill is **retrieval-only** — it assumes the offline graph index has already been built by the user; if `related` / `graph inspect` returns empty, treat that as "no signal available" and continue with the remaining evidence. Prefer this skill for structure-aware Markdown search, chapter-level context extraction, and cross-document navigation on the local filesystem. It does not fetch URLs, does not modify source files, and only calls an LLM inside `suggest`.
---

# grga skill

grga v2 is a **local, structure-aware search toolbox** built on top of
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
- **Post-processing is fine.** `sed`, `awk`, `wc`, `sort`, `uniq`,
  `jq`, `cut`, and friends remain the right tools for slicing,
  counting, aggregating, or reformatting `grga` output. Pipe `grga
  --json` (or plain) into them freely.
- **Naming the bypass.** If a task truly needs a non-`grga` search
  tool, first name the specific `grga` verb being bypassed and the
  concrete reason it is insufficient (see the anti-patterns section).
  Absent that justification, the urge is muscle memory, not necessity.

## 30-second quickstart

```bash
grga probe "your query" --directory ./docs --json                            # 1) cheap size check before a full search
grga search "your query" --directory ./docs --json                           # 2) baseline hits + metadata
grga expand "path/to/file.md:42" --directory ./docs --level chapter --json   # 3) widen one hit into chapter/paragraph/lines
grga related "keyword" --json  # 4) candidate related keywords via co-occurrence (needs a pre-built offline index); pipe them into rga for passages. Empty result → sweep 2-4 domain-adjacent keywords with T0 `probe` before giving up.
```

`suggest` is the LLM fallback and runs only on demand — see
`examples/search-suggest.md`.

## Storage model at a glance

grga reads a **shared per-user knowledge graph** at
`~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`), built
**offline by the user beforehand**. This skill never writes to it.

Since v10 (plan-2026-07-14) the graph has a **knowledge / evidence
split**: knowledge tables are sharded by a **logical scope name**
(portable across machines; e.g. `physics-cn`, `/小说/科幻`); evidence
tables (`docs` / `keyword_docs` / `heading_docs` / `scope_directories`)
are machine-local and rebuilt via `grga index build`.

**读侧 scope 语义（v10）**：

- `related --mode graph` 用 `--scope <name>`（**多选**：`--scope a
  --scope b` 走 `WHERE scope_id IN (...)`；不传 `--scope` 则跨全部
  scope 联查）。每条返回的候选带 `scopes[]` 字段，告诉 caller 它落在
  哪几个 scope，用来路由 ripgrep 到对应目录。
- `related --mode heading` 默认跨 scope（heading paths 是结构化过滤，
  抗同名歧义）。加 `--same-scope`（要求恰好一个 `--scope`）以限制。
- 一个 scope 可以是"knowledge-only"（`grga index import` 后本机还没
  build）——`related --mode graph` 依然工作，`--mode heading` 会返回
  空，`graph inspect` 会给出 `evidence_hint`。

**读 verbs 的 `--directory` 语义**：`probe` / `search` / `expand` 是
纯 rga/文件系统扫描动词，它们的 `--directory` 是**物理目录**，不进
图 DB、跟 scope 无关。`related` 只有 `--scope`（逻辑名）；
`deep-search` / `index build` 两者都要（物理目录 + 逻辑归属）。

Full v10 scope semantics、`RelatedKeyword.scopes[]` 用法、
knowledge-only 分支、`--db` override 场景 → `references/storage-model.md`。

## When to use this skill

Use grga when the task involves any of the following on a local directory:

- Full-text or boolean search across mixed content (Markdown, source
  code, PDF/Office via `rga` preprocessors) with **position metadata**
  (`occurrence_index`, `line_ratio`, `nearby_hits`, `heading_path`).
- Turning a single hit into a **wider textual context** (nearby lines,
  paragraph, Markdown chapter, whole file).
- Answering "**what else is related to this?**" via co-occurrence graph
  （v3 起返回候选关键字 + `npmi`，caller 用 ripgrep 找片段）, or shared
  Markdown heading paths（返回文档）.
- Asking an LLM to expand keywords, rerank hits, or reformulate a vague
  query — only after cheaper layers have been tried.

Do **not** use for: web/HTTP fetches, editing files, autonomous "agent"
loops (grga verbs are stateless — the caller owns the loop), or the
separate `grga deep-search` LLM-planned QA command.

## The five verbs (contract summary)

Every verb accepts `--json`. All are **idempotent** and **read-only**,
except `suggest` (which calls an LLM). Full signatures and per-field
semantics in `references/verbs.md`; authoritative JSON schemas in
`docs/api.md`.

| Verb | One-line purpose | Use when |
|---|---|---|
| `grga probe <q> -d <dir>` | `rga --count-matches` — returns `total_lines`, `file_count` without bodies. Makes no decision. | About to `search` an unfamiliar corpus / broad keyword / huge file — want a cheap size signal first. |
| `grga search <q> -d <dir>` | Hits + Layer 2 position metadata (`occurrence_index`, `line_ratio`, `nearby_hits`) + Layer 1 `heading_path` for Markdown. Space-separated terms are **implicit AND** (v2 breaking; `--implicit-or` restores OR). `--max-total N` (default 1000) sets `truncated`. | Concrete query string. First **evidence-fetching** verb — but for open-ended queries a T2-bookend `related` sweep precedes T1 (see workflow). |
| `grga expand <path>:<line> --level <lines\|paragraph\|chapter\|document>` | Turn one hit into a `Context` block. Degrades `chapter → paragraph → lines(±3) → lines(±0)`. Always read the returned `level`. Never invokes an LLM. | Hit is interesting and surrounding text is needed; want a whole Markdown chapter; quoting a block into another prompt. |
| `grga related <kw…> --mode <graph\|heading> [--scope <name> …] --db <path>` | Neighbours of seed keywords：`graph` 返回**候选关键字 + `npmi` + `scopes[]`**（v3 起不再返回文档，caller 按 `scopes[]` 定位 scope 绑定目录再 ripgrep；查 `keyword_pairs`），`heading` 返回共享 Markdown heading path 的**文档**（`scope` 字段区分同名文件）。`--scope` 可**重复**做多选（`WHERE scope_id IN (...)`）；不传 `--scope` 跨全部 scope 联查。v3 起 `--mode dir` 已删除——改用 `grga search --json` 后按目录聚合。**Precondition**: the offline index must already have been built by the user; if not, `related` returns `{"results": []}` — treat empty as "no signal, move on". | Caller has keywords and needs to broaden — not deepen — the exploration. |
| `grga suggest <q> --intent <expand\|rerank\|reformulate> --hits <hits.json>` | **Only** verb that calls an LLM. Returns reformulated queries / expanded keywords / rerankings. | Search returns too few / low-quality results, or query is intrinsically vague. **Never** the first verb (see "Stuck signals" for triggers and relaxed guardrails). |

### Supporting commands (read-side only)

- `grga graph inspect <keyword> --db <path>` — diagnostic on a keyword's
  scope distribution, top co-occurring keywords, polysemy signal. **Not
  a substitute for `related`** (see `references/reflection-loop.md`
  § T2 is `related`, not `graph inspect`). Empty output means the
  keyword is not in the offline index — treat as "no signal" and skip.

> Offline index construction (`grga index build` / `index rebuild` /
> `index promote`) is **out of scope for this skill**. The user is
> expected to have built the graph beforehand. Do not run those
> commands from within the skill loop.

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
for this seed". Before spending on T3, try one **T0 `probe` sweep** of
2-4 hand-picked domain-adjacent keywords (synonyms, sibling concepts,
super/sub-terms): it is the cheapest way to check whether the vocabulary
gap is one hop away, and non-zero counts feed straight back into T1.
Only fall through to T3 or to the existing T1 evidence if the probe
sweep also comes up empty.

## Caller anti-patterns

Mental-model traps that defeat the loop even when every verb behaves
correctly. Full case-by-case walk-through, symptoms, and legitimate
exceptions in `references/anti-patterns.md`.

**Meta-rule for the tool-substitution family** (2-4 below): when
tempted to drop into a lower-level tool, name the specific grga verb
being bypassed and why it is genuinely insufficient. If no verb can be
named, the urge is muscle-memory, not necessity. Dropping out silently
gives up the skill's stability guarantees, position metadata, and
partition semantics.

1. **Do not pre-disambiguate identity questions** — when the question
   is *"are A and B the same entity?"*, run **two independent evidence
   chains** rather than baking the hypothesis into one query.
2. **Do not fall back to `grep` mid-workflow** — `grga search -B 0 -A 0
   --max-total N` is the loop-preserving equivalent.
3. **Do not open the shared DB with `sqlite3`** — the schema is not a
   caller contract; use `grga graph inspect` for keyword introspection.
4. **Do not copy source data to a "clean" path** — `--directory` accepts
   any path with any Unicode / punctuation; copying invalidates the
   scope's `scope_directories` binding + `docs.doc_path` and creates
   silent staleness.

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

Composition notes:

- **Probe first when in doubt**. Re-probing 2-3 times costs almost
  nothing.
- **Skip expand for tiny hits** — quote `matches[i].text` if the hit
  line already answers.
- **Chain expand + related** — after `expand --level chapter`, feed
  chapter headings into `related --mode heading`.
- **Rerank before expand** on a broad `search` (50+ hits): `suggest
  --intent rerank` first, then `expand` top 3-5.
- **Re-probe LLM output** from `suggest --intent reformulate` before
  searching it.

## Practical CLI invariants

- Every verb accepts `--json` and prints a single JSON document to
  stdout on success. On failure, exit non-zero and either return
  `"ok": false` in the JSON (probe / search / deep-search / suggest) or
  a plain error string on stderr (expand / related / index). `probe`
  failures stay schema-homogeneous.
- Paths returned by `search` are **relative to the search directory**
  unless absolute inputs were passed. `expand` accepts either form.
- Shared SQLite DB defaults to `~/.config/grga/deep_keywords.db`
  (honours `$XDG_CONFIG_HOME`); `--db <path>` overrides.
- Markdown structure sidecar caches under `.grga/structure/` in the
  search directory root (or `$GRGA_SIDECAR_ROOT`). Rebuilt on `mtime`
  change.

## What this skill is not

- Not a QA agent — provides evidence; the caller composes the answer.
- Not a web / file-editing tool — grga never edits files.
- Not a replacement for `rg` / `rga` — for raw byte-level search prefer
  `rg` directly; grga adds structure and graph on top.

## References

Detailed material loaded on demand:

| File | Read when |
|---|---|
| `references/verbs.md` | Need a verb's full signature or per-field semantics. |
| `references/storage-model.md` | Scoped query is thin and weighing `-d` vs cross-scope 求和; deciding `--db` override; indexing multiple corpora. |
| `references/reflection-loop.md` | Executing a tier (T0-T3); need per-tier decision tables, seed selection, stuck-signal thresholds, `graph inspect` disambiguation, or `suggest` context relaxation. |
| `references/anti-patterns.md` | Considering a `grep` / `sqlite3` / copy-to-clean-path shortcut, or an identity-disambiguation query. |
| `references/troubleshooting.md` | A verb misbehaves — empty `related`, `expand` degraded, `suggest` hangs. |
