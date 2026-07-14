# 安装、构建与进阶用法

本文汇总 grga 的完整安装方式、外部工具下载、镜像/打包构建以及面向 AI Agent
的进阶说明。只想快速上手的普通用户看 [README](../README.md) 即可，本文供需要
自定义安装、离线部署、打包分发或把 grga 作为 agent 原语接入的读者参考。

- 环境变量清单：[env-vars.md](env-vars.md)
- 工具解析顺序：[tool-resolution.md](tool-resolution.md)
- 退出码与 `--json` 错误契约：[exit-codes.md](exit-codes.md)
- CLI 参数细节：[CLI.md](CLI.md)
- 发布流程：[dev-release.md](dev-release.md)

---

## 快速开始

```bash
# 1. 安装 grga
python -m pip install -e .

# 2. 首次使用先装外部工具（搜索富文档时必需）
grga tools install

# 3. 基础全文搜索
grga search "面壁计划"

# 4. 图形界面
grga gui

# 5. 想用共现关系图？先建立索引（增量，首次跑全量，之后只处理变化的文件）
#    --scope 是逻辑域名（跨机器可迁移，与本机目录解耦）；--directory 省略则默认当前目录
#    可选 --tag 给 scope 附加多值标签（v11，随 knowledge pack 迁移）
grga index build --scope my-notes --directory ./my-notes
grga index build --scope sci-fi --directory ./novels --tag 科幻 --tag 小说
grga related 面壁计划 --scope my-notes
grga related 面壁计划 --tag 科幻 --tag 小说 --tag-logic and   # 按标签选 scope（顺序无关）
```

---

## 安装方式

### 从源码安装

```bash
python -m pip install -e .
```

安装完成后即可**直接搜索纯文本类文件**（`.txt` / `.md` / 源码 / 日志 / JSON /
CSV 等），无需额外配置。若需要搜索 **PDF / Office / 电子书**（`.pdf` /
`.docx` / `.xlsx` / `.pptx` / `.epub` / `.odt` …），再运行一次
`grga tools install` 安装 `rga` / `pandoc` / `pdftotext` 等工具链即可。

可选依赖：

- `.[standalone]`：构建独立可执行文件所需

```bash
python -m pip install -e '.[standalone]'
```

安装后提供两个入口：`grga` 与 `grga-gui`；也可用 `python -m grga.cli` 在源码
目录中运行。

### 工具的下载与解析

`grga` 运行时的工具解析顺序为：**托管缓存 → 旧 site-packages 缓存 → 系统
`PATH`**（详见 [tool-resolution.md](tool-resolution.md)）。托管缓存目录优先级：

- `$GRGA_TOOLS_DIR`
- `$XDG_DATA_HOME/grga/tools/<platform>`
- Linux/macOS：`~/.local/share/grga/tools/<platform>`；Windows：`%LOCALAPPDATA%\grga\tools\<platform>`

按需安装（缺失时首次运行也会交互提示）：

```bash
grga tools install            # 安装当前平台支持的全部工具
grga tools install rg rga     # 只装指定工具
grga tools update             # 更新
grga tools check              # 查看状态
grga tools migrate            # 从旧 site-packages 缓存迁移到新目录
```

离线 / air-gapped 安装（从本地镜像目录安装）：

```bash
grga tools install --from ./mirror rg
```

首次运行行为与退出码契约见 [exit-codes.md](exit-codes.md)。Agent 场景建议加
`--json` 获取结构化错误，或 `--offline` 让缺工具时立即失败。

工具缓存位置、离线策略、系统工具优先等都可用环境变量（`GRGA_TOOLS_DIR` /
`GRGA_OFFLINE` / `GRGA_PREFER_SYSTEM` …）调整，完整清单见
[env-vars.md](env-vars.md)。

### 从源码构建工具镜像（开发者）

```bash
python scripts/download-tools.py --platform all --out ./mirror --write-hashes --yes
```

该脚本仅供开发者/发布使用，不参与 `python -m build`。详见
[dev-release.md](dev-release.md)。

---

## 打包

### 构建 wheel

```bash
python -m pip install build
python -m build
```

打包产物位于 `dist/`。wheel 为 `py3-none-any`，不内嵌任何平台二进制（工具在
运行时下载）。

### 构建单文件可执行程序

构建 CLI 单文件程序：

```bash
python scripts/build-onefile.py --clean
```

构建 GUI 单文件程序：

```bash
python scripts/build-onefile.py --gui --name grga-gui --clean
```

输出文件位于 `dist/`。外部工具（rg / rga / pandoc / fzf）**不再打入** onefile
可执行程序，由 `grga.toolchain` 在运行时按需从 `$PATH` 或 XDG 缓存加载，必要时
首次运行会提示下载。

---

## 面向 AI Agent（进阶）

grga 的另一条设计线是把自己做成 **agent 友好的检索原语**——所有命令都支持
`--json`、返回结构化字段与稳定契约。

**面向 caller 的两句话：**

> **grga 返回与你的 seed 共现的词/实体，每项携带 df、共现次数、结果分布形态
> 等原始统计。**
>
> **它不做相关性判断、不 rerank、不预筛"重要"结果 —— 这是 caller 的活。**

分层原语：

- **L1**（就近上下文）：物理邻近扩展（前后行 / 段落 / 章节），对应 `grga expand`
- **L2**（共现关系图）：离线预建的关键词共现图，查询快，对应 `grga index` /
  `grga related` / `grga graph`
- **L3**（LLM 语义扩展）：只在 L1/L2 无功时召唤，贵，对应 `grga suggest` /
  `grga deep-search`

**详细设计规范** → 见 [`docs/design/`](design/) 下的设计文档；代码导航见
[navigation.md](navigation.md)。
