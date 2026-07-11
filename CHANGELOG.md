# Changelog

All notable changes to `grga` are recorded here.

## [0.2.0] - 2026-07-09

### BREAKING

- **Wheel no longer bundles binaries.** `rg`, `rga`, `pandoc`, `fzf`, and
  `pdftotext` are downloaded at first use (or on demand via
  `grga tools install`). This trims the wheel from ~187 MB to under 1 MB
  and makes distribution license-compliant (upstream AGPL/GPL binaries
  are fetched at runtime, not redistributed).
- **Managed tools cache moved out of `site-packages`.** New default:
  `$XDG_DATA_HOME/grga/tools/<platform>` on Linux/macOS,
  `%LOCALAPPDATA%\grga\tools\<platform>` on Windows. Override with
  `$GRGA_TOOLS_DIR`. Legacy `site-packages/grga/data/tools/` is still
  discovered as a fallback; run `grga tools migrate` to copy it into the
  new location.
- **Stable exit-code contract frozen.** See `docs/exit-codes.md`. Codes
  may only be *added* in future releases, never removed or renumbered.
  `--json` errors now use a fixed schema on stdout so agents can pipe
  results reliably.

### Added

- `grga tools migrate` — copy legacy site-packages binaries into the
  XDG cache with `--dry-run` support.
- `grga tools install --from <dir>` — install from a local mirror
  directory (offline / air-gapped installs). Optional `--fallback-online`
  falls back to GitHub when a local asset is missing.
- `grga tools install --no-verify` and `GRGA_INSECURE=1` — bypass sha256
  verification (with a loud warning) for mirror scenarios.
- `tools.json` now supports optional `sha256` per tool; `install_tool`
  verifies the downloaded archive when present.
- `grga skill install --to <dir>` (+ `list` / `path`) — copy bundled
  agent skills into any workspace.
- `--offline` / `GRGA_OFFLINE=1` — fail fast instead of prompting to
  download missing tools.
- `MIN_VERSIONS` map (empty by default) lets future releases pin minimum
  versions per tool without more code.
- `GRGA_PREFER_SYSTEM=1` — invert tool resolution to prefer system-PATH
  binaries over managed ones.

### Changed

- `find_tool` now honors `GRGA_PREFER_SYSTEM` and applies `MIN_VERSIONS`.
- `check_tools` recognizes bundled binaries in both the XDG cache and
  the legacy site-packages location.
- `scripts/download-tools.py` extended with `--platform`, `--out`,
  `--write-hashes`, `--yes`. It is a *developer* tool only; wheel
  builds no longer invoke it.
- Project metadata (license, urls, classifiers, `Development Status`)
  hardened for PyPI publishing.

### Fixed

- Downloads write to a `.part` file then atomically rename on success,
  so aborted downloads no longer leave half-installed tools.

## Unreleased

### Breaking changes (chunk-level graph migration, schema v4)

- **The L2 graph moved from document-level to segment (chunk) level.**
  `related --mode graph` now runs Personalized PageRank over segment
  co-occurrence instead of whole-document co-occurrence. The doc-level graph
  was structurally unable to produce signal on large single-file corpora
  (a 22 MB novel collapsed every keyword into one `doc_path`, giving PPR no
  real edges). See `docs/design/plan-2026-07-11-chunk-migration.md`.

  The CLI surface is unchanged: `related --mode graph|dir|heading` keeps its
  name and parameters, so existing caller skills need no edits.

- **`keyword_docs` table dropped.** The doc-level inverted index is gone.
  `related` / `graph inspect` now read the new `keyword_chunks` table.

- **`keyword_pairs` schema rebuilt.** Primary key is now
  `(directory, keyword_a, keyword_b, source)`. New columns: `source`
  (`'chunk_cooccur'` | `'llm_promoted'`), `cooccur_count`, `npmi`. The old
  `(scope, directory, keyword_a, keyword_b)` / `count` shape is gone.

- **One-shot v3 → v4 migration.** `ensure_schema` detects a legacy database
  and, in a single atomic transaction: drops `keyword_docs`; rebuilds
  `keyword_pairs`; salvages old promotion rows (`scope='llm_promoted'`) as
  `source='llm_promoted'`; clears every other legacy (doc-level) pair — the
  segment build recomputes it. `PRAGMA user_version` bumps 3 → 4. Failure
  rolls back; no half-migrated state is left on disk.

- **`grga index build` gains chunk knobs.** `--chunk-mode {line,chars}`
  (default `line`), `--chunk-max-chars` (600), `--chunk-min-chars` (300),
  `--min-pair-count` (3). The old `--min-count` / `--max-terms` flags are
  removed. `chars` mode splits on `heading > blank line > hard char cut`.

- **`deep_search` LLM pairs route through votes.** `KeywordLibrary` no
  longer writes `keyword_pairs` directly; deep-search co-mentions accumulate
  in `llm_extension_votes` and graduate via `grga index promote` — the same
  anti-pollution gate the `suggest` flow already used. `keyword_counts`
  (self-evolving heat table) is unchanged.

### Added (chunk migration)

- **`graph_signals` metadata on `related --mode graph --json`.** Each result
  carries `seed_chunk_df` (per-seed segment df), `total_chunks_in_scope`, and
  `neighbor_edges` (PPR neighbour edges with `npmi` / `cooccur_count` /
  `source`). Callers can classify seeds (hub / domain / rare) and re-rank
  neighbours themselves — grga does no filtering / rerank / warning on top.
  `--graph-signals-limit N` caps `neighbor_edges` (default 30). The block is
  emitted only under `--json`; human-readable output stays terse.

- **`chunks` / `keyword_chunks` tables.** `chunks` records each chunk's line
  range + char count; `keyword_chunks` is the keyword → chunk inverted index
  (segment df is derivable via `COUNT(*) GROUP BY keyword`, no extra column).

### Performance

- **`related --mode graph` is 2–5× faster across every corpus size.** Five
  stacked optimisations (PERF plan §P0-1 through §P1-2), each with bench
  data in its own commit:
  - **P0-1** LRU cache of ``(edges, adjacency, indexed_graph)`` keyed on
    ``(db_path, mtime, scope)`` (cap 8, mtime-invalidated by `index build`
    / `suggest` / `promote`).
  - **P0-2** Preview reuses the main PPR result — a single unrestricted
    PPR now feeds both the scope-limited result list and the cross-scope
    `crossed_scopes_preview` annotation. `--preview` no longer costs a
    second full PPR.
  - **P0-3** PPR internals: hoisted ``sum(neighbours.values())`` into an
    ``out_degree`` map computed once per graph, and switched
    ``seed_in_graph`` to a set for O(1) exclusion. Semantics pinned by a
    reference-implementation diff test (score drift < 1e-9).
  - **P1-1** `IndexedGraph` — integer-indexed adjacency (``list[list[(int,
    float)]]``) with `personalized_pagerank_indexed`. The legacy dict API
    now delegates. Eliminates the 2.33M `dict.get` calls that dominated
    the profile.
  - **P1-2** ``keyword_docs.keyword`` stored casefolded (schema v3, one-
    shot migration bumps ``PRAGMA user_version`` 2→3, merging any
    same-fold collisions in place). Every read path drops the non-
    sargable ``LOWER(keyword)`` wrapper. The self-join in ``load_edges``
    keeps a ``+column`` optimiser barrier so SQLite still joins on
    ``(directory, doc_path)`` instead of range-scanning ``(directory,
    keyword > ?)``.

  Bench (600 docs / 3 scopes / 7414 keyword_docs rows, 5-call avg):

  |               | v2.2 baseline | v2.3 target |
  | ------------- | ------------- | ----------- |
  | scoped        | 151 ms        | **42 ms**   |
  | global        | 350 ms        | **94 ms**   |
  | cross-scope   | 354 ms        | **72 ms**   |
  | preview=True  | 527 ms (3.8×) | **96 ms**   |
  | PPR own time (3 calls) | 1.83 s | **0.18 s** |

  Numpy fallback is intentionally not shipped (PERF plan §P2-1); the pure-
  Python indexed path lands well inside the 100 ms perception threshold
  for personal-corpus graphs.

### Changed

- **Storage model reframed as user-scoped.** grga's DB is a single per-user
  knowledge graph (`~/.config/grga/deep_keywords.db`). All `index build`
  scopes write into it; `--directory` is a query-time filter, not a
  partition key. See "Storage model (user-scoped)" in SKILL.md.

### Breaking changes

- **`related --mode heading` now searches across scopes by default.** Pass
  `--same-scope` to restore the old scope-limited behaviour. Heading paths
  are structural filters (`Install > macOS` means the same thing across
  corpora), so cross-scope is the more useful default.

- **`grga search` multi-word semantics: space now means AND, not OR.**
  Adjacent terms with no explicit operator (e.g. `grga search "韩立 师傅"`)
  used to compile to `韩立 || 师傅` (union of matches). They now compile to
  `韩立 && 师傅` (both terms on the same line), matching the Google / ripgrep
  intuition and the existing skill docs. When implicit AND fires, grga prints
  a one-line notice to stderr:

  ```
  [grga] multi-term query treated as AND: "韩立 师傅" → "韩立" AND "师傅"
  ```

  Explicit `||` / `OR` and `&&` / `AND` are unchanged. Pass `--implicit-or`
  to restore the v1 space-is-OR behaviour, or `--implicit-and` to force the
  new default and suppress the notice. Python callers can pass
  `search_directory(..., implicit_operator="or")` for the same effect.

  Deep-search subprocess invocations of `grga search` continue to use
  `--implicit-or` internally so the deep-search planner's expressions keep
  their previous semantics.

### Added

- **`related --cross-scope` flag** (`mode=graph` only): keep seeds in
  `--directory` but let PPR expansion cross into every other scope in the
  shared DB. Off by default — cross-corpus association is an *escalation*,
  not the default retrieval path.

- **`related --same-scope` flag** (`mode=heading` only): force heading
  matches to stay within `--directory` (the previous default).

- **`crossed_scopes_preview` metadata on `related --mode graph --json`.**
  Opt-in via `--preview`: the graph JSON envelope then carries a
  `crossed_scopes_preview` object (`would_add`, `top_other_scopes`,
  `top_bridge_keywords`) so a caller can see what opening cross-scope would
  pull in *before* committing to it. It is opt-in because the preview runs
  a full-graph PPR whose cost scales with every other scope in the shared
  DB. The graph `--json` output is now an envelope
  `{results, crossed_scopes_preview}` rather than a bare array (`dir` /
  `heading` modes still return arrays).

- **`Related` objects now carry `directory` and `qualified_path`.** Every
  related result records the indexed scope root it came from, so
  cross-scope / global queries no longer conflate identically-named files
  (`README.md`, `docs/install.md`) from different scopes.

- **`grga graph inspect <keyword>` command.** Reports a keyword's
  `docs_by_scope` distribution, `top_cooccurring_keywords`, and a
  `suspected_polysemy` heuristic (per-scope neighbour sets with pairwise
  Jaccard < 0.2) so callers can spot same-name-different-meaning collisions
  before enabling `--cross-scope`.

- **`--max-total / -N` global result cap on `grga search`.** Defaults to
  `1000` (`0` disables). grga now streams rga's JSON output, and once the
  cap is hit it stops reading, kills the rga subprocess, and returns a
  partial result with `truncated: true` in the envelope. Layer 2 metadata
  (`occurrence_index` / `total_occurrences` / `line_ratio` / `nearby_hits`)
  is computed *after* truncation, so the counts reflect the returned slice
  rather than the untrimmed rga output.

  This closes the "22 MB single novel + two-word query returned 111 525 hits
  in 40 s" pathology that previously wedged caller agents against their
  default 60 s tool timeout.

  Deep-search runs its own generous cap (`DEEP_SEARCH_MAX_TOTAL = 20000` per
  iteration, exposed as the `max_total` parameter on
  `_run_grga_search`) because its planner needs many raw hits per query for
  keyword ranking. Set `max_total=None`/`0` on that helper to disable.

- **`--strict-cap` on `grga search`.** Instead of truncating, run a cheap
  `rga --count-matches` pre-check and return
  `{"ok": false, "error": "too_many_matches", "total_hits": N, "message": …}`
  when the cap is exceeded. Useful when callers would rather fail loudly
  than reason about partial data.

- `search_directory(...)` gains `max_total`, `strict_cap`, and
  `implicit_operator` keyword arguments. `build_rga_command_preview(...)`
  gains `implicit_operator`.

- `grga search --json` output now always includes a `truncated: bool` field
  and, when adjacency was treated as implicit AND, an `implicit_notice`
  string. See `docs/api.md`.

### Fixed

- Very large rga result sets no longer force grga to buffer the entire
  stdout (24 MB in the pathology case) before parsing.
