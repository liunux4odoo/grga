# SKILL.md 二次 offload 提案（related 采用率改进之后续）

**日期**：2026-09-10
**状态**：提案（待 code agent 执行，未落地）
**动机**：related 采用率改进（`8d6e0a5`）与首轮精简（`fa9b20b`，376 行）已落地后，
进一步评估 SKILL.md 中是否还有内容可在**不损害规则执行**的前提下 offload 到
`references/`。
**关联**：`docs/design/plan-2026-09-10-related-adoption-analysis.md`（行为案例与四病因，
本提案的判据来源）、`src/grga/skills/grga/SKILL.md`（被改文件）、
`references/{verbs,storage-model,reflection-loop}.md`（offload 目标）。

---

## 0. TL;DR

1. **可 offload ≈ 40-50 行**（376 → ~330），共 7 个候选段，全部为**语义细节 / 背景 /
   不变式**——agent 不读也不会违背强制规则。
2. **绝对不可动**：MANDATORY RULES、Start here 入口路由、T2 bookends + Pre-answer
   checklist、Consuming related output 配方、Stuck signals 精简表、Caller
   anti-patterns、Friction-free basics 三条。
3. **判据**（本提案的核心约束）：offload 只允许"**不读也不违背强制规则**"的内容；
   禁止把"读了才有收益、不读就犯错"的内容（如消费配方）offload——09-09 案例证明
   agent 不读 example（`search-related-graph.md` 一直存在但未被使用）。
4. **offload 的真正收益是响度不是行数**：强制规则在主文件的密度提高，P3 注意力扫描
   更可能命中规则。

---

## 1. 背景：为什么现在做二次 offload

- `fa9b20b` 已做首轮精简（"消除与 references 的重复"），SKILL.md 376 行。
- references/ 五个文件已覆盖全部细节：`verbs.md`（247 行，含 graph inspect）、
  `storage-model.md`（176 行，含读侧 scope 语义）、`reflection-loop.md`（308 行，
  含卡壳信号完整版与 T2 定义）。
- 因此本轮 offload **大多是"删掉主文件里的展开，指向已有 reference"，不是新写内容**。
- 判据来自 09-09 行为案例：agent 是注意力扫描型读者，主文件越长，强制规则越容易被
  淹没；但 agent 也不会主动查 reference，所以 offload 只适用于"不读也无害"的内容。

---

## 2. 候选 offload 清单（7 项）

> 执行原则：每一项都是**主文件删减 + 确认 reference 目标已有等价内容**（已有，无需
> 新写）。若发现 reference 缺某细节，补到 reference，不回填主文件。

### 2.1 读侧 scope 语义（~10 行 → storage-model.md）

- **当前**：`Storage model at a glance` 里的 `--tag` / `--tag-logic and|or` /
  `related --mode heading` 默认跨 scope / `--directory` 与 scope 无关 /
  "热路径动词不接受 `--tag`" 展开段。
- **目标**：`references/storage-model.md` § Read-side scope semantics（已有完整版）。
- **主文件保留**：一句指针——"scope/tag 组合语义见 `references/storage-model.md`；
  记住 `related` 用 `--scope` 不用 `-d`"。
- **判据**：低频深度语义；核心摩擦点（`related` 用 `--scope`）已在 Friction-free
  basics 与 MANDATORY 中保留，不依赖此段。

### 2.2 `Practical CLI invariants`（~8 行 → verbs.md / storage-model.md）

- **当前**：--json 单文档输出、失败 exit 非零、路径相对性、DB 默认路径、
  sidecar 缓存。
- **目标**：`references/verbs.md`（JSON 契约）+ `references/storage-model.md`
  （DB 路径）。若两处均已有，直接删。
- **主文件保留**：至多一句 "--json 契约与 DB 路径见 references"；或全删（MANDATORY
  已要求 `grga --json` 输出可管道化，隐含 JSON 契约）。
- **判据**：实现不变式，非执行规则；agent 遇到格式问题会查 verbs.md。

### 2.3 composition notes（~8 行 → examples/ 或 reflection-loop.md）

- **当前**：`Typical composition patterns` 下的五条——Probe first when in doubt /
  Skip expand for tiny hits / Chain expand + related / Rerank before expand /
  Re-probe LLM output。
- **目标**：`references/reflection-loop.md`（跨层 guardrail 已有 "Re-probe LLM
  output" 规则版）或对应 examples 文件；"Chain expand + related" 已存在于
  `examples/search-expand-chapter.md` 心智模型。
- **主文件保留**：examples 列表本身（4 个 example 的 one-line 描述，是 agent 知道
  "有什么可查"的索引）。
- **判据**：优化建议非规则；"Re-probe LLM output" 的规则版已在 workflow
  cross-tier guardrail，不因删 notes 丢失。

### 2.4 graph inspect 详述（~10 行 → verbs.md）

- **当前**：`Supporting commands (read-side only)` 里 graph inspect 的 `--scope` /
  `--tag` 参数、`scope_filter_hint`、empty 语义展开。
- **目标**：`references/verbs.md` § Supporting command（已有）。
- **主文件保留**：2-3 行——"graph inspect 是只读诊断命令，不是 `related` 的替代
  （T2 是 `related` 不是 graph inspect，见 reflection-loop）；细节见 verbs.md"。
- **判据**：诊断命令非主流程动词；防错规则（T2 是 related）保留，参数细节 offload。

### 2.5 `Storage model at a glance` 前两段（~5 行 → storage-model.md）

- **当前**：DB 路径、scope 概念、离线构建说明。
- **目标**：`references/storage-model.md`（已有）。
- **主文件保留**：一句 "per-user graph at `~/.config/grga/deep_keywords.db`，用户
  离线构建，本 skill 只读不写"（作为 MANDATORY #5 的上下文）。
- **判据**：背景知识；离线构建约束已在 MANDATORY #5（build trigger）保留。

### 2.6 `When to use this skill` 的 Use 列表（~15 行 → 并进 five verbs 表）

- **当前**：Use 列表三行（full-text search / context / related / suggest）+ "Do not
  use" 三行。
- **目标**：Use 列表每行本就对应一个动词，信息已在 five verbs 表的 "Use when" 列
  与 frontmatter description 中；直接删 Use 列表，保留 "Do not use" 三行
  （web/editing/agent-loop/deep-search 边界）。
- **判据**：与 description 和动词表重复；删后激活判断能力不降（description 已含
  "entity → related-first / literal → probe→search" 路由）。
- **⚠️ 回滚观察点**：若删后 agent 判断"是否激活 skill"变慢，回滚本条。

### 2.7 `What this skill is not`（~5 行 → 并入 Do not use）

- **当前**：not a QA agent / not web-editing / not rg replacement 三行。
- **目标**：与 2.6 的 "Do not use" 合并；**保留** "not a replacement for `rg` /
  `rga`" 的澄清（与 MANDATORY #1 有张力，需保留原文语义）。
- **判据**：与 description 和 Do not use 重复。

---

## 3. 绝对不可动清单（规则执行核心）

以下内容**不得**因本提案删减或移动（任何 offload 都视为重蹈 09-09 覆辙）：

1. **MANDATORY RULES 五条** —— 规则本身。
2. **Start here: pick the entry verb** —— 任务分类 if-then（`8d6e0a5` 核心改进）。
3. **Friction-free basics 三条** —— AND / `-d` 目录 / `--scope`：09-09 案例证明
   这三个摩擦点是"grga 无用"印象的来源。
4. **T2 bookends（双强制 + 一中触）与 Pre-answer checklist** —— 强制流程定义。
5. **Consuming related output 配方** —— 09-09 案例核心断点（消费层盲区）；agent
   不读 example 的证据直接禁止把配方移出主流程。
6. **Stuck signals 精简表**（信号名 + First choice）—— 卡壳升级路径。
7. **Caller anti-patterns**（meta-rule + 4 条）—— 行为约束。

---

## 4. 验收标准

1. SKILL.md 行数从 376 → ~325-335（7 项全做）或 → ~358（只做 2.1+2.2 最小版）。
2. 上述 7 项清单内容在主文件中消失（或只剩 ≤1 句指针），reference 目标无新增重复。
3. **规则执行完整性**：MANDATORY / Start here / T2 bookends / checklist /
   消费配方 / Stuck signals / anti-patterns / Friction-free basics 全部逐字保留
   （diff 中这些段落零改动）。
4. 全文 grep 确认无残留重复（如 "scope/tag" 语义同时出现在主文件与
   storage-model.md）。
5. 一个示例任务（如"国字脸"式身份消歧）按新 SKILL.md 走一遍，能命中
   "Start here → related-first" 路径（不实际执行，逻辑推演即可）。

---

## 5. 决策留痕 / 待确认

- [ ] 全做 7 项 vs 最小版（2.1+2.2）？默认建议全做——收益是响度提升，风险已被
      §3 清单封死。
- [ ] 2.6 是否回滚由后续观察决定（§2.6 的 ⚠️ 观察点）。
- [ ] 本 plan 是否需要在 decisions.md 留一行决策记录（关联 `8d6e0a5`）？
