# Design decisions log

Records deviations and explicit decisions made outside the main plan
documents. Each entry names the plan step it relates to, the decision,
and the rationale.

## 2026-07-13 · build 期写入门槛自适应（Gate 1 独立性 + Gate 2 阶梯）

**Plan step**：resolved_seeds 探针发布后从 caller 视角复盘"已知但孤立"的
高频触发场景（小语料上 `total_occurrences=1` 或 low-count pair 全部被
`min_pair_count=5` 砍空）。用户反馈两条硬约束：build 时同一目录内文件
大小分布很宽（单一参数覆盖不了）；caller 已从 build 参数声明中移除
（只能调检索参数）。

**Decision**：`grga.graph.build._finalize_pairs_from_temp` 与
`grga.graph.schema._prune_low_score_pairs` 从 v3.2 起改用三门槛结构：

1. **Gate 1 · 独立性（无参、恒开）**：`df_a == df_b == co`（chunk 级三元
   相等）。两个词若在 chunk 级完全绑定（每次出现都带着对方），其 npmi
   蜕化为频率无关的 1.0（`p_ab == p_a == p_b`），是纯 artefact。用**严格
   三元相等**而非 `co >= max(df_a, df_b)`：`mode='build'` 增量构建的
   `cooccur_count` 是 grow-only（D6），而 `chunk_df` 每次重算，doc 更新
   后会出现 `co > min(df_a, df_b)` 的**合法单向依附**——`co >= max` 会
   误杀它，三元相等不会。此判据补住了 pair-count floor 单独无法防的低频
   npmi 虚高（各 1 次同现 = npmi=1.0 = 满分噪声）。保留"单向依附"
   （rare 词只跟 common 词同现）——这类是真信号（专有名词的典型行为）。
2. **Gate 2 · 频次下界（scope 自适应阶梯）**：
   `resolve_min_pair_count(n_chunks)` 阶梯——
   `≤ 200 → 2` / `≤ 2000 → 3` / `≤ 20000 → 4` / `> 20000 → 5`。
   `min_pair_count=None`（新默认哨兵 `ADAPTIVE_MIN_PAIR_COUNT`）触发；
   显式 int 覆盖阶梯（兼容旧 CLI 用法与测试 fixture）。
3. **Gate 3 · npmi 下界（不变）**：`BUILD_MIN_NPMI = 0.2`。语义关联门槛
   与规模无关。

**Rationale**：

- **npmi 门槛在低频区失效**：`(co, fa, fb) = (1,1,1)` npmi = 1.0；
  `(2,2,2)` npmi = 1.0；`(N,N,N)` 任意 N npmi = 1.0。这些是完全绑定，
  是 npmi 虚高的**结构性来源**。`min_npmi` 挡不住，pair_count floor 单调
  提高只能砍掉边界情况，本质问题不解决。Gate 1 直接测量"是否绑定"，
  规模无关，无参数，正好命中。**严格三元相等 vs `co >= max`**：`mode='build'`
  下 `cooccur_count` 是 grow-only（D6），可以出现 `co > min(df_a, df_b)`
  的合法单向依附。`co >= max` 会误判其为 glued 并删除；三元相等只在
  `df_a == df_b == co`（真绑定）时触发，容忍增量累积 skew。
- **scope 自适应而非全局**：user 约束 1 明确"同目录里文件大小差异大"，
  scope 级 `n_chunks` 是最粗但正确的粒度。若真要按文件粒度自适应，
  scope 的 pair 表是跨文件聚合的、无法拆分。阶梯（而非连续函数）便于
  log / 排查、10× 边界符合量级习惯。
- **不引入 `--profile small` 之类的预设开关**：那是"把两条参数捆成一个
  开关" = 隐藏取舍。阶梯 + 独立性判据都是自动的，caller / 操作员完全
  不需要知道有多少档。
- **`_prune_low_score_pairs` 同步升级**：这个函数是 v7→v8 迁移期的
  "存储契约执行者"，也用在 fresh install 的 no-op 分支。改成 per-scope
  查 `scope_stats.n_chunks` 求 floor，缺失时（legacy DB 或 phase-1
  backfill 之前）回退到固定 `BUILD_MIN_PAIR_COUNT = 5`。SQL 里用相关
  子查询做 Gate 1 三元相等 join（`co == COALESCE(df_a, 0) AND co ==
  COALESCE(df_b, 0)`），避免 Python 循环。`keyword_stats (dir_id, kw_id)`
  是 WITHOUT ROWID 主键，索引查找 O(log N)。
- **v7→v8 迁移警告消息同步更新**（schema.py `_migration_announce`）：
  枚举三门槛 + 引导用户 `grga index rebuild` 作为唯一恢复路径，避免
  "静默删除高 npmi glued pair" 让用户困惑。

**Impact**：

- 小语料（≤ 2000 chunks）的 `related --mode graph` 覆盖率显著提升
  （旧默认 5 → 新 2-3），low-count 真信号不再被一刀切。
- 大语料（> 20000 chunks）行为**保持不变**：Gate 2 阶梯仍是 5；Gate 1
  在大规模上极少触发（真信号语料里完全绑定的对天然稀少）。
- 现有测试有两处 fixture 恰好是"完全绑定 artefact"，被 Gate 1 精准命中：
  `test_build_min_pair_count_opt_out`（`"alpha beta"` × 2 chunks 无 solo）
  与 `test_build_keeps_low_df_char_pairs`（`氯` 只在 1 chunk）。修补：
  加 solo chunk。这些 fixture 之前正是我们要挡的 npmi 满分噪声，Gate 1
  逼出了它们的语义模糊性。
- BuildStats / CLI JSON 输出形状零变化。`related --json` 契约无变化。
- CLI 破坏性：`grga index build --min-pair-count` 默认从 `5` → 未设置
  （= 自适应）。显式传值仍生效；无脚本依赖旧默认，pre-1.0 直接改。

**Files**：
- `src/grga/graph/build.py`（`ADAPTIVE_MIN_PAIR_COUNT` /
  `resolve_min_pair_count` 阶梯 + `build_graph_index` 签名 +
  `_finalize_pairs_from_temp` Gate 1）
- `src/grga/graph/schema.py`（`_prune_low_score_pairs` scope-aware
  + Gate 1 SQL；`_migrate_v7_to_v8_inplace` docstring 更新）
- `src/grga/cli.py`（`--min-pair-count` build/rebuild 默认 → `None`
  + help 文案）
- `tests/test_graph_build.py`（4 个新用例覆盖阶梯边界 / 完全绑定 drop /
  单向依附 keep / None 哨兵；1 个 fixture 修补）
- `tests/test_scope_stopwords.py`（1 个 fixture 修补）
- `tests/graph/test_chunk_migration.py`（2 个新用例：三门槛联动 +
  scope_stats 缺失回退）

## 2026-07-13 · related v3.1 · `resolved_seeds` 多 seed 探针

**Plan step**：related-signals v3（`docs/design/related-signals-v3.md`）
落地后，从 caller 视角审视多 seed related 的空结果二义性。

**Decision**：`related --mode graph` 的 envelope 加**唯一**一个字段
`resolved_seeds: [{seed, in_index}]`（每个去重 + casefold + 保序的输入
seed 一条）。`related_keywords()` 返回类型从 `List[RelatedKeyword]` 改为
`RelatedKeywordsResult{results, resolved_seeds}`。非 JSON 输出把 dead
seed 以 stderr 两行提示呈现，不动 stdout 候选流。

**Rationale**（AGENTS.md §信号暴露原则逐条过）：

- **信号是 caller 拿不到的内部状态**（第 2 条）：空 `results` 对单个 seed
  二义——"未知词"vs"已知但孤立（写入期/读侧 npmi 门槛滤空）"。这两种
  情形下一步动作相反（reformulate vs 换更具体 seed / 松 min_npmi），但
  caller 从 `results` 派生不出来（信息在结果集里天然重合）。`in_index`
  正好补这一 bit。
- **能派生的坚决不加**（第 1 条）：产出过候选的 seed 集 =
  `{r.via_seed for r in results}`，命中数 = 其长度，都留给 caller 自己
  求。envelope 只放"这 seed 在不在词表"这一不可派生位。
- **低频动词优先**（第 3 条）：落在 `related`，热路径 `search`/`probe`/
  `expand`/`deep-search` 的 `--json` 一字节不动。
- **不做完备 schema**：拒绝 `per_seed_stats{hit_count, npmi_max, ...}`
  之类——那是把 caller 能派生的东西塞进共用层的杠杆反向。只加 bit。

**丢弃了什么**：per-seed 的候选计数 / npmi 分布 / "被哪种门槛滤掉"的
细分（写入期 vs 读侧）。前者可派生；后者需要额外无过滤查询，且 caller
自己调 `--min-npmi` 复查即可确认，不值当在热查询里多跑一遍。

**契约影响**：v3.0 → v3.1，additive MINOR（graph envelope 加 required
字段属破坏读旧 schema 的 caller，但 pre-1.0 且 v3 尚未外发，按新增处理）。
`schemas/related-signals-v3.json` graph 分支加 `resolved_seeds` +
`seed_resolution` $def；`related-signals-v3.md` 升 v3.1 + §1.4。

**Files**：
- `src/grga/related.py`（`SeedResolution` / `RelatedKeywordsResult` /
  `related_keywords` 返回类型 + `_related_keywords_impl` 提前解析 seed）
- `src/grga/cli.py::related_command`（graph 分支 emit + stderr dead-seed 提示）
- `schemas/related-signals-v3.json`、`docs/design/related-signals-v3.md`、
  `docs/api.md`
- `tests/test_related_keywords.py`、`tests/test_related_signals.py`、
  `tests/test_cli_json.py`

## 2026-07-13 · v9.1 pair 聚合回归 SQL self-join（TEMP TABLE 路线）

**Plan step**：修复 v9 keyword-primary（`plan-2026-07-12-keyword-primary.md`
Phase 3b-3）落地后，`index build` 在大语料上的性能崩溃 / OOM。完整
经验沉淀见 `docs/design/db-memory-optimization-lessons.md`。

**Decision**：Pass 2 不再在 Python 里枚举 pair、不再维护 `pair_delta`
Counter + `touched_pair_keys` set；改为把 `(doc_id, chunk_id, kw_id)`
三元组批量 INSERT 进 build-local `TEMP TABLE _build_chunk_kws`，Pass 3
用 pre-v9 形状的 SQL self-join 直接 UPSERT-add 进 `keyword_pairs`。

**核心 SQL**（`_finalize_pairs_from_temp`）：

```sql
INSERT INTO keyword_pairs (dir_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at)
SELECT ?, a.kw_id, b.kw_id, 'chunk_cooccur', COUNT(*), NULL, ?
FROM _build_chunk_kws a
JOIN _build_chunk_kws b
  ON a.doc_id = b.doc_id AND a.chunk_id = b.chunk_id AND a.kw_id < b.kw_id
GROUP BY a.kw_id, b.kw_id
HAVING COUNT(*) >= ?
ON CONFLICT(dir_id, kw_a_id, kw_b_id, source) DO UPDATE SET
    cooccur_count = cooccur_count + excluded.cooccur_count, ...
```

**Rationale**：

- **v9 遗留的两个回归**（都源于删掉 `keyword_chunks` 表却没恢复 SQL
  self-join）：
  - `touched_pair_keys` 累积整个 scope 的所有 distinct pair，从不清理 →
    22 MB 中文小说上撑到 2–3 GB Python set，`_finalize_pair_npmi` 直接
    OOM。
  - 所有 pair（含 count=1）都先 UPSERT 进 `keyword_pairs`（WITHOUT
    ROWID + 2 secondary indexes），最后再 DELETE 掉低于 `min_pair_count`
    的 → 多 GB 磁盘 churn，且 index 维护成本 O(N²)。
- **F3-corrected 加速了崩溃**：把 flush 从"每 doc 一次"改成"每 100k
  pair 一次"，让每次 flush 都对一个逐渐变大的 index 化 WITHOUT ROWID
  表做 UPSERT，写 chunks 阶段直接从秒级涨到 11 分钟。
- **TEMP TABLE 恢复 pre-v9 数学结构、不动 v9 schema**：`_build_chunk_kws`
  只是构建期状态，不进 `sqlite_master` 的持久表，不影响读侧（`related`
  仍然只读 `keyword_pairs` / `keyword_docs`）。
- **HAVING 早于 disk**：SQLite hash-aggregate 在 C 层完成聚合并按 HAVING
  过滤，低于阈值的 pair 从不落盘。Pre-v9 就是这个不变量，v9 丢了，
  v9.1 找回来。
- **grow-only 语义保留**：`ON CONFLICT DO UPDATE SET cooccur_count =
  cooccur_count + excluded.cooccur_count` + 尾部 `DELETE WHERE
  cooccur_count < min_pair_count` 两步共同覆盖 rebuild / build 两条路径
  （design D6）。

**实测（157120 chunks / 2 doc / 29246 短语）**：

| 阶段              | v9 (regressed)     | v9.1 (fixed)     |
|-------------------|---------------------|------------------|
| n-gram 分词       | ~40s                | ~40s             |
| 写入 chunks       | **11 分钟**         | **10s**          |
| 生成共现对        | **OOM (SIGKILL)**   | **81s / 166815 对** |
| 总耗时            | 未完成              | **182s**         |
| DB 大小 (最终)    | 1.9 GB (未提交)     | ≈ 数百 MB        |
| 峰值 RAM          | > 系统上限          | SQLite 自管       |

**Impact**：

- 删除：`_upsert_pair_delta` / `_finalize_pair_npmi` / `_flush_pair_delta`
  / `PAIR_DELTA_FLUSH_THRESHOLD` 常量 / `pair_delta` Counter /
  `touched_pair_keys` set。
- 新增：`_finalize_pairs_from_temp`（`src/grga/graph/build.py`）+
  `_build_chunk_kws` TEMP TABLE + `CHUNK_KW_TEMP_FLUSH=200_000` 常量。
- `tests/test_incremental_build.py`：monkeypatch 目标从 `_upsert_pair_delta`
  / `_finalize_pair_npmi` 迁移到 `_finalize_pairs_from_temp`。
- schema / `--json` 契约 / 读侧代码路径**零变化**。

## 2026-07-13 · related 信号契约 v3（随 v9 keyword-primary 落地）

**Plan step**：`related --json` 契约随 v9 keyword-primary（Phase 3b-2/3）
从"文档 + PPR 元数据"重构为"候选关键字 + 关联证据"。规范见
`docs/design/related-signals-v3.md` + `schemas/related-signals-v3.json`；
v2 归档到 `docs/design/deprecated/related-signals-v2.md` +
`schemas/deprecated-related-signals-v2.json`。

**Decision**：`related --json` 走 v3 契约：

1. `--mode graph` envelope 简化为 `{"results": [RelatedKeyword]}`，
   每个 `RelatedKeyword = {kw, npmi, cooccur_count, via_seed, chunk_df,
   source}`。v2 的 `expansion` / `crossed_scopes_preview` / `keyword_contributions`
   / `graph_signals` 全部删除（对应 PPR + doc 聚合层的移除）。
2. `--mode heading` envelope 保持 `{"results": [Related]}`，但 `Related`
   dataclass 里的 `keyword_contributions` 字段已从 Python API 层删除
   （随 `KeywordContribution` 一起，review F9）。
3. `--mode dir` 彻底移除；`related(mode="dir")` 抛 `ValueError` 指向
   `grga search --json`。
4. `--alpha` / `--iters` / `--cross-scope` / `--preview` / `--edge-quantile`
   CLI flag 全部删除（Phase 3b-2）。

**Rationale**：

- **信号暴露原则**：`RelatedKeyword` 的六字段是 caller 拿不到的内部证据
  （build-time npmi + 存储层 chunk_df）；文档级证据从"grga 派生 doc 集"
  变成"caller 用返回的关键字自己 ripgrep"。杠杆方向反了，把 doc 聚合
  从共用层下放到 caller 侧。
- **多 seed 不融合**（design D4）：`via_seed` 每行独占，同一 kw 若从多 seed
  可达则重复出现。caller 自决合并策略。
- **契约冻结与破变**：pre-1.0 项目、现有 caller 尚在少量、v2→v3 一次
  性 major bump，无 fallback / 兼容路径。JSON schema 用 `anyOf` 覆盖
  graph + heading 双 envelope，空 `results` 无歧义两侧都合法。

**Impact**：
- `schemas/related-signals-v3.json` 新增，`related-signals-v2.json`
  → `schemas/deprecated-related-signals-v2.json`
- `docs/design/related-signals-v3.md` 新增，v2 doc 归档
- `tests/test_related_signals.py` 换用 v3 schema，加 5 项 graph-mode
  形状/多 seed/未知 seed/mode=dir 拒绝验证
- 相关 skill / navigation 引用同步（Phase 5）

## 2026-07-12 · related 信号契约 v2（撤 trace-signals，改 expansion + keyword_contributions）

**Plan step**：`related --json` 的信号契约重构。完整规范见
`docs/design/deprecated/related-signals-v2.md`（已归档），旧契约归档于
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

### 2026-07-12（同日）· v2.2 · edges_to_seeds 扁平化到 entry 自身

**触发**：v2.1 落地后 caller 立即反馈：`edges_to_seeds` 数组在**单 seed
查询**（大部分调用）时永远是"一个数组包一个字典"，字典里 `seed` 字段
只是种子名回显；`source` 又几乎恒为 `"chunk_cooccur"`。3 层嵌套里只有
2 个字段（`npmi` / `cooccur_count`）承载真实信号，密度极低。

**决定**：
- **删 `edges_to_seeds` 列表包装**。每个 `expanded_keywords[i]` 直接携带
  **最强直接边**（npmi desc, cooccur desc, seed asc 排序取第一条）的
  `npmi` + `cooccur_count`，两字段一起出现或一起缺席。
- **`source` 零值省略**：只在 != `"chunk_cooccur"` 时 emit（即
  `llm_promoted`）。缺席即默认。
- **`via_seeds` 只在多 seed + 多边邻居里出现**：单 seed 查询直接省略
  （从 `seeds[0].kw` 就知道），单边邻居也省略（无歧义）。真正需要它的
  场景是"这个邻居桥接了我 2 个以上的 seed"——这才是"跨 seed 关联度"
  的有价值信号。
- **hop-1 门与 npmi 门解耦**：`_edges_by_neighbor` 查 `keyword_pairs`
  时保持不受 `min_npmi` 过滤（v2.1 既有行为）。因此存在"直接边低于阈
  值、PPR 绕开走长路径" 的邻居：此时 `hops_from_seed >= 2` 但仍带内联
  `npmi`（数值必然 < `min_npmi`）。含义是"真实存在但弱"，作为反向探
  针信号保留。

**丢弃了什么**：多 seed 时每条边的独立 npmi 明细。极端场景"邻居 X 到
seedA 0.8, 到 seedB 0.3" 的旧 `edges_to_seeds` 表达消失，v2.2 只保留
"X 桥接了 [seedA, seedB]"（按 npmi desc 排序）的名字列表，最强边的
npmi 内联。理由：多 seed 时 caller 通常要的是"这个邻居有没有跨 seed
桥接价值"，是 bit 级信息，不是完整数值。若未来出现明确用例可加
`via_seed_edges: [{seed, npmi}]` additive 字段（不复活旧列表）。

**Impact**：
- 单 seed hop-1 邻居的 JSON 体积净减 ~40%（3 层嵌套 → 平铺）。
- caller 判断连接强度：`e["npmi"] > 0.5` 一行，从原来的
  `next((edge["npmi"] for edge in e["edges_to_seeds"]), 0)` 简化。
- 契约变更：BREAKING（未发版内演进）。

**Files**：
- `docs/design/related-signals-v2.md`（升到 v2.2，追加 §2.1 + §7 + §6.1.5）
- `src/grga/related.py::_build_expansion`（结构调整；`_edges_by_neighbor`
  语义不变，只是消费方式改变）
- `schemas/related-signals-v2.json`（`edges_to_seeds` → 顶层 `npmi` /
  `cooccur_count` / `source?` / `via_seeds?`）
- `tests/test_related_signals.py`、`tests/graph/test_chunk_migration.py`
  （断言迁移）
- `docs/api.md`、`src/grga/skills/grga/references/{verbs,reflection-loop}.md`、
  `src/grga/skills/grga/examples/probe-search-reflect.md`（同步）

---

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

## 2026-07-12 · keyword-primary 架构（schema v8）：pair 字典化 + 移除 PPR

**Plan doc**: `docs/design/plan-2026-07-12-keyword-primary.md`
**关联实验**: `experiments/subgraph_ppr_2026_07_12/`

**背景**：v7 里 `keyword_chunks` 随语料线性膨胀（2 文件 93 MB → 1000 文件外推 45 GB），
`keyword_pairs` 靠 `recompute_pairs` 从 chunk 派生。`related --mode graph` 冷启 3s，
用户提出"能否把 pair 当核心知识资产 + 移除全图 PPR"。

**决策**：
1. `keyword_pairs` 提升为**第一性资产**（不再是派生），写入期严过滤（co≥5, npmi≥0.2）；
2. **删除** `keyword_chunks` + `chunks` 两张表（chunks 唯一读者 n_chunks 改由 `scope_stats` 提供）；
3. **移除 PPR**（局部 PPR 与全图 PPR 双否决），`related --mode graph` 返回类型从
   `List[Related]`（文档）改为 `List[RelatedKeyword]`（候选关键词 + npmi/cooccur），
   caller 用 ripgrep 从关键词找原文；
4. **存 npmi**（不现算）；
5. **知识资产原则**：pair 表随语料更新而 refine，不随语料删除而销毁，可跨语料迁移复用。

**Rationale**：
- 实验证明局部 PPR 天花板（oracle 诱导子图）只有 Jaccard 0.65，且 1 跳加权求和基线
  已达 Jaccard 0.51 —— PPR 传递的信号量白花，且 SQL 有 200-500ms/hop 硬地板；
- fanout=1498 时存 npmi 查询 0.076 ms vs 现算 2.889 ms（38×），随 fanout 发散；
- pair 严过滤后 279 万 → 16.7 万行，随 vocab²+top-M 边饱和，是**唯一次线性**的知识层；
- 用户核心原则："删原始语料不销毁已记忆的共现"消解了增量删除的校准复杂性。

**Impact**：MAJOR schema 迁移（v7→v8 有损），`related-signals` 契约 bump v2→v3
（删 `ppr_score` / `hops_from_seed` / `keyword_contributions`，改 `via_seeds` → `via_seed`）。
`mode=dir` 去向 §7.1 待定（倾向移除）。分 5 phase 落地，Phase 1 迁移最高风险。

## 2026-09-08 · rga 缓存上限 50M + probe timeout 180s + 默认排除压缩包/无文本二进制

**Plan doc**：`docs/design/plan-2026-09-08-rga-cache-timeout-excludes.md`

**背景**：`grga search/probe` 在 `/data/ports/sync`（11GB / 23,437 文件）全量
79.5-96.8s，probe 默认 60s 必超时。分层定位结论：Python 层非瓶颈（解析 2 万+
行 JSONL 只要 205ms），瓶颈在 rga 扫描本身——其中两个大 pdf 因提取文本压缩后
>2MB（rga 缓存默认上限）**永不进缓存、每次重提取 20-30s**，是最大单点。

**Decision**：

1. **缓存上限 2M → 50M**：构建所有 rga 命令时显式传
   `--rga-cache-max-blob-len=52428800`（`search._RGA_CACHE_MAX_BLOB_LEN`）。
   实测：17.9MB pdf 首次提取 20.8s → 二次 308ms（快 67 倍），缓存行 909→910。
   只影响新建缓存，不失效/不清除已有缓存。
2. **probe 默认 timeout 60s → 180s**：CLI、MCP、`probe_directory` 后端默认
   三处同步。`search` 默认 600s 不变。
3. **默认排除压缩包 + 无文本二进制**：`search` / `probe` / `preview` 默认追加
   `DEFAULT_EXCLUDE_GLOBS`（45 项：压缩包 + 可执行/库/字节码 + 安装包/镜像 +
   音视频），配 `--no-default-exclude` 关闭。与用户 `-g` 追加合并，用户 glob 在后。

**Rationale**（按 AGENTS.md §信号暴露原则逐条自问）：

- **排除列表绝不含文档类**（docx/pdf/pptx/xlsx/doc/xls/odt/rtf）：实测排除全部
  二进制丢 88% 匹配（29,345 行中仅 3,986 行来自纯文本）。用户搜"小堆"就是在
  文档里找，这是搜索主目标。`--no-default-exclude` 作为可逆开关兜住"想搜压缩包
  内部"的边角。
- **图片格式也不排除**（png/jpg/jpeg/gif/bmp/webp/ico）：默认排除会把
  `--path-only` 文件名搜索里的图片一并藏掉，图片语料（截图/封面/素材库）用户
  常按文件名找；rga 本就不处理图片，保留它们零扫描开销。
- **为什么排除是默认而非 opt-in**：压缩包 + 无文本二进制在 rga 侧要么导致每次
  递归解压、要么产生 `No adapter found` 失败噪音；排除它们零匹配损失（rga 白
  名单外）、省时减噪。默认排除压缩包实测只省 ~5s，真正的大头（大 pdf）由缓存
  上限解决——排除是"无副作用 + 减噪"，不是性能主解。
- **杠杆方向**：`--rga-cache-max-blob-len` / `--iglob '!…'` 都是 rga 一行参数，
  不新增 grga 契约字段、不动任何 `--json` 输出，热路径返回结构零变化。

**Impact**：additive MINOR。新 CLI flag `--no-default-exclude`（search/probe/
preview）；后端 `search_directory` / `probe_directory` / `build_rga_command_preview`
新增 `default_exclude=True` 参数（默认开启，旧调用不传参数行为与改动前兼容性 =
默认排除生效——这是有意行为变更，非契约破坏）。`deep-search` 通过 `grga search`
子进程自动继承。MCP `grga_probe` timeout 默认同步 60→180。

### 2026-09-09：Office 文档自定义 adapter（pandoc_office）

**问题**：rga 0.10.10 内置 pandoc adapter 白名单只有
`.epub/.odt/.docx/.fb2/.ipynb/.html/.htm`（源码 custom.rs 冻结于 2019，
pandoc 2.18 才加 xlsx/pptx reader）。xlsx/pptx/doc/xls/ppt 报 `No adapter found`、
永不缓存、每次重试 —— 真实语料**漏搜 84+ 个 Office 文件**。

**决策**：grga 通过 rga 的 `custom_adapters` 配置注册 `pandoc_office` adapter
（xlsx/pptx/doc/xls/ppt → pandoc），在**检查 / 安装 / 更新工具**时幂等写入
rga 用户配置文件（`~/.config/ripgrep-all/config.jsonc`，合并保留用户已有配置，
JSONC 注释由自写剥离器处理）。`--no-office-adapters` 按次排除（`--rga-adapters=
-pandoc_office`）。

**Rationale**：
- **写配置而非每次传参**：用户明确要求避免每次命令行传 `--rga-config-file`；
  配置常驻 + 按次排除开关，默认路径零额外参数。
- **默认开**：漏搜 Office 文档是功能缺陷（搜工作文档是主用途），优先于首次
  提取速度（二次有缓存 50M 上限保底）。
- **合并不覆盖**：写入前解析现有 config（剥离注释），只追加 `pandoc_office`；
  解析失败/不可写时静默跳过，绝不破坏用户配置。
- **binary 用相对名 `pandoc`**：依赖 `add_tools_to_path()` 把 managed pandoc
  目录加 PATH，rga-preproc 子进程经继承 env 找到它（实测 39ms 命中）。
- **与排除列表无冲突**：Office 文件不在 `DEFAULT_EXCLUDE_GLOBS`，adapter 按
  扩展名处理，不经过 zip adapter。

**Impact**：additive。新 CLI flag `--no-office-adapters`（search/probe/preview）；
后端 `_shared_rga_options` / `search_directory` / `probe_directory` /
`build_rga_command_preview` / `_count_total_matches` 新增 `office_adapters=True`
参数。副作用：grga 会 touch 用户 rga 全局 config（只追加，不破坏）。

### 2026-09-10：DEFAULT_EXCLUDE_GLOBS 统一 build/search 双阶段

**问题**：`DEFAULT_EXCLUDE_GLOBS`（rga 风格 glob）只对 search/probe 生效；
`grga index build` 用独立的 Python fnmatch matcher（`iter_text_files`
exclude_globs），默认只靠 `ignore_dirs`（.git/.grga/.venv/node_modules/
__pycache__/dist/build/target）过滤临时目录，**压缩包/无文本二进制仍会进索引**。

**决策**：把 `DEFAULT_EXCLUDE_GLOBS` 扩为单一排除来源，search 与 build 共用：
- search.py 新增目录排除（`!**/node_modules/**`、`!**/__pycache__/**`、
  `!**/.venv/**`、`!**/venv/**`、`!**/.mypy_cache/**`、`!**/.pytest_cache/**`、
  `!**/.ruff_cache/**`、`!**/dist/**`、`!**/build/**`、`!**/target/**`、
  `!**/.grga/**`、`!**/.stversions/**`）——只列**非隐藏**临时目录；隐藏
  点目录（.git 等）rga 默认已跳过、build Python walk 也跳过，不加死重量
- 新增 `search._to_build_globs()`：把 rga 风格 glob 翻译成 build fnmatch
  方言（剥 `!` 前缀；`**/x/**` 展开成 `x/**` + `**/x/**` 双模式，覆盖根级 +
  嵌套级——fnmatch 的 `**` 与 `*` 等价且首段 `**/` 需要非空前缀）
- `index build` / `index rebuild` 加 `--no-default-exclude` 开关，默认把
  `_to_build_globs()` 合并进 exclude_globs（用户 `--exclude-glob` 追加其后，
  exclude-wins 语义仍生效）；build 自身 `ignore_dirs` 安全网保留

**Rationale**：
- **单一来源防漂移**：同一排除列表，search 与 build 不再各自维护
- **build 不该吃压缩包**：`indexable_suffixes` 含 `.zip/.jar/.tar`，build
  之前会索引压缩包内部共现——统一排除后知识图不再被垃圾污染
- **globs 与 .gitignore 叠加**（09-09 实验确认）：用户 .gitignore 照样生效，
  DEFAULT_EXCLUDE_GLOBS 只是额外排除，不覆盖不冲突
- **`--no-default-exclude` 保留退出通道**：想搜/想索引压缩包时显式打开

**Impact**：additive。search.py 的 `DEFAULT_EXCLUDE_GLOBS` 从 45 项扩到
57 项；新增 `_to_build_globs()`；`_run_index_build` + `index build/rebuild`
命令加 `default_exclude=True` / `--no-default-exclude`。build 默认行为变更：
压缩包 + 临时目录不再进索引（可用 `--no-default-exclude` 恢复）。

### 2026-09-09：pandoc_office 接入 graph 索引（补充决策）

**问题**：上述 adapter 只影响 `grga search`/`probe`（rga CLI 直读 config）。
`grga index build` 的文件枚举 gate `indexable_suffixes()`（`file_read.py` 的
`RGA_SUPPORTED_SUFFIXES`）不含 `.xlsx/.pptx/.doc/.xls/.ppt` → 这些文件在
`iter_text_files` 就以 `bad_suffix` 跳过、从未进 `docs` 表 —— adapter 对
graph 索引无效。

**决策**：把 `.xlsx/.pptx/.doc/.xls/.ppt` 加进 `file_read.py` 的
`RGA_SUPPORTED_SUFFIXES` 与 `RGA_BINARY_ONLY_SUFFIXES`：
- `RGA_SUPPORTED_SUFFIXES`：build 枚举后经 `read_file_lines_ex` →
  `_read_via_rga`（`rga --passthru`）→ pandoc_office 提取干净文本。
- `RGA_BINARY_ONLY_SUFFIXES`：rga 失败时返回 `FAILED` 而不是 native UTF-8
  读二进制垃圾灌进索引。
- 附带收益：`expand` 上下文（`read_file_lines`）对 Office 命中文件也走
  pandoc_office，不再是二进制乱码。

**修复的既有 bug**：`_compute_toolchain_fingerprint` 依赖进程 PATH，而 PATH
只在文件读取时才被 `add_tools_to_path()` 打上 managed 工具目录 —— 数据构建
（有读取）与随后的 no-op 增量构建（无读取）的 fingerprint 不同 → 每次 no-op
构建都误报 "toolchain fingerprint changed"。修复：fingerprint 内部先调用
`add_tools_to_path()`，保证两次解析同一组二进制。

**Impact**：additive MINOR。增量语义不变：之前被 `bad_suffix` 跳过的 Office
文件无 `docs` 行 → 新 build 以 `DOC_ADDED` 处理；已索引文件仍按 size/mtime
跳过。
