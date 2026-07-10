---
name: grga-trace-viz
description: "复盘一次 grga 搜索会话（或任何多步检索+反思式工作流）成竖版 swim-lane 时序图 HTML。触发表达：复盘 grga 会话、画搜索过程图、trace 一下这次搜索、把搜索路径可视化、make trace visualization、post-mortem this search。服务对象是人（做复盘的开发者 / agent 回看自己的搜索路径），不是 agent 实时检索。不要在常规 grga 查询里触发。"
metadata:
  qwenpaw:
    emoji: "📊"
    requires: {}
---

# grga-trace-viz

将一次 grga 搜索会话（或任何"多步检索 + 反思"式工作流）复盘成一张
**竖版 swim-lane 时序图 HTML**：X 轴 = 查询关键字家族，Y 轴 = 时间步
（step），节点悬停显示该步的命令 / 信号 / 决策 / 教训。

**服务对象是人**（做复盘的开发者 / agent 事后回看），不是 agent 实时
检索。**不要在常规 grga 查询里触发**——只在"稀疏的、值得复盘的"
事件里用。

## 什么时候触发

- 一次 grga 会话暴露了 skill 引导缺口（反模式 / 卡壳信号），要写
  SKILL.md 前先看清"路径长什么样"
- co-design 一个新 skill / RAG 策略前，需要一张图对齐"以前是怎么搜索
  的、卡在哪儿"
- 用户明确说"复盘一下"、"画搜索过程图"、"trace 一下"、"把搜索路径
  可视化"

**不适用**：日常单次 grga 查询、agent 自动 fallback chain、超过 60 步
的长会话（图会太长，可视化收益递减）。

## 快速上手

```bash
cd <本skill目录>
python3 build_html.py data.example.json out.html
# 双击 out.html
```

`data.example.json` 是一份真实的 34 步复盘（凡人修仙传·草原元婴悖论）。
你的场景准备一份符合同样 schema 的 JSON 就能直接跑。

## 数据格式

**契约在** `../../schemas/trace-viz-session-v1.json`（相对本 skill
目录）。`session_version` 目前是 `"1.0"`。

一个最小 session JSON 长这样：

```json
{
  "session_version": "1.0",
  "title": "我这次的复盘",
  "subtitle": "N 步 · M 家族",
  "family_order":  ["setup", "family-a", "family-b"],
  "family_colors": {"setup":"#607d8b", "family-a":"#1e88e5", "family-b":"#43a047"},
  "kind_markers":  {"prep":"○","probe":"◇","search":"◆","expand":"▶","antipattern":"✗","synthesize":"★"},
  "kind_labels":   {"prep":"准备","probe":"probe","search":"search","expand":"expand","antipattern":"反模式","synthesize":"综合"},
  "steps": [
    {"step": 1, "family": "setup", "kind": "prep", "cmd": "grga probe -q ...",
     "signal": null, "decision": "读文档", "insight": "熟悉五动词"},
    {"step": 2, "family": "family-a", "kind": "probe", "cmd": "probe -q 'X Y'",
     "signal": "3 hits", "decision": "太少，扩展", "insight": ""}
  ],
  "summary": {
    "stats_html":         "<span class='stat'>总步数 2</span>",
    "path_features_html": "<ul><li>...</li></ul>"
  }
}
```

### `kind` 字段的语义

| kind | marker | 何时用 |
|---|---|---|
| `prep`        | ○ | 环境准备 / setup / 索引构建 |
| `probe`       | ◇ | 便宜的探测（小 -A/-B、只看数量、不读上下文） |
| `search`      | ◆ | 正式搜索（要读命中的） |
| `expand`      | ▶ | 扩展命中的邻近上下文（B/A/段/章节/图扩展） |
| `related`     | ◇ | L2 图扩展（跨文档） |
| `suggest`     | ◆ | LLM 查询重写 |
| `antipattern` | ✗ | 走了错路（sqlite3 直查 / cp 到 /tmp 等）— 自动加**黑边** |
| `synthesize`  | ★ | 最终综合答案 / 决策收敛 — 自动加**大号**样式 |

### `signal` 字段的写法

- **命中数**：`"7 hits"`、`46`、`"0"` — 直接展示数字
- **结论词**：`"降级"`、`"empty"`、`"22MB"` — 短语
- **🎯 前缀**：`"🎯 7 hits"` — **自动触发金色高亮**（黄金命中，命中原文
  自答那种）。整张图里通常只有 1 个黄金 step

### 命令 `cmd` 简写规范

tooltip 里以 `<code>` 呈现，越短越好。把 `-d /path/to/repo` 之类的路径
参数省掉，只保留动词 + 关键 query：

- ✅ `probe -q '慕兰 大法士' -A 46`
- ❌ `grga probe -d /media/lqw/... -q '慕兰 大法士' -A 46 --json`

## 三步工作流

### 1. 复制模板到工作目录

**不要**在 skill 目录里改 `data.example.json`——skill 目录是版本控制的
参考实现。拷一份到临时工作区：

```bash
cp <本skill目录>/data.example.json /tmp/my-trace/session.json
```

### 2. 改 session.json 适配你的会话

- 改 `title` / `subtitle`
- 重写 `steps` 数组（每一步一个 dict）
- 更新 `family_order` / `family_colors`（反模式家族用红色系放最左，主线
  家族按会话出现顺序，收敛家族放最右）
- 更新 `summary.stats_html`（统计数字要跟 steps 匹配）
- 更新 `summary.path_features_html`（5 条要点各引用具体 step 号）

### 3. 生成 HTML

```bash
cd <本skill目录>
python3 build_html.py /tmp/my-trace/session.json /tmp/my-trace/out.html
```

输出是 **stdlib-only 生成的单文件 HTML**（约 50-60 KB，无外部资源，
双击可开），支持横竖坐标 sticky 的 pan/zoom + 节点 hover tooltip。

## 关键心法

`decision` / `insight` 这两个字段**不是**从 grga 输出里能自动提取的
——它们是 caller 的主观标注。**填这些字段的过程本身就是反思**，图表
只是把反思结果画出来的呈现媒介。别本末倒置想着自动化标注。

## 文件说明

```
skills/grga-trace-viz/
├── SKILL.md                ← 本文件
├── data.example.json       ← 34 步示例（可跑通的最小复现）
├── build_html.py           ← 装配脚本（read JSON → write HTML）
├── svg_gen.py              ← 3 块 SVG 生成器（header/yaxis/main）
└── template.html           ← HTML/CSS/JS 模板（pan/zoom + tooltip + 折叠侧栏）

# grga 仓库根：
schemas/trace-viz-session-v1.json  ← 数据契约（session JSON 必须遵守）
```

## 布局微调

`svg_gen.py` 顶部 4 个常量控制画布单位（当前是**全局参数**，未来 v1.1
可以移到 session JSON 的 `layout` 字段实现 per-session 覆盖）：

```python
LANE_W = 92      # 每个家族列的宽度（px 单位）
STEP_H = 34      # 每一步的高度
HEADER_H = 46    # 顶部家族标签栏高度
YAXIS_W = 46     # 左侧 step 编号栏宽度
```

- 家族多（≥ 15）时可以缩 `LANE_W` 到 78-84
- 步骤多（≥ 60）时可以缩 `STEP_H` 到 28-30
- 中文家族名超过 6 字时把 `LANE_W` 加到 100+

## 与 grga trace-signals 的关系

**这个是 caller 侧的复盘数据，不是 grga 输出的 trace_signals。**

- `schemas/trace-signals-v1.json` — grga CLI 输出契约（`related --json`
  / `suggest --json` 里的图内部动作）
- `schemas/trace-viz-session-v1.json` — caller 汇总的复盘会话（**本
  skill 的输入**）

一次典型的复盘流程：

1. caller 在做 grga 搜索的过程中，把每一步的 `cmd` / `signal` 记下
   （grga CLI 输出直接抄）
2. 事后补 `family` / `kind` / `decision` / `insight` 这四个字段——**这个
   过程本身就是反思**，图表只是把反思结果画出来
3. 组装成一份 `session.json` → 跑 `build_html.py` 出图

## 已知限制

- 只支持竖版布局（曾试过横版，34 步以上视觉上不友好）
- `family` 数量超过 20 时列会挤成一团；20+ 家族的场景建议合并家族
  or 分两页画
- `cmd` 超过 60 字符会撑破 tooltip 宽度，手动截断到关键部分
- **图是给人看的**。future agent 拿到 HTML 只能看 DOM，看不到"视觉印象"。
  agent 要程序化访问 trace，直接消费 `session.json` 里的 `steps` 数组
  比解析 HTML 靠谱

## 参考实现出处

首次实现于 2026-07-10，用于复盘 grga 在《凡人修仙传》语料上"草原资源
匮乏为何能出多位元婴后期"的搜索会话（34 步、13 家族、3 处反模式、
1 处黄金命中）。这次复盘直接催生了 grga skill 的两条 anti-pattern
和一整节 stuck-signals 章节。**默认配色、字号、布局参数都是那次会话
调出来的**——如果你的场景差异很大，先跑一遍再调参数，别空想。
