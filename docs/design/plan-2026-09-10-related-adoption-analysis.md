# Agent 只用 search 不用 related —— 实证案例分析（2026-09-10）

> **类型**：行为实证分析（case study），为 SKILL.md 改进提供证据基础。
> **对应讨论**：`related` 采用率低 / agent 只用 `grga search`（2026-09-10 主会话）。
> **状态**：诊断完成，改进方向已列（§5）；实现留待后续 skill 变更（角色边界：本
> 文档只记 WHAT / 证据，不写 HOW）。
> **数据源**：`default` workspace `history.db`，session
> `ai-developer:to:default:1783727235984:4323944d`（2026-09-09 12:35-17:18，423 条）。

---

## 1 · 案例背景

**任务**：推断《这游戏也太真实了》小说里"国家部门的国字脸男人"是哪位玩家。

- 语料：`/data/ports/sync/temp/这游戏也太真实了.txt`（单文件 TXT，约 830 万字）
- grga scope：`work`（绑定 `/data/ports/sync`，`temp` 为其子目录，小说在索引内）
- 执行者：`default` workspace 的 agent，**用户三次提示"用 grga / 用 related"**

**为什么选这个案例**：它直接回答"agent 为何不用 related"——不是"不知道"，而是
用了、发现输出不符合预期、一次就放弃，退回 grep/search。

---

## 2 · 行为时间线（seq 为 history.db 记录号）

| 时间 | seq | agent 动作 | 工具 | 结果 |
|---|---|---|---|---|
| 12:35 | 955-956 | 首答"来日方长" | **`grep -n "国字脸"`** | 无 grga；结论来自 grep + 推理 |
| 12:43 | 978-980 | 用户提示"用grga"；加载 skill | `sed`+`grep` 切片 | 加载了 skill 仍用 grep |
| 12:53 | 1010-1014 | 用户第二次提示"grga能找人物关系" | `grga search "方长 沪 上海 公司 财务自由"` | **AND 空手**（0 match） |
| | 1013 | `国字脸.*银行\|...` 正则 | 空手 | |
| | 1014 | `grga search -d txt文件` | **报错 "Directory ... is a file"** | |
| 13:25 | 1112 | 用户第三次提示"用grga察人物关系" | 加载 skill，计划 index build → search → expand → related | |
| 13:29 | 1117-1119 | index build **被用户打断**："不需要你做index build，已经手动完成了" | — | |
| 13:29-33 | 1120-1135 | 摸 scope 名（直查 sqlite DB 表 `scopes`）；`related "国字脸"` | `related --scope work` | **只返回"男人"（npmi 0.56）** |
| | 1130 | `related "张海"` | 返回 拉面/捂着/屁股/道 | 质量差 |
| | 1133-34 | **判定"graph 结果太稀疏" → 放弃 related**，退回同框组合搜索 | `grga search`（AND） | 全部 0 同框 |
| | 1135-36 | related（不同 seed）返回 **方长/狂风/老白/夜十 人名邻居** | — | ⚠️ **未被识别为线索，当噪声丢弃** |
| 13:33 | 1143 | `grga search "国字脸 专案"` / `"国字脸 转业"` | AND | 空手 |
| 13:33-16:36 | 1143-1262 | grep/sed 反复挖；用户逐步喂线索（方长沪上、张海嫌疑、零同框、拉面、最后离开） | grep/sed | 无 grga |
| 16:36 | 1263-1307 | 整理证据总表，结论"张海=国字脸" | — | **用户推理主导** |
| 16:51-17:18 | 1271-1347 | 用户反驳/修正（戒烟/狂风/落羽身份） | grep/sed | |

**grga 动词总调用量**：`related` 2 次（其中 1 次还是用户第三次提示后才跑）；
`search` 约 6 次；其余约 10+ 次搜索全部为 `grep` / `sed | grep`。

---

## 3 · 四个病因

### 病因一（核心）· 期望错位 + 不会消费 related 输出

agent 期望 `related` **直接给出"国字脸=谁"的答案**。但 v3 契约输出的是
`RelatedKeyword` 列表（`kw/npmi/cooccur_count/via_seed/chunk_df/source`）——
**契约上就是"线索"，不是"答案"**。

- `related "国字脸"` 返回"男人"（npmi 0.56）→ agent 判定"graph 结果太稀疏"→ 放弃。
- **关键证据**：同一时段 related 输出里出现
  `方长 npmi=0.50 cooccur=412`、`狂风 0.46/154`、`老白 0.46`、`夜十 0.48`——
  这正是任务需要的**人物关系信号**。agent 被"热熔切割斧/龇牙"等噪声词干扰，
  **没识别出人名邻居的价值，也没想到把它们喂回 `search` 验证**。

结论：**不是"不会调用"，是"调用后不会消费输出"**。

### 病因二 · 任务分类失败

任务本质是**实体身份消歧**（哪个玩家 = 国字脸）——正是 07-11 讨论的 L2 alias-check
场景。agent 未做任务分类，当成"关键词查找"处理 → search/grep 优先。用户**三次提示**
"grga可以帮你找出更多人物间的关系"才碰 related。

### 病因三 · 前置技术摩擦形成负面印象（自我实现）

一连串失败固化"grga 无用"印象（agent 在 seq 1113 明确总结："grga search 多词 AND
没用，真正有效的是 grep"）：

1. **多词默认 AND**：`"方长 沪 上海 公司 财务自由"` → 5 个词 AND → 0 match。
   关键线索从不落同一段落，AND 语义天然空手。
2. **`-d` 传文件报错**：`grga search -d txt文件` → "Directory ... is a file"。
3. **related 第一次 Usage error**：不支持 `--directory`，要 `--scope`。
4. **scope 名不可知**：agent 被迫 `sqlite3` 直查 DB 表 `scopes` 才找到 `work`
   （违背 caller anti-pattern #3，但暴露了真实摩擦：**skill 没告诉 scope 名从哪来**）。

失败链 → 负面印象 → 后续连 search 都不愿碰 → 全部退回 grep。**这是最重要的可修摩擦**。

### 病因四 · skill 读了没执行

seq 1116 完整加载了 skill 内容，但决策链是"用户说不用 build → 直接用
search/expand/related 挖"——**没有任何 T2 bookend（session-open / pre-answer
sweep）痕迹**。workflow 章节未被吸收。

---

## 4 · 修正后的结论：三个叠加断点

> 相比 09-10 主会话早段的三个前提（P1 伪归因 / P2 成本摩擦 / P3 注意力扫描），
> 本案例把问题精确为三个**叠加**的断点，不是单选：

| 层 | 现象 | 证据 |
|---|---|---|
| **调用层** | 会调 related（用户提示后），但"输出不符合直接答案预期 → 一次就放弃" | seq 1129→1133 的转向 |
| **消费层（最大盲区）** | related 返回了需要的信号（人名邻居），不会识别、不会二次消费，当噪声丢弃 | seq 1135-36 的人名未被利用 |
| **印象层** | search 前置失败（AND 语义、`-d` 参数、scope 不可知）固化"grga 无用"，后续连 search 都不碰 | seq 1113 的总结 |

三个断点依次触发：前置摩擦 → 印象崩坏 → 唯一一次 related 期望落空 → 完全退场。

---

## 5 · 改进方向（WHAT，非 HOW）

> 以下只列方向与依据；具体 SKILL.md 改写 / CLI 改动留给后续变更（实现是 code
> agent 的活，本文档冻结诊断与判据）。

1. **skill 主流程教"related 输出消费配方"**（对消费层盲区）
   - 拿到 `RelatedKeyword` 列表 → 筛出实体词（人名/专名，`chunk_df` 高者优先）
     → 喂回 `search` 做同框/证据验证。
   - `examples/search-related-graph.md` 已是此配方，但 agent 没读——
     **需提升到主流程显眼位置，而非仅作 example**。

2. **降前置摩擦**（对印象层）
   - 明确"多词默认 AND，OR 用 `\|` 或分开跑"；
   - 明确"txt 单文件 `-d` 传目录不是文件"；
   - 明确"`related` 用 `--scope`；scope 名从 `grga scope list` 拿"——
     消除 agent 被迫 `sqlite3` 直查的摩擦。

3. **期望管理**（对调用层）
   - skill 明说"`related` 返回候选关键字，不是答案；给你线索，自己拼"。

4. **（可选）任务分类 if-then 前置**
   - 身份消歧 / 概念关系 / "还有哪些相关的" → 先 related 再 search；
     字面查找 / grep-like → search。放进 skill 最响的位置（MANDATORY / quickstart 顶部）。

---

## 6 · 决策留痕 / 待确认

- [ ] 改进方向 1-3 是否采纳？采纳后落到 skill 还是 CLI 哪一层？
- [ ] 方向 4 的任务分类 if-then 是否与 T2 bookend 并存、还是替代？
- [ ] 本案例是否足以作为"skill 变更"的触发证据（用户此前要求"先积攒案例"）？
