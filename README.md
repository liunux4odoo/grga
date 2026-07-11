# grga

`grga` 是一个基于 `ripgrep-all` 的全文搜索工具，提供命令行入口和 Tkinter 图形界面。它封装 `rga`、`rg` 等底层工具，支持在普通文本、Markdown、PDF、Office 文档等文件中搜索内容，也支持只按文件路径搜索。

项目目标是把常用搜索能力、工具链检查和内置工具管理集成到一个 Python 包中，尽量减少用户手动配置系统命令的成本。

> **外部工具运行时下载**：自 0.2.0 起，wheel 不再内嵌 `rg` / `rga` / `pandoc` / `fzf` / `pdftotext` 等二进制文件。首次使用时会按需下载到用户缓存目录（见下文），也可提前运行 `grga tools install`。这样既能把 wheel 体积压到 1 MB 以内，也满足许可证合规：上游 AGPL/GPL 二进制仅在运行时下载，不随包分发。
>
> **许可证**：`grga` 本身以 **MIT** 许可证发布（见 [LICENSE](LICENSE)）。它调用的外部工具各自遵循其上游许可证（`rg`：MIT/Unlicense；`rga`：AGPL；`pandoc`：GPL；`fzf`：MIT），请分别遵守。

## 主要功能

**日常搜索**

- **全文搜索**：`grga search 关键词` —— 在文本、Markdown、PDF、Office、电子书里找内容
- **图形界面**：`grga gui` 或 `grga-gui`
- **查询表达式**：支持短语、正则、glob、逻辑或 `||`、逻辑与 `&&` 和括号分组；空格分隔的多个词默认按 **AND** 合成（v2 起，与 v1 的 OR 语义不同），用 `--implicit-or` 可恢复旧行为
- **搜索范围**：目录、路径 glob、大小写、每文件命中上限（`-n`）、全局命中上限（`-N`，默认 1000）、上下文行数
- **路径搜索**：`--path-only` 只匹配文件路径，不读取内容
- **命令预览**：`grga preview` 查看将要执行的底层命令
- **搜索前试探**：`grga probe` 只统计命中规模，不读取内容

**上下文扩展**

- **`grga expand`** —— 把搜索命中扩展成更大范围的上下文块（按行 / 段落 / 章节逐级）

**知识发现（可选，需先建立索引）**

- **`grga index build`** —— 为目录建立**关键词共现关系图**索引（首次跑全量、之后增量）
- **`grga related`** —— 查找与关键词相关的文档（基于上面的共现图）
- **`grga graph inspect`** —— 查看某个关键词在各目录下的分布

**AI 辅助（可选，需 LLM 配置）**

- **`grga deep-search`** —— 让大模型在多轮里帮你扩展 / 收窄搜索关键词
- **`grga suggest`** —— 让大模型建议相关关键词、重排结果或改写查询

**运维**

- **`grga check`** —— 检查搜索依赖是否可用
- **`grga tools install/update/check`** —— 管理随包工具（`rg` / `rga` / `pandoc` / `fzf` ...）
- **`grga skill install`** —— 安装内置的 agent 技能包（可选）

更多 CLI 用法见 [docs/CLI.md](docs/CLI.md)。

## 快速开始

```bash
# 安装（源码目录内）
python -m pip install -e .

# 首次使用先装外部工具
grga tools install

# 基础全文搜索
grga search "面壁计划"

# 图形界面
grga gui

# 想用共现关系图？先建立索引（增量，首次跑全量，之后只处理变化的文件）
grga index build --scope ./my-notes
grga related 面壁计划 --directory ./my-notes
```

## 安装方式

### 从源码安装

进入项目目录后安装：

```bash
python -m pip install -e .
```

如需构建独立可执行文件，安装额外依赖：

```bash
python -m pip install -e '.[standalone]'
```

安装后会提供两个入口：

```bash
grga --help
grga-gui
```

也可以在源码目录中通过模块方式运行：

```bash
python -m grga.cli --help
```

### 工具的下载与解析

`grga` 运行时的工具解析顺序为：**托管缓存 → 旧 site-packages 缓存 → 系统 `PATH`**（详见 [docs/tool-resolution.md](docs/tool-resolution.md)）。托管缓存目录优先级：

- `$GRGA_TOOLS_DIR`
- `$XDG_DATA_HOME/grga/tools/<platform>`
- Linux/macOS：`~/.local/share/grga/tools/<platform>`；Windows：`%LOCALAPPDATA%\grga\tools\<platform>`

按需安装（缺失时首次运行也会交互提示）：

```bash
grga tools install            # 安装当前平台支持的全部工具
grga tools install rg rga     # 只装指定工具
grga tools update             # 更新
grga tools check              # 查看状态
grga tools migrate            # 从旧 site-packages 缓存迁移到新目录
```

离线 / air-gapped 安装（从本地镜像目录安装）：

```bash
grga tools install --from ./mirror rg
```

首次运行行为与退出码契约见 [docs/exit-codes.md](docs/exit-codes.md)。Agent 场景建议加 `--json` 获取结构化错误，或 `--offline` 让缺工具时立即失败。

### 从源码构建工具镜像（开发者）

```bash
python scripts/download-tools.py --platform all --out ./mirror --write-hashes --yes
```

该脚本仅供开发者/发布使用，不参与 `python -m build`。详见 [docs/dev-release.md](docs/dev-release.md)。

### 打包

#### 构建 wheel

```bash
python -m pip install build
python -m build
```

打包产物位于 `dist/`。wheel 为 `py3-none-any`，不内嵌任何平台二进制（工具在运行时下载）。

#### 构建单文件可执行程序

构建 CLI 单文件程序：

```bash
python scripts/build-onefile.py --clean
```

构建 GUI 单文件程序：

```bash
python scripts/build-onefile.py --gui --name grga-gui --clean
```

输出文件位于 `dist/`。外部工具（rg / rga / pandoc / fzf）**不再打入** onefile 可执行程序，由 `grga.toolchain` 在运行时按需从 `$PATH` 或 XDG 缓存加载，必要时首次运行会提示下载。

## 面向 AI Agent（进阶）

grga 的另一条设计线是把自己做成 **agent 友好的检索原语**——所有命令都支持 `--json`、返回结构化字段与稳定契约。

**面向 caller 的两句话：**

> **grga 返回与你的 seed 共现的词/实体，每项携带 df、共现次数、结果分布形态等原始统计。**
>
> **它不做相关性判断、不 rerank、不预筛"重要"结果 —— 这是 caller 的活。**

分层原语：

- **L1**（就近上下文）：物理邻近扩展（前后行 / 段落 / 章节），对应 `grga expand`
- **L2**（共现关系图）：离线预建的关键词共现图，查询快，对应 `grga index` / `grga related` / `grga graph`
- **L3**（LLM 语义扩展）：只在 L1/L2 无功时召唤，贵，对应 `grga suggest` / `grga deep-search`

**详细设计规范** → [`docs/design/v2-design.md`](docs/design/v2-design.md)
**为什么 grga 不做 rerank（给未来贡献者的护栏）** → [`docs/design/graph-l2-postmortem-2026-07-10.md`](docs/design/graph-l2-postmortem-2026-07-10.md)
**增量索引 / 遗忘机制** → [`docs/design/plan-2026-07-11-build-vs-rebuild.md`](docs/design/plan-2026-07-11-build-vs-rebuild.md)

## 致谢

`grga` 的诞生离不开这两个工具在日常开发中的持续加成：

- **[QwenPaw](https://github.com/liunux4odoo/qwenpaw)** —— 本地多智能体框架。`grga` 从"检索能力"演进到"Agent-Native 检索契约"，很多设计决策（skill 分发、退出码契约、`--json` schema、caller 反思循环）都是先在 QwenPaw 的 agent 场景里被反复打磨，才沉淀成规格的。
- **[Kilo Code](https://kilocode.ai)** —— IDE 里的 AI 编程助手。`grga` 的大部分实装、测试、重构都由它落地；作者只写"目标 / 约束 / 边界"，实现细节交给它——这套"规格给约束，实现给自由"的分工正是这个项目能小而快演进的关键。
