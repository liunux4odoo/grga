# Design decisions log

Records deviations and explicit decisions made outside the main plan
documents. Each entry names the plan step it relates to, the decision,
and the rationale.

## 2026-07-12 · related 信号契约 v2（撤 trace-signals，改 expansion + keyword_contributions）

**Plan step**：`related --json` 的信号契约重构。完整规范见
`docs/design/related-signals-v2.md`，旧契约归档于
`docs/design/deprecated/trace-signals.md`。

**Decision**：移除 trace-signals v1（`trace_signals_version` +
`graph_actions` op log）与其 `suggest` 侧的 `llm_propose`，改为聚焦
"caller 关联判断证据"的两块新信号：

1. `related --json --mode graph` 顶层新增 `expansion`：
   `{seeds, expanded_keywords[{kw, ppr_score, hops_from_seed?}]}`——查询级
   PPR 扩展状态。
2. 每条 graph 模式 `results[]` 内嵌 `keyword_contributions`：
   `[{kw, occurrences}]`——文档级 per-kw 命中明细。
3. `related --mode dir|heading` 与 `suggest --json` 去掉 trace envelope。

**Rationale**：

- **信号暴露原则**（AGENTS.md）：旧契约里 `file:` 级 `score_node` =
  `related[].score`（重复），`keyword_doc` 边权 = 关键字级 PPR 分 ×
  `keywords`（派生），`llm_propose` = `Suggestion.keywords`（派生）。三者都是
  caller 能自己派生的，按第 1 条不该占共用层。真正拿不到的只有 PPR 分、
  BFS hops、per-doc-per-kw occurrences 三样。
- **用途错位**：旧契约瞄准"图游走可视化"，但生产实测 caller 的痛点是
  "判断 related 结果是不是噪声"（稀有种子经 PPR 扩展拉入通用词命中的假阳性，
  如 `苍坤` seed 拉入《三体》）。linear op log 无法表达这种证据；per-doc
  命中矩阵 + 查询级扩展表可以，caller ~5 行即可过滤。
- **热路径保护不变**：`search`/`probe`/`expand`/`deep-search` 的 `--json`
  仍一字节不动。

**Impact**：

- **BREAKING**：`trace_signals_version` / `graph_actions` 从
  `related --json` 与 `suggest --json` 移除；graph 模式增 `expansion` +
  `keyword_contributions`；dir/heading/suggest 去 envelope。pre-1.0 直接
  声明破坏性，无兼容路径。
- 删除 `src/grga/trace.py`、`schemas/trace-signals-v1.json`、
  `schemas/trace-viz-session-v1.json`；新增 `schemas/related-signals-v2.json`。
- 移除依赖 `graph_actions` 的两个内置 skill 工件（`skills/grga-trace-viz/`
  与 `skills/grga/workflows/trace-viewer/`）。
- 性能：删 `_build_graph_trace` BFS-tree 重建，改为轻量 `_compute_hops`；
  `keyword_contributions` 复用 doc-agg 已有 rowmap，零新增 SQL。

**Files**：
- `docs/design/related-signals-v2.md`（新规范；v2.1 同日 revision）
- `docs/design/deprecated/trace-signals.md`（归档 + 撤销说明）
- `src/grga/related.py`（`KeywordContribution` / `expansion` / `_compute_hops`
  / `_chunk_df_for_kws` / `_edges_by_neighbor`）
- `src/grga/suggest.py`、`src/grga/cli.py`（去 trace envelope；v2.1 去 `--graph-signals-limit`）
- `schemas/related-signals-v2.json`、`tests/test_related_signals.py`（替代 `test_trace_signals.py`）

### 2026-07-12（同日）· v2.1 · graph_signals 折叠进 expansion

**触发**：v2.0 落地后 caller 反馈两处冗余：
1. `graph_signals.neighbor_edges[].a` 恒等于 seed，纯回显 caller 输入；
2. 关键字级信号散落在三处 —— `expanded_keywords[].ppr_score` /
   `neighbor_edges` 里的 `{npmi, cooccur_count, source}` / 无处的
   `chunk_df` —— caller 要 join 三个数组才能拼出一个关键字的完整档案。

**决定**：把所有关键字级图信号一体化到 `expansion`：
- `seeds: List[{kw, chunk_df?}]`；
- `expanded_keywords[i]` 增 `chunk_df?` + `edges_to_seeds: [{seed, npmi,
  cooccur_count, source}]`（每条邻居自带回 seed 的直接边集）；
- `total_chunks_in_scope` 从 `graph_signals` 提升到 `expansion` 顶层；
- `Related.graph_signals` 整块删除（原是查询级常量在每条 Related 里的
  引用重复）；
- `--graph-signals-limit` CLI 参数删除（`expanded_keywords` 由 PPR
  `top_k = max(limit*3, limit)` 自然限长，不需要额外旋钮）。

**关键增值**：`chunk_df` 是 hub 词识别的直接信号。三体假阳性场景里
`上古` 的 `chunk_df ≈ 45k` vs `苍坤` 的 `chunk_df = 3`，比例本身就是
最强判据；caller 三行 Python 即可过滤。

**HC2 合规**：`chunk_df` 是既有 `keyword_chunks` 的 `COUNT(*) GROUP BY`
派生，零新语料先验。查询次数无净增（seed_chunk_df 那次 batch 里顺带查
neighbours）。

---

## 2026-07-12 · `related --mode graph` 性能优化(冷 3s / 热 200ms)

**Plan step**:生产 `related --mode graph` 冷启动 109.7s / 热启动 48.1s 的性能治理。
完整规范见 `docs/design/plan-2026-07-12-related-perf.md`,实验证据见
`experiments/related_perf_2026_07_12/`。

**Decision**:7 项独立优化叠加,按落地优先级:

1. **P2.3 doc-agg id-native**(最大单项收益 -78%):`aggregate_docs` 的
   `k.kw IN (文本)` 换成 `kw_id IN (整数)`,直接命中 `keyword_chunks` 复合主键
   `(kw_id, doc_id, chunk_id)`,doc_agg 从 11s → 53ms(实测 SQL 单查询 279×)。
2. **P3 numpy CSR + `np.bincount` backend**(-46%):PPR 35s → 96ms;
   numpy 作 optional-dependency(`grga[perf]`),不进 install_requires,无 scipy。
3. **P1.2 min_npmi 默认 0.2**(-38%):边 310 万 → 43 万;且质量提升——
   npmi=0.1 的 PPR top-k 被功能词污染,0.2 全是语义词。
4. **P2.2 id-native 图构建**(-5%,P2.3 前置):加载边直接用 int id,
   跳过 JOIN + casefold,`IndexedGraph.node_ids` 仍填 str(契约零改动)。
5. **P2.1 删热路径 `build_adjacency`**:从不使用的死代码,生产 310 万边省 ~2.7s。
6. **P1.1 stopword ∪ core_boundaries**:索引层复用已有 26 字 `_CORE_STOP_BOUNDARIES`,
   零新增语料先验;收益小(-10%),定位为质量兜底而非性能主力。
7. **P1.4a `edge_quantile`**:min_npmi 的便捷入口糖,opt-in,与 min_npmi 互斥。

**Rationale**:

- **实验催生新阶段**:P2.3 不在最初三阶段方案(P1/P2/P3)里。它是 v2 阶段(PPR 用
  numpy 加速后)才暴露的新瓶颈——doc_agg 从"被 PPR 掩盖"变成占比 90% 的头部成本
  (Amdahl's law"尾巴变头")。方案设计必须允许多阶段优化中途增补,不能一次性 freeze。
- **code review 的价值**:v1 首跑后 review 发现 6 处测量偏差(warm_p50 实为 max、
  numpy tie-break 与 py 不一致、CSR 构建时间误计入 PPR、`np.add.at` 慢 10-100 倍、
  id_aggregate/fold_aggregate 时序不对称、stopword 过滤层不对称)。修正后重跑,
  backend 等价性从"可能虚假的 1.0"变成真 1.0,单项归因数字才可信。
- **HC2 无语料先验合规**:全部优化是数学信号(npmi 显著性、已有词表复用、SQL 执行
  计划、算法后端替换),无新增语料先验。
- **P4 Forward Push 归档**:PPR 已 96ms,局部扩散算法不需要,归 `docs/design/future/`。

**Impact**:

- 冷启动 68s(bench)→ 3.1s(-95.5%),生产热启动 adjacency_cache 命中后预期 ~200ms
- top-20 doc Jaccard = 1.0(P2.3/P2.2/P3 全保序),backend 等价性 1.0
- 契约:`graph_signals.neighbor_edges` 门槛语义微变(npmi≥0.2),trace-signals.md 补版本说明;
  其余字段字节级不变
- 依赖:numpy 加 optional-dependency,不进必需依赖

**Files**:
- `docs/design/plan-2026-07-12-related-perf.md`(新增,完整规范 + 8 阶段落地清单)
- `experiments/related_perf_2026_07_12/`(实验目录:SPEC/QUESTIONS/FINDINGS + bench + 11 变体数据)
- 代码层待落地:`src/grga/graph/ppr.py`(load_edges_indexed + numpy backend)、
  `src/grga/related.py`(doc-agg id-native + min_npmi 透传)、
  `src/grga/graph/adjacency_cache.py`(删 build_adjacency)、
  `src/grga/graph/build.py`(stopword union)、`src/grga/cli.py`(--min-npmi/--edge-quantile)

---

## 2026-07-11 · 增量 build / rebuild / forget + 知识资产分层

**Plan step**：`grga index build` 的增量化需求 + `auto_ngram_dict` /
`keyword_pairs` 生命周期定性。完整规范见
`docs/design/plan-2026-07-11-build-vs-rebuild.md`。

**Decision**：

1. **本体论**：知识 = 识别专名的能力（词汇表），不是语料的一比一记忆。
   `auto_ngram_dict` 是系统里**唯一资产**；`keyword_pairs` 共现关系是
   能力作用于当前语料的**观察记录**，属派生层。
2. **三层分工**：资产层 `auto_ngram_dict`（只增不减，仅 `forget` 可删）/
   索引层 `chunks`+`keyword_chunks`+`heading_docs`（与磁盘严格同步）/
   派生层 `keyword_pairs`（每次 build/rebuild 全量重算，cooccur+npmi 同步
   物化，查询直读）。
3. **四动词**：`build`（默认增量，词汇表只增，`--refresh-ngram=append`）/
   `rebuild`（保留词汇表校准索引，`--refresh-ngram` 默认 append）/
   `forget --phrase X`（遗忘短语 + 级联清理，衰减最简版）/
   `forget --all`（核弹，= 旧 `build --replace`）。
4. **文件变化三级判定**：`(size,mtime)` 快过 → rga 输出 `content_hash`
   慢过 → 变了才重分词。`docs` 表加 `size/mtime/content_hash`，
   `SCHEMA_VERSION` 6→7。
5. **mtime 以原文件为准、content_hash 以 rga 输出为准**：索引层消费的是
   rga 输出，判定基准必须是输出而非原字节（PDF 改元数据但抽取文本不变 →
   正确跳过）。
6. **工具链变化只提醒不强制重验**：`toolchain_fingerprint` 变化时 emit
   warning，用户自行 rebuild。强制重验成本确定、收益概率性，杠杆反了。
7. **npmi 是派生层不是快照**：归一化基线 `n_chunks` 随语料漂移，每次全量
   重算保证与索引层一致，避免"用过期世界模型判新事实"。

**Rationale**：

- **杠杆方向**：往共用基础库加负担、给 N 个 caller 减负是反杠杆。共现关系
  可从 `keyword_chunks` 纯 SQL 派生，不必存为独立资产（存储零增量）。
- **心智模型最简**：只有一个资产（词汇表），其他都是当前状态的派生。C4
  立场（rebuild 只保留词汇表）避免了 C1/C2/C3 的幽灵证据与 counter 桶
  存储代价。
- **拒绝 per-doc 检测的理由被修正**：三条理由两条不成立（见主文档 §13.1）；
  增量 build 用 per-doc 局部聚合是有意识的代价，被"词汇表只增不减"吸收。
- **未来衰减机制**作用点在资产层，复用 `forget` 级联路径，与 rebuild 正交。

**Impact**：

- 新增 `docs/design/plan-2026-07-11-build-vs-rebuild.md`（633 行完整规范
  含 §13 决策推理链）。
- `docs/design/tokenization.md` §5 重写为三层模型（资产/索引/派生）+ 级联
  规则 + 增量代价 A/B。
- `experiments/autongram_2026_07_11/QUESTIONS.md` NQ4 部分限定（全 scope
  聚合退居 rebuild 路径，增量 build 用局部聚合）。
- 代码层待落地：`schema.py`（v7 迁移）、`build.py`（增量核心 + 级联 +
  pair 重算）、`cli.py`（build 增量默认 + rebuild/forget 子命令）。

**Files**：
- `docs/design/plan-2026-07-11-build-vs-rebuild.md`（新增）
- `docs/design/tokenization.md`（§5 重写）
- `experiments/autongram_2026_07_11/QUESTIONS.md`（NQ4 更新）
- `docs/design/README.md`（索引更新）

---

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
