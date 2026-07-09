# 项目规则

## 基本原则
- 所有涉及系统平台的操作，都要确保跨平台一致性
  
## rga 命令路径

- `rga` 命令安装在 `/media/lqw/DATA/bin` 下面。
- 在测试命令或获取命令帮助时，可临时使用该路径（如 `/media/lqw/DATA/bin/rga --help`）。
- 代码里仍然直接使用系统命令名（`rga`），不要写死绝对路径。

## Python 运行与验证

- 本项目使用 `/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3` 运行 Python 命令、语法检查、测试和安装库。
- 修改 Python 代码后，至少运行 `/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3 -m py_compile src/grga/search.py src/grga/gui.py src/grga/cli.py src/grga/toolchain.py src/grga/__init__.py src/grga/__main__.py` 做语法检查。
- 涉及搜索逻辑变更时，优先补充最小后端验证脚本，直接调用 `grga.search.search_directory` 验证核心行为。

## 内置命令行工具工作流

- 项目倾向于把 `rg`、`rga`、`rga-preproc`、`pandoc`、`pdftotext`、`fzf` 等二进制文件在运行时按需下载到用户缓存目录（自 0.2.0 起 wheel 不再内嵌二进制），减少用户手动配置系统命令。
- **首次运行须提前 `grga tools install` 或允许网络访问**；Agent/CI 等非交互场景缺工具时会以退出码 4 返回结构化 `missing_tools` 错误（`--json`），`--offline` 则以退出码 5 立即失败。
- 托管工具缓存目录：`$GRGA_TOOLS_DIR` > `$XDG_DATA_HOME/grga/tools/<platform>` > 平台默认（Linux/macOS `~/.local/share/grga/tools/<platform>`，Windows `%LOCALAPPDATA%\grga\tools\<platform>`）；旧 `site-packages` 缓存可用 `grga tools migrate` 迁移。
- 构建工具镜像用 `/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3 scripts/download-tools.py --platform all --out ./mirror --write-hashes`，该脚本仅供开发者使用，不参与 `python -m build`。
- 需要指定工具时，可运行 `/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3 scripts/download-tools.py rg rga pandoc fzf`。
- 运行时工具查找顺序为：托管缓存优先 → 旧 site-packages 缓存 → 系统 `PATH`（`GRGA_PREFER_SYSTEM=1` 可反转）。
- 下载工具时可使用 `grga tools install` / `grga tools update`；网络受限时用 `grga tools install --from <mirror>` 从本地镜像离线安装。
- 不要在代码中写死开发机上的绝对工具路径；仅测试命令可临时使用本机路径。
- 退出码与 `--json` 错误 schema 见 `docs/exit-codes.md`，工具解析顺序见 `docs/tool-resolution.md`，发布流程见 `docs/dev-release.md`。

## 功能实现偏好

- GUI 新增搜索参数时，同步处理：变量初始化、配置保存、trace 自动搜索、控件禁用列表、命令预览、后台线程参数传递。
- 后端新增搜索参数时，保持 `search_directory` 返回结构与现有 rga JSON 解析结果兼容，避免破坏 GUI 渲染流程。
- 仅用于执行搜索的系统命令依赖应尽量局限在需要该命令的分支；纯 Python 可完成的模式不应被 rga 环境检查阻塞。
