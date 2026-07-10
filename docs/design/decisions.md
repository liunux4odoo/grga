# Design decisions log

Records deviations and explicit decisions made outside the main plan
documents. Each entry names the plan step it relates to, the decision,
and the rationale.

## 2026-07-11 · Caller-first 定位精修与 `related` API 契约扩展

**Plan step**:2026-07-10 postmortem 之后与外部旁观者(小Q agent)对话
沉淀出的多项精修,以及用户对"L2 自省"边界的修正。

**Decision**:

1. **README 顶层从"侦探比喻"改为"两句话操作性框架"**:面向 caller 的
   文档不做心智锚点,直接讲 what you get + what's on you。侦探比喻和 K
   线图比喻沉入 `docs/design/` 作为**给贡献者的护栏**
2. **v2-design.md §1.2** 侦探比喻保留但重命名"心智模型 A(给未来贡献者):
   为什么 grga 不做 rerank"
3. **v2-design.md §1.3 新增 K 线图比喻**(心智模型 B):回答"grga 输出如何
   被消费" —— top-K 邻居的分布 = K 线形态,caller 自行识形
4. **v2-design.md §1.5 新增两条命名铁律**:
   - 命名规则:事实字段安全,评价化字段陷阱
   - 最严边界:工具不评价任何东西,包括不评价自己的输出
5. **v2-design.md §4.0 新增 K 线图厚书本心智模型**(心智模型 C):把
   L1/L2/L3 从"能力递进 fallback chain"改讲成"同一件事(信号补足)在
   不同代价档位下的实现"
6. **v2-design.md §4.5-4.7 追加**:Hub seed 结构性属于 L3 范畴 / L3 使用
   规约 / caller 必须能识形
7. **v2-design.md §7.3 `related()` 返回值扩展**:每邻居完整
   `(term, co_count, df, df_ratio, npmi)` 元组;新增 `result_shape` 描述
   性形态字段(`top_k_dropoff_ratio` / `neighbor_df_median` / 可选
   `neighbor_count_series`);默认 limit 从 20 提到 100
8. **不新增 `graph diagnose` 独立命令**:通过 `related --stats=full` 参数
   控制字段丰富度,不需要新命令
9. **postmortem §4.5 追加"能力诚实 reframe"**:实验结论驱动哲学,不是
   哲学驱动限制
10. **postmortem §4.6 追加 df 接口决策精修**:对 seed 的诊断字段(如
    `seed_is_hub`)仍不做;对返回结果的形态统计要做,两者是不同性质

**Rationale**:

- **外部旁观者的价值**:2026-07-10 深挖八小时后,让没参与讨论的 assistant
  agent(小Q) test 一遍,一开口就指出多个盲区(比喻的"扩野" vs "放大"、
  "能力诚实"reframe、命名决定 banner 命运、心智模型对消费者不必要)。
  **深度参与者盲区结构性无法自愈,必须外部对齐**
- **Banner 化机制精修**:昨晚归因为"关于输入 vs 关于输出",今天压测后修正
  为"处方性 vs 描述性密度"。命名决定 banner 命运,事实字段永远安全
- **用户对边界的最深修正**:即使"L2 自省"这类听起来技术客观的表达依然
  是评价姿态。抗 banner 最严边界是**工具不评价任何东西,包括不评价自己
  的输出** —— 只汇报事实和描述性数字

**Impact**:

- README 顶层大幅收敛(不到 10 行)
- v2-design.md §1 新增 §1.5 命名铁律章节;§4 新增 §4.0/4.5/4.6/4.7 四节
- postmortem 新增 §4.5/4.6 两节
- `related()` API 契约变化(需 CHANGELOG 关注):默认 limit 20→100,返回
  结构从 `list[Related]` 改为 `RelatedResult{neighbors, seed_stats,
  result_shape}`
- Workspace `AGENTS.md` 同步沉淀了 4 节跨项目通用铁律

**Files**:
- `README.md`(顶层设计哲学节)
- `docs/design/v2-design.md`(§1.2/1.3/1.5/1.6, §4.0/4.5/4.6/4.7, §7.3)
- `docs/design/graph-l2-postmortem-2026-07-10.md`(§4.5/4.6 新增)
- `docs/design/README.md`(索引更新)

---

## 2026-07-10 · L2 co-occurrence graph: hub handling + df metadata interface

**Plan step**: L2 关键字共现图扩展的邻居质量改进,以及为 caller 提供 hub
判定能力(候选新增 `graph nodes` 接口暴露 per-scope df)。

**Decision**: **不在共现关系图 + 单一 ranking 框架内继续救 hub;不做 df 元
数据接口**。V5_npmi 在非 hub seed 上的 caller-first 结果(top-5 Useful%
均值 87.5%)可作为独立改进考虑进入主干,与本次 hub 决策解耦。

**Rationale**: 四轮实验(V0/V1-3 行窗口, V4 章节窗口, V5_npmi 归一化) +
α 手工推演(6 seed × 3 caller archetype)提供分类性证据:

1. 共现关系图 + 单一 ranking 结构性无法救 hub —— 四种变量组合(raw / DF
   剪枝 / 章节窗口 / npmi)都让 hub 邻居退化为高频虚词或独占叙述词
2. df 元数据接口 α 推演净收益仅 7.7%,且在 domain 词场景下 -2000 token
   负收益 —— df 单维度**结构性无法区分** hub(主角)vs domain(语域词)
3. 甜蜜点场景(极端 hub / rare seed)caller 用现有工具就能识别,不需要新
   接口

**Impact**: 详细复盘、决策路径、数学证据、拒绝的候选方案、重启前置条件全部
沉淀在 `docs/design/graph-l2-postmortem-2026-07-10.md`。未来重启此话题前必
读该文档 §7。

**Files**:
- `docs/design/graph-l2-postmortem-2026-07-10.md`(本决策的完整复盘)
- `experiments/cooccur_2026_07_10*/`(三轮实验目录,accept 全过)

---

## 2026-07-09 · `--prefer-system` CLI coverage

**Plan step**: `PLAN-0.2.0-followup.md` Task 3 lists `search / deep-search
/ probe / expand / related / suggest / tools install` for the new
`--prefer-system` flag.

**Decision**: Added `--prefer-system` to the commands that actually
resolve external tools:

* `grga search`
* `grga deep-search`
* `grga probe`
* `grga tools check`

Skipped `expand`, `related`, `suggest` (they operate on the SQLite graph
DB / local files / LLM only — no `rg` / `rga` invocation, so the flag
would be a no-op that lies about behaviour). Skipped `tools install` /
`tools update` because those commands unconditionally target the managed
cache; a system-preference has no meaning there.

**Rationale**: A CLI option that silently does nothing on some commands
is worse UX than a targeted flag that only appears where it matters. The
underlying `runtime_env.prefer_system_tools()` + `GRGA_PREFER_SYSTEM=1`
still provide a global escape hatch for anyone who wants to force system
resolution across every command (including the graph/LLM subcommands, in
case future refactors ever route tools through them).

**Impact**: Documented; tests exercise the flag on `grga search` and
`grga tools check` and additionally verify the env-only path.

## 2026-07-09 · Skill install exit-code contract

**Plan step**: `PLAN-0.2.0-followup.md` Task 1 asked whether the JSON
mode should emit a second `emit_error` payload when a skill already
exists.

**Decision**: JSON mode stays with a single top-level `reports` array
carrying the per-skill `status` (`installed` / `exists`). The process
exit code (`3` for any `exists` without `--force`, `0` otherwise) is the
only success/collision discriminator; the JSON payload is not duplicated
by `emit_error`.

**Rationale**: `reports` is already structured and richer than a flat
error payload — agents can inspect which specific skill collided and
which succeeded. Duplicating that as an error object would force
consumers to reconcile two shapes.

**Impact**: Documented at the docstring of
`cli.skill_install_command`; covered by
`tests/test_skill_install_cli.py`.
