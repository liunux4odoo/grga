# 项目规则

## rga 命令路径

- `rga` 命令安装在 `/media/lqw/DATA/bin` 下面。
- 在测试命令或获取命令帮助时，可临时使用该路径（如 `/media/lqw/DATA/bin/rga --help`）。
- 代码里仍然直接使用系统命令名（`rga`），不要写死绝对路径。

## Python 运行与验证

- 本项目使用 `/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3` 运行 Python 命令、语法检查、测试和安装库。
- 修改 Python 代码后，至少运行 `/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3 -m py_compile src/grga/search.py src/grga/gui.py src/grga/cli.py src/grga/__init__.py src/grga/__main__.py` 做语法检查。
- 涉及搜索逻辑变更时，优先补充最小后端验证脚本，直接调用 `grga.search.search_directory` 验证核心行为。

## 功能实现偏好

- GUI 新增搜索参数时，同步处理：变量初始化、配置保存、trace 自动搜索、控件禁用列表、命令预览、后台线程参数传递。
- 后端新增搜索参数时，保持 `search_directory` 返回结构与现有 rga JSON 解析结果兼容，避免破坏 GUI 渲染流程。
- 仅用于执行搜索的系统命令依赖应尽量局限在需要该命令的分支；纯 Python 可完成的模式不应被 rga 环境检查阻塞。
