# grga 梦境整理（dream）开发方案

> **状态**：✅ 已实现（2026-10-22）；`--auto-seed` 已落地（冷启动启发式：高 chunk_df + 非停用词）
> **范围**：grga 只提供**原料导出 + 存储契约**；整理逻辑由外部 caller agent 用自己的 LLM 完成。`related` 读取梦境资产是后置独立开关，不在本方案。

## 背景与定位

grga 核心资产（`keyword_pairs` 等）是纯统计共现对——客观但不含语义：`related` 只能告知"两词常共现"，给不出关系类型；语义判断（同一实体？什么关系？）目前由 caller 自己完成。本方案为 caller 提供可选的 L3 补充工作流：**梦境（dream）**——外部决定时机，caller 用自己的 LLM 从 grga 导出的原料里整理出**实体 + 关系**资产，写成旁路 wiki 文件。

**定位**：辅助 caller 从原文找线索的 L3 补充，不是完备知识图谱。

**关键边界**：梦境由外部执行 → **不用 grga 内部 LLM**（`GRGA_OPENAI_API_KEY` 等），grga 只做"导出原料 + 定义存储契约"，否则等于 grga 自己在产出侧做语义判断。**废除内置 `grga index dream`** 的双重理由：①哲学——grga 不自己做语义判断；②可靠性——否则长时稳定/打断/失败恢复全成 grga 的 checkpoint/限流/崩溃恢复负担（见 §长时运行）。

## 设计决策（唯一决策出处）

| # | 决策 | 要点 |
|---|---|---|
| D1 | 资产类别 | MVP 只做 **Entity + Relation**；Fact/Belief 留扩展位；不做属性章节（边界模糊，搁置） |
| D2 | 存储形式 | **wiki 文件非数据库**（可手改、可 diff、DB 零改动）；实体一文件，关系用 `[[链接]]` 内嵌 |
| D3 | grga 角色 | 只提供原料导出 + 存储契约；`grga index dream` 废除，零 LLM 调用 |
| D4 | 整理者 | 外部 caller agent，用自己的 LLM，按 skill 指南整理 |
| D5 | 原料导出 | 独立命令 `grga index export-candidates`（内部复用 related 邻域 + expand 上下文），合成 JSONL 候选包；不进 `related`，检索侧零负担 |
| D6 | 去重 | `members` 命中跳过；`checked.md` 记录查过未入梦短语 |
| D7 | 结束机制 | 硬上限/软收敛/scope 边界，三层 OR，任一即停 |
| D8 | 读原文 | 允许 caller LLM 读原文：`related` 筛候选(L2) → `expand` 取上下文(L1) → LLM 判断(L3)；Relation 必读、Entity 轻读 |
| D9 | 执行时机 | 外部决定，grga 不自动触发 |
| D10 | 核心动词 | search/probe/expand/related 行为不变 |
| D11 | 消费方式 | skill 引导 caller 在 **L1 检索开始前**条件性读取梦境资产（别名消歧 + 查询词扩展）；作为 **hint 非 truth**，须回原文验证；无梦境资产时 L1 流程不变 |
| D12 | 产出克制 | caller 整理时**只产出高置信、必要的实体/关系**，不过度扩展（不追求单次覆盖整个 scope）；宁可多次小规模、不要单次大规模，控制单次 LLM 成本 |
| D13 | 置信度迭代 | **迭代读取已有资产**（members 去重 + confidence 更新 + 关系补全）；confidence **可正常增减**，但每次变动必须伴随**正反证据记录**（实体文件 body"## 证据"小节）；**下调必须有反证**，无证据不允许；证据数量**不设固定值，由 caller 自行判断**（保持记录精简即可） |

## 契约

### CLI：`grga index export-candidates`

```
grga index export-candidates --seeds "韩立,李化元" --scope work \
     --top 20 --out candidates.jsonl [--max-context-chars 2000] [--cross-scope]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--seeds` | 无 | 初始概念集，逗号分隔 |
| `--top N` | 20 | 每 seed 取 top-N 邻域（npmi 排序） |
| `--out` | stdout | 输出路径（JSONL） |
| `--max-context-chars` | 2000 | 每个候选对原文上下文上限 |
| `--scope` | 无 | 范围；`--cross-scope` 才跨 |
| `--auto-seed N` | 无 | 冷启动自动选 N seed（可选，不阻塞 v1） |

**边界**：`--seeds` 与 `--auto-seed` 互斥；默认 scope 内，越界即止。

### 原料包格式（JSONL，每行一个候选对）

```json
{"seed": "韩立", "candidate": "李化元", "npmi": 0.42, "cooccur_count": 87,
 "source_kws": ["韩立", "李化元"], "scope": "work",
 "context": "韩立拜入李化元门下，习得青元剑诀……"}
```

`context` = `expand --level line` 命中行附近片段，截断到 `--max-context-chars`。grga 不消费此文件，只负责产出。

### wiki schema（caller 遵循）

实体文件 `dream/<scope>/entities/<name>.md`：

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

front matter 7 字段：`name`(主名) / `members`(别名组=去重核心) / `confidence`(LLM 自评信任锚) / `source_kws`(支撑共现对) / `scope` / `kind`(人物/组织/概念/地点) / `updated`(版本感知)。

**置信度证据（D13）**：confidence 变动必须伴随正反证据记录，放实体文件 body 的 `## 证据` 小节（不进 front matter，保持 7 字段精简）：

```markdown
## 证据
- [run_01] + 韩立/韩天尊 同段共现 8 次（source: 韩立, 韩天尊）
- [run_02] - 韩天尊 在 207 章仅指称号非别名（source: 韩天尊）
```

- 每条 = `run + 方向(+/-) + 一句话理由 + source_kws`，一句话不写小作文
- 下调必须有反证条目；无证据的裸数字改动不允许
- **证据数量不设固定上限，由 caller 自行判断**（保持记录精简，可聚合计数如 `+3 / -1`）

关系规则：**双向**（韩立.md 有 `[[李化元]]：师从`，李化元.md 应有 `[[韩立]]：弟子`）；谓词必须读 `context` 判断；端点无文件时可只存链接。

### 目录布局（旁路，DB 零改动）

```
<dream 根目录>/          # 建议 <grga data>/dream/
  README.md              # 最近整理状态
  checked.md             # 查过未入梦短语（扁平，每行一个，≤10k 行滚动）
  <scope>/entities/*.md
```

## caller 工作流（skill 指南：生产 + 消费）

### 生产：梦境整理

1. 导出：`grga index export-candidates ... --out candidates.jsonl`
2. **先读已有资产**（迭代，D13）：读 `entities/` 现有 members 与证据，避免重复归并
3. 用自己的 LLM 整理：读每行 → 归并实体进 `members` → 判断关系输出谓词（必须读 `context`）→ 低置信/无价值追加 `checked.md`
4. 落盘：按 wiki schema 写实体文件，关系双向补全；**confidence 变动同时追加 `## 证据` 条目**（下调必须带反证）
5. 去重：`members` 内词跳过；`checked.md` 已有短语不再整理
6. 结束：软收敛（一层新增高置信 <3 条即停）/ 无价值剪枝 / caller 步数上限；新实体作新 seed 再导出
7. **产出克制（D12）**：只落高置信、必要的实体/关系；不追求单次覆盖整个 scope，按 seed 分批小规模推进；可留待下次再扩，勿贪多

### 消费：L1 检索时的前置步骤（D11）

1. **条件触发**：scope 下存在 `dream/<scope>/entities/` 才执行；无梦境资产时 L1 流程与现在完全一致
2. **动作**：检索开始前扫一遍相关实体的 `members`，做别名消歧 + 查询词扩展（如已知"大史 = 史强"，搜"大史"前先纳入"史强"）
3. **原则**：梦境资产是 **hint 非 truth**——读到的别名/关系必须回原文验证（`search`/`expand`），不得盲信

## 长时运行与打断

**责任划分**：长时稳定 + 随时打断归 caller 编排；grga 只保证单次调用短、原子、幂等。

**grga 三保证**：短调用（跑完即退）；原子输出（临时文件→rename，Ctrl-C 无半截 JSONL，不持跨调用 DB 写事务）；幂等（只读不写 DB，可安全重放）。

**caller 产物即检查点**：wiki 文件落盘即进度记录，重跑靠 members 去重天然跳过；`run_state.md` 持久化种子队列（当前待处理/已跑轮次/累计产出），中断恢复=读 run_state + 扫 members + 续跑。

**失败对策**：LLM 限流→单条跳过（幂等）不中断整轮；上下文溢出→按批处理+落盘清 history；种子爆炸→三层结束+队列上限；进程被杀→产物即检查点续跑。

## 硬约束

1. **核心 DB 零改动**：不建新表、不改现有表；梦境资产只写文件系统旁路
2. **检索侧零负担**：search/probe/expand/related 契约一行不改、无新分支、无新 I/O；`index export-candidates` 是独立新命令
3. **grga 不调用 LLM**：零 LLM 调用（不读 `GRGA_OPENAI_API_KEY`）；整理 LLM 全在 caller 侧
4. **grga 只搬运不判断**：只拼原料包，归并/谓词/置信度全是 caller LLM 的事
5. **wiki 文件可手改**：纯文本 markdown，用户编辑后 caller 下次读取生效；front matter 解析失败报错指明文件
6. **原子 + 幂等**：临时文件→rename 写入；同 seed 重复调用结果一致，可安全重放

## 验收标准

1. 导出命令产出合法 JSONL，每行含全部字段，`context` 已截断
2. 不运行导出时，`grga related`/`grga search` 输出逐字节一致（回归）
3. 无 LLM 凭据也能跑；caller 拿到的原料包可被其 LLM 消费
4. `--seeds` 与 `--auto-seed` 互斥报错；`--scope` 限定范围；`--cross-scope` 才跨
5. 按 skill 指南 caller 能独立完成小规模整理（2 seed → 实体文件 + checked.md），产出符合 wiki schema
6. Ctrl-C 打断导出：`--out` 文件不存在或内容完整；DB 无残留锁，同参数重跑一致
7. 跑完导出后 `related`/`search` 输出与之前完全一致（DB 未变）
8. 消费路径可执行：scope 有梦境资产时，caller 按 skill 指南能在 L1 检索前完成别名消歧（如"大史"→ 扩展"史强"）；无梦境资产时 L1 流程无任何额外步骤
9. 产出克制：单次小规模整理（`--seeds 2`）产出的实体数保持克制（低置信/重复的不落盘），不会一次膨胀出大量低质量实体
10. 迭代 + 证据：第二轮梦境（不 `--force`）不会重复处理已入梦实体；confidence 变动伴随 `## 证据` 条目，下调有反证、无证据不改动；证据数量由 caller 判断，无固定上限

## 边界（D 表之外的排除项）

- ❌ `related` 读取梦境资产：后置独立开关（如 `related --use-dream`），届时"多查一层文件"，不涉及架构改动
- ❌ 强制 L2 实现：属检索思路/纪律（SKILL.md 已体现），不在方案；梦境目标 1 降低强制成本，二者互补但互不依赖
- ❌ 导出 complementary 候选：complementary 仍观察验证阶段，只导出 related 正侧
- ❌ 资产校验/质量打分：延续不评价原则，`confidence` 是 caller LLM 自评
- ❌ 长时编排/checkpoint/限流退避/崩溃恢复：caller 责任（§长时运行）

## 待确认项

1. dream 根目录位置：建议 `<grga data>/dream/`
2. `--max-context-chars` 与 expand（`--level line`）映射：code agent 按现有契约实现
3. 双向关系相反谓词由 caller LLM 生成（师从/弟子语义不同）还是程序反查
4. `--auto-seed N` 是否 v1 就做：倾向后置（先 `--seeds` 跑通流程）

---

## 实现状态（2026-10-22）

**已实现**，范围 = D5（原料导出）。

- **后端** `src/grga/export_candidates.py::export_candidates(...)`：
  - 邻域直接复用 `related._related_keywords_impl`（同 seed/scope/no-fusion
    语义，`--scope` 过滤、`--cross-scope` union）；
  - `context` 按 `expand --level line` 语义从 seed 的最佳证据文档取命中行
    附近 ±3 行片段，截断到 `--max-context-chars`；文档读取按
    `(scope_id, kw_id)` 缓存（同 seed 的所有候选共享一次读盘）；任何失败
    静默降级为 `""`；
  - `--auto-seed N` 已落地：scope 内按 `chunk_df` DESC 选 N 个种子，排除
    单字 token 与 `_INDEX_STOPWORDS` / `_COMPLEMENTARY_STOPWORDS`；
  - JSONL 行 schema 与方案一致（seed / candidate / npmi / cooccur_count /
    source_kws / scope / context）。
- **CLI** `cli.py::index_export_candidates_command`：`--seeds`/`--auto-seed`
  互斥（exit 2）；`--scope` 必填除非 `--cross-scope`（exit 2）；`--out` 原子
  写入（同目录 temp + `os.replace`，Ctrl-C 无半截文件）；stdout 为默认。
- **保证**：零 LLM 调用（不读 `GRGA_OPENAI_API_KEY`）；核心 DB 零改动
  （纯 SELECT，导出前后 DB 字节一致）；幂等可重放；检索动词零改动。
  （2026-10-23 加固：`--db-path` 指向不存在的库时直接报错 exit 1，绝不
  创建空 DB —— 缺失索引是"无信号"，不是写触发；`--seeds` 与 `--auto-seed`
  都缺时 exit 2。）
- **测试** `tests/test_export_candidates.py`（13 项）：schema 完整、context
  截断、scope 限定、cross-scope union、mutex、auto-seed、DB 字节不变、
  幂等、`--out` 无 temp 残留、unknown seed 报告、后端直调。
- **Skill 指南**（2026-10-23 补）：`src/grga/skills/grga/references/dream.md`
  新增「生产 + 消费」两篇——生产（导出 → 先读资产 → LLM 整理 → wiki 落盘 +
  `## 证据` → 去重 → 三层结束 → 产出克制）+ 消费（L1 前条件性读
  `dream/<scope>/entities/` 做别名消歧，hint 非 truth，无资产则 L1 流程不变）；
  `SKILL.md` 前端描述、Supporting commands（`export-candidates`）、
  References 表同步。顺带修正 skill 参考里互补对（`complementary`）的陈旧
  描述（迭代 3 已从 `probe`/`related` 输出移除）。

**未实现（按方案边界排除）**：wiki schema 落盘（caller 责任）、
`checked.md`/`run_state.md`（caller 责任）、`related --use-dream` 后置开关、
complementary 候选导出、资产校验。
