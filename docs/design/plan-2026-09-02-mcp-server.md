# plan-2026-09-02 · MCP Server：grga 内置模块 MVP

> 状态：**已确认，待实施**。触发讨论时间：2026-09-02。
> 决策方式：discuss agent 设计 → code agent 实现 → 验收

## 1. 背景与目标

grga 的 skill 版本在 QwenPaw 环境里工作良好，但生态边界受限于 skill 体系。很多 AI 客户端（Claude Desktop、Cline、Cursor、Windsurf、IDE 插件等）不支持 QwenPaw skill，但都支持 **MCP（Model Context Protocol）**。

**目标**：在 grga 库内新增一个独立模块 `grga.mcp`，把 grga CLI 的 5 个核心动词 + `scope list` 封装成 MCP tools，通过 stdio 方式对外提供服务。不做独立包，不加新 CLI 原语，不碰 build/migrate 流程。

### 1.1 不做的事（边界钉死，防止膨胀）

| 项 | 做 / 不做 | 理由 |
|---|---|---|
| 封装 5 个动词（probe / search / expand / related / suggest） | ✅ 做 | 核心能力 |
| 封装 `grga scope list` | ✅ 做 | 已存在的 CLI 原语，caller 需要知道能查哪些 scope |
| graph build / migrate / delete | ❌ 不做 | 归用户手动处理，MCP 是只读 + 检索侧 |
| HTTP / SSE 传输 | ❌ 不做 | MVP 只做 stdio，后续按需加 |
| 结果缓存 | ❌ 不做 | 每次 fresh call，复杂度留给 caller |
| 并发控制 | ❌ 不做 | grga CLI 本身单进程，MCP 调用自然串行 |
| scope 热加载 | ❌ 不做 | MCP server 重启才刷新 scope 列表 |
| 独立 PyPI 包 | ❌ 不做 | 作为 grga 库的子模块，`pip install grga[mcp]` 即可 |

### 1.2 一个重要的前提修正（决策留痕）

讨论初期曾假设「`scope list` 需要作为新 CLI 原语新增」。实际探查代码后发现 **`grga scope list` 从 v10 起就存在**（`src/grga/cli.py` 里 `@scope_app.command("list")`，底层调 `grga.graph.scope.list_scopes`），`--json` 输出格式已经结构化。因此：

- **不需要新增任何 CLI 原语**
- **不需要更新 skill 文档**（skill 里 scope 已经是一等公民）
- MCP 层只多封装一个已存在的命令，不触发"新原语该不该存在"的讨论

这个发现让方案范围比最初讨论的更小、更干净。

## 2. 技术选型

| 选型 | 选择 | 理由 |
|---|---|---|
| 语言 | Python | 和 grga CLI 同栈 |
| MCP SDK | `mcp` 官方包（FastMCP 风格） | 最轻量、最主流的 Python MCP 实现 |
| 传输 | stdio 模式 | MVP 唯一支持，所有本地 MCP 客户端都兼容 |
| 调用方式 | 直接 import grga 的 Python API | 比 subprocess 快 10-100x，不用解析 JSON 字符串 |
| 分发 | grga 包的 optional dependency（`[mcp]` extra） | 不增加基础包依赖体积 |
| 入口命令 | `grga mcp`（新增 CLI 子命令） | 和现有 `grga index` / `grga scope` 风格一致 |

### 2.1 为什么用 import 不用 subprocess

两个候选：

- **方向 A**：MCP server 直接 import `grga.probe` / `grga.search` / ... 等模块的函数
- **方向 B**：MCP server 用 subprocess 调 `grga` CLI，解析 stdout 的 JSON

选 **A**。理由：

1. **性能差 10-100 倍** —— subprocess 的 fork + Python 启动开销不可忽略，agent 交互里 probe/search 经常连续调十几次
2. **错误处理更干净** —— Python 异常直接转 MCP ToolError，不用解析 stderr
3. **类型安全** —— 直接传 Python 对象，没有 JSON 序列化/反序列化的中间层
4. **调试方便** —— 单元测试能直接测，不用起子进程

否决点：唯一的代价是 MCP server 和 grga CLI 必须在同一个 Python 环境里。但 MCP server 本身就是 grga 包的子模块，不存在这个问题。

## 3. 模块结构

```
src/grga/
├── cli.py                  # 加 mcp_app + @app.command("mcp") 入口
├── mcp/                    # ← 新模块
│   ├── __init__.py
│   ├── server.py           # FastMCP 实例 + 6 个 tool 注册
│   ├── tools/              # 每个 tool 一个文件，逻辑薄如蝉翼
│   │   ├── __init__.py
│   │   ├── probe.py
│   │   ├── search.py
│   │   ├── expand.py
│   │   ├── related.py
│   │   ├── suggest.py
│   │   └── scope.py        # scope_list
│   └── utils.py            # 公共工具：参数校验、错误包装、返回值格式化
├── graph/
│   └── scope.py            # 已有 list_scopes，直接复用
├── probe.py                # 已有
├── search.py               # 已有
...
```

**每个 tool 文件的职责**：只有三件事——参数校验 → 调 grga 内部函数 → 格式化返回值。不做业务逻辑。

## 4. MCP Tools 清单（6 个）

### 4.1 `grga_probe`

**对应**：直接调 `grga.probe.probe_directory(...)`，不绕 CLI

**参数**：

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string[] | ✅ | - | 关键字列表（caller 已分好词，MCP 不做中文分词） |
| `directory` | string | ✅ | - | 探测目录 |
| `case_sensitive` | bool | ❌ | false | 大小写敏感 |
| `multiline` | bool | ❌ | false | 多行模式 |
| `glob_patterns` | string[] | ❌ | [] | 文件 glob 过滤 |
| `per_file` | bool | ❌ | false | 是否返回每文件命中数 |
| `implicit_operator` | string | ❌ | `and` | 多词隐式组合 `and` / `or` |

**返回**：直接透传 `probe_directory()` 的 `json.loads()`：

```json
{
  "ok": true,
  "directory": "/abs/path",
  "patterns": ["rga-patterns"],
  "terms": ["苍坤", "上人"],
  "pcre2": false,
  "implicit_operator": "and",
  "implicit_notice": null,
  "total_lines": 42,
  "file_count": 3,
  "truncated": false,
  "warning": null,
  "skipped_files": [],
  "per_file": [
    {"path": "...", "matches": 18}
  ]
}
```

> `per_file=false` 时 `per_file` 字段不存在（CLI 也是这个行为，**不补空数组**，保持字段语义一致）。

### 4.2 `grga_search`

**对应**：直接调 `grga.search.search_directory(...)`，不绕 CLI

**参数**：

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string[] | ✅ | - | 关键字列表（caller 已分好词） |
| `directory` | string | ✅ | - | 搜索目录 |
| `pattern_mode` | string | ❌ | `auto` | `auto` / `literal` / `regex` / `pcre2` |
| `case_sensitive` | bool | ❌ | false | 大小写敏感 |
| `max_total` | integer | ❌ | None | 最大返回命中数（None=不限） |
| `before_context` | integer | ❌ | 3 | 命中前上下文行数 |
| `after_context` | integer | ❌ | 3 | 命中后上下文行数 |
| `multiline` | bool | ❌ | false | 多行模式 |
| `glob_patterns` | string[] | ❌ | [] | 文件 glob 过滤 |
| `glob_exclude` | bool | ❌ | false | glob 模式是否用作 exclude |
| `path_only` | bool | ❌ | false | 仅搜索路径（不读文件内容） |
| `implicit_operator` | string | ❌ | `and` | 多词组合 `and` / `or` |

**返回**：直接透传 `search_directory()` 的 `json.loads()`：

```json
{
  "ok": true,
  "directory": "/abs/path",
  "matches": [
    {
      "path": "...",
      "line_number": 1234,
      "text": "命中行内容",
      "char_span": [start, end],
      "submatches": [["匹配起止", "匹配内容"]],
      "before_context": [...],
      "after_context": [...]
    }
  ],
  "stats": {
    "elapsed": 0.42,
    "bytes_searched": 1234567,
    "files_matched": 3
  }
}
```

> 搜索真实结构由 `grga.search.search_directory()` 决定，**MCP 层不重构字段**。具体 `matches[]` 元素字段以实际返回为准（`Hit.to_dict()` + rga 输出）。

### 4.3 `grga_expand`

**对应**：直接调 `grga.expand.expand(hit, level, size, base_dir)`，不绕 CLI

**参数**：

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `file_path` | string | ✅ | - | 要展开的文件路径 |
| `anchor_line` | integer | ✅ | - | 锚点行号（构造 anchor hit 用） |
| `level` | string | ✅ | - | `lines` / `paragraph` / `chapter` / `document` / `toc` |
| `size` | integer | ❌ | 3 | `lines` 级别的上下行数 |
| `base_dir` | string | 二选一 | - | 解析 file_path 的基准目录 |
| `scope` | string | 二选一 | - | 逻辑 scope 名（替代 base_dir） |

**返回**：直接透传 `Context.to_dict()`，结构为：

```json
{
  "hit": {
    "path": "...",
    "line_number": 1234,
    "text": "...",
    "char_span": [0, 0],
    "occurrence_index": 1,
    "total_occurrences": 77,
    "line_ratio": 0.42,
    "nearby_hits": 0,
    "heading_path": null,
    "chapter_id": null,
    "chapter_title": null
  },
  "text": "展开后的正文内容...",
  "level": "chapter",
  "range": [1200, 1450],
  "truncated": false
}
```

**实现注意**：`level="document"` 有 1MB 硬上限（`expand._MAX_DOCUMENT_BYTES`），无需 MCP 层额外保护。`truncated=True` 时 caller 应重新构造更小窗口的 hit。

### 4.4 `grga_related`

**对应**：直接调 `grga.related.related_keywords(seeds, limit_per_seed, min_npmi, scopes, tags, tag_logic, db_path)`

> 命名约定：CLI 里是 `grga related --mode graph`，MCP 工具名直接用 `grga_related`，因为 `graph` 是 caller 反思循环 T2 的默认用法，**MCP 层不暴露 `heading` 模式**（那是另一个独立流程，不混进来）。

**参数**：

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `seeds` | string[] | ✅ | - | 种子词列表（支持多个） |
| `limit_per_seed` | integer | ❌ | 30 | 每个 seed 返回的邻居数 |
| `min_npmi` | float | ❌ | None | NPMI 下限（只能收紧 build 时的 0.2 floor） |
| `scopes` | string[] | ❌ | [] | 逻辑 scope 名过滤（多选 = 并集） |
| `tags` | string[] | ❌ | [] | scope tag 过滤（多选） |
| `tag_logic` | string | ❌ | `or` | tag 组合方式 `or` / `and` |

**返回**：直接透传 `RelatedKeywordsResult.to_dict()`：

```json
{
  "results": [
    {
      "kw": "遗书",
      "npmi": 0.87,
      "cooccur_count": 42.0,
      "via_seed": "苍坤上人",
      "chunk_df": 3,
      "source": "line",
      "scopes": ["fanren"]
    }
  ],
  "resolved_seeds": [
    {"seed": "苍坤上人", "in_index": true}
  ]
}
```

> `related` 的 MCP 工具名不加 `_keywords` 后缀，因为 caller 视角就是一个图扩展动词；`_keywords` 是 Python 内部命名空间区分（related 文档模式 vs related 关键词模式），不该泄漏到 caller 契约。

### 4.5 `grga_suggest`

**对应**：直接调 `grga.suggest` 模块（MVP 阶段先看该模块的实际函数名；`SKILL.md` §… 反模式未明确前，先在实施时由 code agent 决定调哪个具体函数）。语义是 caller 提供部分查询词，grga 返回补全建议（基于已知 keyword 列表的前缀匹配 / 频次排序）。

**参数**（待 code agent 实施时根据真实 API 校准）：

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `partial_query` | string | ✅ | - | 不完整的查询词 |
| `scope` | string | ❌ | None | 逻辑 scope 名过滤 |
| `limit` | integer | ❌ | 10 | 返回建议数 |

**返回**：以 `grga.suggest` 实际返回为准（透传不做字段重命名）。

> **诚实标注**：suggest 函数的返回结构这次没在探查里完全确认。code agent 实施时直接读 `src/grga/suggest.py` 源码，按真实签名 / 字段透传。

### 4.6 `grga_scope_list`

**对应**：`grga.graph.scope.list_scopes()`

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `db_path` | string | ❌ | SQLite 路径（默认共享 DB） |

**返回**：直接透传 `list_scopes()` 返回的列表（每个元素调 `.to_dict()`），结构为：

```json
[
  {
    "name": "fanren",
    "tags": ["novel", "chinese"],
    "directories": [
      {"path": "/path/to/fanren", "role": "primary", "added_at": "2026-07-..."}
    ],
    "n_pairs": 156789,
    "n_keywords": 8432,
    "n_docs": 12,
    "n_chunks": 12345,
    "indexed_at": "2026-07-..."
  }
]
```

> 字段以 `grga.graph.scope.Scope.to_dict()` 实际输出为准。

## 5. CLI 入口

在 `cli.py` 里新增 `mcp_app`，挂到根 app 下：

```python
mcp_app = typer.Typer(no_args_is_help=True, help="Start an MCP (Model Context Protocol) server.")
app.add_typer(mcp_app, name="mcp")

@mcp_app.command("start")
def mcp_start_command():
    """Start the MCP server in stdio mode."""
    from grga.mcp.server import run_server
    run_server()
```

用户调用方式：
```bash
grga mcp start
```

为什么用 `grga mcp start` 而不是直接 `grga mcp`？因为 `mcp` 是一个子命令族，未来可能加 `grga mcp list-tools` / `grga mcp doctor` 等诊断命令。保持和 `index` / `scope` / `tools` 一致的命名风格。

## 6. 公共设计

### 6.1 参数语义与 CLI 保持一致

**字段命名 / 含义**严格对齐 grga 内部函数签名（`probe_directory` / `search_directory` / `expand` / `related_keywords` / `list_scopes`）和其返回的 `to_dict()` / `json.loads()` 结构。MCP 层**不做字段重命名、不做语义转换**。

**目录 vs scope**：CLI `grga probe` / `grga search` 用 `--directory` 而非 `--scope`（scope 只在 index / related 这一侧生效）。所以 MCP 这边的 5 个检索工具**只接受 `directory` 参数**，不引入 scope → directory 的桥接（caller 自己决定要查哪个物理目录，或先 `grga_scope_list` 拿 scope → 找 directory）。

### 6.2 错误处理

**统一一种错误码，由 code agent 决定具体暴露方式**。约束：

- 任何 grga 内部函数抛出的异常 → 包成 MCP `ToolError`（用 mcp SDK 的标准错误类）
- ToolError 的 `message` 字段放人类可读的错误描述（**不暴露 Python traceback**）
- ToolError 的额外元数据（如 `code` 字段、保留原异常类型）由 code agent 在实施时根据 mcp SDK 的能力决定，**不必遵守特定 schema**

理由：MCP 错误码的规范还在演进（不同 SDK 不同 mcp 版本差异较大），过度设计会绑死 code agent。统一要求只有一个：**异常一律 ToolError 包出，message 人类可读**。具体细节留实施时定。

### 6.3 返回值约定

- 所有工具返回 `dict` / `list`，MCP SDK 序列化成 JSON
- **字段透传**：直接用 grga 内部函数的返回（`.to_dict()` 或 `json.loads()`），不加 wrapper
- 命名 / 类型 / 嵌套结构以 grga 源码为准
- `probe` 的 `per_file=False` 时不补空数组（与 CLI 行为一致）
- `expand` 的 `level="document"` 自带 1MB 硬上限（`expand._MAX_DOCUMENT_BYTES`），MCP 层不重复保护
- `truncated=True` 透传给 caller，由 caller 决定是否重新构造更小窗口

### 6.4 可选依赖

`pyproject.toml` 里加一个 `[mcp]` extra：

```toml
[project.optional-dependencies]
mcp = ["mcp>=1.0"]
```

用户安装：
```bash
pip install "grga[mcp]"
```

基础包不受影响，`grga` 没装 `mcp` 时 `grga mcp start` 给出友好错误提示（"请先 `pip install grga[mcp]`"）。

## 7. 配置与部署

### 7.1 Claude Desktop 配置示例

`~/.config/claude.json`：
```json
{
  "mcpServers": {
    "grga": {
      "command": "grga",
      "args": ["mcp", "start"]
    }
  }
}
```

### 7.2 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GRGA_MCP_LOG_LEVEL` | `WARNING` | MCP server 日志级别（DEBUG/INFO/WARNING/ERROR） |
| `GRGA_DB_PATH` | （默认共享 DB） | 覆盖默认的共享图索引 DB 路径 |

不做配置文件 —— 够用就行。

## 8. 测试

### 8.1 单元测试

- 每个 tool 至少 2 个用例：正常返回 + 错误路径
- 用 mock grga 内部函数，不依赖真实语料
- 测试位置：`tests/unit/mcp/`

### 8.2 集成测试（轻量）

- 用测试语料（已有 `tests/` 里的 fixtures）跑通 6 个 tool
- 验证返回值结构符合 schema
- 测试位置：`tests/integration/mcp/`

### 8.3 手动验收

- 在 Claude Desktop 里配置 grga MCP
- 跑一遍「苍坤上人」场景，验证 5 个动词 + scope list 全正常工作
- 验证错误场景（scope 不存在、参数冲突）有清晰的错误返回

## 9. 实施顺序

| 步骤 | 内容 | 预估工作量 |
|---|---|---|
| 1 | 搭 `grga/mcp/` 模块骨架 + `grga mcp start` CLI 入口 | 30 min |
| 2 | utils.py（参数校验 + 错误包装） | 30 min |
| 3 | 逐个实现 6 个 tool（每个 30-60 min，从最简单的 probe 开始） | 4 h |
| 4 | 单元测试 | 2 h |
| 5 | 集成测试 + 手动验收 | 1 h |
| 6 | pyproject.toml 加 [mcp] extra + README 更新 | 30 min |

**总计**：约 1 天可完成 MVP。

## 10. 验收标准（停止门）

- [ ] `grga mcp start` 能正常启动，通过 MCP stdio 协议握手成功
- [ ] 6 个工具（probe / search / expand / related / suggest / scope_list）在 MCP 客户端可见
- [ ] 每个工具至少 1 个正常用例 + 1 个错误用例的单元测试通过
- [ ] 在 Claude Desktop 中手动跑通「苍坤上人」完整检索流程
- [ ] scope/directory 互斥校验正确，错误消息清晰
- [ ] 基础包（不带 `[mcp]` extra）安装不受影响，`grga mcp start` 给出友好提示
- [ ] 现有所有测试（pytest）全绿，无回归

## 11. 后续可选方向（MVP 之外，不承诺）

- HTTP / SSE 传输模式（给远程客户端、云 agent 用）
- 调用统计 + 审计日志
- 权限控制（哪些 scope 可被哪些 MCP 客户端访问）
- 多 scope 联合检索
- `grga mcp doctor` 诊断命令（检查 scope、索引、依赖）

## 12. 决策留痕（与 plan §1.1 互补，记录"为什么是这个形态"）
