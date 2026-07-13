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
| Hits concentrated in 1-2 files, but topic likely covered elsewhere | **T2-escalate** `related --mode heading` for structural breadth, or `related --mode graph` 拿候选关键字 + ripgrep 铺开 | 找同结构文档 / 借共现网扩词并回搜 |
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
- **Need other documents / concepts on the same topic** → `grga related`。
  `mode=graph`（需事先由用户离线构建索引）返回**候选关键字 + `npmi`**
  （v3 起不再返回文档；caller 拿候选关键字后走 ripgrep 找片段）；
  `mode=heading` 走 `heading_docs`，返回共享 Markdown 章节的文档。v3 起
  `--mode dir` 已移除，改用 `grga search --json` 后按目录聚合。若索引
  未覆盖 seed，`{"results": []}` 视为"无信号，跳过"。

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
| `related <seed> --mode graph` | "还有**哪些相关的关键词**能沿着这个 seed 展开？" | 候选关键字数组（`{kw, npmi, cooccur_count, via_seed, chunk_df, source}`）；caller 拿去 ripgrep 找片段 | **T2-bookend**（session-open + pre-answer, mandatory）和 **T2-escalate**（mid-session widening） |
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
seed returns a compact on-topic candidate keyword set; a poorly-chosen
seed returns `{"results": []}`, an irrelevant list, or hub noise. Seed
choice is the single biggest lever at T2 — **grga will not reject bad
seeds**, it computes 1-跳共现邻居 on whatever is provided.

**Prefer as seeds**: low-DF specific nouns (proper names of techniques,
entities, APIs, chapter titles — check `results[].chunk_df` on a probe
call, single-digit to low-hundreds is the sweet spot); names surfaced by
T1 `search` (harvest 2-3 low-DF specifics from `matches[i].text`);
multi-seed passes (2-4 diverse specifics beat one hub; grga 独立展开每个
seed，caller 拿多组候选后自行去重/取交集)。

**Avoid as sole seed**: hub words (protagonist, framework name,
top-level concept — high DF means co-occurs with everything，共现邻居里
全是 stopword)；generic descriptors ("main technique", "the API")；
terms not in the index（`{"results": []}` 常是这种：seed 从未进 `keywords`
表——用 T1 `search` 确认原文有该词但没被 tokenizer 抽出，选一个相邻的
low-DF surrogate）。

**Meta-rule**: seed selection is a **caller judgment**, not a grga
policy. The verb surfaces 每条候选自带的 `npmi` 与 `chunk_df`；the caller
decides whether to keep the seed or swap.

### Seed-quality decision table for `related --mode graph`

v3 起 `related --mode graph` 的返回不再有 `expansion` 顶层块——所有
信号扁平到每条 `results[]` 上（`{kw, npmi, cooccur_count, via_seed,
chunk_df, source}`）。种子自身的 `chunk_df` 需要用 `grga graph inspect
<seed>` 单独查（`total_docs` / `docs_by_scope` / `top_cooccurring_keywords`）
或从 `results[].via_seed`  + `results[].chunk_df` 反推（候选的 df 只反映
候选词自身，非 seed 自身）。

| Signal | Reading | Action |
|---|---|---|
| `graph inspect <seed>` → `total_docs == 0` | Seed 不是索引里的关键字 | 用 T1 `search` 确认原文有此词；若有，则 tokenizer 跳过——挑一个共现的 indexed 词做代理 |
| `graph inspect <seed>` → `total_docs` 1-few | Rare seed；通常信号强 | Proceed；检查 `results[].npmi` > 0.5 的候选 |
| `graph inspect <seed>` → `total_docs` 十至低百 | **大语料上的甜蜜点** | Proceed；expect a tight neighbourhood |
| `graph inspect <seed>` → `total_docs` 千+ / 邻居列被 stopword 主导 | Hub word — co-occurs with everything | Swap seed；pick a low-DF specific surrogate |
| `{"results": []}` on indexed seed | Seed 已被索引但孤立（所有边被写入期 npmi 门槛过滤） | 换 seed 或用多 seed；改 `--mode heading` 找结构邻居 |
| `results[].npmi > 0.5` | Strong semantic link | Trust；考虑把该候选作为下一轮 seed |
| `results[].npmi` 0.2-0.5 | Weak-to-moderate | Cross-check with T1 `search` before trusting |
| `results[].npmi < 0.2` | 写入期门槛已过滤——不会出现，除非 build 用了更低的 `--min-npmi-build` | 若真出现视作 noise-adjacent |
| 候选被 stopword / 冗余变体主导 | Seed is hub even if DF looks modest | Swap seed；multi-seed passes help |

**Multi-seed rule of thumb**: 2–4 diverse specifics fused into one call
让 grga 独立展开每个 seed，caller 拿到多组 `via_seed` 分组的候选后，用
交集/并集/取 top-npmi 自选融合策略（design D4：grga 不替 caller 决定
融合）。When in doubt about which specific to pick, run a T1 `search` on
the task topic first and harvest low-DF nouns from `matches[i].text`.

**Self-diagnosis loop when T2 returns nothing useful**:

1. `graph inspect <seed>` → `total_docs == 0` → seed 未索引 → 换 seed
2. `total_docs` 高、`top_cooccurring_keywords` 全是 stopword → hub →
   换 low-DF specific noun
3. `total_docs` 合理但 `{"results": []}` → 孤立 seed → multi-seed 或
   `--mode heading`
4. 一切看着正常但候选跑偏 → 一发 T3 `suggest --intent expand` 打破
   vocabulary 框架

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

## Task boundary

`suggest` output (keywords, queries, rankings) is consumed **in-session**
by feeding it back into `search` / `related` / `expand`. This skill does
not persist LLM suggestions into the offline graph; promotion into the
long-lived index is a user-owned offline task.

## Stuck signals & escalation (detail)

SKILL.md keeps the short list of four signals + the escalation table.
This section is the **binding definition** — thresholds, symptoms, and
multi-signal interactions.

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

### A missing offline index is not a build trigger

`related --mode graph` needs an offline index built by the user. If it
is absent or does not cover the seed, `related` returns
`{"results": []}`. This skill is **retrieval-only** — do **not** run
`grga index build` from inside the loop to fill the gap. Read empty
`related` as "no graph signal for this seed" and fall through: try a
different seed, `--mode heading`, T3 `suggest`, or continue on the T1
evidence already in hand. This applies equally to the T2-bookends — a
missing index simply means the bookend produced no additional signal,
not that a build must be triggered.

### `suggest` guardrails: production vs. exploration

The verb-level guardrail "never the first verb, capped at 1-2
escalations per task" is written for the **production caller context** —
a real end-user query on a well-understood corpus where LLM cost
matters.

Relax in two other contexts:

- **Cold-start on an unfamiliar corpus.** The corpus's terminology is
  not yet known; a `suggest --intent expand` early in the session is
  legitimate and often cheaper than five wrong-terminology probes.
- **Skill / workflow testing, exploratory analysis.** Deliberately
  exercising the loop; `suggest` is what is being tested.

The 1-2-escalation cap still applies **per user task**, not per session
in exploratory contexts.
