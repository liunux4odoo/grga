# plan-2026-09-08 · rga 缓存上限 50M + timeout 调大 + 默认排除压缩包/无文本二进制

> 状态：**已实施**（2026-09-08，code agent 落地，含单测与文档同步）。
> 触发：`grga search/probe` 在 `/data/ports/sync`（11GB / 23,437 文件）上慢至 73-97s、
> probe 默认 60s 必超时的根因调查完结后，用户拍板：「把默认缓存上限调到 50M，并将
> rga 默认 timeout 适当调大，连同默认排除方案，一起写个开发计划」。
> **结论先行**：三件事都是**小而独立**的改动，各配独立开关，additive 原则，不破坏现有调用。

## 0 · 检查过程（今天的诊断，附完整证据链）

> 完整过程与原始数据见 `memory/2026-09-08/grga-check-probe-search-fixes.md`。此处为方案所需的证据摘要。

### 0.1 现象

- 用户实际操作目录 `/data/ports/sync` = **23,437 文件 / 11GB**，含 972 docx + 905 pdf +
  383 xlsx + 205 pptx + 250 doc ≈ 2,700+ 二进制文档。
- `grga probe "小堆"` 在该目录**必超时**（默认 timeout 60s < 实际 73.7s）。
- `grga search` 全量 **96.8s**（rga 79.5s + Python 解析 ~17s）。

### 0.2 分层定位

| 层 | 结论 | 证据 |
|---|---|---|
| grga Python 层 | **非瓶颈** | 解析 29,365 行 JSONL 只要 205ms；`_stream_rga_search` 2.7s；`search_directory` 直调 2.4s |
| rga 扫描 | **瓶颈本体** | 裸 `rga --json` 全量 79.5s；`--count-matches` 73.7s；排除全部二进制文档后 **3.35s**（快 24 倍） |
| probe 超时 | **预期行为** | probe 默认 60s < 全量 73.7s → 必超时 |

### 0.3 73s 的真实构成（缓存机制深挖）

- **缓存有效**：preproc_cache mtime 匹配率 100%（907/907）；单文件缓存命中 68ms、0 个
  pdftotext 子进程；flag 组合 / 相对 vs 绝对路径不影响命中。
- **973 个文件永不缓存，每次重新 preproc**：
  - **865 个 xlsx/doc/pptx/xls**：rga 0.10.10 pandoc adapter 白名单只有
    `.epub/.odt/.docx/.fb2/.ipynb/.html/.htm`，**不含 xlsx/pptx/doc/xls** → rga-preproc
    报 `No adapter found ... passthrough disabled` → 永远失败、永远不缓存、每次重试。
    （上游至今未支持：2024-09 有人提 PR 未合并；非工具链版本问题 —— grga 自带 pandoc，
    docx 能正常处理。）
  - **2 个大 pdf**：`学霸的黑科技系统.pdf`（17.9MB）提取文本压缩后 **4.86MB**、
    `这游戏也太真实了.pdf`（27.8MB）~5-6MB，**超过 rga 缓存默认上限 2MB**
    （`--rga-cache-max-blob-len` 默认值）→ **永不缓存，每次重新提取 20s / 30s+**。
  - **5 个伪 pdf / 损坏 pdf / 空文件**（HTML×2 / data×1 / empty×1 / 加密×1）：提取失败
    不缓存，但单次仅 ~50ms，非大头。
  - **压缩包**：52 rar + 30 zip + 6 xz/gz，每次递归解压。
- **rg `--pre` 串行**：监控到单个 `rg --pre rga-preproc` 进程卡 10s+，慢文件拖住后面全部。

### 0.4 决定性实验（本次方案的依据）

| 实验 | 耗时 | 说明 |
|---|---|---|
| 全量（基线） | 79.5s | rga 默认 |
| 排除重灾区（大 pdf + 压缩包 + 不认的 Office） | **7.8s** | 输出 29,345 vs 29,365 行，几乎不丢 |
| **调大缓存上限 `--rga-cache-max-blob-len=20M` 处理 17.9MB pdf** | 首次 20.8s → **二次 308ms** | 缓存行 909→910，blob 4.86MB；**快 67 倍** |

**结论**：调大缓存上限是**治本**（大 pdf 首次提取后进缓存，后续命中 ~300ms）；
默认排除是**锦上添花**（省每次扫描的压缩包/无文本二进制开销，约数秒）。

---

## 1 · 目标（WHAT）

三件事，各自独立：

1. **rga 缓存 blob 上限默认调到 50MB**：grga 构建 rga 命令时显式传
   `--rga-cache-max-blob-len=52428800`，覆盖 rga 默认 2MB。
2. **rga 默认 timeout 适当调大**：`grga probe` 默认 60s → **180s**；`grga search`
   默认 600s 保持（已够大），仅确认内部 `_stream_rga_search` / `_run_checked`
   的 timeout 传递链路一致。
3. **默认排除**：search / probe 默认追加排除**压缩包 + 无文本价值的二进制**，
   提供 `--no-default-exclude` 关闭。

## 2 · 背景与理由

| # | 问题 | 为什么改 |
|---|---|---|
| 1 | 大 pdf（17.9/27.8MB）因提取文本 >2MB 永不进缓存，每次搜索重提取 20-30s | 50MB 上限让大文档进缓存，首次提取后后续 ~300ms；50MB 覆盖当前语料最大 blob（4.86MB）且留余量 |
| 2 | probe 默认 60s < 11GB 目录全量 73.7s → 必超时 | 默认值贴近真实语料规模，减少"假死"误报 |
| 3 | 压缩包每次递归解压 + exe/AppImage/deb 等 rga 本来就不处理却仍被扫描 | 排除无副作用（rga 白名单外），省时且减噪 |

**关键决策留痕（为什么这样排除）**：
- **排除列表只含压缩包 + 无文本二进制，绝不含 docx/pdf/pptx/xlsx**。用户搜"小堆"就是要在
  工作文档里找 —— 实测排除全部二进制丢 88% 匹配（29,345 行中仅 3,986 行来自纯文本）。
- 压缩包排除实测只省 ~5s（79.5→74.7s），**不是大头**，但因"无副作用 + 减噪"仍值得默认。
  真正的大头（大 pdf）由缓存上限解决。

## 3 · 硬约束（Constraints）

- **Additive 原则**：所有改动默认开启但**可显式关闭**；不破坏任何现有 invocation。
- 排除列表**不允许**包含 docx/pdf/pptx/xlsx/doc/xls（工作文档，搜索主目标）。
- `--rga-cache-max-blob-len=50M` 只影响**新建缓存**；已有缓存不失效、不清除。
- 缓存膨胀评估：50MB 上限 × 大文件数，理论最坏 = 每个大二进制 50MB。当前语料超 2MB 的
  提取仅 2 个大 pdf，实际膨胀 ~10MB 量级，可接受。
- py_compile 通过；现有 141 项测试零回归；新增针对本次改动的单测。
- 提交信息参考历史风格；**不 push，仅本地提交**。

## 4 · 实施清单（WHAT to change）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src/grga/search.py` `_shared_rga_options` | 追加 `--rga-cache-max-blob-len 52428800`（所有 rga 命令共享，search/probe 自动生效） |
| 2 | `src/grga/search.py` `_shared_rga_options` | 新增默认排除 glob 列表常量，追加 `--iglob '!*.zip' '!*.rar' ...` |
| 3 | `src/grga/probe.py` `_count_per_file` / `_probe_content` | 复用 `_shared_rga_options`（若已共享则 #1/#2 自动生效，仅确认） |
| 4 | `src/grga/cli.py` probe `--timeout` 默认 | `60.0` → `180.0` |
| 5 | `src/grga/cli.py` search/probe | 新增 `--no-default-exclude` flag，透传到 `glob_patterns` 合并逻辑 |
| 6 | `docs/CLI.md` | 更新 `--timeout` 默认值说明 + `--no-default-exclude` + 默认排除列表文档 |
| 7 | `docs/install-and-usage.md` | 如有 `-t` / glob 相关说明则同步 |
| 8 | `CHANGELOG.md` | `[Unreleased]` 下加新条目 |
| 9 | `tests/unit/` | 新增：命令构建含 `--rga-cache-max-blob-len`；`--no-default-exclude` 去掉排除 glob |
| — | `docs/design/decisions.md` | **追加**本次决策留痕（为什么排除列表不含文档类 / 为什么 50MB） |
| — | `memory/projects/grga.md` | 方案落地后同步本次性能修复结论 |

### HOW 留给 code agent（仅给落点，不给伪代码）

- 默认排除 glob 列表建议独立常量（如 `DEFAULT_EXCLUDE_GLOBS`），集中一处，便于审计。
- `--no-default-exclude` 语义：置空默认列表，但用户显式 `-g` 仍生效。
- 默认排除与用户 `-g` 的关系：**追加合并**（用户排除 + 默认排除都生效），除非
  `--no-default-exclude`。
- 50MB 常量建议也提为模块级常量（如 `RGA_CACHE_MAX_BLOB_LEN = 52428800`）。

## 5 · 验收标准（Acceptance）

1. `grga search 小堆 -d /data/ports/sync` 第二次运行：
   - 大 pdf 进缓存 → 总耗时较首次显著下降（首次含提取，后续命中缓存）。
   - 可用 `~/.cache/ripgrep-all/cache.sqlite3` 验证 `学霸的黑科技系统.pdf` 已入缓存（poppler 行新增）。
2. `grga probe 小堆 -d /data/ports/sync`（默认 timeout 180s）**不再报超时**，正常返回元数据。
3. `grga search 小堆 -d /data/ports/sync --no-default-exclude` 行为与改动前一致（回归对照）。
4. 默认排除后结果**仍包含 docx/pdf 匹配**（排除列表不含文档类）。
5. `py_compile` 通过；全部单测绿；新增单测覆盖 #1/#5。

## 6 · 风险与反面证据

- **缓存膨胀**：若未来语料含大量 30MB+ 二进制，50MB × 数量可能膨胀缓存。缓解：上限可配置，
  后续可降。当前语料实测安全（仅 2 个大 pdf 超 2MB）。
- **默认排除误伤**：若某用户真的想搜压缩包内文本（rga 支持 zip 内递归搜索），默认排除会漏。
  缓解：`--no-default-exclude` 显式开关；且默认排除列表不含文档类，主场景不受影响。
- **timeout 调大**：180s 会让真正挂死的调用多等 2 倍。缓解：probe 是只读探测，且有
  `start_new_session + killpg`（commit fc766a1）保证超时干净退出，不留孤儿进程。

## 7 · 决策留痕（本轮否决的分支）

- ❌ **排除全部二进制**（含 docx/pdf）：丢 88% 匹配，违反搜索主目标。否决。
- ❌ **升级 rga 解决 xlsx/pptx**：上游 2024 至今未支持，不可控。可后续观察，不作为本次依赖。
- ❌ **加 depth 参数**：治标不治本（重灾区文件在浅层），且引入新语义。暂缓。
- ✅ **调大缓存上限**：一行参数，治大 pdf 大头，收益已验证（67 倍）。
- ✅ **默认排除压缩包+无文本二进制**：无副作用减噪，配开关可逆。
