# grga 公开发布 · 二进制解耦执行计划

> **来源**：`ai-developer` 通过 `chat_with_agent` 委托 `default` agent（ark-code-latest）产出。
> **Session**：`ai-developer:to:default:1783586918549:e77a3da6`
> **日期**：2026-07-09
> **目标读者**：拿到该计划的 code agent 直接执行
> **不要**：把执行任务转交给其他 agent；每一步都由拿到本方案的 agent 亲手执行 + 亲手验证。

---

## 上下文快照（code agent 必读）

- 项目根：`/media/lqw/DATA/myrepo/grga`
- 当前包体积问题：`src/grga/data/tools/linux-aarch64/` 有 **187 MB** binary（pandoc 独占 166 MB），且只覆盖一个平台
- 已有基础设施（**直接复用，不要重写**）：
  - `src/grga/toolchain.py`：`find_tool()` 已是 managed → system fallback；`install_tools()` / `update_tools()` / `check_tools()` / `add_tools_to_path()` 齐全；`platform_key()` 覆盖 5 个平台
  - `src/grga/cli.py`：`tools` / `index` / `graph` typer 子命令组已注册
  - `scripts/download-tools.py`：打包时预下载脚本
  - `tools.json` manifest：已有 platform / repo / version / asset
- 已敲定不再讨论：
  - License：**MIT**
  - 缓存路径优先级：`$GRGA_TOOLS_DIR` > `$XDG_DATA_HOME/grga/tools/<platform>` > `~/.local/share/grga/tools/<platform>`（Linux/macOS）/ `%LOCALAPPDATA%\grga\tools\<platform>`（Windows）
  - 不发 platform wheel，只发 `py3-none-any`
  - AGPL/GPL 上游 binary 只在运行时下载，不打包分发
  - Skill 分发：加 `grga skill install --to <dir>` 子命令

---

## 阶段总览

| 阶段 | 目标 | Definition of Done | 可并行 |
|---|---|---|---|
| P1 | 打包瘦身 + 元数据合规 | `python -m build` 产出单个 `py3-none-any` wheel，体积 < 1 MB；`pyproject.toml` 含 license / readme / urls；`LICENSE` 存在 | 与 P2 前半可并行 |
| P2 | 缓存路径迁移到 XDG + 环境变量 | `toolchain.tools_root()` 返回 XDG 路径；所有读写点走新解析器；老路径能被识别（迁移或忽略） | 依赖 P1 的 `TOOLS_ROOT` 常量拆分 |
| P3 | 首次运行 UX + Agent 友好非阻塞 | `grga search` 缺工具时：交互 tty 提示 / `--json` 结构化错误 / `--offline` 立即失败；退出码稳定 | 依赖 P2 |
| P4 | 安全 (sha256) + 离线 install | `tools.json` 支持 `sha256`；`grga tools install --from <dir>` 可用；`--no-verify` 可跳过 | 与 P3 可并行 |
| P5 | Skill 分发 + 发布流水线 | `grga skill install --to <dir>` 可用；`scripts/download-tools.py` 打包前置钩子解耦；CI 构建 + TestPyPI 冒烟通过；PyPI 名称可用性确认；`0.2.0` tag | 串行收尾 |

关键里程碑：**M1**（P1 完）wheel 可上传 TestPyPI；**M2**（P3 完）Agent 可用；**M3**（P5 完）正式 PyPI 发布。

---

## P1. 打包瘦身与元数据合规

### Step 1.1 从 wheel 中剔除 binary，切换到运行时下载模型
**文件**：`pyproject.toml`, `MANIFEST.in`（新增或核对）, `.gitignore`
**动作**：`[tool.setuptools.package-data]` 只保留 `prompts/*.txt`（以及必要的 `data/tools/README.txt`、`skills/**` 静态资源），删除 `data/tools/**/*` 通配符。同时把 `src/grga/data/tools/<platform>/` 加入 `.gitignore`，防止误提交。
**验证**：
- `rm -rf dist build *.egg-info && python -m build --wheel`
- `unzip -l dist/grga-*.whl | awk '{s+=$1} END{print s}'` → 预期总字节 < 1 MB
- `unzip -l dist/grga-*.whl | grep -E '(rg|rga|pandoc|fzf)$'` → 预期无匹配

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| 空 `data/tools/`（干净 checkout） | 构建成功，wheel 内无 tools 目录 |
| 本地遗留旧 `data/tools/linux-aarch64/` binary | 构建不打入 wheel（package-data 已收紧）；本地开发不受影响 |
| `prompts/*.txt` 缺失 | 构建应 fail-loud，不静默产出残缺 wheel |
| sdist 场景 | `MANIFEST.in` 显式排除 `src/grga/data/tools/*/` 二进制，仅保留 README |

**风险**：破坏性 —— 旧用户 `pip install --upgrade grga` 后 site-packages 里不再有 binary，首次调用会需要触发下载路径（P3 覆盖）。
**回滚**：`git revert`；wheel 未上传前无外部影响。

---

### Step 1.2 补齐 project metadata + LICENSE
**文件**：`pyproject.toml`, `LICENSE`（新增，MIT 全文，署 `grga maintainers`）, `README.md`
**动作**：
- 新增 `readme = "README.md"`、`license = {text = "MIT"}` 或 `license-files = ["LICENSE"]`（按 setuptools 版本）。
- 新增 `[project.urls]`：`Homepage / Repository / Documentation / Changelog / Issues`。
- 补 classifier：`License :: OSI Approved :: MIT License`、`Operating System :: OS Independent`、`Development Status`（升到 4 - Beta）。
- README 顶部加"外部工具运行时下载 + AGPL/GPL 声明"段落。
- 版本从 `0.1.0` → `0.2.0`（本次为 breaking：wheel 布局变化）。

**验证**：
- `python -m build && twine check dist/*` → 预期 `PASSED`。
- `python -c "import importlib.metadata as m; print(m.metadata('grga')['License'], m.metadata('grga')['Home-page'])"`（在 venv 里 pip install 完 wheel 之后）。

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| PyPI 已被占用（`grga` 冲突） | 发布前先 `curl -sI https://pypi.org/pypi/grga/json`；若 404 通过，若命中则改名并同步所有引用 |
| README 含相对图片链接 | `long_description_content_type = "text/markdown"`；图片改为绝对 URL 或 raw.githubusercontent |
| setuptools 版本过低不识别 `license-files` | build-system requires 提升到 `setuptools>=77`（或退化到 `license = {file="LICENSE"}`） |

**风险**：低。**回滚**：仅元数据改动，直接 revert。

---

## P2. 缓存路径迁移（site-packages → XDG）

### Step 2.1 引入 `resolve_tools_dir()` 中央解析器
**文件**：`src/grga/toolchain.py`, `src/grga/paths.py`（新增，集中路径逻辑）
**动作**：新增 `paths.resolve_tools_dir() -> Path`，实现优先级：`$GRGA_TOOLS_DIR` > `$XDG_DATA_HOME/grga/tools/<platform>` > 平台默认（Linux/Mac: `~/.local/share/grga/tools/<platform>`；Windows: `%LOCALAPPDATA%\grga\tools\<platform>`）。`toolchain.tools_root()` / `current_tools_dir()` / `manifest_path()` / `find_managed_tool()` / `managed_bin_dirs()` 全部改走该解析器，删除 `TOOLS_ROOT = PACKAGE_ROOT / "data" / "tools"` 硬编码（只保留 legacy 常量供 2.2 迁移探测用）。

**验证**：
- `GRGA_TOOLS_DIR=/tmp/x python -c "from grga.toolchain import current_tools_dir; print(current_tools_dir())"` → `/tmp/x/<platform>`（或按语义 `/tmp/x`，需在 SKILL 里定型）。
- 单测覆盖三条优先级分支 + Windows 分支（monkeypatch `sys.platform`, `os.environ`）。

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| 三个环境变量都未设置 | 走平台默认 |
| `$GRGA_TOOLS_DIR` 存在但为空字符串 | 视为未设置，回退下一级 |
| `$XDG_DATA_HOME` 被显式设置为非默认（如 `/data/xdg`） | 使用之，不再拼 `~` |
| 路径含空格 / 中文（Windows: `C:\用户\张三\AppData`） | Path 对象透传，所有 subprocess 调用参数化传入不做 shell 拼接 |
| 只读文件系统（`mkdir` 失败） | `resolve_tools_dir()` 只返回路径不创建；创建时机延后到 `install_tools`，失败给出明确错误码 |
| 自反：多次调用返回值稳定 | 幂等 |

**风险**：中。破坏"重装 grga 清空 binaries"的旧行为（本身就是目标）。**回滚**：保留 `TOOLS_ROOT` 常量作为 fallback flag，一行开关切回。

---

### Step 2.2 旧 site-packages binary 迁移策略
**文件**：`src/grga/toolchain.py`, `src/grga/cli.py`（`tools` 子命令组）
**动作**：新增 `grga tools migrate`（也在首次 `install/check` 时懒触发一次，记录 `~/.local/share/grga/.migrated` 哨兵防重复）。行为：探测老 `PACKAGE_ROOT/data/tools/<platform>/`，若存在则**复制**（不是 symlink，避免跨 fs 与卸载失效）到新 XDG 目录；复制后不删除源（老 wheel 可能仍被 pip 管理），仅打印一条提示。

**验证**：
- 手动放置一份 mock binary 到 legacy 目录 → `grga tools migrate --dry-run` 列出计划 → 实跑后 `grga tools check` 显示 managed 命中新路径。

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| 空 legacy（新用户） | no-op，静默返回 |
| 新路径已有同版本 | 跳过（按文件名 + 哨兵），不覆盖 |
| 新路径有旧版本、legacy 有新版本 | 以 `tools.json` version 比较，高者胜；相同则跳过 |
| legacy 部分损坏（rg 有 rga 缺） | 逐工具处理，报告哪些迁移哪些跳过 |
| 只读源 / 只读目标 | fail-loud，退出码 ≠ 0，`--json` 时输出 `{"error":"readonly","path":...}` |

**风险**：低。**回滚**：`grga tools migrate` 是新增命令，删除即可。

---

## P3. 首次运行 UX & Agent 友好非阻塞

### Step 3.1 定义"运行环境检测"工具函数
**文件**：`src/grga/runtime_env.py`（新增）
**动作**：新增 `is_interactive()`（`sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("CI")`）、`is_json_mode(ctx)`、`is_offline()`（读 `--offline` flag 或 `GRGA_OFFLINE=1`）。所有子命令入口在需要工具时先过这个门。

**验证**：单测 monkeypatch 各组合，断言 8 种笛卡尔积返回值。

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| stdin 非 tty + `--json` 未传 | 视为非交互，禁弹交互提示 |
| `CI=true` + tty | 视为非交互（CI 环境优先） |
| `GRGA_OFFLINE=1` + `--offline` 未传 | offline 生效 |
| 冲突：`--offline` 传了但 `--no-offline` 也传 | typer 层由互斥组处理，报错退出码 2 |

**风险**：无。**回滚**：新文件，删除即可。

---

### Step 3.2 `search / probe / expand / related / suggest` 加统一 `ensure_tools()` 前置
**文件**：`src/grga/cli.py`, `src/grga/search.py`, `src/grga/probe.py`, `src/grga/expand.py`, `src/grga/related.py`, `src/grga/suggest.py`
**动作**：新增 `toolchain.ensure_tools(required: list[str], *, interactive: bool, json_mode: bool, offline: bool) -> None`。缺工具时的分支矩阵：
- 交互 + 非 json + 非 offline → 打印缺失列表，`input("Install? [y/N]")`，y 则调 `install_tools`，n 则退出码 3。
- 非交互 或 json → 立即 `{"error":"missing_tools","missing":[...],"install_hint":"grga tools install"}` + 退出码 4。
- offline → `{"error":"offline_and_missing","missing":[...],"manual_urls":{...}}` + 退出码 5。

**验证**：
- `echo "" | grga search foo`（stdin 非 tty）→ 结构化错误，不阻塞。
- `grga search foo --json`（tty）→ 结构化错误。
- `GRGA_OFFLINE=1 grga search foo` → offline 报错含 manual_urls。

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| 空 required（子命令声明不依赖任何工具，如 `grga --version`） | 直接跳过 ensure |
| 单元素 required 且系统命中 | 直接过，不下载 |
| 多元素部分缺失 | 只对缺失项走安装流程，不重装已有 |
| 用户回答 `n` | 退出码 3，不写任何缓存；下次仍会问 |
| 用户回答 `y` 但下载中 Ctrl+C | 清理 partial 文件（`.part` 后缀原子重命名），下次可续 |
| 降级路径：managed rg 装完但 rga 装失败 | 报告"rg 已装, rga 失败"，退出码 6，不误报 success |
| 自反：连续两次 `ensure_tools` 无缺失 | 第二次 no-op，O(1) |

**风险**：中，改所有主入口。**回滚**：`ensure_tools` 默认不启用，靠 feature flag `GRGA_STRICT_TOOLS=1` 开启，稳定后翻转默认值。

---

### Step 3.3 稳定退出码 + `--json` schema 冻结
**文件**：`src/grga/cli.py`, `docs/exit-codes.md`（新增）
**动作**：常量表 `EXIT_OK=0 / USAGE=2 / USER_ABORT=3 / MISSING_TOOL=4 / OFFLINE=5 / PARTIAL_INSTALL=6 / DOWNLOAD_FAIL=7 / VERIFY_FAIL=8 / IO=9`。所有错误路径统一走 `emit_error(code, kind, **fields)`，json 模式打 stdout JSON、非 json 打 stderr 人类可读。

**验证**：`grga search --json 2>/dev/null; echo $?` → 4；stdout 是合法 JSON（`python -c "import json,sys;json.load(sys.stdin)"`）。

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| stdout 被管道 + `--json` 未传 | 仍走人类模式（不自动切 json，Agent 必须显式 opt-in） |
| json 模式下的 warning | 走 stderr JSON line，不污染 stdout |
| 多个错误叠加 | 只报首个致命错误，其余进 `details` 数组 |

**风险**：低。**回滚**：退出码为新增契约，未来只能加不能减，视为一次性冻结。

---

## P4. sha256 校验 + 离线 install

### Step 4.1 `tools.json` schema 扩展 sha256（向后兼容）
**文件**：`src/grga/toolchain.py`, `scripts/download-tools.py`
**动作**：在 asset 描述中新增可选字段 `sha256`（asset 文件级）和可选 `sha256_bin`（解包后可执行文件级，二选一或都填）。`install_tool()` 下载后按存在的字段校验；两个都缺则视为"未校验"并按 4.3 的策略处理。`scripts/download-tools.py` 打包时顺便算 sha256 写回。

**验证**：
- `python scripts/download-tools.py --platform linux-x86_64 --write-hashes` → `tools.json` 内 assets 出现 `sha256`。
- 单测：手动篡改 downloaded bytes → `install_tool` 抛 `VerifyError`，退出码 8。

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| 空 sha256（旧 manifest） | 打警告（json 模式为 warning line），继续安装；退出码 0 |
| 单文件下载（无归档） | 只校验 `sha256_bin` |
| 归档下载 | 优先 `sha256`（archive），解包后可选二次校验 `sha256_bin` |
| 冲突：两者都填但归档校验通过、bin 校验失败 | 视为 VerifyError（篡改攻击） |
| 重叠：多 asset 共享同一 sha256（不太可能，但要判等） | 允许 |
| 自反：重复安装同版本 | 命中缓存跳过下载，仍校验本地文件哈希 |

**风险**：低。**回滚**：字段可选，兼容旧 manifest。

---

### Step 4.2 `--no-verify` 与 `GRGA_INSECURE` 逃生舱
**文件**：`src/grga/cli.py`（tools 子命令组）
**动作**：`grga tools install --no-verify` / `grga tools update --no-verify`；也支持 `GRGA_INSECURE=1` 环境变量（air-gapped mirror 场景）。使用时 stderr 打红字警告，json 模式打 warning line。

**验证**：`grga tools install rg --no-verify --json` → 输出 warning 但退出码 0。

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| `--no-verify` + manifest 无 sha256 | 静默通过（本来就没有校验） |
| `--no-verify` + sha256 不匹配 | 通过但打警告；json warning 里包含 `expected` / `actual` |
| 生产 CI 中误用 | 通过环境变量 `GRGA_STRICT=1` 强制拒绝 `--no-verify`（可选，P5 再补） |

**风险**：低。**回滚**：删 flag。

---

### Step 4.3 `grga tools install --from <dir>` 离线安装
**文件**：`src/grga/toolchain.py`, `src/grga/cli.py`
**动作**：新增 `install_tools_from_dir(src: Path, names=None)`。约定源目录布局 = `<dir>/<platform>/<asset-filename>`，或平铺 `<dir>/*.tar.gz`（自动按 asset 名匹配）。仍走 sha256 校验（除非 `--no-verify`）。

**验证**：
- 准备一个装了 rg tarball 的目录 → `grga tools install --from ./mirror rg` → 成功。
- `grga tools check` 显示 managed。

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| 空目录 | 报错 `no_assets_found`，退出码 9 |
| 部分工具存在 | 装能装的，报告缺的，退出码 6（partial） |
| 全部工具存在 | 全装，退出码 0 |
| 混错平台（目录里是 linux-x86_64 但当前是 aarch64） | 按 asset 名匹配失败 → 报 `platform_mismatch`，不静默乱装 |
| 归档损坏 | 报 `unpack_failed`，不留残余（临时目录清理） |
| 与在线安装的降级 | 若同时给了 `--from` 和 `--base-url`，`--from` 优先，找不到再回落在线（默认不回落，需 `--fallback-online`） |

**风险**：低，纯新增。**回滚**：去掉子命令 flag。

---

### Step 4.4 `find_tool` 解析策略明确化：managed 优先 + 显式覆盖
**文件**：`src/grga/toolchain.py`, `docs/tool-resolution.md`（新增）
**动作**：**维持现状** managed → system fallback（避免破坏 Agent 场景可复现性），但新增：
- `GRGA_PREFER_SYSTEM=1` 或 `--prefer-system` 反转优先级。
- `find_tool()` 内对系统命中的工具做**最低版本检查**（可选，仅对 `rg` 做，硬门槛写 `MIN_VERSIONS = {"rg": "13.0.0"}`）：系统版本过老 → 打警告并降级到 managed（若 managed 存在），否则继续使用但警告。
- `rga` 找到时同步校验 `rg` 是否可解析，缺则报 `dependency_missing`。

**验证**：
- 单测：mock `subprocess.run("rg --version")` 返回过老版本 → 断言警告 + 降级。
- 集成：系统装 rg + managed 未装 → `find_tool("rg")` 命中系统；managed 装了同版本 → 命中 managed。

**边界情况 → 期望行为**：
| 边界 | 期望 |
|---|---|
| 空 PATH | 只看 managed |
| PATH 中多个