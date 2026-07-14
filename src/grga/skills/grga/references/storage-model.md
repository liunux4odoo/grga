# Storage model (deep dive)

Referenced from the "Storage model at a glance" section of `SKILL.md`.
This file holds the full detail; `SKILL.md` keeps only the summary needed
at trigger time.

## The core idea (v10 knowledge / evidence split)

grga reads from a **single per-user knowledge graph** at
`~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`). The DB is
**built offline by the user** (outside this skill).

Since v10 (plan-2026-07-14) the DB draws a hard boundary between two
kinds of rows:

* **Knowledge (portable across machines)** — `scopes` + `keywords` +
  `keyword_pairs` + `keyword_stats` + `scope_stats` + `auto_ngram_dict`
  + `llm_extension_votes`. The shard key is a **logical scope name**
  (e.g. `physics-cn`, `/小说/科幻`), not a filesystem path. A pair
  learned on machine A survives verbatim on machine B under the same
  scope name; the caller can `grga index export` / `import` these rows
  wholesale.
* **Evidence (machine-local)** — `scope_directories` (scope → local
  path bindings) + `docs` + `keyword_docs` + `heading_docs` +
  `keyword_counts` + `keyword_sources`. These are wiped/rebuilt on
  transfer; a knowledge pack never includes them.

From this skill's perspective all query verbs simply read that shared
graph. If a scope is missing from the index, `related` / `graph
inspect` return empty — treat as "no signal" and proceed with the
other evidence.

## Read-side scope semantics

`related --mode graph` filters by **`--scope <name>`** (v10 rename from
pre-v10 `--directory`). Multi-select is supported:

* `--scope physics-cn` — restrict to that scope's `keyword_pairs`
  (composite index `(scope_id, kw_a_id, npmi DESC)` gives index-ordered
  early stop; sub-millisecond hot path).
* `--scope a --scope b` — union across the listed scopes (`WHERE
  scope_id IN (?,?)`).
* omit `--scope` — cross every indexed scope (accepts a temp-sort
  cost; TODO(perf) marker in `related.py`).

Every returned `RelatedKeyword` carries a `scopes: List[str]` field
(v3.1) — the scopes within the caller's query range that actually
memorise the pair. Cross-scope query → every scope in the DB that
carries it; single-scope query → singleton; multi-scope → the subset
that matched. Sorted ascending, never empty.

`related --mode heading` returns documents that share a Markdown
heading path with a seed-carrying document. Cross-scope by default
(heading paths are structural filters, resist same-name-different-
meaning noise); add `--same-scope` (with exactly one `--scope`) to
restrict to a single scope. Each result carries `scope: <name>`
disambiguating same-relative-path docs across scopes.

## Locating a candidate's original text

Post-v10, the read side deliberately no longer returns file paths from
`related --mode graph` — candidates are keywords, and the caller
resolves them to passages via ripgrep. The `scopes[]` field tells the
caller **which scope binding to point ripgrep at**:

```python
# 1. pick a candidate
cand = payload["results"][0]           # {"kw": "曲率", "scopes": ["physics-cn"]}
# 2. resolve scope → local directory (evidence layer)
scopes = json.loads(sh("grga scope list --json"))
bound = next(b["directory"] for s in scopes if s["name"] == cand["scopes"][0]
             for b in s["directories"])
# 3. ripgrep in the right corpus
sh(["rga", "-i", "-e", cand["kw"], bound])
```

`grga scope list` is authoritative for scope → directory bindings.
Never join `related` output with a hard-coded directory — a scope may
bind multiple directories, or may be knowledge-only (freshly imported,
no local corpus yet; `directories: []`).

## `chunk_df` semantics

`RelatedKeyword.chunk_df` is a hub-word discriminator, not a ranking
input. It reads `keyword_stats.chunk_df`:

* With `--scope` (single or multi): `SUM(chunk_df) WHERE scope_id IN
  (...)` across the queried scopes.
* Without `--scope`: `SUM(chunk_df)` across every scope.

Caller-side pruning: `if r["chunk_df"] > threshold: drop`. Write-time
floors (`--min-pair-count` / `--min-npmi-build`) already dropped the
noisiest edges before they entered `keyword_pairs`; the read side can
only tighten with `--min-npmi`, never relax.

## Knowledge-only scopes (`grga index import`)

A scope that arrived via `grga index import <pack> --as <name>`
carries `keyword_stats` / `keyword_pairs` (knowledge) but no `docs` /
`keyword_docs` / `scope_directories` rows (evidence). `related --mode
graph` still works — knowledge is enough for candidate expansion. But
`related --mode heading` will return empty on that scope, and `graph
inspect` will emit an `evidence_hint` explaining the state:

> "'physics-cn' has knowledge in [physics-cn] but no local corpus
> evidence. Run `grga index build --scope <name> --directory <path>`
> to attach local documents, or use `grga related --scope <name>` for
> pure knowledge-graph expansion (evidence not required)."

Building the local evidence is a user-side action (outside this
skill).

## Observability

`grga graph inspect <keyword> --db <path> --json` — keyword
diagnostic:

* `docs_by_scope`: the keyword's doc distribution grouped by **logical
  scope name** (v10). Empty on a knowledge-only scope (see previous
  section).
* `top_cooccurring_keywords`: top co-occurrence neighbours with
  `shared_scopes` (list of scope names carrying each neighbour).
* `suspected_polysemy` heuristic: true when the neighbour lists in
  different scopes have Jaccard < 0.2 — signals same-name-different-
  meaning noise.
* When the keyword is not in the index (`total_docs == 0` **and** the
  seed does not appear in `keyword_stats`): outputs `index_hint` +
  `similar_keywords` (substring/superstring vocab probe, `kind ∈
  {substring, superstring}`). `suspected_polysemy` / `polysemy_hint`
  fields are absent — key on field presence to distinguish the two
  branches.
* Knowledge-only branch (in `keyword_stats` but no `keyword_docs`):
  outputs `knowledge_scopes` + `evidence_hint` alongside a normal
  cooccurrence report. Distinguishes "genuinely unknown seed" from
  "seed known via imported knowledge, no local corpus".

Use it for **seed-quality assessment** (hub? rare?), **cross-scope
distribution audit**, and **polysemy detection**. It does not replace
`related` — see `references/reflection-loop.md` § "T2 is `related`,
not `graph inspect`".

## When to override with `--db <path>`

The shared per-user DB is the default. Pass an explicit `--db` when:

- **Multi-user machines** where per-user separation is not enough
  (each user still gets a separate `~/.config/grga/deep_keywords.db` by
  default; use `--db` only to escape that): `--db /secure/team.db`.
- **CI / test fixtures** that must not touch your personal DB:
  `--db "$TMPDIR/ci.db"`.
- **Sensitive projects** that must not co-occur with anything else:
  `--db ~/work/secret/graph.db`.

## Schema cheat-sheet (v10)

| Layer | Table | Portable? | Purpose |
|---|---|---|---|
| **Scope** | `scopes` | ✅ | Logical scope name intern (id ↔ name; v10 PK for every knowledge asset) |
| **Scope** | `scope_directories` | ❌ | Scope → local directory bindings; bookkeeping only |
| **Global intern** | `keywords` | ✅ | Casefold vocabulary (id ↔ text 1:1) |
| **Knowledge** | `keyword_pairs` | ✅ | Core asset: `(scope_id, kw_a_id, kw_b_id, source)` + `cooccur_count` + `npmi` |
| **Knowledge** | `keyword_stats` | ✅ | per-`(scope_id, kw_id)` frequency (chunk_df / doc_df / total_occurrences) |
| **Knowledge** | `scope_stats` | ✅ | per-scope scalar: `n_chunks` (npmi denominator N) / `n_docs` / `vocab_size` |
| **Knowledge** | `auto_ngram_dict` | ✅ | tokenizer-learned n-gram dictionary per scope |
| **Knowledge** | `llm_extension_votes` | ✅ | LLM-proposed pair votes + `promoted_at` marker |
| **Evidence** | `docs` | ❌ | Per-file metadata (size/mtime/hash) for incremental build |
| **Evidence** | `keyword_docs` | ❌ | Doc-level inverted index (seed → docs), replaces the deleted `keyword_chunks` |
| **Evidence** | `heading_docs` | ❌ | Markdown chapter structure per doc (for `related --mode heading` / `expand --level chapter`) |
| **Evidence** | `keyword_counts` / `keyword_sources` | ❌ | v1 deep-search heat / source counters |

**Removed in v10**: pre-v10 `directories` intern (absolute path
shard), `resolve_scope_key`, `scope_write_key`. Pre-v10 DBs are
rejected by `ensure_schema` with a rebuild hint — no in-place upgrade.
Historic design: `docs/design/plan-2026-07-12-keyword-primary.md` +
`docs/design/plan-2026-07-14-scope-directory-split.md`.
