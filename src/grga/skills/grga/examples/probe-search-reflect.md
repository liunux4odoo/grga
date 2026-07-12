# Pattern: probe → search → reflect (tiered size-first loop)

**When to use.** The corpus size or the query breadth is unknown: an
unfamiliar repository, a broad single keyword, or a directory that may
contain GB-scale files. Sizing the query with `probe` first avoids both a
wasted full `search` that returns nothing and a truncated `search` that
floods the context window.

This is the **default composition** for open-ended search. It layers the
five verbs by cost — `probe ≪ search ≪ expand ≪ related ≪ suggest` — with
an explicit caller-owned evaluation checkpoint after every tier.

## Storyboard

1. User: *"这个项目怎么做鉴权的？"* — a real question, unknown corpus size.
2. **T2-bookend (session open, MANDATORY)** Agent runs
   `grga related 鉴权 --mode graph` from the raw seed to expand into a
   working seed set (surfaces e.g. `token`, `session`, `middleware`).
3. **T0** Agent probes `鉴权` — `total_lines` is huge and one file dominates.
4. Agent tightens to `鉴权 && token`, re-probes — lands in a sane band.
5. **T1** Agent runs `search` with the validated query — gets ~12 hits
   across 4 files with useful `heading_path`s.
6. Evaluation: hits are distributed and well-labelled → looks good.
7. **T2-escalate** Agent `expand`s the top hit to chapter level for a
   quotable block. (Optional — driven by need for prose, not by a rule.)
8. (Only if T1 had been thin/empty) **T3** `suggest --intent reformulate`,
   then loop the new query back through **T0**.
9. **T2-bookend (pre-answer, MANDATORY)** Agent runs
   `grga related "$FINAL_SEEDS" --mode graph` on the final seed set to
   catch any missed aliases / adjacent evidence. Only after reading its
   `results` does the agent commit the answer.

## Commands

```bash
DIR=./src
DB=~/.config/grga/deep_keywords.db

# ---- T2-bookend (session open, MANDATORY): expand the raw seed --------
grga related "鉴权" --mode graph -d "$DIR" --db "$DB" --json > /tmp/seed0.json
#   read results[] + expansion.expanded_keywords[] -> harvest low-DF neighbours
#   (token / session / middleware ...) into the working seed set

# ---- T0: size the query cheaply -------------------------------------
grga probe "鉴权" -d "$DIR" --per-file --json > /tmp/p0.json
#   read total_lines / file_count / per_file[0]
#   -> too broad, one file dominates. Tighten:
grga probe "鉴权 && token" -d "$DIR" --sample 20 --json > /tmp/p1.json
#   read total_lines (sane) + sample.est_total_bytes (fits budget) -> proceed

# ---- T1: baseline search with the *same* validated params -----------
grga search "鉴权 && token" -d "$DIR" --json > /tmp/hits.json
#   evaluate matches[]: count, truncated, heading_path spread

# ---- T2-escalate: widen the single best hit into a chapter ----------
grga expand "$(jq -r '.matches[0].path' /tmp/hits.json):$(jq -r '.matches[0].line_number' /tmp/hits.json)" \
    -d "$DIR" --level chapter --json > /tmp/ctx.json

# ---- T3 (only if T1 was empty/weak): LLM reflect, then loop back ----
grga suggest "这个项目怎么做鉴权的？" \
    --intent reformulate --hits /tmp/hits.json \
    -d "$DIR" --db "$DB" --json > /tmp/sugg.json
for q in $(jq -r '.queries[]' /tmp/sugg.json); do
    # re-validate the LLM's new query with a cheap probe before searching
    total=$(grga probe "$q" -d "$DIR" --json | jq -r '.total_lines')
    [ "$total" -gt 0 ] && grga search "$q" -d "$DIR" --json >> /tmp/all.jsonl
done

# ---- T2-bookend (pre-answer, MANDATORY): final-seed sweep -----------
grga related "鉴权 token session" --mode graph -d "$DIR" --db "$DB" --json > /tmp/seedN.json
#   read results[] -> fold any new doc/alias into the answer or dismiss it.
#   Only now commit the answer.
```

## Evaluation checkpoints (caller-owned)

| Tier | Read from JSON | Decide |
|---|---|---|
| T2-bookend (open) | `results[]`, `expansion.expanded_keywords[].npmi`, `expansion.seeds[].chunk_df` | harvest low-DF neighbours into the working seed set before T0 |
| T0 probe | `total_lines`, `file_count`, `per_file`, `sample.est_total_bytes` | 0 → loosen / reformulate; huge → tighten; band → proceed |
| T1 search | `matches` length, `truncated`, `heading_path`, `nearby_hits` | empty → T0/T3; truncated → cap/tighten; thin → T2-escalate/T3; good → toward answer |
| T2-escalate expand/related | returned `text` / neighbour `doc_path`s | sufficient → toward answer; corpus lacks material → T3 |
| T3 suggest | `queries[]`, `keywords[]`, `ranking[]` | feed back to T0/T1, or reorder with `ranking[]` |
| T2-bookend (pre-answer) | `results[]`, `expansion.expanded_keywords[]` | new doc/alias → fold in or dismiss; only then commit answer |

## Why probe first (and not just search)

- `probe` runs `rga --count-matches` — no match bodies, no context lines,
  no `--max-total` streaming. It is strictly cheaper than the `search` it
  precedes.
- `total_lines` is the **real** hit count `search` would return, so it is a
  faithful predictor. It ignores context expansion and caps by design.
- Re-probing after a tighten/loosen costs almost nothing, so 2-3 sizing
  iterations are fine where a single re-`search` on a huge corpus would not
  be.

## Guardrails

- Cap T0 re-probes at ~2-3 and the whole T3 loop at a small number of LLM
  escalations; if the query will not fall into band, pick the closest tier
  and proceed rather than spinning.
- `probe` never decides for you — it returns numbers; the thresholds
  ("huge", "thin", "band") are the caller's policy.
- Always re-probe an LLM-reformulated query before searching it: the new
  query is unvalidated and may be empty or explosive.
- `est_total_bytes` is order-of-magnitude only; do not do exact arithmetic
  on it.
