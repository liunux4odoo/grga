# grga 代码导航索引

> **目的**：让 code agent 在开始新任务时快速定位相关代码，减少"从头读起"的探索开销。
> **原则**：只写**稳定信息**——模块语义、关键入口函数名、职责边界。不写行数、
> 文件大小、参数签名等易变细节。code agent 拿到入口名后自己 grep / 读代码。
> **相关文档**：`docs/design/v2-design.md`（设计规范）、`AGENTS.md`（硬规则）。

---

## 顶层地图

```
src/grga/
├── cli.py, __main__.py         # CLI 入口（Typer 命令注册）
├── {probe,search,expand,       # v2 五动词原语（design §4）
│   related,suggest}.py
├── mcp/                        # MCP server（可选 extra `[mcp]`）：6 个 tool 适配器
├── deep_search.py              # v1 多跳搜索（重量级，legacy 主体）
├── models.py, meta.py          # 数据类 Hit / Context + 位置元数据填充
├── file_read.py                # 统一 file→list[str]：文本走 open()、非文本 suffix 走 rga --passthru
├── graph/                      # 离线图索引（共现资产 / 投票 / schema）
├── structure/                  # Markdown 章节解析 + sidecar 缓存
├── skills/, skill_install.py   # 内置 Skill 树 + 安装器
├── prompts/                    # suggest 的三个 intent 提示词
├── toolchain.py                # 外部工具 (rga/rg/pandoc/fzf) 下载与解析
├── runtime_env.py              # 交互性 / 离线 / prefer-system 判定
├── exit_codes.py               # 冻结的退出码契约 + JSON 错误 emit
├── paths.py                    # 工具缓存路径优先级解析
└── gui.py                      # Tk 桌面 GUI（独立层，不影响 CLI）
```

---

## 五动词原语（v2 核心）

grga v2 的检索原语，按 caller agent 的推理层级由轻到重排列。设计规范见
`docs/design/v2-design.md` §4；各动词的 CLI 声明在 `cli.py`。

| 模块 | 语义定位 | 关键入口 | 相关设计节 |
|---|---|---|---|
| `probe.py` | **L0 廉价探针**：只数命中数量、不读 body、不改写 query | `probe_directory` | §4.0 |
| `search.py` | **L1 命中检索**：ripgrep-all 结构化封装 + 命中元数据注入 | `search_directory`, `build_rga_command_preview` | §4.1 |
| `expand.py` | **L1 上下文扩展**：命中 → 段落/章节/整档 | `expand(hit, level, size, base_dir)` | §4.1 |
| `related.py` | **L2 关键字扩展**：查 `keyword_pairs` 共现资产返回**候选关键字**（v3 起不再返回文档），支持 `graph` / `heading` 两种 mode（`dir` 已删） | `related_keywords`（graph）/ `related`（heading） | §7.3 |
| `suggest.py` | **L3 LLM 兜底**：唯一调 LLM 的原语；三种 intent (`expand`/`reformulate`/`rerank`) | 提示词在 `prompts/suggest_*.txt` | §4.3, §7.4 |

**caller 反思链**：`probe` → `search` → `expand`/`related` → `suggest`（bail out）

---

## 深度搜索（v1 遗产）

- `deep_search.py`：**多跳搜索的重量级实现**，是 v1 主体。核心是
  `deep_search_directory`。内部包含 LLM 意图兜底查询构造、`KeywordLibrary`
  操作（v1 的 keyword_counts / keyword_pairs 表）。改动此文件时须同步考虑
  `graph/` 里对应的 v2 增量表。

---

## 数据模型

| 模块 | 内容 |
|---|---|
| `models.py` | `Hit`（命中）+ `Context`（扩展块）dataclass；`Hit.from_raw_match` 是 v1 dict → v2 类型化的桥 |
| `meta.py` | `annotate_hits` 填充位置元数据：`occurrence_index` / `line_ratio` / `nearby_hits` |
| `file_read.py` | `read_file_lines(path)` / `read_file_lines_ex(path)`：suffix 分派——文本白名单走 `open()`、rga adapter 支持的 suffix（PDF/Office/归档/sqlite）走 `rga --passthru --json`。行号与 `search` 输出对齐。`TEXT_SUFFIXES` / `RGA_SUPPORTED_SUFFIXES` / `indexable_suffixes()` 是集合级出口。**expand 和 graph.build 是唯二消费者**，其它模块用之前先想清楚是否真的需要走 rga |

命中的 v1 raw 结构（dict）与 v2 `Hit` 类共存，**不互相替换**（design §7.2）。

---

## 图子系统 `graph/`

离线预建 + 在线读取的关键字共现图。SQLite 存储。**v10 scope / directory 分野**
（schema v10，plan-2026-07-14）：知识资产（`keyword_pairs` / `keyword_stats` /
`scope_stats` / `auto_ngram_dict` / `llm_extension_votes`）按**逻辑 scope 名**
shard（`scopes` 表），跨机器可迁移；证据层（`scope_directories` / `docs` /
`keyword_docs` / `heading_docs` / `keyword_sources`）机器本地，迁移时清空/重建。
pair 是核心知识资产，累积增长；graph 查询走亚毫秒 index-only 早停。

| 模块 | 职责 |
|---|---|
| `schema.py` | v10 建表（`scopes` / `scope_directories` intern + 资产表 / 证据表）+ `scope_tags`（v11 多值标签） + `resolve_scope_name`（纯字符串归一化，**不 resolve 路径**）+ `SCHEMA_VERSION = 11`。`ensure_schema` 对 `user_version < MIN_UPGRADABLE_VERSION`（=10）的老库**直接报错**给出重建指引；`user_version` 在 `[MIN_UPGRADABLE_VERSION, SCHEMA_VERSION]` 之间走**加表无损升级**（v10 → v11 仅 `CREATE TABLE IF NOT EXISTS scope_tags` + 重打版本戳） |
| `ids.py` | `IdInterner`：`scope_id` / `scope_name` 系列（v10 取代 v9 `dir_id` 系列）+ keywords / docs intern。新增 `bind_directory(scope_id, directory)` / `list_directories(scope_id)` / `unbind_directory` 管理 `scope_directories` 旁路表 |
| `build.py` | 段级离线索引构建入口 `build_graph_index(scope, *, directory, ...)`；`scope` 是**逻辑名**、`directory` 是**物理路径**，两者独立参数。build 时把 `scope_directories` 绑定 UPSERT 上去；`keyword_pairs`（chunk_cooccur，UPSERT 增量）+ `keyword_stats`（chunk_df/doc_df/occurrences）+ `keyword_docs`（doc 级倒排）+ `scope_stats`（n_chunks/n_docs/vocab_size）全部按 scope_id shard。写入期三门槛：Gate 1 独立性 + Gate 2 `min_pair_count` + Gate 3 `BUILD_MIN_NPMI`。`complementary_pairs` 写入前另有**互补对噪声过滤**（默认开启）：阶段 1 全局停用词门槛 + 迭代 2 互补对专用停用词 + 迭代 2 c/E 强度门槛（均无开关）+ 阶段 2 语体隔离检测（`--no-filter-comp-segregation` 可关阶段 2；见 `docs/design/plan-2026-09-12-complementary-noise-filtering.md`） |
| `votes.py` | LLM 提出的关键字对**先写投票表**，不直接进图（反污染门）。v10 API：`scope: Optional[str]` 参数，`None` 时静默跳过写入（知识必须有名）。`get_votes` JOIN `scopes.name` 回文本 |
| `promote.py` | `grga index promote` 的实现：投票达阈值才升级到 `keyword_pairs`（`source='llm_promoted'`，`npmi` NULL）；`promoted_at` 列做幂等标记，防止同一投票被二次升级 |
| `inspect.py` | `inspect_keyword`：种子多义性/分布探针，读 `keyword_stats` + `keyword_pairs` 回文本；`docs_by_scope` 按逻辑 scope 名分组。**v10 knowledge-only 分支**：当 seed 在 `keyword_stats` 有条目但 `docs` 为空（`grga index import` 后典型），返回 `evidence_hint`。**v11 top_docs**：indexed 分支返回 `top_docs`（`{scope, doc_path, occurrence_count, chunk_count}`，`--docs-limit` 控制条数）作为定位捷径。**v11 scope/tag 过滤**：`--scope` / `--tag` / `--tag-logic` 与 `related` 同款语义（name ∩ tag、命中零 scope → `scope_filter_hint`），下发到 4 个 SQL 助手（`_seed_docs` / `_cooccurrences_batch` / `_knowledge_only_scopes` / `_similar_keywords`）经 `_scope_filter_clause` 统一构造 `IN (…)` 片段 |
| `scope.py` | **v10 scope 管理**：`list_scopes` / `bind_directory` / `unbind_directory` / `rename_scope` / `remove_scope`。**v11 tag 管理**：`add_tags` / `remove_tags` / `list_tags` / `rename_tag`（单 scope 内改名，严格拒绝碰撞）/ `resolve_scope_ids_by_tags`（tag → scope_id 集合，`logic='and'|'or'`）。`remove_scope` 默认只删证据层（`scope_directories` + `docs` + `keyword_docs` + `heading_docs`），知识保留；`purge_knowledge=True` 才连 `keyword_pairs` / `keyword_stats` / `scope_stats` / `auto_ngram_dict` / `llm_extension_votes` / `scope_tags` + `scopes` 行一起清（design D6 grow-and-refine 例外闸门）。对应 CLI `grga scope list/bind/unbind/rename/rm` + `grga scope tag add/rm/list/mv` |
| `pack.py` | **知识包 export / import**（plan-2026-07-14 §5）：SQLite `.grgapkg` 文件，只含知识层（scopes + keywords + keyword_pairs + keyword_stats + scope_stats + auto_ngram_dict + llm_extension_votes + meta），证据层剥离。`export_knowledge_pack(scope=..., out_path=...)` 支持单 scope 与 `scope=None` 全量；`import_knowledge_pack(pack, as_scope=...)` 只允许 `--as`，`merge_into` 显式 NotImplementedError（D9）。`meta.grga_pack.*` 命名空间做格式/版本校验；kw_id 通过 get-or-insert 全量 remap，pack 内不同机器切换零冲突。对应 CLI `grga index export/import` |

**反污染流程**（design §4.4）：LLM 提议 → `votes` 表暂存 → `promote` 达阈值 →
进入 `keyword_pairs`(`source='llm_promoted'`) → `related --mode graph` 免费拿到。

**scope 多选**（v10）：`related --scope a --scope b` 走 `WHERE scope_id IN (?, ?)`；
单 scope 用 `scope_id = ?` 保持 `idx_kp_a_npmi` 的 index-ordered LIMIT 亚毫秒
早停；不加 `--scope` = 跨全部 scope union。见 `docs/design/plan-2026-07-14-scope
-directory-split.md` §4。**v3.1 契约**（同文档 §5.3）：`RelatedKeyword.scopes:
List[str]` 附在每条候选上，告知它在本次查询范围内命中的 scope 名子集——`--json`
消费者可以据此路由后续 ripgrep 到正确的 `scope_directories` 绑定，不需要再跑
`graph inspect` 反查。

**scope tag 过滤**（v11，plan-2026-07-14-scope-tags）：`grga index build --tag <t> --tag <t>`
把多值标签写入 `scope_tags`（知识层，随 knowledge pack 迁移）；`grga related --tag <t>
--tag-logic and|or` 先查 `scope_tags` 得到 scope_id 集合，再喂给现有
`keyword_pairs.scope_id IN (...)` 过滤链路。`--scope` 语义**不变**（仍是精确
name 匹配），同时给 `--scope` + `--tag` 时两者**取交集**；两者都为空 = cross-scope
union。热路径动词（`search` / `probe` / `expand`）**不**接受 `--tag`——caller 想
按 tag 缩范围时先跑 `grga related` 或 `grga scope list --json` 拿 scope name。

**知识可迁移工作流**：`grga index export --scope <name> --out X.grgapkg` →
拷贝到另一台机器 → `grga index import X.grgapkg --as <local-name>` → 可选
`grga scope bind <local-name> <本机语料路径>` → `grga index build --scope
<local-name> --directory <本机语料路径> --seed-ngram-from <local-name>` 重建
证据 + 借用刚导入的分词字典冷启动（否则需要重新检测短语）。

> v10 已删除：pre-v10 `directories` intern 表（绝对路径 shard）、`resolve_scope_key`、
> `scope_write_key`。历史设计见 `docs/design/plan-2026-07-12-keyword-primary.md`。

---

## 结构化解析 `structure/`

Markdown 章节树 + 磁盘缓存 sidecar。为 `expand --level chapter` 和
`heading_path` 元数据服务。

| 模块 | 职责 |
|---|---|
| `md.py` | `parse_markdown(path) -> StructureDoc`；基于 markdown-it-py（ADR-0001） |
| `sidecar.py` | `load_or_build`：磁盘缓存 `.grga/structure/`，按源文件 mtime 失效 |

**降级承诺**：任何解析失败都返回空 `StructureDoc`，caller 靠"heading_path 为空"
识别，不抛异常（design §4.1）。

---

## 外部工具管理

| 模块 | 职责 |
|---|---|
| `toolchain.py` | 下载 / 校验 / 缓存 `rga` `rg` `rga-preproc` `pandoc` `pdftotext` `fzf` |
| `paths.py` | 工具根目录优先级：`$GRGA_TOOLS_DIR` > XDG > 平台默认 |
| `runtime_env.py` | 交互性 / offline / prefer-system / insecure 判定的统一入口 |

**工具解析顺序**（AGENTS.md 有述）：托管缓存 → site-packages 旧缓存 → 系统 PATH。
`GRGA_PREFER_SYSTEM=1` 反转此顺序。

---

## 契约与错误

- `exit_codes.py`：**冻结的退出码契约**（0.2.0 起）。只加不改。JSON 错误 emit
  函数 (`emit_error` / `emit_warning`) 也在此。
- `docs/exit-codes.md`：外部消费者可见的退出码列表。
- `docs/design/related-signals-v3.md`：`related --json --mode graph` 的**候选
  关键字**契约（2026-07-13 随 v9 keyword-primary 落地；v3.1 加
  `resolved_seeds` 探针）。graph 模式返回
  `{"results": [{kw, npmi, cooccur_count, via_seed, chunk_df, source}],
  "resolved_seeds": [{seed, in_index}]}`，
  由 `schemas/related-signals-v3.json` 校验；`--mode dir` 已移除，`--mode
  heading` 保留但 `keyword_contributions` 字段已删。热路径动词
  (`search`/`probe`/`expand`/`deep-search`) 不 emit 任何相关字段。
- `docs/design/deprecated/related-signals-v2.md`：**已归档**的 v2 契约
  （PPR + `expansion` + `keyword_contributions`），供历史参考。
- `docs/design/deprecated/trace-signals.md`：**已废弃**的 trace-signals
  v1 规范（`graph_actions` op log）。为什么撤见其头部说明。

改任何 CLI 的 `--json` 输出前，先看这两份文件的冻结点。

---

## Skills 与 Prompts

- `skills/grga/`：内置 skill 的源模板，`grga skill install` 会把它**拷贝**（非
  symlink）到目标位置——拷贝语义等于给用户一份 pip 升级动不了的稳定副本；
  想要 live-edit 语义可以自己 `ln -s "$(grga skill path)/<name>" <dst>/<name>`
- `skill_install.py`：安装器逻辑（拷贝 + `--force` 语义）
- `prompts/suggest_*.txt`：suggest 三种 intent 的 LLM 提示词模板，改这里就等于
  改 L3 行为，属于**语义敏感区**

---

## GUI

`gui.py` 是**独立的桌面 GUI**（Tkinter），消费 `search_directory` 的返回结构。
CLI 与 GUI 共享后端，GUI 不引入新后端逻辑。AGENTS.md 有一条针对 GUI 参数
同步的清单。

---

## 测试与 bench

- `tests/`：pytest 测试。改核心模块时优先跑对应子集，别一上来 `pytest` 全跑
- `bench/`：性能基线脚本，改核心算法（尤其 deep_search / 索引构建）时用它对比

---

## 文档地图

| 文件 | 内容 |
|---|---|
| `docs/design/v2-design.md` | 五动词、三层扩展模型、设计哲学（**首读**） |
| `docs/design/v2-plan.md` | v2 落地阶段拆分 |
| `docs/design/decisions.md` | 关键决策留痕 |
| `docs/design/plan-2026-07-14-scope-directory-split.md` | **v10 架构**：scope（逻辑名，可迁移）/ directory（物理路径，机器本地）分野；schema v10 + `resolve_scope_name`（不 resolve 路径）+ `scope_directories` 旁路表 + `related --scope` 多选 |
| `docs/design/plan-2026-07-14-scope-tags.md` | **v11 scope tag**：多值标签叠加在 scope 上（知识层），build 期 `--tag` UPSERT `scope_tags`，query 期 `--tag` + `--tag-logic and|or` 先解析成 scope_id 集合再走现有 `IN (...)` 过滤；`--scope` 语义不变、和 `--tag` 同时给时取交集 |
| `docs/design/plan-2026-07-12-keyword-primary.md` | v9 架构：pair 字典化 + 删 keyword_chunks（schema v8 落地文档） |
| `docs/design/db-memory-optimization-lessons.md` | **DB/内存优化方法学**：TEMP TABLE + SQL self-join 替代 Python 大 Counter；改任何"Python 聚合→落 SQLite"模块前必读 |
| `docs/design/plan-2026-07-15-build-memory-oom.md` | **build OOM 修复方案**：ngram 检测 + pair npmi 重算 TEMP TABLE 化（v9.1 方法学的第二/第三次应用）；附文件级断点续跑完整设计留档（本轮不做，证据触发再启） |
| `docs/design/plan-2026-09-11-complementary-pairs.md` | **互补对（complementary_pairs）设计**：显著负 npmi + 低 p-value 的 pair 单独存表；probe/related 读侧附加 complementary 信号 |
| `docs/design/plan-2026-09-12-complementary-noise-filtering.md` | **互补对噪声过滤**：阶段 1 全局停用词门槛（无开关，默认开）+ 迭代 2 c/E 强度门槛与互补对专用停用词表 `_COMPLEMENTARY_STOPWORDS`（无开关，默认开）+ 阶段 2 语体隔离检测（文件重叠下限 + chunk-vs-file npmi 偏离，`--no-filter-comp-segregation` 可关）；各层均纯减法，只影响 `complementary_pairs` 写入 |
| `docs/design/plan-2026-09-10-build-interrupt-recovery.md` | **build 中断恢复与状态管理提案（待评估）**：中断行为核实结论固化（处理中批次重处理）、构建完成标记识别残缺状态、中断恢复测试、per-batch floor 累计化、WAL、rebuild 提前清空缺口 |
| `docs/design/related-signals-v3.md` | `related --json` 当前契约（graph 返候选关键字） |
| `docs/design/deprecated/related-signals-v2.md` | **已归档** · v2 契约（PPR + expansion + keyword_contributions） |
| `docs/design/deprecated/trace-signals.md` | **已废弃** · 旧 graph_actions 契约（2026-07-12 撤除） |
| `docs/design/release-0.2.0-plan.md` | 0.2.0 发版计划 |
| `docs/adr/0001-md-parser.md` | 选用 markdown-it-py 的架构决策 |
| `docs/design/plan-2026-07-11-schema-v5-normalization.md` | v5 规范化迁移设计 + 消费者/契约影响 |
| `AGENTS.md` | 硬规则、路径约定、工作流偏好 |
| `docs/install-and-usage.md` | 完整安装、工具下载、镜像/打包构建、面向 AI Agent 的进阶说明（README 的详细版） |
| `docs/env-vars.md` | 所有可识别的环境变量清单（LLM 凭据、工具缓存、图查询阈值、GUI 缩放） |
| `docs/build-tuning.md` | **build 调优指南**：`--min-npmi-build` / `--batch-chunks` / `--top-n-kw` 的取舍、存储与内存权衡、常见场景配方 |

---

## 常见任务的入口速查

| 我想做的事 | 从哪里读起 |
|---|---|
| 加一个 CLI 参数 | `cli.py` 找对应 `@app.command`，然后到该动词的核心模块 |
| 改搜索行为 | `search.py::search_directory`；GUI 需同步（见 AGENTS.md） |
| 改图扩展算法 | `graph/build.py`（写入期共现资产）+ `related.py::related_keywords`（读侧查询）；bench 里跑对比 |
| 加一个 MCP tool | `mcp/tools/<name>.py`（薄适配器：参数校验 → 调 grga 内部函数 → 格式化）；`mcp/server.py` 里注册 |
| 改 build 期 pair / 聚合逻辑 | `graph/build.py::_finalize_pairs_from_temp`（TEMP TABLE + SQL self-join，方法学见 `docs/design/db-memory-optimization-lessons.md`） |
| 改 related 性能 / 候选查询 | `related.py::related_keywords`（index-only 早停查 `keyword_pairs`）；写入期见 `graph/build.py`；设计见 `docs/design/plan-2026-07-12-keyword-primary.md`，历史 perf 实验 `experiments/related_perf_2026_07_12/` |
| 加一种 LLM intent | `suggest.py` + 新增 `prompts/suggest_<intent>.txt` |
| 加新 Markdown 结构信号 | `structure/md.py`（解析）+ `structure/sidecar.py`（缓存） |
| 加退出码 / JSON 错误码 | `exit_codes.py`（**只加不改**） |
| 调工具下载逻辑 | `toolchain.py`；`paths.py` 处理路径优先级 |
| 加新 skill | 在 `skills/` 下建目录；`skill_install.py` 会自动拷贝 |

---

## 维护约定

- **本文件写稳定信息**：模块名、目录、语义定位、入口函数名
- **不写易变信息**：行数、文件大小、参数签名、具体实现细节
- **架构大变时同步**：新增模块或大重构后更新本文；日常改函数体不影响本文
- **不完备也没关系**：找不到时 code agent 自己 grep 是可以的，此文件的价值在"起点"
