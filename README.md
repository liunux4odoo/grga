# grga

`grga` 是一个基于 `ripgrep-all` 的全文搜索工具，提供命令行入口和 Tkinter 图形界面。它封装 `rga`、`rg` 等底层工具，支持在普通文本、Markdown、PDF、Office 文档等文件中搜索内容，也支持只按文件路径搜索。

项目目标是把常用搜索能力、工具链检查和内置工具管理集成到一个 Python 包中，尽量减少用户手动配置系统命令的成本。

> **外部工具运行时下载**：自 0.2.0 起，wheel 不再内嵌 `rg` / `rga` / `pandoc` / `fzf` / `pdftotext` 等二进制文件。首次使用时会按需下载到用户缓存目录（见下文），也可提前运行 `grga tools install`。这样既能把 wheel 体积压到 1 MB 以内，也满足许可证合规：上游 AGPL/GPL 二进制仅在运行时下载，不随包分发。
>
> **许可证**：`grga` 本身以 **MIT** 许可证发布（见 [LICENSE](LICENSE)）。它调用的外部工具各自遵循其上游许可证（`rg`：MIT/Unlicense；`rga`：AGPL；`pandoc`：GPL；`fzf`：MIT），请分别遵守。

## 主要功能展示

- 命令行搜索：`grga search 关键词`
- 图形界面：`grga gui` 或 `grga-gui`
- 查询表达式：支持短语、正则、glob、逻辑或 `||`、逻辑与 `&&` 和括号分组；空格分隔的多个词默认按 **AND** 合成（v2 起，与 v1 的 OR 语义不同），用 `--implicit-or` 可恢复旧行为
- 搜索范围控制：支持指定目录、路径 glob 过滤、大小写控制、每文件命中上限（`-n / --max-count`）、全局命中上限（`-N / --max-total`，默认 1000，`0` 关闭）和上下文行数
- 多格式内容搜索：通过 `ripgrep-all` 搜索文本、PDF、Office、电子书等格式
- 路径搜索：`--path-only` 可只匹配相对文件路径，不读取文件内容
- 命令预览：`grga preview` 可查看将要执行的底层搜索命令
- 环境检查：`grga check` 检查搜索依赖是否可用
- 内置工具管理：`grga tools install/update/check` 管理随包使用的 `rg`、`rga`、`pandoc`、`fzf` 等工具

更多 CLI 用法见 [docs/CLI.md](docs/CLI.md)。

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
