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

### `related` vs `graph inspect`: verb-choice reminder

SKILL.md covers the rule in full ("T2 is `related`, not `graph inspect`");
the one-line reminder here: **T2 exploration is always `related`.**
`graph inspect` is a *diagnostic* verb that answers questions about a
single keyword's shape in the graph, not a substitute for the
document-ranking that `related` does. If a first `related` call returns
`[]`, the correct next move is a **seed change**, not a switch to
`graph inspect`.

### Seed-quality decision table for `related --mode graph`

Read the `graph_signals` block off the `related` JSON, then decide:

| Signal | Reading | Action |
|---|---|---|
| `seed_chunk_df[seed] == 0` | Seed is not a keyword in the index | Run T1 `search` to confirm the term exists in corpus text; if yes, the tokenizer skipped it — pick a co-appearing indexed term as surrogate |
| `seed_chunk_df[seed]` 1-few | Rare seed; usually strong signal | Proceed; check `neighbor_edges` for npmi > 0.5 |
| `seed_chunk_df[seed]` tens - low hundreds | **Sweet spot on a large corpus** | Proceed; expect a tight neighbourhood |
| `seed_chunk_df[seed]` thousands+ | Hub word — co-occurs with everything | Swap seed; use `graph inspect <seed>` to *confirm* stopword-dominated `top_cooccurring_keywords`, then pick a low-DF specific surrogate |
| `results: []` with non-zero `seed_chunk_df` | Seed indexed but isolated (no chunk-cooccur edges above threshold) | Add another co-appearing seed; or fall back to `--mode dir` / `--mode heading` |
| `neighbor_edges[].npmi > 0.5` | Strong semantic link | Trust; consider adding the neighbour as a follow-up seed |
| `neighbor_edges[].npmi` 0.2-0.5 | Weak-to-moderate | Cross-check the neighbour with T1 `search` before trusting |
| `neighbor_edges[].npmi < 0.2` | Noise-adjacent | Ignore unless corroborated by other signals |
| Neighbours dominated by discourse markers / stopwords | Seed is a hub (even if DF looks modest) | Swap seed for a low-DF specific noun; multi-seed passes help |

**Multi-seed rule of thumb**: 2–4 diverse specifics fused into one call
concentrate PPR on the shared neighbourhood better than any single hub.
When in doubt about which specific to pick, run a T1 `search` on the
task topic first and harvest low-DF nouns from `matches[i].text`.

**Self-diagnosis loop when T2 returns nothing useful**:

1. `graph_signals.seed_chunk_df` empty → seed not indexed → switch seed
2. `seed_chunk_df` high, `neighbor_edges` = stopword-adjacent → hub →
   switch to a specific noun
3. `seed_chunk_df` reasonable, `results: []` → isolated seed → try
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
