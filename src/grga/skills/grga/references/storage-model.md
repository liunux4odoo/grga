# Storage model (deep dive)

Referenced from the "Storage model at a glance" section of `SKILL.md`.
This file holds the full detail; `SKILL.md` keeps only the summary needed
at trigger time.

## The core idea

grga reads from a **single per-user knowledge graph** at
`~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`). The DB is
**built offline by the user** (outside this skill).

The graph separates two kinds of information:

* **Knowledge (portable across machines)** — the learned keyword
  co-occurrence graph, keyed by a **logical scope name** (e.g.
  `physics-cn`, `/小说/科幻`), not a filesystem path. A pair learned on
  machine A survives verbatim on machine B under the same scope name;
  the user can `grga index export` / `import` it wholesale.
* **Evidence (machine-local)** — scope→directory bindings and the
  per-document indexes that let `related --mode heading` and doc-locating
  ripgrep find real files. Rebuilt locally; a knowledge pack never
  includes it.

From this skill's perspective all query verbs simply read that shared
graph. If a scope is missing from the index, `related` / `graph inspect`
return empty — treat as "no signal" and proceed with the other evidence.

## Read-side scope semantics

`related --mode graph` filters by **`--scope <name>`**. Multi-select is
supported:

* `--scope physics-cn` — restrict to that single scope (sub-millisecond
  hot path).
* `--scope a --scope b` — union across the listed scopes.
* omit `--scope` — cross every indexed scope.

Every returned candidate carries a `scopes: List[str]` field — the scopes
within the caller's query range that actually memorise the pair.
Cross-scope query → every scope in the DB that carries it; single-scope
query → singleton; multi-scope → the subset that matched. Sorted
ascending, never empty.

### Tag filter

Scopes carry **multi-valued tag labels** (built via `grga index build
--tag <label>` or `grga scope tag add`). `related --mode graph` accepts
tag filters that resolve into a scope-id set:

* `--tag 科幻` — every scope carrying that label.
* `--tag 科幻 --tag 小说 --tag-logic or` (default) — scopes with **any**
  listed label.
* `--tag 科幻 --tag 小说 --tag-logic and` — scopes with **every** listed
  label.

`--scope` and `--tag` combine as **intersection** — a scope must be in
the name list AND satisfy the tag filter. If either filter is non-empty
but resolves to zero scopes, `related` returns empty (does NOT fall
through to cross-scope). Both empty → cross-scope union.

`--tag` applies only to `--mode graph`; heading mode rejects it. The
hot-path verbs (`probe` / `search` / `expand`) do **not** accept
`--tag` — to narrow those by tag, first run `grga related --tag ...`
(or `grga scope list --json`) to enumerate scope names, then feed those
names into `search --scope <name>`.

`related --mode heading` returns documents that share a Markdown heading
path with a seed-carrying document. Cross-scope by default (heading paths
are structural filters, resist same-name-different-meaning noise); add
`--same-scope` (with exactly one `--scope`) to restrict to a single
scope. Each result carries `scope: <name>` disambiguating
same-relative-path docs across scopes.

## Locating a candidate's original text

`related --mode graph` returns keywords, not file paths — the caller
resolves them to passages via ripgrep. The `scopes[]` field tells the
caller **which scope binding to point ripgrep at**:

```python
# 1. pick a candidate
cand = payload["results"][0]           # {"kw": "曲率", "scopes": ["physics-cn"]}
# 2. resolve scope → local directory
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

`chunk_df` is a hub-word discriminator, not a ranking input — a candidate
with a very high `chunk_df` co-occurs with everything and is usually
noise. With `--scope` it sums over the queried scopes; without `--scope`
it sums across every scope.

Caller-side pruning: `if r["chunk_df"] > threshold: drop`. The read side
can only tighten recall with `--min-npmi`, never relax it below the
write-time floor.

## Knowledge-only scopes (`grga index import`)

A scope that arrived via `grga index import <pack> --as <name>` carries
the knowledge graph but no local evidence. `related --mode graph` still
works — knowledge is enough for candidate expansion. But
`related --mode heading` will return empty on that scope, and
`graph inspect` emits an `evidence_hint` explaining the state:

> "'physics-cn' has knowledge in [physics-cn] but no local corpus
> evidence. Run `grga index build --scope <name> --directory <path>`
> to attach local documents, or use `grga related --scope <name>` for
> pure knowledge-graph expansion (evidence not required)."

Building the local evidence is a user-side action (outside this skill).

## Observability

`grga graph inspect <keyword> --db <path> --json` — keyword diagnostic:

* `docs_by_scope`: the keyword's doc distribution grouped by **logical
  scope name**. Empty on a knowledge-only scope (see previous section).
* `top_cooccurring_keywords`: top co-occurrence neighbours with
  `shared_scopes` (list of scope names carrying each neighbour).
* `suspected_polysemy` heuristic: true when the neighbour lists in
  different scopes barely overlap — signals same-name-different-meaning
  noise.
* When the keyword is not in the index: outputs `index_hint` +
  `similar_keywords` (substring/superstring vocab probe, `kind ∈
  {substring, superstring}`). `suspected_polysemy` / `polysemy_hint`
  fields are absent — key on field presence to distinguish the two
  branches.
* Knowledge-only branch (known via imported knowledge but no local
  corpus): outputs `knowledge_scopes` + `evidence_hint` alongside a
  normal cooccurrence report. Distinguishes "genuinely unknown seed"
  from "seed known via imported knowledge, no local corpus".

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
