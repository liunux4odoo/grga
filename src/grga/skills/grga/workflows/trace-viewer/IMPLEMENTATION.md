# Implementation Contract

**面向 code agent。** 规格是 `render.py` 与 `template.html` 的对外契约与
边界行为。**只写 WHAT，不写 HOW。** 具体代码结构、变量名、抽象层次由实现
者决定。

---

## 交付物

两个文件放在本目录：

1. `render.py` — CLI 工具，读 JSONL，写单文件 HTML
2. `template.html` — HTML 骨架，含明确的替换标记；`render.py` 消费它

外加 `vendor/cytoscape.min.js`（MIT，checked in）和
`example/sample.jsonl`（demo 数据，用来 smoke test）。

---

## `render.py` 契约

### 命令行接口

```
python render.py <input.jsonl> -o <output.html> [options]
```

- **必需位置参数**：输入 JSONL 路径
- **必需选项** `-o` / `--output`：输出 HTML 路径
- **可选选项**：
  - `--title TEXT`：HTML `<title>` 与页面标题，默认从 session-id（输入
    文件 basename 去后缀）派生
  - `--quiet`：抑制 stderr 的进度/警告，只保留错误

### 退出码

- `0` — 成功
- `1` — 输入不可读 / 输出不可写
- `2` — 输入 JSONL 为空、或所有行都无效
- `3` — 参数错误（缺 `-o` 等）

### 依赖

- **仅 Python 3.8+ stdlib**。禁止 `pip install` 任何东西
- 允许用的模块：`json` / `argparse` / `pathlib` / `html` / `string` /
  `sys` / `datetime` / `re` / `collections`
- 特别禁止：`jinja2` / `requests` / `bs4` / 其他第三方

### 副作用

- **只读**：input JSONL 文件
- **只写**：`-o` 指定的输出文件
- **绝不**：调网络、改 grga 数据库、写用户 home 目录、创建 `-o` 以外的
  任何文件

### 输入契约

- 每行一个 JSON 对象，格式见 `collect.md`
- **健壮性要求**：跳过并在 stderr 报告的场景：
  - 空行、纯空白行
  - JSON 解析失败的行（打印行号 + 前 80 字符）
  - 缺必需字段（`step` / `verb` / `graph_actions`）的行
  - `verb` 不是 `"related"` / `"suggest"` 的行
  - `graph_actions` 里 `op` 不在 v1 冻结集合（`traverse_edge` /
    `score_node` / `llm_propose`）—— 跳过该 action，保留同记录里的其他 action
- **不做 schema 校验**（不引入 `jsonschema`）。做**结构性检查**即可

### 输出契约

- 单一 HTML 文件，UTF-8，含 `<!DOCTYPE html>`
- **自包含**：不引用任何外部 URL / 本地文件路径
  - cytoscape.js 完整内容内联在一个 `<script>` 标签里
  - 所有样式内联在 `<style>` 标签里
  - trace 数据序列化为 JSON 后内联为一个 JS 变量（例如
    `<script>window.__TRACE__ = {...}</script>`）
- 顶部注释包含：
  - 生成时间戳（ISO 8601 UTC）
  - 输入文件名
  - `trace_signals_version`（从记录里取，若混合版本取第一条）

### 数据组织（HTML 里的 JS 变量应至少含）

- `steps`：按 `step` 排好序的记录数组，每条含 `step` / `ts` / `verb` /
  `args` / `actions`
- `nodes`：全 session 累积去重后的节点列表；每个节点记录**首次出现的 step**
- `edges`：全 session 累积去重后的边列表；每条边记录**首次出现的 step**
  与 `edge_kind` / 累积 `weight`（多次出现取最大或最新，实现决定但要在
  代码里注释说明）

这三部分的具体字段可扩展，但**至少要有**上述信息以支撑时间轴回放。

---

## `template.html` 契约

### 占位符

用 Python `string.Template` 语法（`$name` / `${name}`），至少含：

- `${title}` — 页面标题
- `${cytoscape_js}` — vendored cytoscape.min.js 完整内容
- `${trace_data_json}` — 序列化后的 trace 数据（`json.dumps` 结果）
- `${generation_header}` — 顶部注释（含时间戳、输入文件、版本）

实现者可增加占位符，但**不可删除**上述四个。

### 视觉与交互要求（"折中"的定义）

**必需**：

1. **主画布**占页面 70%+，cytoscape 图渲染
2. **时间轴**在顶部，横向 slider，从 step 1 到 max step
   - 拖动 slider 时，图上"当前 step 及之前"的节点/边显示为正常状态，
     "更晚 step 的"淡化或隐藏（实现决定）
   - slider 旁显示当前 step 号 + `ts` + `verb` + 简短 `args` 摘要
3. **详情面板**在右侧或底部，点击节点/边时显示 `meta`（`edge_kind` /
   `weight` / `hops_from_seed` / `algo` 等）
4. **图例**（一次固定显示，不折叠）：
   - 三种节点前缀（`kw:` / `file:` / `head:`）的视觉区分
   - 三种 op（`traverse_edge` 实线、`llm_propose` 虚线、`score_node`
     不是边而是节点属性）的视觉编码

**禁止**：

- 全屏动画、粒子效果、页面切换效果
- 自动播放（除非用户显式点"播放"按钮，且默认关闭）
- 引入任何非 vendor 的字体 / 图标（`system-ui` sans-serif 就够）
- 深色/浅色主题切换等锦上添花的功能

**允许但不要求**：

- "适应窗口"按钮
- 节点搜索框
- 布局算法选择（cytoscape 内置的 fcose / cose 等）

### 布局哲学

**信息密度 > 视觉惊喜**。每个 UI 元素应服务于"看清扩展过程"这一目标，
删掉任何不服务此目标的东西。

---

## 边界行为（必须显式覆盖）

| 场景 | 期望行为 |
|---|---|
| 输入文件不存在 | exit 1，stderr 输出人类可读消息 |
| 输入文件全空 | exit 2 |
| 输入文件全是坏行 | exit 2 |
| 输入含混合有效/无效行 | 忽略无效行，用有效行渲染，exit 0 |
| 单条记录 `graph_actions=[]` | 记录进 `steps`，图上不新增节点/边；时间轴仍显示该 step |
| 所有记录 `graph_actions=[]` | 生成空画布 HTML + 时间轴，exit 0（合法情况）|
| 记录里出现未来 op（词汇表里但 v1 不实现）| 跳过该 action，stderr 提示；同记录其他 action 保留 |
| 节点 id 里含 URL-encoded 字符 | 原样保留作为 id，展示时可解码 |
| 同一条边在多个 step 反复出现 | 只保留一次；`step` 记为**最早出现**的 step；`weight` 处理策略在代码里注释说明 |
| session-id 里含路径分隔符 | 用 basename 派生标题，正常运行 |
| 极大 trace（> 10k actions）| 至少能生成 HTML；性能优化不是本次目标 |
| 输出路径已存在 | 覆盖（不询问、不备份） |

---

## 验收标准

1. **CLI 冒烟**：
   `python render.py example/sample.jsonl -o /tmp/trace.html && test -f /tmp/trace.html`
2. **自包含检查**：
   `grep -E 'src=["'\'']https?://' /tmp/trace.html` **必须无输出**
3. **stdlib-only 检查**：
   `python -c "import ast; ast.parse(open('render.py').read())"` 通过，且
   `grep -E '^import |^from ' render.py` 只出现 stdlib 模块
4. **边界表覆盖**：上表每一行至少一条 pytest 用例或 shell 测试
5. **文件大小**：输出 HTML 应 < 500 KB（cytoscape.min.js ~330KB + 骨架
   与数据）；超过说明模板有冗余
6. **视觉验收**（人肉）：
   - 打开生成的 HTML，能看到画布 + 时间轴 + 图例
   - 拖动 slider，图内容随 step 变化
   - 点击节点显示 `meta`
7. **确定性**：同一份输入两次跑 render，diff 只应出现在顶部生成时间戳
   注释行

---

## 实现建议（非强制）

以下**只是提示**，实现者可完全无视：

- 数据处理和 HTML 生成分离，方便测试
- 用 `string.Template` 而非 f-string 拼 HTML，避免 HTML 里的 `{}` 冲突
- cytoscape 的样式配置作为 JS 对象放在模板里，不用 CSS 变量玩花活
- 边去重时用 `frozenset({from, to, edge_kind})` 作 key

---

## 冻结点

以下一旦发布视为外部契约，改动需 MAJOR bump：

- `render.py` 的 CLI 参数集合（位置参数 + `-o` + `--title` + `--quiet`）
- 退出码 0/1/2/3 的语义
- `template.html` 的 4 个必需占位符名
- "自包含"承诺（不引用外部 URL）
