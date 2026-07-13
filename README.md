# grga

> **一款向文档学习、却不依赖文档，可迁移的0成本检索器。** — 从你的文档中自动发现概念连接，学习到的知识可直接用于同领域文档的检索。完全本地运行，不依赖 Embedding 和 LLM。

`grga` 是一个基于 `ripgrep-all` 的 Agent 友好全文搜索工具，提供命令行入口和 Tkinter 图形界面。它封装 `rga`、`rg` 等底层工具，支持在普通文本、Markdown、PDF、Office 文档等文件中搜索内容，也支持只按文件路径搜索。

项目目标是把常用搜索能力、工具链检查和内置工具管理集成到一个 Python 包中，并支持打包成 standalone 可执行文件，尽量减少用户手动配置系统命令的成本。

### 与普通 grep 有什么不同？

grga 是"grep 家族的最外层封装"：底层复用 `ripgrep` 的速度和 `ripgrep-all`
的文档适配器，再往上叠**上下文扩展**、**共现关系图**、**LLM 深度搜索**几层能力。下表按"从 grep 一路加码到 grga"的顺序对比各工具的支持程度，并给出
grga 侧的具体命令与依赖：

| 能力 | grep | `rg` | `rga` | grga 命令 / 用法 | 依赖 / 配置 |
|---|:---:|:---:|:---:|---|---|
| 纯文本 / 源码搜索 | ✅ | ✅ | ✅ | `grga search 关键词` | 无 |
| 大仓库速度（并发 + `.gitignore`） | ❌ 单线程 | ✅ 多线程 | ✅ 继承 rg | 同上，自动透传 `rg` | `rg`（首次 `grga tools install` 自动下载） |
| PDF / Office / EPUB / 归档 / sqlite | ❌ | ❌ | ✅ 内建适配器 | 同上，自动透传 `rga` | `rga` + `rga-preproc` + `pandoc` + `pdftotext`（自动下载） |
| 查询表达式（短语 / 正则 / glob / `\|\|` / `&&` / 括号） | ⚠️ 仅正则 | ⚠️ 仅正则 | ⚠️ 仅正则 | 默认多词 **AND**，`--implicit-or` 恢复 v1 OR 语义 | 无 |
| 路径搜索（只匹配文件名，不读内容） | ⚠️ 需配合 `find` | ⚠️ 需 `--files \| rg` | ⚠️ 同 rg | `grga search --path-only` | 无 |
| 命令预览与试探（先看规模再决定读不读） | ❌ | ❌ | ❌ | `grga preview` / `grga probe` | 无 |
| Markdown / PDF 的段落 / 章节上下文扩展 | ❌ | ❌ | ❌ | `grga expand`（按行 / 段落 / 章节逐级） |  |
| 关键词共现关系图（离线索引 + 亚毫秒查询） | ❌ | ❌ | ❌ | `grga index build` → `grga related` / `grga graph inspect` | |
| LLM 深度搜索、查询改写、结果重排 | ❌ | ❌ | ❌ | `grga deep-search` / `grga suggest` | OpenAI 兼容 API；至少 `GRGA_OPENAI_API_KEY` + `GRGA_OPENAI_MODEL`（详见 [docs/env-vars.md](docs/env-vars.md)） |
| Tk 图形界面 | ❌ | ❌ | ❌ | `grga gui` 或 `grga-gui` | 系统自带 Tk |
| Agent 友好（`--json` + 稳定退出码契约） | ❌ | ⚠️ 部分 | ⚠️ 部分 | `grga skill install` 分发内置 agent 技能包 | 错误契约见 [docs/exit-codes.md](docs/exit-codes.md) |

所有环境变量都可以写进 `$XDG_CONFIG_HOME/grga/.env`（首次运行会自动生成
一份注释模板），完整清单见 [docs/env-vars.md](docs/env-vars.md)。更多 CLI
参数细节见 [docs/CLI.md](docs/CLI.md)。

> **外部工具运行时下载**：自 0.2.0 起，wheel 不再内嵌 `rg` / `rga` / `pandoc` / `fzf` / `pdftotext` 等二进制文件。首次使用时会按需下载到用户缓存目录（见下文），也可提前运行 `grga tools install`。
>
> **许可证**：`grga` 本身以 **MIT** 许可证发布（见 [LICENSE](LICENSE)）。它调用的外部工具各自遵循其上游许可证（`rg`：MIT/Unlicense；`rga`：AGPL；`pandoc`：GPL；`fzf`：MIT），请分别遵守。


### 与其它 GraphRAG 有什么不同
- 不依赖 LLM 抽实体
- 不依赖 Embedding 和向量库
- 仅依赖统计学从语料中学习短语以及进行短语的自连接，学习到的内容存入 Sqlite，与原始语料库解绑。
- 一方面可以只选取部分代表性语料进行学习；另一方面可以直接应用于其它同领域文档库。代价是理论上的精度漂移，实践中完全可接受。如果漂移到不可接受，在新语料上再增量学习一遍即可。
- 不过分追求 Recall 的准确度，提供5类原语动词，依赖 Agent 进行多轮循环检索补齐证据。

## 快速上手

```bash
# 安装 grga
pip install grga
```

安装后即可**直接搜索纯文本类文件**（`.txt` / `.md` / 源码 / 日志 / JSON / CSV
等），无需额外配置：

```bash
cd /path/to/your/documents
grga search "面壁计划"              # 全文搜索
grga search "面壁计划" --path-only  # 只按文件名搜
grga gui                            # 图形界面（或 grga-gui）
grga --help                        # 查看命令帮助
```

**给你的 AI Agent 装 skill**（可选，一行搞定，让 Kilo / Claude Code / QwenPaw 等直接学会用 grga）：

```bash
grga skill install --to ~/.kilocode/skills   # 换成你的 agent skill 目录即可

# 预学习（可选，视语料规模需要花费一定时间，但可以提供图扩展能力）
cd /path/to/your/documents
grga index build
```
[搜索演示](docs/search-demo)下提供了“纯LLM回答”、“简单文本检索回答”、“图扩展回答”三种搜索结果，可以看到明显的差别。

### 按场景选用

**搜索 PDF / Office / 电子书** —— 首次需装一次工具链（`rga` / `pandoc` /
`pdftotext` 等，之后自动复用）：

```bash
grga tools install
grga search "面壁计划" -g "*.pdf"
```

**上下文扩展** —— 把命中扩展成段落 / 章节：

```bash
grga expand ...   # 按行 / 段落 / 章节逐级扩展
```

**关键词共现关系图**（不依赖 LLM，先建索引再查）：

```bash
cd /path/to/your/documents
grga index build
grga related 面壁计划
grga graph inspect 面壁计划
```

**LLM 深度搜索**（需配置 OpenAI 兼容凭据，见 [docs/env-vars.md](docs/env-vars.md)）：

```bash
export GRGA_OPENAI_API_KEY=sk-...
export GRGA_OPENAI_MODEL=gpt-4o-mini
# or write them to $HOME/.config/grga/.env

grga deep-search "面壁计划"
```

> 注：作者到现在还没用过深度搜索功能，基本靠文本检索+图扩展就解决问题了。


**打包成可执行文件 / 离线部署 / 作为 agent 原语接入** —— 见
[docs/install-and-usage.md](docs/install-and-usage.md)：

```bash
python -m build                       # 构建 wheel
python scripts/build-onefile.py --clean   # 构建单文件可执行程序
```

> 完整安装、工具下载、镜像/打包构建、面向 AI Agent 的进阶说明都在
> **[docs/install-and-usage.md](docs/install-and-usage.md)**；环境变量清单见
> [docs/env-vars.md](docs/env-vars.md)，CLI 参数见 [docs/CLI.md](docs/CLI.md)。

## 致谢

`grga` 的诞生离不开这两个工具在日常开发中的持续加成：

- **[QwenPaw](https://github.com/liunux4odoo/qwenpaw)** —— 本地多智能体框架。`grga` 从"检索能力"演进到"Agent-Native 检索契约"，很多设计决策（skill 分发、退出码契约、`--json` schema、caller 反思循环）都是先在 QwenPaw 的 agent 场景里被反复打磨，才沉淀成规格的。
- **[Kilo Code](https://kilocode.ai)** —— IDE 里的 AI 编程助手。`grga` 的大部分实装、测试、重构都由它落地；作者只写"目标 / 约束 / 边界"，实现细节交给它——这套"规格给约束，实现给自由"的分工正是这个项目能小而快演进的关键。
