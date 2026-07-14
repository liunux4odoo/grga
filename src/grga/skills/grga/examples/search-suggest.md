# Pattern: search → suggest (cold start / vague query loop)

**When to use.** The user's query is too vague, or `grga search` returns
too few useful hits. This pattern falls back to L3 (LLM) to reformulate
or expand the query in-session.

The pattern is deliberately **the last resort**: it is the only grga verb
that costs tokens. Exhaust `search`, `expand`, and `related` first. The
LLM output is consumed in-session only — this skill does not persist it
into the offline graph.

## Storyboard

1. User: *"我想找乐观的例子"* (a vague, semantic query).
2. Agent runs `grga search` — few or zero hits.
3. Agent runs `grga suggest --intent reformulate` on the raw query.
4. Agent takes the returned `queries[]` / `keywords[]` and re-runs
   `grga search`.
5. Agent continues the reflection loop with the new hits (expand /
   related as needed).

## Commands

```bash
# 1) First-pass search. Save the (probably thin) result.
grga search "乐观的例子" \
    --directory tests/fixtures/docs \
    --json > /tmp/hits.json

# 2) If hits are too few / off-topic, ask the LLM to reformulate.
#    Feed the raw hits back so the model has context.
#    --scope 是 v10 逻辑 scope 名（可选：仅用于把 LLM 提出的关键字对
#    记账进 llm_extension_votes；不传则跳过投票记录）。
grga suggest "乐观的例子" \
    --intent reformulate \
    --hits /tmp/hits.json \
    --scope docs \
    --json > /tmp/sugg.json

# 3) Re-run search with each reformulated expression, merge results.
for q in $(jq -r '.queries[]' /tmp/sugg.json); do
    grga search "$q" -d tests/fixtures/docs --json >> /tmp/all_hits.jsonl
done

# 4) Guardrail: re-probe any LLM-produced query before trusting it —
#    it is unvalidated and can be either empty or explosive.
for q in $(jq -r '.queries[]' /tmp/sugg.json); do
    grga probe "$q" -d tests/fixtures/docs --json
done
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
  "model": "gpt-4o-mini"
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

## Loop hygiene

- **Always re-probe LLM output** before spending on `search` — the
  suggestion is unvalidated.
- Cap the T3 loop at 1-2 escalations per user task; if it still stalls,
  report the evidence gap rather than looping further.
- LLM suggestions are consumed **in-session**. Persisting them into the
  offline graph (vote accumulation / promotion) is a user-side offline
  task, out of scope for this retrieval skill.
