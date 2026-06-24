# grga CLI 使用文档

`grga` 是一个基于 `ripgrep-all` 的命令行搜索工具，同时提供工具链管理和 Tkinter GUI 启动入口。

## 安装与入口

安装项目后会提供两个命令：

```bash
grga
grga-gui
```

也可以在源码目录中使用 Python 模块方式运行：

```bash
python -m grga.cli --help
```

查看顶层帮助：

```bash
grga --help
```

## 命令概览

```text
grga search   执行搜索
grga preview  预览底层搜索命令
grga check    检查搜索运行环境
grga gui      启动图形界面
grga tools    管理内置命令行工具
```

## 搜索文件内容

基本用法：

```bash
grga search 关键词
```

在指定目录中搜索：

```bash
grga search 关键词 --directory /path/to/project
```

等价短参数：

```bash
grga search 关键词 -d /path/to/project
```

搜索多个关键词时，CLI 会把多个参数用空格拼接为一个查询表达式：

```bash
grga search hello world
```

### 查询表达式

`grga` 支持单词、短语、逻辑或、逻辑与和括号分组。

```bash
# 包含 hello 或 world
grga search 'hello || world'

# 同一行同时匹配 hello 和 world
grga search 'hello && world'

# 分组表达式
grga search '(hello || hi) && world'

# 带空格的短语
grga search '"hello world"'
```

未显式写 `||` 或 `&&` 的相邻查询项会按逻辑或处理。

### 匹配模式

使用 `--mode` 或 `-m` 控制关键词解释方式：

```bash
grga search 'foo.*bar' --mode regex
grga search '*.md' --mode glob
grga search '*.md' --mode auto
```

可选值：

| 模式 | 说明 |
| --- | --- |
| `auto` | 默认模式，自动判断 glob 或正则 |
| `regex` | 按正则表达式匹配 |
| `glob` | 按 glob 模式匹配 |

### 大小写

默认区分大小写：

```bash
grga search keyword --case-sensitive
```

忽略大小写：

```bash
grga search keyword --ignore-case
```

### 限制结果数量

```bash
grga search keyword --max-count 20
```

短参数：

```bash
grga search keyword -n 20
```

### 设置超时

默认超时时间为 600 秒：

```bash
grga search keyword --timeout 30
```

短参数：

```bash
grga search keyword -t 30
```

### 上下文行数

默认显示匹配行前后各 5 行上下文：

```bash
grga search keyword --before-context 2 --after-context 3
```

短参数：

```bash
grga search keyword -B 2 -A 3
```

设置为 0 可关闭对应方向的上下文：

```bash
grga search keyword -B 0 -A 0
```

### 多行搜索

```bash
grga search 'foo.*bar' --multiline
```

启用后会向底层 `rga` 传递多行搜索参数。

### 按文件路径过滤

`--iglob` 或 `-g` 可按文件路径 glob 过滤搜索范围，可重复使用：

```bash
grga search keyword --iglob '*.md'
grga search keyword -g '*.py' -g '*.md'
```

排除模式可使用 `!` 前缀：

```bash
grga search keyword -g '*.py' -g '!tests/*'
```

### 只搜索相对文件路径

`--path-only` 不搜索文件内容，只匹配目录中的相对文件路径：

```bash
grga search README --path-only
```

路径搜索由底层 `rg --files` 枚举文件路径，再对路径名做查询表达式匹配。`rg` 会自动处理 `.gitignore`、glob 过滤和隐藏文件规则。

### JSON 输出

```bash
grga search keyword --json
```

成功时返回结构包含：

```json
{
  "ok": true,
  "patterns": ["keyword"],
  "directory": "/path/to/dir",
  "matches": []
}
```

失败时返回结构包含：

```json
{
  "ok": false,
  "error": "error message",
  "matches": []
}
```

## 预览底层命令

`preview` 用于查看搜索会转换成什么底层命令，不实际执行搜索：

```bash
grga preview keyword
```

常用参数与 `search` 基本一致：

```bash
grga preview 'foo && bar' --ignore-case -g '*.md' -B 0 -A 0
```

支持的参数：

```text
--mode, -m
--case-sensitive / --ignore-case
--max-count, -n
--before-context, -B
--after-context, -A
--multiline
--iglob, -g
--path-only
```

## 检查搜索环境

```bash
grga check
```

该命令会检查：

```text
rg
rga
rga-preproc
pandoc
```

其中 `rg` 和 `rga` 缺失会导致普通内容搜索不可用；`rga-preproc` 和 `pandoc` 缺失时，Office 等非纯文本格式支持可能受限。

输出 JSON：

```bash
grga check --json
```

## 启动 GUI

```bash
grga gui
```

也可以使用安装后提供的独立入口：

```bash
grga-gui
```

## 管理内置工具

`grga tools` 用于查看、安装和更新打包到本地的命令行工具。运行时会优先使用 wheel 内置工具，找不到时再回退系统 `PATH`。

### 检查工具状态

```bash
grga tools check
```

输出 JSON：

```bash
grga tools check --json
```

### 查看当前平台工具目录

```bash
grga tools path
```

### 查看工具根目录

```bash
grga tools root
```

### 列出支持的工具

```bash
grga tools list
```

输出格式示例：

```text
rg: BurntSushi/ripgrep (supported)
rga: phiresky/ripgrep-all (supported)
```

### 安装工具

安装当前平台支持的全部工具：

```bash
grga tools install
```

安装指定工具：

```bash
grga tools install rg rga pandoc fzf
```

即使工具已存在也重新安装：

```bash
grga tools install --force
```

使用下载镜像前缀：

```bash
grga tools install --base-url https://example.com/mirror
```

### 更新工具

更新全部支持的工具：

```bash
grga tools update
```

更新指定工具：

```bash
grga tools update rg rga
```

使用下载镜像前缀：

```bash
grga tools update --base-url https://example.com/mirror
```

### 查看平台标识

```bash
grga tools platform
```

## 常用示例

```bash
# 在当前目录搜索关键词
grga search TODO

# 在项目目录中忽略大小写搜索
grga search config -d ~/project --ignore-case

# 只搜索 Markdown 文件
grga search 'install' -g '*.md'

# 搜索 Python 文件但排除 tests 目录
grga search 'def main' -g '*.py' -g '!tests/*'

# 同一行同时包含 error 和 timeout
grga search 'error && timeout'

# 搜索文件路径中包含 README 的文件
grga search README --path-only

# 输出机器可读 JSON
grga search keyword --json

# 预览搜索命令
grga preview 'foo.*bar' --mode regex --multiline

# 检查环境
grga check

# 检查内置工具
grga tools check
```

## 退出码

| 命令 | 成功 | 失败 |
| --- | --- | --- |
| `search` | 搜索成功，包括 0 个匹配 | 参数错误、执行失败或超时 |
| `preview` | 预览成功 | 参数错误 |
| `check` | 必需工具可用 | 必需工具缺失 |
| `tools check` | 命令执行成功 | 通常只报告工具状态，不以缺失工具作为失败 |
| `tools install` | 安装成功 | 下载、解压或平台不支持等错误 |
| `tools update` | 更新成功 | 下载、解压或平台不支持等错误 |

## 注意事项

- 普通内容搜索依赖 `rga`，而 `rga` 通常依赖 `rg`。
- 搜索 Office、PDF 等格式时，可能需要 `rga-preproc`、`pandoc`、`pdftotext` 等工具支持。
- `--path-only` 只匹配相对文件路径，不读取文件内容，依赖 `rg`（通过 `rg --files` 枚举路径）。
- `--iglob` 是大小写不敏感的路径 glob 过滤。
- 逻辑与表达式会使用 PCRE2 lookahead，底层命令会启用 `-P`。
