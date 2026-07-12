# Changelog

All notable changes to `grga` are recorded here.

## [Unreleased]

### BREAKING · related 信号契约 v2 / v2.1（docs/design/related-signals-v2.md）

分两步落地：v2.0 撤 trace-signals v1（"图游走可视化"），v2.1 同日
收敛冗余（把 `graph_signals` 折叠进顶层 `expansion`，去 `neighbor_edges`
里 `a=seed` 的重复，每个关键字带 `chunk_df` 做 hub 识别）。动机：生产
实测稀有种子经 PPR 扩展会拉入通用词命中的假阳性文档（`苍坤` seed 拉进
《三体》），旧 `graph_actions` op log 无法帮 caller 识别；且 v2.0
的 `graph_signals`/`neighbor_edges`/`ppr_score` 三处分别描述同一个关键
字，caller 要 join 三个数组。

- **移除**：`trace_signals_version` + `graph_actions` 从
  `grga related --json` 与 `grga suggest --json` 撤除。
- **移除（v2.1）**：`Related.graph_signals` 整块（含 `seed_chunk_df` /
  `neighbor_edges` / 里面的 `total_chunks_in_scope`），`--graph-signals-limit`
  CLI 参数。
- **移除**：`src/grga/trace.py`、`schemas/trace-signals-v1.json`、
  `schemas/trace-viz-session-v1.json`，以及依赖 `graph_actions` 的两个内置
  skill 工件（`skills/grga-trace-viz/`、`skills/grga/workflows/trace-viewer/`）。
- **新增**：`grga related --json --mode graph` 顶层 `expansion` 一体化承载：
  * `seeds: List[{kw, chunk_df?}]` — 每个 seed 附加 in-scope chunk df。
  * `expanded_keywords: List[{kw, ppr_score, hops_from_seed?, chunk_df?,
    edges_to_seeds: [{seed, npmi, cooccur_count, source}]}]` — 每个 PPR
    邻居一处出现，直接边并入本条记录，无跨数组 join。
  * `total_chunks_in_scope: int` — `chunk_df` 的分母。
- **新增**：graph 模式每条 `results[]` 内嵌 `keyword_contributions`
  （`[{kw, occurrences}]`，按 `(-occurrences, kw)` 排序）。
- **变更**：`related --mode dir|heading` 与 `suggest --json` 去 envelope，
  现返回 `{"results": [...]}` 对象。
- **不变**：`search` / `probe` / `expand` / `deep-search` 的 `--json` 输出。
- caller 用 `expansion.seeds` + `keyword_contributions` 即可 ~5 行过滤
  "仅由 PPR 扩展词命中" 的假阳性；加 `expanded_keywords[].chunk_df` 阈值
  可再叠一层 hub-word 剪枝。契约 schema：`schemas/related-signals-v2.json`。
  旧规范归档于 `docs/design/deprecated/trace-signals.md`。

### 性能 · `related --mode graph` 优化 (docs/design/plan-2026-07-12-related-perf.md)

7 项独立优化叠加，把冷启动从 ~110s 压到 ~3s、生产热启动预期 ~200ms。
top-20 结果 top-20 doc Jaccard=1.0（vs 旧行为），numpy 后端与纯 Python
逐位一致。实验证据：`experiments/related_perf_2026_07_12/`。

- **P2.3 doc 聚合 id-native**：`aggregate_docs` 用 `kw_id IN (int)` 替换
  `k.kw IN (text)`，直接命中 `keyword_chunks` 复合主键
  `(kw_id, doc_id, chunk_id)`。doc 聚合 SQL 从 ~11s → ~53ms（-99.5%）。
  设 `GRGA_DOC_AGG_LEGACY=1` 走旧路径。
- **P3 numpy CSR + `np.bincount` backend**：`_ppr_numpy` SpMV 幂迭代，PPR
  从 ~35s → ~96ms（-99.7%）。numpy 是**可选依赖**：`pip install grga[perf]`
  安装；缺失时自动退回纯 Python `_ppr_python`。`GRGA_PPR_BACKEND=py|numpy|auto`
  可强制选后端，`GRGA_PPR_NUMPY_MIN_EDGES`（默认 50000）控制 auto 切换阈值。
- **P2.2 id-native 图构建**：新增 `load_edges_indexed` 跳过 JOIN + casefold，
  `IndexedGraph.node_ids` 仍填字符串（契约兼容）。
- **P2.1 删热路径 `build_adjacency`**：`adjacency_cache` 不再构建从不使用的
  dict adjacency；`get_adjacency` 返回 3-tuple 兼容语法但 `adj` 位恒为 `None`。
  `build_adjacency` / `personalized_pagerank`（dict 版）函数本身保留供 legacy
  caller / 单元测试使用。
- **P1.2 `min_npmi` 默认 0.2**：`related --min-npmi FLOAT` 参数化；
  `GRGA_PPR_MIN_NPMI` env 覆盖。SQL WHERE 过滤 `chunk_cooccur` 边使 npmi <
  阈值的边不参与 PPR。`neighbor_edges` schema 不变但**门槛语义微变**（详见
  `docs/design/trace-signals.md` §7.1）。
- **P1.4a `--edge-quantile` 便捷入口**：`grga related --edge-quantile 0.1`
  保留 top 10% 边（按 npmi 采样），内部换算为等效 `min_npmi`；与
  `--min-npmi` 互斥（同给报 exit 2）。
- **P1.1 stopword ∪ core_boundaries**：建图层 `_INDEX_STOPWORDS` 复用已有
  的 26 字 `_CORE_STOP_BOUNDARIES` 结构虚词集，索引层不再放行 `在/是/也/就/但/
  这/有/都/后/上/…` 等高频功能词。需要 `grga index build --rebuild` 生效；detect
  层的 `_CORE_STOP_BOUNDARIES` 语义分层不变（构词后缀如 面壁**者** 仍安全）。

### Changed

- `adjacency_cache` 的缓存键增加 `min_npmi` 维度，不同阈值不串扰。
- `edge_weight` 中 `chunk_cooccur` 边的 `0.01 * log1p(count)` 小地板**保留**
  作为小语料下 npmi=0 的防御性 fallback（与 P1.2 的 SQL WHERE 过滤解耦，
  见 `src/grga/graph/ppr.py::edge_weight` docstring）。

### Added

- `pyproject.toml [project.optional-dependencies].perf = ["numpy>=1.20"]`
  —— 一体化开关 `pip install grga[perf]` 启用 numpy 加速后端。

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
