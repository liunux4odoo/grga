# grga

`grga` 是一个基于 `ripgrep-all` 的全文搜索工具，提供命令行入口和 Tkinter 图形界面。它封装 `rga`、`rg` 等底层工具，支持在普通文本、Markdown、PDF、Office 文档等文件中搜索内容，也支持只按文件路径搜索。

项目目标是把常用搜索能力、工具链检查和内置工具管理集成到一个 Python 包中，尽量减少用户手动配置系统命令的成本。

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

### 下载工具包

`grga` 运行时优先使用 wheel 内置工具，找不到时再回退系统 `PATH`。构建或分发前，可将当前平台支持的工具下载到 `src/grga/data/tools/<platform>/`：

```bash
python scripts/download-tools.py
```

只下载指定工具：

```bash
python scripts/download-tools.py rg rga pandoc fzf
```

强制重新下载：

```bash
python scripts/download-tools.py --force
```

安装完成后可检查工具状态：

```bash
grga tools check
```

或通过 CLI 管理工具：

```bash
grga tools install
grga tools update
```

### 打包

#### 构建 wheel

先下载需要内置的工具：

```bash
python scripts/download-tools.py
```

再构建 wheel：

```bash
python -m pip install build
python -m build
```

打包产物位于 `dist/`。由于 `pyproject.toml` 已配置 `grga = ["data/tools/**/*"]`，`src/grga/data/tools/` 下的工具会被包含进 wheel。

#### 构建单文件可执行程序

构建 CLI 单文件程序：

```bash
python scripts/build-onefile.py --download-tools --clean
```

构建 GUI 单文件程序：

```bash
python scripts/build-onefile.py --gui --name grga-gui --download-tools --clean
```

输出文件位于 `dist/`。默认会把 `src/grga/data/tools/` 一并打入可执行程序；如需依赖系统 `PATH` 中的工具，可添加 `--no-tools`。
