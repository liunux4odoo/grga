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
├── deep_search.py              # v1 多跳搜索（重量级，legacy 主体）
├── models.py, meta.py          # 数据类 Hit / Context + 位置元数据填充
├── graph/                      # 离线图索引（PPR / 投票 / schema）
├── structure/                  # Markdown 章节解析 + sidecar 缓存
├── skills/, skill_install.py   # 内置 Skill 树 + 安装器
├── prompts/                    # suggest 的三个 intent 提示词
├── trace.py                    # trace-signals v1 契约（related/suggest 的 graph_actions）
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
| `related.py` | **L2 图扩展**：关键字共现图找相邻文档，三种 mode (`graph`/`dir`/`heading`) | 见文件顶部 docstring 列出的模式说明 | §7.3 |
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

命中的 v1 raw 结构（dict）与 v2 `Hit` 类共存，**不互相替换**（design §7.2）。

---

## 图子系统 `graph/`

离线预建 + 在线读取的关键字共现图。SQLite 存储，与 v1 `KeywordLibrary` 共库。

| 模块 | 职责 |
|---|---|
| `schema.py` | v2 表建立：`keyword_docs` / `heading_docs` / `llm_extension_votes`；只 CREATE IF NOT EXISTS，不动 v1 表 |
| `build.py` | 离线索引构建入口 `build_graph_index`；对应 CLI `grga index build` |
| `ppr.py` | Personalized PageRank 手写 power iteration；`related --mode graph` 的算力核心 |
| `adjacency_cache.py` | 邻接表缓存，按 `(db_path, db_mtime, scope_key)` 失效 |
| `votes.py` | LLM 提出的关键字对**先写投票表**，不直接进图（反污染门） |
| `promote.py` | `grga index promote` 的实现：投票达阈值才升级到 v1 `keyword_pairs` |
| `inspect.py` | `inspect_keyword`：种子多义性/分布探针，跨 scope 展开前用 |

**反污染流程**（design §4.4）：LLM 提议 → `votes` 表暂存 → `promote` 达阈值 → 进入
v1 `keyword_pairs` → `related --mode graph` 免费拿到。

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
- `docs/design/trace-signals.md`：`related` / `suggest` 的 `graph_actions` 输出规范。
- `trace.py`：trace-signals v1 的实现层。`GraphAction` 数据类 + 冻结的 op /
  node 前缀词汇表 + node id 编码器 (`encode_keyword` / `encode_file` /
  `encode_heading`) + `envelope()`。只有 `related`（graph 模式）与 `suggest`
  在 `--json` 下 emit；热路径动词 (`search`/`probe`/`expand`/`deep-search`)
  **绝不 emit**。契约由 `schemas/trace-signals-v1.json` 校验。

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
- `bench/`：性能基线脚本，改算法（尤其 PPR / deep_search）时用它对比

---

## 文档地图

| 文件 | 内容 |
|---|---|
| `docs/design/v2-design.md` | 五动词、三层扩展模型、设计哲学（**首读**） |
| `docs/design/v2-plan.md` | v2 落地阶段拆分 |
| `docs/design/decisions.md` | 关键决策留痕 |
| `docs/design/trace-signals.md` | related/suggest 的 graph_actions 输出规范 |
| `docs/design/release-0.2.0-plan.md` | 0.2.0 发版计划 |
| `docs/adr/0001-md-parser.md` | 选用 markdown-it-py 的架构决策 |
| `AGENTS.md` | 硬规则、路径约定、工作流偏好 |

---

## 常见任务的入口速查

| 我想做的事 | 从哪里读起 |
|---|---|
| 加一个 CLI 参数 | `cli.py` 找对应 `@app.command`，然后到该动词的核心模块 |
| 改搜索行为 | `search.py::search_directory`；GUI 需同步（见 AGENTS.md） |
| 改图扩展算法 | `graph/ppr.py` + `related.py`；bench 里跑对比 |
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
