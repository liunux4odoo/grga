# plan-2026-07-18 · 文档结构查询：是否要新增 file-entry 子命令

> 状态：**部分落地，其余待定**。触发讨论时间：2026-07-18。
> 决策方式：证据触发——积累到跨仓库 / 跨 caller 的重复派生笨拙证据后
> 再回来敲板。
>
> **后续更新（2026-07-18，commit a6eb30c）**：需求 ①（完整 TOC）已由
> `expand --level toc` 落地，无需新增 `outline` 动词；②③（所在 / 上级
> 目录）仍按"派生零成本"由 caller 侧使用 `search.heading_path` 完成。
> 本 plan 剩余待定部分仅剩需求 ③（同级目录）在 caller 侧遍历是否笨拙。

## 1. 触发场景

Caller（用户会话）在 grga skill 上下文里问了三个具体的"目录结构"需求：

1. 拿到单个文档的**完整目录（TOC / 章节树）**。
2. 查询指定行**所在的目录**（章节路径）。
3. 查询指定行的**同级 / 上级目录**（兄弟章节 / 父章节）。

现状盘点（2026-07-18）：

| 需求 | 现有能力 | 是否够用 |
|---|---|---|
| ② 所在目录 | `search` 命中自带 `heading_path`；`expand --level chapter` 返回 `range` | ✅ 派生零成本 |
| ③ 上级目录 | `heading_path` 数组的前缀本身就是各级父标题 | ✅ 派生零成本 |
| ① 完整 TOC | `expand --level toc`（commit a6eb30c）返回整篇大纲，每行标题带 `[start_line-end_line]`；`search '^#{1,6} '` / 读 sidecar JSON / 调 `structure.md.parse_markdown()` 也可 | ✅ 已由 `--level toc` 落地 |
| ③ 同级目录 | 在完整 TOC 上按 `level` + 父章节 `[start_line, end_line]` 区间筛 | ⚠️ 完全靠 caller 侧遍历 |

底层数据两处都有：

- **sidecar JSON**（`.grga/structure/*.json`，`structure/sidecar.py`）——
  一次一文件的完整章节树 + 段落表，由 `expand --level chapter` 与 `search`
  的 `heading_path` 注入按 mtime 懒建。
- **`heading_docs` 表**（`graph/schema.py`，schema v11 证据层）——
  每个 `doc_id` 的章节列表（`heading_path` / `chapter_id` /
  `start_line` / `end_line`），由 `grga index build` 填，`related --mode
  heading` 消费，索引 `idx_heading_docs_head` 优化跨文档 heading 查询。

但项目规则明确挡住"caller 直接 sqlite3 读表"这条路（`SKILL.md`
Caller anti-patterns §3，`AGENTS.md` 反模式家族的元规则）——DB 是内部
存储、不是 caller 契约。

## 2. 待定问题

是否新增一个**以 file 为入口**的子命令族，把"单文档目录结构"派生做成
一等公民？候选形态（未定）：

```
grga outline <path> [-d <dir>] [--json]
  → 返回该文档的完整章节树 (heading_path / level / start_line / end_line / chapter_id)
      — 需求 ① 已由 `expand --level toc` 满足，此形态不再必要

grga outline <path>:<line> --at [--json]
  → 返回该行所在章节（等价于 expand chapter 的元数据切片，不返回正文）

grga outline <path>:<line> --siblings [--parents] [--json]
  → 返回同级 / 上级章节列表
```

## 3. 决策原则回顾（AGENTS.md 信号暴露原则）

按项目哲学的三问顺序：

1. **消费者能从现有返回值派生吗？**
   - ② ③（上级） → **能派生**，`heading_path` 直接给。→ 停在这里。
   - ① ③（同级） → 能派生（`expand --level toc` + 自己组织），但需要
      caller 侧写遍历逻辑；派生**不算零成本**，但也不算笨拙。
2. **是否是"消费者绝对拿不到的内部状态"？**
   - 否。数据从公开输入（Markdown 文件本身）可完全重构。
3. **是否影响热路径动词？**
   - 不影响 `search` / `probe`。若落地放在低频动词侧，符合原则。

**目前不满足强触发条件**：没有跨仓库、跨 caller 的重复"派生笨拙"证据。
只是**单一会话**里问了一次。按"完备 schema 诱惑 / 预留即污染"反模式，
不应仅凭"我觉得他们会想要"就把契约扩大。

## 4. 决策：**① 已落地，其余暂不实施**，进入观察

保持现状：

- 继续引导 caller 用 `expand --level toc`（完整大纲）、
  `search.heading_path` / `expand --level chapter`（所在 / 上级目录）。
- 需求 ③（同级目录）的绕道方案（`expand --level toc` + caller 侧按
  `level` + 父区间筛）保留在 `SKILL.md` 的 FAQ 或本 plan 里，不上升为
  动词。
- **不**建议 caller 直接 `sqlite3` 读 `heading_docs`（违反反模式 §3）。

## 5. 触发升级的条件（证据触发清单）

以下任一成立时，回来重新评估这个 plan：

- [ ] 出现 ≥ 2 个独立 caller / 项目重复实现"单文档 TOC 抓取"派生逻辑，
      且都不约而同做成一样的形态（heading_path + start/end line + level）。
- [ ] 出现"同级章节导航"的重复需求（agent 侧、GUI 侧、或第三方 caller），
      且现有派生路径明显笨拙（例如需要跑两次 `search` + 自己维护父区间
      算法）。
- [ ] GUI / deep-search / 其它内部消费者中至少一个开始重复实现同一段
      派生逻辑——这才是"公共基础设施杠杆方向对了"的证据。
- [ ] 出现明确的性能诉求：单文档 TOC 用 `expand --level toc` 在超大 md
       上慢到不可接受（当前无此证据）。

## 6. 若未来实施，需明确的边界

> 备忘已部分落地：`expand --level toc`（commit a6eb30c）就是本节的
> "更省契约面"路线，已满足需求 ①。以下其余备忘仍然有效。

- **入口动词名**：需求 ① 已由 `expand --level toc` 满足（单选"expand
  新 mode"路线）。若未来仍需覆盖同级 / 上级导航，`outline` 仍是候选
  动词名。
- **存储后端选择**：优先复用 sidecar（单文件冷路径成本低、不受 index
  build 是否跑过影响）；`heading_docs` 表只在 caller 已经在 scoped
  索引里、且需要跨文档信号时被 `related --mode heading` 消费——不应
  为了单文档 TOC 强制走 DB。
- **JSON 契约**：一旦发布即冻结，字段命名需与 `search.heading_path` /
  `expand.range` 保持一致，避免同一概念的不同表达。
- **热路径隔离**：新动词必须落在低频侧，不改 `search` / `probe` 的
  golden test 面积。
- **反模式 §3 不松绑**：即使新增了动词，也不要在文档里推荐"直接读
  `heading_docs`"作为替代路径。

## 7. 相关文档索引

- `src/grga/skills/grga/SKILL.md` — 设计哲学·信号暴露原则的落点。
- `AGENTS.md` — 项目层面的信号暴露原则、反模式家族。
- `src/grga/structure/md.py` / `structure/sidecar.py` — sidecar 后端。
- `src/grga/graph/schema.py` — `heading_docs` 表 DDL（v11）。
- `src/grga/expand.py` — `--level toc` / `chapter` 级 expand 的实现
  （复用 sidecar）。
- `docs/design/plan-2026-07-14-scope-tags.md` — 参考"证据触发新增能力"
  的类似决策格式。
