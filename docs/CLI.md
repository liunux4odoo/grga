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
grga search       执行搜索
grga probe        探测查询规模（只读，不返回正文）
grga deep-search  使用 LLM 规划关键词并迭代深度检索
grga preview      预览底层搜索命令
grga check        检查搜索运行环境
grga gui          启动图形界面
grga tools        管理内置命令行工具
grga mcp start    以 stdio 模式启动 MCP 服务器（需 `pip install grga[mcp]`）
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

> 注意（v2 起的破坏性变更）：空格分隔的多个词默认按**逻辑与（AND）**处理，
> 上例等价于 `hello && world`（要求同一行同时包含二者）。这与 Google / ripgrep
> 的直觉一致。需要旧的“空格即逻辑或”行为时加 `--implicit-or`。触发隐式 AND 时，
> grga 会在 stderr 打印一行提示。

### 查询表达式

`grga` 支持单词、短语、逻辑或、逻辑与和括号分组。

```bash
# 包含 hello 或 world
grga search 'hello || world'

# 同一行同时匹配 hello 和 world（等价于 grga search hello world）
grga search 'hello && world'

# 分组表达式
grga search '(hello || hi) && world'

# 带空格的短语
grga search '"hello world"'
```

未显式写 `||` 或 `&&` 的相邻查询项默认按**逻辑与（AND）**处理；
加 `--implicit-or` 可切回按逻辑或处理。

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

`--max-count / -n` 是 **每文件** 命中上限（透传给 rga）：

```bash
grga search keyword --max-count 20
```

短参数：

```bash
grga search keyword -n 20
```

### 全局命中上限（--max-total / -N）

`--max-total / -N` 是 **单次调用的全局** 命中上限，默认 `1000`。在超大单文件
（如整本小说、GB 级日志）场景下，`--max-count` 几乎不起保护作用，`--max-total`
则保证可预测的响应时间：grga 流式读取 rga 输出，达到上限后立即结束并杀掉 rga
子进程，返回结果里 `truncated: true`。位置元数据（occurrence / ratio / nearby）
在截断之后才计算，因此不会为丢弃的命中白算。

```bash
# 最多返回 50 条真实命中，超出即截断
grga search keyword -N 50

# 关闭全局上限
grga search keyword --max-total 0

# 宁可失败也不返回部分结果：先用 rga --count-matches 预估总数，
# 超过上限时返回 {"ok": false, "error": "too_many_matches", "total_hits": …}
grga search keyword -N 50 --strict-cap
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

## 探测查询规模

`probe` 是一条**只读、廉价、结构化**的探测命令：它把当前查询编译后交给 `rga --count-matches`（`--path-only` 时用 `rg --files`），返回命中文件数、命中行数、（可选）估算字节量，以及查询"被如何解释"的元信息。它**不返回正文、不返回上下文、不改写关键字、不做任何"多/少"的阈值判断**。

其用途是让调用方（人或 caller agent）在执行完整 `search` **之前**先判断规模，从而决定是收紧还是放宽关键字组合。

> `probe` 返回的是**当前查询在磁盘上的真实规模**，不含上下文行，也不受 `--max-count` / `--max-total` 影响。它本身不做任何策略决策。

基本用法：

```bash
grga probe 关键词 -d /path/to/project --json
```

与查询语义相关的参数（`--mode` / `--case-sensitive` / `--iglob` / `--multiline` / `--path-only` / `--implicit-and` / `--implicit-or`）都与 `grga search` 同名同义，因此探测用的查询与最终执行的查询是同一套。

probe 独有参数：

- `--sample N`：抽样至多 N 个命中行，估算平均每行字节数与总字节量（默认 0，即不抽样）。
- `--per-file`：在 JSON 中附带每个文件的命中行数（按命中数降序）。
- `--timeout`：探测超时秒数，默认 60（比 `search` 的 600 短，避免调用方长时间等待）。

JSON 返回结构（成功）：

```json
{
  "ok": true,
  "directory": "/abs/path",
  "patterns": ["(?=.*(?:foo))(?=.*(?:bar))"],
  "terms": [{"value": "foo", "pattern": "foo"}, {"value": "bar", "pattern": "bar"}],
  "pcre2": true,
  "implicit_operator": "and",
  "implicit_notice": "multi-term query treated as AND: ...",
  "total_lines": 1284,
  "file_count": 37,
  "per_file": [{"path": "docs/a.md", "matches": 812}],
  "sample": {"sampled_lines": 32, "avg_line_bytes": 118, "est_total_bytes": 151512},
  "elapsed_ms": 87,
  "truncated": false,
  "warning": null,
  "skipped_files": []
}
```

字段要点：

- `total_lines`：命中行数总和；`file_count`：至少命中一行的文件数。
- `patterns` / `terms` / `pcre2` / `implicit_operator` / `implicit_notice`：与 `search` 输出一致，帮助调用方了解查询被如何解释。
- `sample.est_total_bytes`：`total_lines * avg_line_bytes` 的**粗估**，只给数量级参考，不宜做严格算术。

失败时 JSON 结构与成功时**同构**（同样包含 `patterns` / `terms` / `pcre2` / `implicit_operator` / `implicit_notice` / `directory` / `total_lines` / `file_count` / `elapsed_ms` 等字段，值为默认零/空），额外携带 `ok: false` 与 `error` 字段，方便调用方用统一的 schema 解析：

```json
{
  "ok": false,
  "error": "rga probe timed out",
  "directory": "/abs/path",
  "patterns": ["search"],
  "terms": [{"value": "search", "pattern": "search"}],
  "pcre2": false,
  "implicit_operator": "and",
  "total_lines": 0,
  "file_count": 0,
  "elapsed_ms": 60000
}
```

**超时预算**：`--timeout` 是**全局**上限，涵盖 per-file 计数与可选的抽样两阶段。抽样阶段共享剩余预算，不会额外占用一整个 `timeout`。

### 推荐的调用方 workflow

`probe` 只提供基础设施，不内置策略。推荐（非强制）的编排方式：

1. `probe(query)` → 得到 `total_lines` / `file_count` / `est_total_bytes`。
2. 判断：
   - `total_lines == 0` → 放宽（`and→or` / 去词 / 去 glob），回到 1。
   - 数量极大或估算字节超出预算 → 收紧（`or→and` / 加词 / 加 `--iglob` / 执行时上 `--max-count`），回到 1。
   - 落在舒适区间 → 用**相同参数**执行 `grga search`。
3. 自行限制迭代次数（通常 2-3 次），不收敛则取最接近的一档执行。

与 `deep-search --tiered-search` 的区别：后者是把"从严到松"的策略**硬编码在后端**；`probe` 则把决策权交给调用方，两者并存、互不冲突。

## 深度检索

`deep-search` 使用 OpenAI 兼容客户端调用 LLM，把自然语言查询转换为 `grga` 查询表达式，检索后再从结果中抽取关键词并迭代扩展查询。

> `deep-search` 的内部检索使用 v1 的“空格即逻辑或”语义（`--implicit-or`），
> 并使用一个较高的每轮全局上限（`DEEP_SEARCH_MAX_TOTAL = 20000`）而非普通
> `search` 的默认 1000，因为它需要较多原始命中来做关键词排序；该上限仍会兜底，
> 防止超大语料把调用方拖挂。

```bash
grga deep-search "查找支付超时相关错误" \
  --directory /path/to/project \
  --llm-base-url https://api.example.com/v1 \
  --llm-api-key sk-xxx \
  --llm-model model-name
```

LLM 参数也可以通过环境变量配置：

```text
GRGA_OPENAI_BASE_URL
GRGA_OPENAI_API_KEY
GRGA_OPENAI_MODEL
```

也可以写入用户配置文件，适合保存相对固定的深度检索参数：

```text
~/.config/grga/settings.json
```

示例：

```json
{
  "deep_search": {
    "llm_base_url": "https://api.example.com/v1",
    "llm_api_key": "sk-xxx",
    "llm_model_name": "model-name",
    "pattern_mode": null,
    "case_sensitive": null,
    "max_count": null,
    "timeout": null,
    "before_context": null,
    "after_context": null,
    "multiline": null,
    "path_only": null,
    "max_related_keywords": 8,
    "repeat_count": 1,
    "max_extract_segments": 20,
    "db_path": "~/.config/grga/deep_keywords.db",
    "decay_factor": 0.9,
    "min_keyword_count": 0.1,
    "global_library": false
  }
}
```

其中与普通检索同名的参数默认为 `null`，表示沿用普通检索的配置；只有显式设置时才覆盖。

命令行参数优先级最高；未传命令行参数时读取配置文件。对于 `pattern_mode`、`case_sensitive`、`max_count`、`timeout`、`before_context`、`after_context`、`multiline`、`path_only` 等与普通检索同名的参数，若 `deep_search` 配置中值为 `null`（即默认值），会自动回退到普通检索的对应设置值；如果普通检索也未设置，最终使用内建默认值。读取配置文件时会检查 `deep_search` 中的固定 key，缺失项会自动补充为默认值并保存回 `settings.json`。`llm_base_url`、`llm_api_key`、`llm_model_name` 如果仍为空，会继续回退到上面的环境变量。

常用参数：

```text
--max-related-keywords 8   每轮最多选择的相关关键词数量
--repeat-count 1           追加迭代检索次数
--max-extract-segments 20  每轮发送给 LLM 抽取关键词的结果段落数量
--db-path PATH             SQLite 关键字库路径
--global-library           查询规划时使用全库关键字
--directory-library        查询规划时只使用当前目录关键字（默认）
--decay-factor 0.9         每次运行前对当前目录关键字频率做衰减
--min-keyword-count 0.1    衰减后低于该值的记录会被删除
--json                     输出机器可读 JSON
```

关键字库默认保存在：

```text
~/.config/grga/deep_keywords.db
```

关键字库使用 SQLite 存储，包含用户查询共现词、文档抽取共现词、单词频率和关键词来源文件/行号。默认只读取当前搜索目录对应的历史关键字；使用 `--global-library` 可让 LLM 查询规划参考全库历史。循环过程中如果无法抽取候选关键词，或 LLM 选出的相关关键词没有新的 seed，会提前停止。

深度检索的普通搜索参数与 `search` 基本一致，包括 `--mode`、`--ignore-case`、`--max-count`、`--timeout`、`--before-context`、`--after-context`、`--multiline`、`--iglob` 和 `--path-only`。

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

## 启动 MCP 服务器

`grga mcp start` 以 stdio 模式启动一个 MCP（Model Context Protocol）服务器，
把 grga 的 5 个检索动词（`probe` / `search` / `expand` / `related` /
`suggest`）+ `scope list` 暴露成 MCP tools，供 Claude Desktop、Cline、Cursor
等本地 MCP 客户端使用。

```bash
pip install "grga[mcp]"   # 首次使用需安装可选依赖
grga mcp start
```

可以指定默认搜索目录，这样调用 `grga_search` / `grga_probe` / `grga_expand`
时可以省略 `directory` / `base_dir` 参数：

```bash
grga mcp start --directory /path/to/corpus
```

MCP 服务器是**只读 + 检索侧**：`suggest`（L3 LLM 兜底）不写入投票。日志走
stderr（`GRGA_MCP_LOG_LEVEL`），共享图索引 DB 可用 `GRGA_DB_PATH` 或
`--db-path` 覆盖。设计见 `docs/design/plan-2026-09-02-mcp-server.md`。

## 建立图索引（`index build` / `index rebuild`）

`grga index build` 增量更新一个 scope 的共现知识图；`grga index rebuild`
全量清空重建。两个命令参数相同。

```bash
grga index build --scope my-notes --directory ./my-notes
grga index rebuild --scope my-notes --directory ./my-notes --refresh-ngram=replace
```

### 主要参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--scope` / `-s` | 必填 | 逻辑 scope 名（v10，跨机器可迁移） |
| `--directory` / `-d` | `.` | 物理语料目录（或单文件） |
| `--db-path` | 共享 DB | SQLite 图库路径 |
| `--chunk-mode` | `line` | 分块粒度：`line`（1 行 1 chunk）或 `chars`（多行窗口） |
| `--chunk-max-chars` / `--chunk-min-chars` | `600` / `300` | `chars` 模式窗口大小 |
| `--min-pair-count` | 自适应（2-5） | 共现对最低 chunk 计数门槛 |
| `--min-npmi-build` | `0.2` | pair 写入期 npmi 门槛（不可逆，见 build-tuning §2） |
| `--max-file-kb` | `0`（不限） | 跳过更大的文件 |
| `--ngram-iters` | `2` | auto n-gram 检测轮数（`rebuild` 生效） |
| `--max-ngram-n` | `4` | 最长检测短语字数 |
| `--refresh-ngram` | `append` | `append`（并入新短语）/ `never`（不检测）/ `replace`（仅 rebuild，清空重建） |
| `--seed-ngram-from` | — | 借用另一 scope 的词典冷启动 |
| `--tag` / `-t` | — | 给 scope 附加多值标签（可重复） |
| `--include-glob` | 空（全收） | 只索引相对 `--directory` 路径命中**任一** pattern 的文件；**弱语义**（既有行保留），可重复 |
| `--exclude-glob` | 空（不排除） | 跳过命中**任一** pattern 的文件；优先级高于 include；**弱语义**（既有行保留），可重复 |
| `--batch-chunks` | `50000` | 分批构建的每批目标 chunk 数；`0` = 关闭分批（bit-exact 单遍，pair recall 1.0 但峰值内存更高） |
| `--top-n-kw` | `64`（env `GRGA_BATCH_TOP_N`） | `--batch-chunks > 0` 时每 chunk 关键字上限 |
| `--json` | — | 输出结构化 JSON stats |

### glob 过滤说明

- 匹配对象是**相对 `--directory` 的路径**；单文件 `--directory` 匹配文件名。
- **宽松匹配**：`*` 跨目录（`docs/*.md` 也匹配 `docs/sub/b.md`），与
  `rg --iglob` 心智一致。
- **不区分大小写**：`*.MD` 匹配 `foo.md`。
- **弱语义**：被 glob 排除的文件只"不参与本次 build"，**既有索引行
  （keyword_docs / heading_docs / docs）原样保留**，`keyword_pairs` 仍基于
  完整语料；下次 build 去掉 glob 后文件自动重新参与。想真正移除文件用
  `grga scope rm --purge-knowledge` / `grga index forget`。

```bash
grga index build --scope X --directory /path \
  --include-glob '*.md' \
  --exclude-glob 'build/**' --exclude-glob '*.tmp'
```

详细调优见 [`build-tuning.md`](build-tuning.md)。

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
