# grga 项目记忆

> 本文件是 grga 项目的跨会话结论沉淀，供 code agent 快速对齐"这个项目曾经
> 踩过什么坑、定过什么规则"。与 `docs/design/decisions.md`（面向外部读者的
> 契约/架构决策）互补：这里记录**执行层面的结论**与证据链入口。

## 3 · caller-first 铁律与检索热路径

- 热路径动词（`search` / `probe` / `expand`）的 `--json` 契约尽量冻结，
  任何新信号优先落在低频动词（`related` / `suggest`）。
- 公共基础设施的杠杆方向：往 grga 共用层加负担、给 N 个 caller 减负是
  反杠杆；判断复用前先找跨仓库的重复证据。
- 详见 `docs/design/decisions.md` 2026-07-09 信号暴露原则条目。

## 2026-09-08 · rga 缓存上限 50M + timeout 调大 + 默认排除

**现象**：`grga search/probe` 在 `/data/ports/sync`（11GB / 23,437 文件）全量
79.5-96.8s，probe 默认 60s 必超时。

**根因（分层定位）**：

- grga Python 层非瓶颈（解析 29,365 行 JSONL 仅 205ms）。
- 瓶颈在 rga 扫描：两个大 pdf（17.9MB / 27.8MB）提取文本压缩后 4.86-6MB
  **超过 rga 缓存默认上限 2MB** → 永不进缓存、每次重提取 20-30s。
- 865 个 xlsx/doc/pptx/xls：rga 0.10.10 pandoc adapter 白名单不含这些格式
  （上游至今未支持，非工具链版本问题），每次 preproc 失败重试。
- 压缩包（52 rar + 30 zip + 6 xz/gz）每次递归解压。

**修复（三件独立，additive）**：

1. `--rga-cache-max-blob-len=52428800`（50M）默认下发所有 rga 命令 → 大 pdf
   首次提取后进缓存，二次 ~300ms（实测快 67 倍）。
2. probe 默认 timeout 60s → 180s（CLI + MCP + 后端三处）。
3. 默认排除压缩包 + 无文本二进制（`DEFAULT_EXCLUDE_GLOBS`，45 项：压缩包 +
   可执行/库/字节码 + 安装包/镜像 + 音视频），`--no-default-exclude` 关闭；
   **排除列表绝不含文档类，也不含图片格式**（图片语料靠 `--path-only` 按
   文件名检索，保留零开销）。

**证据链**：`docs/design/plan-2026-09-08-rga-cache-timeout-excludes.md`（决策
与验收标准）、`docs/design/decisions.md`（2026-09-08 条目）。
