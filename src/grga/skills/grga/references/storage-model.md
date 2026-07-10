# Storage model (deep dive)

Referenced from the "Storage model at a glance" section of `SKILL.md`.
This file holds the full decision-making detail; `SKILL.md` keeps only
the summary needed at trigger time.

## The core idea

grga is designed around a **single per-user knowledge graph**:
`~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`). Every
`index build` writes into that shared DB using the `directory` column as
a partition key, and every query verb defaults to **filtering results by
`--directory`** — scope is a *query-time filter*, not a partition
boundary. Give `-d ./project_a` and you narrow the answer to that corpus;
omit `-d` and the whole per-user graph is in scope.

Why: grga's target caller is an AI agent that hops across corpora
(novels, papers, code, notes) in one session. Keeping the graph unified
turns cross-corpus co-occurrence into a *feature* (recall the concept
regardless of which corpus you were browsing yesterday) while
`--directory` still lets a query stay inside one project when needed.

Index each corpus once, then all query verbs share the graph:

```bash
grga index build -s ~/novels  --db ~/.config/grga/deep_keywords.db --json
grga index build -s ~/papers  --db ~/.config/grga/deep_keywords.db --json
grga index build -s ~/code    --db ~/.config/grga/deep_keywords.db --json
# `related "x" -d ~/papers` sees only papers; omit -d to search all three.
```

## Three levers on `related`

- **Default (isolated)**: `related -d ./proj_a --mode graph` returns only
  `proj_a` documents. This is the safe default — cross-corpus association
  is an *escalation*, not the default retrieval path.
- **`--cross-scope` (advanced, `mode=graph` only)**: seeds stay in `-d`
  but PPR expansion crosses freely into other scopes. Use when the concept
  might be discussed under different names in different corpora and you
  have first read the observability signals below.
- **`--mode heading` default (cross-scope)**: matches shared Markdown
  heading paths across every corpus. Add `--same-scope` to restrict to
  `-d`. Heading paths are structural filters, so cross-scope is the more
  useful default here.

## Observability before escalating

- `related --mode graph --preview --json` computes a `crossed_scopes_preview`
  field (`would_add`, `top_other_scopes`, `top_bridge_keywords`) that
  answers "if I *did* open `--cross-scope`, what would come in?". Each
  `top_bridge_keywords` entry carries a `suspected_polysemy` flag (the same
  heuristic `grga graph inspect` applies), so the preview already tells you
  whether a bridge word is ambient jargon. Since v2.3 the preview shares
  the main query's PPR pass (no separate full-graph walk) so it costs at
  most one extra `keyword_docs` scan on top of a regular scoped query —
  cheap enough to pass routinely when the isolated result looks thin.
- `grga graph inspect <keyword>` reports `docs_by_scope`,
  `top_cooccurring_keywords`, and a `suspected_polysemy` heuristic. Use it
  for a deeper drill-down (per-scope neighbours, the `polysemy_hint` text)
  when the preview's `suspected_polysemy` flag alone is not enough. When
  the keyword was never indexed (`total_docs == 0`), the response omits
  the polysemy fields and returns an `index_hint` instead — key off which
  field is present to tell "indexed but single-meaning" from "never
  indexed".

Cross-scope results include a per-item `directory` field and a
`qualified_path` (``<directory>/<doc_path>``) so identically-named files
from different scopes remain distinct.

## When to enable `--cross-scope` (upgrade sequence)

`--cross-scope` is an **upgrade action**, not the day-to-day default.
Recommended sequence:

1. Run `search + expand + related` inside `-d` first (isolated mode).
2. If the result is thin, re-run `related --mode graph --preview --json`
   (preview is opt-in — this is where you pay for it). Read
   `crossed_scopes_preview`: `would_add` tiny → nothing new. `would_add`
   big **and** `top_bridge_keywords` full of generic words (`model`,
   `agent`, `attention`) with `suspected_polysemy: true` → likely polysemy
   noise. The preview already carries the polysemy verdict per bridge
   keyword, so no separate `grga graph inspect` pass is needed here.
3. Only enable `--cross-scope` when (a) the preview shows real incremental
   docs **and** (b) the bridge keywords are *domain* concepts, not
   ambient jargon.

(For a deeper drill-down on a specific keyword — per-scope neighbours and
the `polysemy_hint` text — `grga graph inspect <keyword>` is still there,
but it is no longer a required step in the escalation sequence.)

Cross-corpus association's value is *serendipity*, but serendipity is not
the same thing as noise. The sequence above lets you see the trade before
opening the gate.

## When to override with `--db <path>`

The shared per-user DB is the default. Pass an explicit `--db` when:

- **Multi-user machines** where per-user separation is not enough
  (each user still gets a separate `~/.config/grga/deep_keywords.db` by
  default; use `--db` only to escape that): `--db /secure/team.db`.
- **CI / test fixtures** that must not touch your personal DB:
  `--db "$TMPDIR/ci.db"`.
- **Sensitive projects** that must not co-occur with anything else:
  `--db ~/work/secret/graph.db`.
