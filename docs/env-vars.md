# 环境变量参考

grga 支持通过环境变量调整 LLM 凭据、工具缓存位置、离线策略、图查询阈值以
及 GUI 缩放等行为。本文列出所有被代码显式识别的变量、默认值以及生效路径。

## `.env` 文件与加载顺序

grga 启动时会自动加载 `$XDG_CONFIG_HOME/grga/.env`（未设置 `XDG_CONFIG_HOME`
时退化为 `~/.config/grga/.env`，Windows 上路径规则一致）。加载规则：

1. 真实 shell 环境中已经存在的变量**不会被覆盖**——命令行/CI 环境永远优先。
2. `.env` 只作"填空"，把缺失的变量补进 `os.environ`。
3. 第一次运行 grga 时若目录下没有 `.env`，会写入一份**全部注释掉**的模板，
   用户按需取消注释即可。POSIX 上文件权限为 `0600`（防止 API key 泄露到
   同机其他用户）。
4. 解析器只认 `KEY=VALUE`，支持 `#` 注释与首尾引号；不做 shell 展开，也不
   识别 `export`（`export FOO=bar` 会被作为普通赋值处理）。

想临时禁用 `.env`，可以把它重命名或删除，grga 下次启动会重新写一份模板。

## 快速索引

| 分类 | 变量 |
|---|---|
| [LLM 凭据](#llm-凭据) | `GRGA_OPENAI_API_KEY` / `GRGA_OPENAI_MODEL` / `GRGA_OPENAI_BASE_URL` / `OPENAI_*` 回退 |
| [托管工具链](#托管工具链) | `GRGA_TOOLS_DIR` / `GRGA_STATE_DIR` / `GRGA_PREFER_SYSTEM` / `GRGA_OFFLINE` / `GRGA_INSECURE` |
| [XDG 基础目录](#xdg-基础目录) | `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_STATE_HOME` / `LOCALAPPDATA`（Windows） |
| [结构缓存](#结构缓存) | `GRGA_SIDECAR_ROOT` |
| [图查询与索引](#图查询与索引) | `GRGA_PPR_MIN_NPMI` / `GRGA_QUIET_MIGRATION` |
| [桌面 GUI](#桌面-gui) | `GRGA_DISPLAY_SCALE` |

布尔类变量（`GRGA_PREFER_SYSTEM` / `GRGA_OFFLINE` / `GRGA_INSECURE` /
`GRGA_QUIET_MIGRATION` 等）识别的真值：`1`、`true`、`yes`、`on`（大小写
不敏感），其它视为假。

---

## LLM 凭据

被 `grga deep-search` 与 `grga suggest` 使用。`GRGA_OPENAI_*` 优先于
`OPENAI_*` 回退变量。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GRGA_OPENAI_API_KEY` | *(空)* | OpenAI 兼容 API 的 key；也可用 `--llm-api-key` 覆盖 |
| `GRGA_OPENAI_MODEL` | *(空)* | 模型名，例如 `gpt-4o-mini`；也可用 `--llm-model` 覆盖 |
| `GRGA_OPENAI_BASE_URL` | 官方 endpoint | 自建/代理网关的 API base；也可用 `--llm-base-url` 覆盖 |
| `OPENAI_API_KEY` | *(空)* | 回退，`GRGA_OPENAI_API_KEY` 缺失时启用 |
| `OPENAI_MODEL` | *(空)* | 回退 |
| `OPENAI_BASE_URL` | *(空)* | 回退 |

未提供 key 或 model 时，`deep-search` / `suggest` 会以 `RuntimeError`
终止并提示补齐参数。

## 托管工具链

控制 `rg` / `rga` / `pandoc` / `pdftotext` / `fzf` 等二进制的缓存位置、
下载策略与解析顺序。完整解析顺序另见
[tool-resolution.md](tool-resolution.md)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GRGA_TOOLS_DIR` | *(未设置)* | 工具缓存根目录的**显式覆盖**。设置后忽略所有 XDG / 平台默认 |
| `GRGA_STATE_DIR` | *(未设置)* | 状态目录（迁移标记、sentinel）的显式覆盖 |
| `GRGA_PREFER_SYSTEM` | `0` | 置 `1` 反转解析顺序：优先系统 `PATH` → 再看托管缓存。等价于 `--prefer-system` |
| `GRGA_OFFLINE` | `0` | 置 `1` 禁止任何网络访问；缺工具时以退出码 5 立即失败。等价于 `--offline` |
| `GRGA_INSECURE` | `0` | 置 `1` 跳过下载的 sha256 校验。**仅用于调试损坏镜像**，生产环境切勿启用 |

**默认工具根目录解析顺序**（未设 `GRGA_TOOLS_DIR` 时）：

1. `$XDG_DATA_HOME/grga/tools/<platform>`
2. Linux/macOS：`~/.local/share/grga/tools/<platform>`
3. Windows：`%LOCALAPPDATA%\grga\tools\<platform>`

**状态目录解析顺序**（未设 `GRGA_STATE_DIR` 时）：

1. `$XDG_STATE_HOME/grga`
2. `$XDG_DATA_HOME/grga`（与工具缓存共根）
3. 平台默认

## XDG 基础目录

grga 遵守 XDG Base Directory 规范。这些不是 grga 专属变量，但会影响
grga 的缓存与配置位置。

| 变量 | 影响 |
|---|---|
| `XDG_CONFIG_HOME` | 决定 `.env` / `settings.json` 的位置（回退 `~/.config`） |
| `XDG_DATA_HOME` | 决定托管工具缓存根（回退 `~/.local/share`） |
| `XDG_STATE_HOME` | 决定状态目录根（回退 `~/.local/state`） |
| `LOCALAPPDATA` | Windows 上作为 XDG 的替代（`%LOCALAPPDATA%\grga\...`） |

## 结构缓存

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GRGA_SIDECAR_ROOT` | 搜索目录下的 `.grga/structure/` | Markdown 章节树 sidecar 缓存的根目录。`grga expand --level chapter` 与 `heading_path` 元数据会读写此目录 |

## 图查询与索引

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GRGA_PPR_MIN_NPMI` | `0.2` | `related --mode graph` 读侧的 pair NPMI 下限。**只能收紧**写入期 v8 门槛（0.2），设置更低值会被忽略。命令行 `--min-npmi` 优先 |
| `GRGA_BUILD_NPMI_BATCH` | `50000` | `index build` pair npmi 重算的 fallback 分批游标批大小（仅当 SQLite 无 `ln()` 时走此路径）。控制该分支的峰值 Python 内存；有 `ln()` 的环境走纯 SQL、此值不生效。见 `docs/design/plan-2026-07-15-build-memory-oom.md` §4 |
| `GRGA_QUIET_MIGRATION` | `0` | 置 `1` 静音 schema 迁移日志，主要用于 CI / 测试降噪 |

## 桌面 GUI

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GRGA_DISPLAY_SCALE` | 自动检测 | 手动覆盖 HiDPI 缩放比例，范围 1.0 – 2.5。也可在 GUI 的 settings 中配置 |

---

## 相关文档

- 工具解析顺序：[tool-resolution.md](tool-resolution.md)
- 退出码与 `--json` 错误契约：[exit-codes.md](exit-codes.md)
- CLI 参数总览：[CLI.md](CLI.md)
- `.env` 模板的权威来源：源码 `src/grga/env_loader.py` 中的
  `EXAMPLE_ENV_TEMPLATE`——本文与该模板保持同步。
