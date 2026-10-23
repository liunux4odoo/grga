# Dream (梦境) — 生产 + 消费指南

Referenced from "Dream (梦境) assets" in `SKILL.md`. Full design:
`docs/design/plan-2026-10-14-dream-ontology.md` (本仓库，非 skill 内)。

梦境是**可选**的 L3 补充工作流：外部 caller agent 用自己的 LLM，从 grga
导出的原料里整理出**实体 + 关系**资产，写成旁路 wiki 文件。grga 自身
**不调 LLM、不写数据库、不改任何检索动词**——它只负责
`grga index export-candidates`（原料导出）。

两条主线，各自独立：

- **生产**：什么时候导出、怎么整理、怎么落盘、怎么去重收敛（本文件上篇）。
- **消费**：L1 检索开始前**条件性**读取已有梦境资产做别名消歧 / 查询词扩展
  （本文件下篇，D11）。**无梦境资产时，L1 流程与现在完全一致**。

---

## 上篇 · 生产：梦境整理

### 0. 定位与克制（D12）

只产出**高置信、必要**的实体/关系，**不过度扩展**：宁可多次小规模、不要
单次大规模。单次整理（例如 `--seeds 2`）产出保持克制，低置信 / 重复的不
落盘，留待下次。控制单次 LLM 成本。

### 1. 导出原料

```bash
grga index export-candidates --seeds "韩立,李化元" --scope work \
     --top 20 --out candidates.jsonl --max-context-chars 2000
```

- 每行一个候选对 `{seed, candidate, npmi, cooccur_count, source_kws,
  scope, context}`；`context` 是原文片段，**必须回原文验证**。
- 范围：`--scope work` 限定；`--cross-scope` 才跨。`--seeds` 与
  `--auto-seed` 互斥（冷启动可 `--auto-seed N`）。
- 命令只读不写、零 LLM、幂等；`--out` 原子写入。

### 2. 先读已有资产（迭代，D13）

开始整理前，先读 `dream/<scope>/entities/*.md` 的现有 `members` 与
`## 证据`，以及 `checked.md`：

- `members` 命中的词 → 跳过，不重复归并；
- `checked.md` 里查过但未入梦的短语 → 不再整理；
- 已有实体的 `confidence` 变动必须伴随正反证据（见 §4）。

### 3. 用自己的 LLM 整理

逐行读候选包：

- 归并实体：把同一实体的别名并入 `members`；
- 判断关系：**必须读 `context`** 才能给谓词，不许脑补；
- 低置信 / 无价值：追加进 `checked.md`（每行一个短语，≤10k 行滚动）；
- 迭代：一轮结束后，新实体作新 seed 再 `export-candidates`。

### 4. 落盘（wiki schema，DB 零改动）

实体文件 `dream/<scope>/entities/<name>.md`，front matter **恰好 7 字段**：

```markdown
---
name: 韩立
members: [韩立, 韩天尊, 韩跑跑]
confidence: 0.9
source_kws: [韩立, 韩天尊]
scope: work
kind: 人物
updated: 2026-10-14T17:15:00+08:00
---

别名说明：韩立是主角，韩天尊是后期称号。

## 关系
- [[李化元]]：师从
- [[南宫婉]]：道侣
```

- 关系**双向补全**（`韩立.md` 有 `[[李化元]]：师从`，`李化元.md` 应有
  `[[韩立]]：弟子`）；端点无文件时可只存链接。
- **置信度证据（D13）**：confidence 每次变动必须追加 body 的 `## 证据`
  条目，格式 `run + 方向(+/-) + 一句话理由 + source_kws`：

  ```markdown
  ## 证据
  - [run_01] + 韩立/韩天尊 同段共现 8 次（source: 韩立, 韩天尊）
  - [run_02] - 韩天尊 在 207 章仅指称号非别名（source: 韩天尊）
  ```

  - **下调必须有反证条目**；无证据的裸数字改动不允许；
  - 证据数量不设固定上限，由 caller 判断（可聚合如 `+3 / -1`）。
- 其他落盘：`README.md`（最近整理状态）、`checked.md`（查过未入梦短语）、
  `run_state.md`（种子队列 / 已跑轮次 / 累计产出，中断恢复用）。
- front matter 解析失败 → 报错并指明文件。

### 5. 结束机制（D7）

三层 OR，任一即停：

- **硬上限**：caller 步数上限；
- **软收敛**：一轮新增高置信实体 < 3 条即停；
- **scope 边界**：不越界。

### 6. 长时运行

长时稳定 + 随时打断归 **caller 编排**。grga 单次调用短、原子、幂等；
wiki 文件落盘即检查点——中断恢复 = 读 `run_state.md` + 扫 `members` + 续跑。
LLM 限流 → 单条跳过（幂等）不中断整轮。

---

## 下篇 · 消费：L1 检索时的前置步骤（D11）

**条件触发**：scope 下存在 `dream/<scope>/entities/` 才执行；**无梦境资产
时 L1 流程与现在完全一致**（不加任何步骤）。

### 步骤

1. **触发检查**：`scope` 下有没有 `entities/` 目录（有 `--scope` 就用它；
   没有就检查默认 scope）。
2. **别名消歧 + 查询词扩展**：检索开始前，扫一遍与本次查询相关的实体的
   `members`。例如已知"大史 = 史强"，搜"大史"前先把"史强"纳入查询。
3. **hint 非 truth**：读到的别名 / 关系**必须回原文验证**（`search` /
   `expand`），**不得盲信**。梦境资产只是减少盲区的线索，不是答案。

### 例子

- 查询 `大史`，实体 `史强.md` 的 `members: [史强, 大史]` → 扩展为
  `"大史 史强" --implicit-or`（或 `大史 || 史强`）后再 `search`。
- 查询 `青元剑诀`，实体 `李化元.md` 的关系 `[[韩立]]：师从` → 这是线索，
  **先 `search` 验证**，不直接当作既定事实回答。

### 边界

- `related --use-dream`（让 related 直接读梦境）是**后置独立开关**，当前
  不实现；消费路径只在本 skill 指南层面生效。
- 梦境资产**不进 DB**，删除 / 重命名实体文件即删除资产。
