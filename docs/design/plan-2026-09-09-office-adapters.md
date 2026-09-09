# Plan: 方案 D — Office 文档自定义 adapter（xlsx/pptx/doc/xls/ppt）

日期：2026-09-09（v2：按用户新要求改实现方式）
状态：**已实施**（2026-09-09）
前置：`287cbfb`（缓存上限 50M + 默认排除）已完成

## 背景

grga 用 rga 0.10.10 检索，但 rga 内置 pandoc adapter 白名单只认
`.epub/.odt/.docx/.fb2/.ipynb/.html/.htm`（源码 custom.rs 冻结于 2019，
pandoc 2.18 才支持 xlsx/pptx reader，上游未跟进）。

后果：**xlsx/pptx/doc/xls/ppt 永远不被处理** —— 报 `No adapter found`、
永不缓存、每次搜索重试。真实语料里 **漏搜 84+ 个 Office 文件**。

rga 提供 `custom_adapters` 配置（`~/.config/ripgrep-all/config.jsonc`），
可自定义 adapter 把 xlsx/pptx 映射到 pandoc。方案 D = 利用该机制。

## 实测验证（2026-09-09，全部通过）

| 验证点 | 结果 |
|---|---|
| custom_adapters 机制 | ✅ `--rga-list-adapters` 列出 pandoc_office |
| xlsx 搜"联盟" | ✅ 39-98ms 命中干净文本（非 XML 噪声） |
| pptx 搜"运维" | ✅ 506ms 命中干净文本 |
| docx 回归 | ✅ 正常 |
| 写缓存 | ✅ pandoc_office 独立缓存条目，二次命中 |
| config 路径 | ✅ XDG (`$XDG_CONFIG_HOME/ripgrep-all/config.jsonc` 优先，否则 `~/.config/...`)；缺失时 rga 用全默认不报错 |
| JSONC 合并 | ✅ 自写剥离器 7 测试通过（含字符串内注释/URL/转义引号），真实 config 解析成功 |
| `--rga-adapters=-pandoc_office` | ✅ 按次排除自定义 adapter（xlsx 0 命中），内置 pandoc 的 docx 不受影响 |

**关键决策证据**：
- `--rga-config-file` 是完全替换语义（实验E：全局 config 的 canary_adapter 被吞）。
  用户否决"每次命令行传参"，改走**写全局 config**。
- `--rga-adapters=-pandoc_office` 能按次排除（实验J 权威：真实搜索 0 命中；
  实验I list-adapters 显示层不反映激活状态，是假象）。→ 作为开关实现。

## 设计（v2）

### 1. toolchain.py：新增 `ensure_rga_office_config()`

- 计算 rga config 路径（平台约定）：
  - Linux: `$XDG_CONFIG_HOME/ripgrep-all/config.jsonc` 或 `~/.config/ripgrep-all/config.jsonc`
  - macOS: `~/Library/Application Support/ripgrep-all/config.jsonc`
  - Windows: `%APPDATA%\ripgrep-all\config.jsonc`
- 幂等合并：
  - 文件不存在 → 写新 config（含 pandoc_office custom_adapter）
  - 存在 → 剥离 JSONC 注释（自写 `_strip_jsonc_comments`）→ json 解析 → 检查
    `custom_adapters` 是否已有 `pandoc_office` → 无则追加 → 写回
    （**保留其他配置项**，如 accurate / cache 设置）
- 写回时：只写必要键（`$schema` 省略？保留）—— 用紧凑 JSON，加头注释说明来源。
- 失败策略：解析失败/不可写 → **静默跳过并告警**（不破坏用户配置，搜索照常，
  只是没有 Office adapter）。

### 2. toolchain.py：`_strip_jsonc_comments(text)`

轻量 JSONC 剥离器，处理 `//` 行注释、`/* */` 块注释，**尊重字符串**（不剥
字符串内注释/URL/转义引号）。已 7 测试通过。

### 3. cli.py：`_guard_tools` 里调用

`ensure_tools` 成功且 `"rga" in required` 时调用 `ensure_rga_office_config()`。
这是所有 rga 搜索命令的统一入口（3 处 `_guard_tools(["rga"], ...)`）。
同时 `tools status / install / update` 命令体也调用（用户："检查或下载工具时"）。

### 4. search.py：`_shared_rga_options` 加 `office_adapters` 参数

- `office_adapters: bool = True`（默认开）
- `False` 时：`options.append("--rga-adapters=-pandoc_office")`（按次排除）
- 穿透到 `search_directory` / `_count_total_matches` / probe

### 5. cli.py：`--no-office-adapters` 开关

- `search_command` / `probe_command` / `preview` 加
  `no_office_adapters: bool = typer.Option(False, "--no-office-adapters", help=...)`
- 透传给 search_directory / probe

### 6. 默认值：**默认开**（配 `--no-office-adapters` 关闭）

用户拍板："默认开，office 文档是刚需"。

## 决策留痕

- **实现方式**：写全局 config（用户要求），不用 asset + `--rga-config-file`。
  副作用：grga 会 touch 用户的 rga 全局 config —— 但只追加 custom_adapters，
  保留其他配置；失败静默跳过不破坏。
- **默认开 vs 默认关**：用户拍板默认开。漏搜是缺陷优先于速度。
- **开关实现**：`--rga-adapters=-pandoc_office` 按次排除（默认路径零传参，
  只有显式关闭才传一行参数，符合"避免每次命令行传参"）。
- **JSONC 处理**：自写剥离器，不引 json5 依赖（grga 轻依赖原则）。
- **写入幂等**：已有 pandoc_office 则跳过，不重复追加。
- **不影响 DEFAULT_EXCLUDE_GLOBS**：Office 文件不在排除列表，互不干扰。

## 验收

1. `grga search 联盟 -d <含xlsx目录>` 能搜到 xlsx 内容（之前漏搜）
2. rga 全局 config 被写入 pandoc_office（`--rga-list-adapters` 可见）
3. `grga search --no-office-adapters` 恢复旧行为（xlsx 不可见，docx 仍可搜）
4. 已有自定义配置的用户 config 合并后保留其他键
5. 45 tests 全绿
6. 二次搜索命中缓存（Office 文件）

## 后续补充（2026-09-09 二次提交）

- **graph 索引接入**：`file_read.py` 的 `RGA_SUPPORTED_SUFFIXES` 与
  `RGA_BINARY_ONLY_SUFFIXES` 加入 `.xlsx/.pptx/.doc/.xls/.ppt`，使
  `grga index build` 能枚举并提取 Office 文件（详见 decisions.md）。此前被
  `bad_suffix` 跳过的 Office 文件无 `docs` 行，新 build 以 `DOC_ADDED` 处理；
  已索引文件仍按 size/mtime 增量跳过。
- **fingerprint 修复**：`_compute_toolchain_fingerprint` 先 `add_tools_to_path()`
  再解析版本，消除 no-op 增量构建误报 "toolchain fingerprint changed"。
