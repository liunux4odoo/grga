# Pattern: search → suggest → promote (cold start / vague query loop)

**When to use.** The user's query is too vague, or `grga search` returns
too few useful hits. This pattern falls back to L3 (LLM) to reformulate
or expand the query, then closes the refeed loop by voting the new
keyword pairs into the graph so future queries hit them for free.

The pattern is deliberately **the last resort**: it is the only grga verb
that costs tokens. Exhaust `search`, `expand`, and `related` first.

## Storyboard

1. User: *"我想找乐观的例子"* (a vague, semantic query).
2. Agent runs `grga search` — few or zero hits.
3. Agent runs `grga suggest --intent reformulate` on the raw query.
4. Agent takes the returned `queries[]` / `keywords[]` and re-runs
   `grga search`.
5. `suggest` already voted the emitted keyword pairs into
   `llm_extension_votes`.
6. Later (batch / task-boundary), agent runs `grga index promote` to
   graduate high-vote pairs into `keyword_pairs`; next time
   `related --mode graph` will surface them without LLM cost.

## Commands

```bash
# 1) First-pass search. Save the (probably thin) result.
grga search "乐观的例子" \
    --directory tests/fixtures/docs \
    --json > /tmp/hits.json

# 2) If hits are too few / off-topic, ask the LLM to reformulate.
#    Feed the raw hits back so the model has context.
grga suggest "乐观的例子" \
    --intent reformulate \
    --hits /tmp/hits.json \
    --directory tests/fixtures/docs \
    --db /tmp/grga.db \
    --json > /tmp/sugg.json

# 3) Re-run search with each reformulated expression, merge results.
for q in $(jq -r '.queries[]' /tmp/sugg.json); do
    grga search "$q" -d tests/fixtures/docs --json >> /tmp/all_hits.jsonl
done

# 4) Later, refeed the graph. --dry-run first to inspect.
grga index promote --threshold 3 --db /tmp/grga.db --dry-run --json

# 5) Commit the promotion when comfortable.
grga index promote --threshold 3 --db /tmp/grga.db --json
```

## Expected shape of `suggest --intent reformulate`

```json
{
  "intent": "reformulate",
  "keywords": ["积极", "正向", "案例", "成功故事"],
  "queries": ["\"积极心态\" && 案例", "正向 || 成功"],
  "keyword_pairs": [["积极","案例"], ["正向","成功"]],
  "ranking": [],
  "scores": {},
  "reason": "查询过于抽象，展开为具体正向关键词",
  "llm_generated": true,
  "tokens_used": 312,
  "prompt_tokens": 248,
  "completion_tokens": 64,
  "model": "gpt-4o-mini",
  "votes": {"pairs_seen": 2, "pairs_recorded": 2, "votes_added": 3.0}
}
```

`queries[]` uses grga's boolean search syntax — feed each one directly
into `grga search`. `keywords[]` is safer to combine as an OR-joined
seed for `grga related`.

## Intent selection

Choose the intent based on what the caller currently has:

| Situation | Intent | What to feed back into |
|---|---|---|
| Vague natural-language query, no useful hits | `reformulate` | `search` (each `queries[i]`) |
| Search returned 30+ hits, need to prioritise | `rerank` | reorder using `ranking[]` |
| Search returned some hits, need more angles | `expand` | `search`/`related` (with `keywords[]`) |

## When *not* to use this pattern

- **The user is browsing a specific directory / file.** Keep it cheap:
  `search + expand` (see `search-expand-chapter.md`).
- **The corpus is small and mostly Markdown.** Structure alone
  (`heading_path`, `--mode heading`) often beats an LLM roundtrip.
- **In evaluation / dry runs.** Pass `--no-votes` to `suggest` so the
  graph is not polluted with experimental pairs. You can still parse the
  suggestion; only the write side is skipped.

## Refeed hygiene

- `promote --dry-run` prints exactly what would be merged; it never
  writes. Use it to sanity-check surprising pairs before committing.
- `llm_extension_votes` rows are **never deleted** by `promote`; if a
  wrong pair is graduated, revert by editing `keyword_pairs` directly.
- Default threshold is `3` votes. Raise it (`--threshold 5+`) for a
  strict corpus; lower it (`--threshold 2`) for aggressive experimentation.
- Scope promotion with `--directory <dir>` when the corpus contains
  unrelated sub-projects and you want to isolate each project's graph.
