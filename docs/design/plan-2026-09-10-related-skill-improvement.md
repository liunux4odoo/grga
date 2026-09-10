# grga skill 改进方案 —— 让使用 skill 的 agent 把 related 用起来、并会消费其输出（2026-09-10）

> **类型**：skill 变更方案（HOW），承接 `plan-2026-09-10-related-adoption-analysis.md`
> （WHAT / 证据）。角色边界：前一份文档冻结诊断与判据，本方案给出**落点 + 草案文本**。
> **视角**：站在"使用 skill 的智能体"角度——它读 SKILL.md 的**顺序**、在哪个词上走神、
> 遇到什么摩擦会放弃、拿到候选列表后不知道下一步是什么。
> **状态**：**已采纳并落地（2026-09-10）**。改动已进
> `src/grga/skills/grga/`（SKILL.md + 引用层）；CLI 侧候选（§4）另行评估。

---

## 1 · 一句话诊断

> 案例 agent 不是"不会调 related"——用户三次提示后它调了。真正断掉的是三件事，
> 而当前 skill 恰好在这三件事上都没拦住它：

1. **入口反着教**：quickstart 把动词按成本序排（probe → search → expand → related），
   与"session-open 必须先跑 related bookend"的工作流**直接矛盾**。照 quickstart 走的
   agent 永远到不了 related。
2. **消费配方只在 example 里**：`examples/search-related-graph.md` 有配方，但 SKILL.md
   主流程从没教"拿到候选关键字列表后怎么办"。案例里 `方长 / 狂风 / 老白 / 夜十`
   正是答案的积木，被当噪声丢掉——这是**主流程缺失**，不是 example 缺失。
3. **摩擦速查不在 quickstart**：implicit AND、`-d` 传文件、`related` 用 `--scope`、
   scope 名从哪来——四件事在 references 里都有，但 quickstart / MANDATORY 里没有，
   于是 agent 先撞墙（0 match、报错、被迫 sqlite3），再形成"grga 无用"印象。

---

## 2 · 现状逐点核对（agent 视角，映射案例 §3 / §4）

> 行号基于落地前当前 `src/grga/skills/grga/SKILL.md`（343 行）。只引用**稳定锚点**
> （section 名 + 原文），不依赖行号定位。

| 案例断点 | 当前 skill 文本 | 为什么没拦住 |
|---|---|---|
| **调用层**：related 输出不符合"直接答案"预期，一次就弃 | 五动词表 related 行："Caller has keywords and needs to **broaden**"；quickstart ④"candidate related keywords via co-occurrence" | 说了"不是文档"，但从没说"**不是答案，是线索**"；也没教线索怎么变成答案 |
| **消费层（最大盲区）**：`方长/狂风/老白/夜十` 当噪声丢弃 | 消费配方**只**在 `examples/search-related-graph.md` 的 `Composition tip`（step 5-7 是"拼进 rga 找原文"） | 配方是"概念扩展"取向（brew→linux/macos），不是"实体识别"取向；且 agent 没读 example |
| **印象层**：AND 语义 0 match | 五动词表 search 行有"implicit AND"，`implicit_notice` 字段存在，verbs.md §1 有细节 | 全在 references / 表里，quickstart 和 MANDATORY 没有；agent 撞墙后不知道为什么、也没有"OR 用于发现"的策略 |
| **印象层**：`-d` 传文件报错 | skill 通篇没有"`-d` 是目录不是文件"；当前报错 `path is not a directory: <path>`（`search.py:349`）无引导 | 无前置提示 |
| **印象层**：`related` 无 `--directory` + scope 名不可知 | "读 verbs 的 `--directory` 语义"在 **Storage model at a glance**（深）；scope 名来源是"via `grga scope list`"（同节一句带过） | MANDATORY #1 禁 sqlite3，却没把"`grga scope list`"放到一眼可见处，agent 只能 sqlite3 |
| **读而没执行**：seq 1116 加载 skill，无 bookend 痕迹 | MANDATORY 四条全是**禁令**（别 grep / 别 build / 别 sqlite3）；T2 bookend 定义在 "Tiered reflection workflow" 深处（约 150 行后） | 正面流程藏太深；最显眼的位置只告诉 agent"别做什么"，没告诉"先做什么" |

**新发现（案例 §3 未点名）**：quickstart 与 T2-bookend 的**顺序矛盾**是结构性缺陷。
"30-second quickstart" 以成本序（probe 第一）呈现，而工作流要求 open-ended 任务
related 在第一次 search 前。一个只读了 quickstart 的 agent 按字面执行，就永远
不会触发 session-open bookend——这解释了 seq 1116"读了 skill 却无 bookend"。

---

## 3 · 改动方案（按杠杆排序）

> 全部改动落在 **SKILL.md 主流程 + 引用层**（§4 说明 skill/CLI 分层）。
> 新增文本用英文写（与 SKILL.md 一致），括号内中文为说明。

### 3.1 【最高杠杆】入口路由 + quickstart 修正（对应病因二/四）

**现状**：MANDATORY 之后直接是 quickstart，quickstart 按成本序教，无任务分类。

**改动 A**：MANDATORY RULES 与 quickstart 之间插入一节 `Start here`，先按问题类型
定入口动词：

```markdown
## Start here: classify the question, then pick the entry verb

Read this before the quickstart. The entry verb is decided by the
**question type**, not by cost order:

- **Entity / identity / relationship questions** — "X 是谁？", "A 和 B 是
  同一个人 / 同一个概念吗？", "和 X 相关的人物 / 概念还有谁？"
  → **`related` first, not `search`.** Run
  `grga related "<seed>" --mode graph --json` as the session-open
  bookend **before** the first `search`. The candidate list is **clues**:
  names / entities in it are your suspect set, not noise. For
  identity-questions, run independent evidence chains per side
  (anti-pattern #1) and harvest person-name candidates to test.
- **Literal / location questions** — "X 出现在哪里？", "包含 X 的段落 /
  章节？" → `probe` → `search` → `expand`.
- **Vague / open-ended** → `probe` first; escalate per stuck signals.
```

**改动 B**：给 quickstart 四个命令加路由注释，消除顺序矛盾（不重排，但把 related
标为"实体/关系问题的入口"）：

```markdown
grga probe "your query" --directory ./docs --json   # 1) 字面定位/规模预估才从这里进
grga search "your query" --directory ./docs --json  # 2) 证据拉取
grga expand "path/to/file.md:42" --directory ./docs --level chapter --json  # 3) 展开
grga related "keyword" --json                       # 4) 实体/关系问题的【入口】：
                                                    #    先跑这个，再回到 1-2
```

**改动 C**：MANDATORY RULES 追加一条正面规则（打破"全是禁令"）：

```markdown
- **Open-ended task? Run `related` before the first `search`.** The
  session-open T2-bookend is mandatory (see "T2 bookends"). The
  quickstart's step ordering is cost order, **not** execution order.
```

### 3.2 【最高杠杆】related 消费配方进主流程（对应病因一，核心盲区）

**现状**：配方只在 `examples/search-related-graph.md`，且是"概念扩展"取向。

**改动**：在 "T2 bookends" 一节之后新增一节 `Consuming related output`（主流程内，
而非仅 example）：

```markdown
### Consuming `related --mode graph` output (the recipe)

`related` returns a **candidate keyword list**, never an answer. Reading
it is a two-step job:

1. **Filter for entities.** Keep proper nouns / names / concrete terms;
   drop stopwords & discourse markers (`不过`, `什么`, `the`, `would`)
   and very-high-`chunk_df` hub words. Sort candidates by `npmi`.
2. **Feed survivors back into `search`.** `grga search --implicit-or
   <cand1> <cand2> ... -d <dir> --json` (or `rga -e`) to locate passages,
   verify co-occurrence, then `expand` the best hits.
3. **Iterate a hop.** Promote a strong candidate (high `npmi`, moderate
   `chunk_df`) to the next `related` seed. 1-2 hops is usually enough.

For an **identity question**, person-name candidates ARE the suspect
list: `related "国字脸"` surfacing `方长 / 狂风 / 老白 / 夜十` means
"these people co-occur with 国字脸" — test each with `search`, do not
discard them as noise.
```

配套：`examples/search-related-graph.md` 顶部加一句指向主流程配方（"recipe now
lives in SKILL.md"），并在 `When to use` 补一个**实体识别** storyboard 变体
（seed = 人物 / 专名，候选里的人名 = 嫌疑人集合）。

### 3.3 摩擦速查表（对应病因三）

**现状**：四件事（AND 语义 / `-d` 传文件 / related 无 `-d` 用 `--scope` / scope 名来源）
分散在 references，quickstart 无。

**改动**：quickstart 后插入 `Friction-free basics (read once)`，把四件事各一句钉死：

```markdown
## Friction-free basics (read once)

- **Multi-word is implicit AND.** `grga search "A B C"` matches lines
  with **all three** terms. For **discovery** use OR (`--implicit-or`,
  or `||` between terms) or run each term separately; reserve AND for
  **verifying** a hypothesis you already have. AND-ing several evidence
  words hoping for one line almost always returns 0.
- **`-d / --directory` takes a directory, not a file.** For a single file
  point at its **parent directory**.
- **`related` takes `--scope`, not `-d`.** Omit `--scope` for cross-scope
  union, or pick one from `grga scope list --json`. Never open the DB
  with `sqlite3` to discover scope names (MANDATORY rule).
```

### 3.4 期望管理：五动词表 related 行（对应病因一调用层）

**现状**：related 行已说"不返回文档"，未说"不是答案"。

**改动**：related 行末尾追加一句：

```markdown
Returns **candidate keywords (clues), not answers** — combine with
`search` to build the answer.
```

### 3.5 引用层微调

- `references/anti-patterns.md` #3（sqlite3）：补一句正向指针
  "scope names come from `grga scope list --json`"。
- `SKILL.md` frontmatter `description`：加"entity/relationship questions
  route to `related`-first"——description 驱动 skill 选择与 agent 记忆，值得一行。
- `references/reflection-loop.md` "Choosing seeds"：无需改（已覆盖 low-DF noun）；
  在 seed 表后加一句交叉引用 "consumption recipe 见 SKILL.md `Consuming related output`"。

---

## 4 · 分层决策：skill 改 vs CLI 改

> 用户问题限定在 skill 层，本方案主体是 skill 改。以下列出**同摩擦的 CLI 侧
> 候选**，标注为可选、另行评估——skill 改动不依赖它们。

| 摩擦 / 缺口 | skill 层（本方案） | CLI 层（可选，另行） |
|---|---|---|
| AND 语义 | §3.3 速查表 | `implicit_notice` 已有；可加强 stderr 文案（不动 JSON） |
| `-d` 传文件 | §3.3 | `search.py:349` 报错后追加一行 hint（**勿动既有子串**，`gui.py:1048` 按它匹配） |
| `related` 无 `-d` | §3.3 | `related_command` 捕获误传 `--directory` 给 hint（typer 报 "No such option"，可加检测） |
| scope 名来源 | §3.3 + anti-patterns #3 | — |
| 消费配方 | §3.2 | —（契约已给 `npmi/chunk_df/via_seed/scopes`，够用） |
| 入口路由 | §3.1 | — |

**原则**：凡 skill 一行文字能消除的摩擦，先落 skill；CLI 改动只补"错误信息本身有
误导"的情况（`-d` 传文件、related 误用 `--directory`）。

---

## 5 · 验收标准

- **行为重放（迷你案例）**：同一任务（国字脸 = 谁）加载改动后 skill，检查——
  1. agent 在首次 `search` 前跑了 session-open `related`；
  2. 拿到 `方长/狂风/老白/夜十` 后识别为人物线索，回喂 `search --implicit-or` 验证，
     而非当噪声丢弃；
  3. 全程无 `sqlite3`、无 `index build` 触发。
- **文本自检**：SKILL.md quickstart / MANDATORY 内出现 `implicit AND`、
  `--scope`、`grga scope list`、`--implicit-or` 四处关键词（防止回退到"只在
  references 里"）。
- **契约不回归**：本方案**不改任何 `--json` 输出**，skill 改动零契约影响
  （golden test 无感）。

---

## 6 · 决策留痕 / 待确认

> 2026-09-10 采纳，按"必要功能与 skill 体量平衡"落地：主流程只加一个入口路由节、
> 一个消费配方节、一条 MANDATORY 正面规则；references 只加交叉引用；CLI 侧候选本轮
> 不做（摩擦已由 skill 层 "Friction-free basics" 覆盖）。

- [x] 3.1 入口路由采纳，与 T2-bookend **并存**：§3.1 `Start here` 决定入口动词，
      MANDATORY 新规则 + quickstart 标注保证 bookend 不被成本序掩盖。
- [x] 3.2 配方落 SKILL.md 主流程（`Consuming related output`），example 保留交叉引用
      + 新增 Identity variant 小节。
- [x] §4 CLI 侧可选改动**本轮不做**：`-d` 传文件、scope 来源已由 "Friction-free
      basics" 一行覆盖；`search.py:349` hint 等留待有误导性报错的证据再启。
- [x] 本案例 + 本方案构成"skill 变更"的触发证据，已落地。
