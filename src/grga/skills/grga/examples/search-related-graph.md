# Pattern: search → related graph

**When to use.** The user has a topic keyword and wants to see **which
other documents in the repository discuss it**, not just where it literally
appears. This pattern leans on the offline L2 graph (`keyword_chunks` +
`keyword_pairs`) so it is fast and does **not** call an LLM.

## Storyboard

1. Prep once: build the graph index for the corpus.
2. User: *"Which files mention brew? What else is related to it?"*
3. Agent runs `grga search` to get literal hits + matched keywords.
4. Agent extracts a small keyword seed set from the top hits.
5. Agent calls `grga related --mode graph` and returns the ranked doc list.
6. If more angles are needed, also call `--mode heading` for cross-file
   chapter sharing.

## Commands

```bash
# 0) One-time (or on-repo-change) offline build.
grga index build \
    --scope tests/fixtures/cross_heading \
    --db /tmp/grga.db \
    --json

# 1) Literal search for the seed term.
grga search "brew" \
    --directory tests/fixtures/cross_heading \
    --json > /tmp/hits.json

# 2) Extract matched terms (keyword seeds) from the top few hits.
jq -r '.matches
       | map(select(.is_context == false))
       | .[0:5]
       | map(.matched_terms // [])
       | flatten | unique
       | .[]' /tmp/hits.json
# -> brew

# 3) Conceptually related docs (PPR over keyword co-occurrence).
grga related "brew" \
    --mode graph \
    --db /tmp/grga.db \
    --json

# 4) Optional: cross-file chapter neighbours ("safe alternative view").
grga related "macOS" \
    --mode heading \
    --db /tmp/grga.db \
    --json
```

## Expected shape of `related --mode graph`

```json
[
  {"doc_path": "uninstall.md", "score": 1.596026, "mode": "graph",
   "reason": "ppr", "keywords": ["brew","linux","macos"]},
  {"doc_path": "install.md",   "score": 1.010467, "mode": "graph",
   "reason": "ppr", "keywords": ["homebrew-only","linux","macos"]}
]
```

Interpretation:

- `keywords[]` on each row lists the graph-derived keywords that pulled
  that document into the neighbourhood (**not** the query). Handy for
  "why is this document here?" explanations.
- Sorted by `score` descending; use it directly as the ranked answer.
- Empty `[]` means the index is missing or the seed has no graph signal.
  In that case, either run `index build` again or degrade to
  `--mode dir` (filesystem proximity) or `--mode heading`.

## When *not* to use this pattern

- **The user gave a natural-language question, not a keyword.** Run
  `grga suggest --intent reformulate` first to convert it to concrete
  keywords, then use those with `related`.
- **The index has not been built.** `related --mode graph` will return
  `[]`. Either build the index or use `--mode dir` (works without an
  index — it uses filesystem structure).
- **You want a specific *chapter* across files** — that is
  `--mode heading` (see the `heading` example in the sidebar file
  `../SKILL.md`).

## Composition tip

Feed `related` output back into `expand`:

```python
related = json.loads(run("grga related 'brew' --mode graph --db /tmp/grga.db --json"))
top = related[0]["doc_path"]
# Expand the first heading of that document to preview it.
ctx = json.loads(run(
    f"grga expand '{top}:1' -d tests/fixtures/cross_heading "
    f"--level chapter --json"))
print(ctx["text"])
```
