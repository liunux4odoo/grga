# Design decisions log

Records deviations and explicit decisions made outside the main plan
documents. Each entry names the plan step it relates to, the decision,
and the rationale.

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
