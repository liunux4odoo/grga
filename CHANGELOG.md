# Changelog

All notable changes to `grga` are recorded here.

## [Unreleased]

### rga 缓存上限 50M + probe 默认 timeout 180s + 默认排除压缩包/无文本二进制

大语料（`/data/ports/sync`：11GB / 23,437 文件，含 2,700+ 二进制文档）
上的性能根因修复。三件事相互独立、各配开关，additive 不破坏现有调用。

- **rga 缓存 blob 上限 2M → 50M**：`search` / `probe` 构建 rga 命令时显式传
  `--rga-cache-max-blob-len=52428800`（模块常量 `RGA_CACHE_MAX_BLOB_LEN`）。
  之前 17.9MB / 27.8MB 的两个大 pdf 提取文本压缩后 >2MB，永不进缓存、每次
  搜索重提取 20-30s；50M 覆盖当前最大 blob（4.86MB）后首次提取进缓存，
  二次命中 ~300ms（实测快 67 倍）。只影响新建缓存，已有缓存不失效不清除。
- **probe 默认 timeout 60s → 180s**：`grga probe`（CLI + MCP + `probe_directory`
  后端默认）全量扫描 11GB 目录实测 73.7s，旧默认 60s 必误报超时。`search`
  默认 600s 不变；内部 `_stream_rga_search` / `_run_checked` timeout 传递链
  确认一致。超时仍走 `start_new_session + killpg` 干净退出，不留孤儿进程。
- **默认排除压缩包 + 无文本二进制**：`search` / `probe` / `preview` 默认追加
  排除 glob（`DEFAULT_EXCLUDE_GLOBS`，45 项）：压缩包（zip/rar/7z/tar/gz/xz/
  bz2/…）、可执行/库/字节码（exe/dll/so/deb/rpm/pyc/…）、音视频（mp3/mp4/…）。
  **绝不含文档类**（docx/pdf/pptx/xlsx/doc/xls/odt/rtf）——在文档里搜到东西
  是主用途；**图片格式也不排除**（png/jpg/gif/webp 等）——图片语料靠
  `--path-only` 按文件名检索，保留它们无扫描开销。默认排除与用户 `-g` 追加
  合并（用户 glob 在后、可覆盖默认）；新增 `--no-default-exclude` 关闭（用户
  `-g` 仍生效）。

设计取舍详见 `docs/design/plan-2026-09-08-rga-cache-timeout-excludes.md` 与
`docs/design/decisions.md`。测试 `tests/unit/test_rga_cache_and_default_excludes.py`
覆盖 18 用例（缓存 flag 下发 / 排除合并 / 文档类保护 / 端到端 zip 排除）。

### `grga tools check` → `grga tools status` + probe orphan 进程修复

- **改名** `grga tools check` → `grga tools status`（输出 schema 不变）。`tools check`
  保留为 deprecated alias，stderr 打印弃用警告后委派到 `status` 逻辑。`grga check`
  (顶层) 完全不动 —— 仍是 search-stack viability gate，缺失必需工具时 exit ≠ 0。

- **修复** `grga probe` / `grga search` 在 timeout 时 orphaned 子进程泄露。
  根因: `subprocess.run`/`Popen` 缺少 `start_new_session=True`，超时后仅杀 rga
  父进程，rga-preproc → pandoc/pdftotext 子进程成为孤儿，占住 stdout pipe。
  修复: 新增 `_run_checked` + `_kill_process_group` helper，在 `probe.py` /
  `search.py` 的所有 rga 子进程调用统一使用 `start_new_session=True` +
  `os.killpg(SIGKILL)`。验证: `/data/ports/sync` (23,429 文件 / 11G) 上
  timeout 前后 orphan 进程数从 7 → 0。

### `grga graph inspect` 契约扩展 · `top_docs` 定位捷径 + `--scope` / `--tag` 过滤

`grga graph inspect <keyword>` 之前只回聚合计数（`docs_by_scope`），caller
想定位"哪几个文件持有这个词"必须再跑一次 `grga search` 全文扫描。数据库
里 `keyword_docs` 已经存了 doc 级倒排索引 + `occurrence_count` /
`chunk_count`，暴露给 caller 零额外成本；同时 `--scope` / `--tag` 只有
`related` 有、`inspect` 缺失，跨 scope DB 上做单词诊断时得先跑
`grga scope list` 手动挑 DB，杠杆方向反了。两件事一次修齐：

- **新增** `top_docs`（indexed 分支专属）：`{scope, doc_path,
  occurrence_count, chunk_count}` 行数组，跨 scope 全局按
  `(-occurrence_count, -chunk_count, scope, doc_path)` 排序。每行带
  `scope` 便于 caller 路由回 `scope_directories` 绑定。`--docs-limit`
  默认 5、`0` 关闭；`top_docs_truncated` 在 `total_docs > docs_limit`
  时为 `true`。not-indexed / knowledge-only 分支不发（没本地证据）。
- **新增** `--scope <name>`（可重复）/ `--tag <label>` / `--tag-logic
  and|or`，与 `grga related` 同款语义：name ∩ tag、两者皆空 = cross-scope
  union、显式过滤命中零 scope 返回 `scope_filter_hint`（区别于 not-indexed
  的 `index_hint`——关键字本身可能已索引，只是不在过滤范围内）。四个 SQL
  助手（`_seed_docs` / `_cooccurrences_batch` / `_knowledge_only_scopes` /
  `_similar_keywords`）经 `_scope_filter_clause` 统一构造 `AND col IN (…)`
  片段，杜绝分支漂移。
- **API**：`inspect_keyword(..., scope_names=None, tag_names=None,
  tag_logic="or", docs_limit=5)`；旧签名调用向后兼容。
- **契约**：新增 `top_docs` / `top_docs_truncated` / `scope_filter_hint`
  三个字段，皆为附加信号；已有字段无变化。属于 MINOR。

契约与设计取舍详情见 `docs/api.md#grga-graph-inspect`。测试
`tests/graph/test_inspect.py` 覆盖 22 用例（top_docs 排序 / 截断 / 关闭 /
非索引分支不发；`--scope` 单选/多选/零命中；`--tag` + `--tag-logic
and|or`；`--scope` ∩ `--tag`；非法 `tag_logic` 早报错）。

### 依赖清理 · 删除已死的 `perf` extra + `markdown-it-py` 提升为核心依赖

- **删除** `pyproject.toml` 的 `[project.optional-dependencies].perf =
  ["numpy>=1.20"]`。这个 extra 是 v8 时代给 PPR 幂迭代的 numpy CSR +
  bincount SpMV 后端准备的（详见 `docs/design/plan-2026-07-12-related-perf.md`
  §2.3 / §P3）。v9 keyword-primary 已整体移除 PPR（`graph/ppr.py` /
  `graph/adjacency_cache.py` 都删了，`related_keywords` 走 index-only 早
  停查 `keyword_pairs`，无矩阵运算）。运行时代码里没有任何 `import numpy`，
  `perf` extra 装了也没有加速目标。用户端 `pip install grga[perf]` 现在
  会安装失败 —— 一次性砍掉，避免 pyproject 里保留死符号误导。
- **合并** `[project.optional-dependencies].v2 = ["markdown-it-py>=3.0"]`
  → 核心 `dependencies`。`markdown-it-py` 是 `structure/md.py` 的主
  Markdown 解析器（ADR-0001），`expand --level chapter` 与 `heading_path`
  元数据必需。v2 分组当初是为了"v1 打包不污染"而设的临时缓冲，v2 早已
  是默认路径，分组抽象无实际意义。核心 install 直接带上，避免 caller
  忘记 `pip install grga[v2]` 后 heading 静默降级为空。
- **不变**：`scripts/build-onefile.py` 的 `EXCLUDED_MODULES` 里仍列了
  `numpy`（防御性排除，避免任何传递依赖把 numpy 拖进单文件），无需改动。

### build 期写入门槛自适应 · Gate 1 独立性 + Gate 2 阶梯（docs/design/decisions.md §2026-07-13）

`min_pair_count` 固定为 5 对小语料过严：一本中篇（几百 chunk）的真实
概念对常只共现 2-3 次，全被砍空，`related --mode graph` 频繁空结果。
但直接降门槛会放进 npmi 虚高的噪声（两个词各出现 1 次恰好同现 → npmi
满分 1.0）。改为两条正交门槛：

- **新增**：写入期 **Gate 1 · 独立性判据**（无参、恒开、严格三元相等）——
  `chunk_df_a == chunk_df_b == cooccur_count`。挡掉"两个词完全绑定"
  （npmi=1.0 artefact）；保留"单向依附"（rare 词只跟 common 词同现）
  的真信号。用严格三元相等（不用 `co >= max`）是因为 `mode='build'`
  下 `cooccur_count` 是 grow-only（D6），可以出现 `co > min(df_a, df_b)`
  的合法单向依附——`co >= max` 会误杀，三元相等不会。
  `_finalize_pairs_from_temp` 与 `_prune_low_score_pairs` 同步实现。
- **变更**：`min_pair_count` 默认从固定 `5` 改为 **scope 自适应阶梯**
  `resolve_min_pair_count(n_chunks)`：`≤200→2` / `≤2000→3` / `≤20000→4`
  / `>20000→5`。`grga index build/rebuild --min-pair-count` 默认值
  `5` → 未设置（哨兵 `None` = 自适应）；显式传 int 覆盖阶梯。
- **变更**：`grga.graph.build.build_graph_index` 的 `min_pair_count`
  形参类型 `int` → `Optional[int]`（`None` = 自适应）。
- **变更**：`_prune_low_score_pairs`（v7→v8 迁移的存储契约执行者）改为
  per-scope 查 `scope_stats.n_chunks` 求 floor + Gate 1；无 scope_stats
  时回退固定 5。
- **不变**：Gate 3（`min_npmi=0.2`）、大语料（>20000 chunks）行为、
  `related --json` 契约、BuildStats / CLI JSON 形状。
- 小语料 `related` 覆盖率显著提升；大语料零回归。

## [Unreleased] · related 信号契约 v3.1 · `resolved_seeds` 多 seed 探针（docs/design/related-signals-v3.md）

多 seed `related --mode graph` 的空结果原本二义——分不清"seed 未索引"
和"seed 已索引但孤立（边被写入期/读侧 npmi 门槛滤空）"。这两种情况
caller 从 `results` 派生不出来（信息天然重合），下一步动作却相反。新增
唯一一个 envelope 级探针补这一 bit。

- **新增**：`grga related --json --mode graph` envelope 加
  `resolved_seeds: [{seed, in_index}]` — 每个去重（casefold + 保序）输入
  seed 一条，`in_index` 报告是否命中 `keywords` 词表（与 scope 无关）。
- **变更**：`grga.related.related_keywords()` 返回类型 `List[RelatedKeyword]`
  → `RelatedKeywordsResult{results, resolved_seeds}`。新增 `SeedResolution`
  dataclass。
- **变更**：graph 模式非 JSON 输出把 dead seed 以 stderr 两行提示呈现
  （`unknown seeds ...` / `indexed but no candidates ...`），不再在空结果时
  提前 `Exit`；exit code 仍为 0，stdout 候选流不受影响。
- **不变**：`RelatedKeyword` 六字段、排序 `(via_seed, -npmi, kw)`、多 seed
  不融合（D4）、heading 模式、热路径动词 `--json`。
- caller 一段 5 行即可分派：`in_index=false` → 换词 / `suggest reformulate`；
  `in_index=true` 但缺席于 `{results[].via_seed}` → 换更具体 seed 或松
  `--min-npmi`。免去逐 seed 的 `grga graph inspect` round-trip。
- schema：`schemas/related-signals-v3.json` graph 分支加 `resolved_seeds`
  （required）+ `seed_resolution` $def。

## [Unreleased] · 历史条目

### BREAKING · related 信号契约 v2 / v2.1 / v2.2（docs/design/related-signals-v2.md）

同日三步落地：v2.0 撤 trace-signals v1（"图游走可视化"），v2.1 把
`graph_signals` 折叠进顶层 `expansion`（去 `neighbor_edges` 里 `a=seed`
重复），v2.2 进一步扁平化 `edges_to_seeds` 列表包装（单 seed 查询里
"数组包一个字典包一个 seed 回显" 的三层嵌套 → 顶层内联 `npmi` /
`cooccur_count`）。动机：生产实测稀有种子经 PPR 扩展会拉入通用词命中
的假阳性文档（`苍坤` seed 拉进《三体》），旧 `graph_actions` op log
无法帮 caller 识别；同时每一次 caller 反馈都指向"信号一处收，去嵌套
去回显"。

- **移除**：`trace_signals_version` + `graph_actions` 从
  `grga related --json` 与 `grga suggest --json` 撤除。
- **移除（v2.1）**：`Related.graph_signals` 整块（含 `seed_chunk_df` /
  `neighbor_edges` / 里面的 `total_chunks_in_scope`），`--graph-signals-limit`
  CLI 参数。
- **移除**：`src/grga/trace.py`、`schemas/trace-signals-v1.json`、
  `schemas/trace-viz-session-v1.json`，以及依赖 `graph_actions` 的两个内置
  skill 工件（`skills/grga-trace-viz/`、`skills/grga/workflows/trace-viewer/`）。
- **新增（v2.1）**：`grga related --json --mode graph` 顶层 `expansion` 一体化承载：
  * `seeds: List[{kw, chunk_df?}]` — 每个 seed 附加 in-scope chunk df。
  * `expanded_keywords: List[{kw, ppr_score, hops_from_seed?, chunk_df?,
    ...}]` — 每个 PPR 邻居一处出现，无跨数组 join。
  * `total_chunks_in_scope: int` — `chunk_df` 的分母。
- **变更（v2.2）**：`expanded_keywords[i]` 里最强直接边扁平化，dropping
  `edges_to_seeds` 列表包装：
  * `npmi` / `cooccur_count`：内联在 entry 自身（两字段一起出现或一起
    缺席）。存在条件：该邻居在 `keyword_pairs` 有直接 seed 边（与
    `hops_from_seed` 解耦，允许"低 npmi 直接边被 PPR 绕开"的信号透过）。
  * `source`：只在 != `"chunk_cooccur"` 时 emit（即 `llm_promoted`）。
  * `via_seeds`：只在**多 seed 查询里 + 邻居有 >=2 条直接边**时出现，
    按 npmi desc 列出被桥接的 seed 名。
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
