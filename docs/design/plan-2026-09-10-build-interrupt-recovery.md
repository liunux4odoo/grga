# build 中断恢复与状态管理改进提案

**日期**：2026-09-10
**状态**：提案（待评估，未落地）——由"核实中断行为"的讨论沉淀而来，后续再考虑是否实施。
**动机**：核实 `grga index build` 在异常中止（断电）后对处理中批次的行为时，发现当前实现
"正确但不透明"：处理中批次会被重处理（正确），但**无法识别残缺的构建状态**，且存在若干
可优化的写放大与并发读阻塞问题。本文把这些观察整理成独立改进项，暂不实施。
**关联**：`docs/design/plan-2026-07-11-build-vs-rebuild.md`（增量/rebuild 语义）、
`docs/design/plan-2026-07-15-build-memory-oom.md`（附文件级断点续跑设计，搁置）、
`docs/design/plan-2026-07-12-keyword-primary.md`（keyword_pairs 累积语义 D6）。

---

## 0. TL;DR

1. **现状核实（正确，无需修）**：跳过判定只依赖 `docs` 表 `(size, mtime, content_hash)`；
   `docs` 元数据与索引行同事务原子写入。batched 路径每批 commit（`build.py:2379`）、单趟路径
   末尾一次性 commit（`build.py:3534`），所以中断时**处理中的批次会被重新处理**，已提交批次
   下次走 `unchanged` 跳过。无文件级 checkpoint，但增量模式已天然"续跑"。
2. **主要缺口 = 状态不可见**：`meta` 表只有 `toolchain_fingerprint`，无法区分"完全构建"与
   "中断残局"；batched 中断后 `keyword_pairs` / `keyword_stats` / `scope_stats` 未最终化，
   读侧静默返回不一致结果。
3. **次要优化 = 写放大与并发**：per-batch floor 用批次内 chunk 数导致早期批次插低计数组 +
   跨批次计数丢失；未启用 WAL，构建期间读侧被阻塞。
4. 本文全部为**待评估提案**，按 P0/P1/P2 分级，落地前需逐项触发证据。

---

## 1. 现状核实（上一轮结论，固化）

### 1.1 为什么中断后处理中批次会被重处理

- 跳过判定唯一依据是 `_classify_doc`（`build.py:1608`）：Level 1 `(size, mtime)` 快过 →
  Level 2 rga 输出 `content_hash` 慢过。不存在其他"已处理缓存"。
- `docs` 元数据与索引行**同事务原子写入**：
  - **batched**（默认，`--batch-chunks 50000`）：每批结束 `conn.commit()`（`build.py:2379`），
    批内 `_update_doc_metadata`（`build.py:2320`）与 keyword_docs/heading_docs/pairs 一起提交。
    处理中批次未提交 → 断电回滚。
  - **单趟**（`--batch-chunks 0`）：全流程末尾一次性 `conn.commit()`（`build.py:3534`）→
    本次全部回滚。
- 下次 `grga index build`：中断批次文件要么无 `docs` 行（新增）要么旧元数据（更新）→
  重新判为 `added`/`updated` → 重处理；已提交批次 → `unchanged` 跳过。

### 1.2 没有 checkpoint / 续跑机制

- `src/grga` 全库 grep 无 `checkpoint/resume/断点`；文件级断点续跑只是设计留档
  （`plan-2026-07-15-build-memory-oom.md`），明确"本轮不做"。
- 结论：**增量模式下每批 commit 已天然提供"续跑"**，真正缺口不是重复扫描，而是 finalize
  缺失（见 §2.1）。

---

## 2. 改进项

### 2.1 [P0] 构建完成标记：识别残缺状态 + 补 finalize

**动机**：batched 路径中断后，已提交批次留下 `keyword_pairs`（只过了 per-batch floor）、
`keyword_stats` / `scope_stats` 未最终化、全局 floor DELETE + npmi 重算（`build.py:3438-3453`）
未跑。DB 中看不出 scope 是"完全构建"还是"中断残局"；读侧（`related`）静默返回不一致结果。

**做法**（`meta` 表 per-scope 标记，非新表）：

1. 构建开始时写 `meta` 键如 `scope:{name}:build_status = in_progress`（含 start 时间戳）。
2. 全部成功（含 Pass 3 finalize）后置 `done`；异常/中断后保持 `in_progress`。
3. 下次 `build` 启动时检测到 `in_progress`：emit warning + **补跑 finalize**（Pass 3 的
   floor DELETE + npmi 重算 + `keyword_stats` / `scope_stats` 重刷），再进入正常增量。
4. 读侧可选：`related`/`scope list` 检测 `in_progress` 时输出 `warnings[]`（非 JSON 人类可读；
   `--json` 按信号暴露原则只加低频告警字段，热路径 `search`/`probe` 不加）。

**成本**：低。一个 meta 键 + 起始/收尾两处写 + 启动检测分支。
**风险**：标记写入必须与业务数据同事务（起始标记可单独 commit，收尾标记与最终 commit 同事务），
否则"标记 done 但数据未落盘"反向误导。
**验收**：模拟中断（批次中途抛异常）后 DB 留有 `in_progress`；下次 build 补跑 finalize 并
修正 scope_stats / npmi；`related` 在残缺状态时给 warning。

### 2.2 [P0] 中断恢复测试：把"重处理 vs 跳过"固化成回归

**动机**：上一轮结论完全来自代码阅读，`tests/` 全库无模拟中断用例。低成本高价值。

**做法**：
1. 单测：用事务回滚（`conn.rollback()`）或 monkeypatch 在 `_run_batched_pipeline` 批次中途
   抛异常，模拟断电。
2. 断言：
   - 处理中批次的文件（新增 + 更新）下次 `build` 被判 `added`/`updated` 并重处理；
   - 已提交批次的文件下次被判 `unchanged` 跳过；
   - 中断后 `keyword_pairs` / `scope_stats` 若已提交部分批次，下次 build 能补齐一致。

**成本**：低。
**验收**：`pytest` 新增 1-2 条用例全绿。

### 2.3 [P1] per-batch floor 用累计 chunk 数，减少写放大

**动机**：`_run_batched_pipeline` 第 10 步用 `resolve_min_pair_count(batch_n_chunks)`
（`build.py:2326`）做 per-batch 过滤。早期小批次 floor 偏低 → 插入大量低计数组 → 靠最终
全局 floor DELETE（`build.py:3438`）清理，白做工作（磁盘 churn）。
更严重的是**跨批次计数丢失**：某对在 batch1 出现 1 次、batch2 出现 1 次，per-batch floor=2
时两批各被滤掉，累计 2 直接丢——这是 batched 与单趟**非 bit-exact** 的已知差异
（CLI `--help` 已声明，`--batch-chunks 0` 可保底）。

**做法**：floor 改按**累计** `n_chunks_total`（当前批次处理完后的总 chunk 数）解析，
使批内过滤更接近最终全局 floor。
**注意**：跨批次计数丢失是 per-batch self-join 的结构性 tradeoff，floor 调整只能减 churn、
不能根治；根治需把 triples 全部落 TEMP 表做单次全局 self-join（= 单趟路径的内存代价）。
是否根治待 §5 证据触发。
**成本**：低（一行解析参数改动）。
**验收**：早期批次插入的 pair 数下降；跨批次累计计数差异在文档中明示，不与单趟逐字节对比。

### 2.4 [P1] 启用 WAL + `synchronous=NORMAL`

**动机**：`connect()`（`schema.py:110`）是默认 DELETE journal + `synchronous=FULL`。
batched 每批 commit 一次 fsync 压力大；单趟长事务期间读侧（另一进程 `search`/`related`）
被阻塞或读旧数据。

**做法**：`connect()` 里 `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL`
（WAL 下 NORMAL 仍保证崩溃不损坏、最多丢最近一次 commit，对 build 场景可接受；
保守可保持 FULL）。
**成本**：低。需注意同一 DB 被多进程并发写时 WAL 仍是"单写多读"；build 自身已是单写者。
**风险**：若工具链/打包对 DB 文件做拷贝，WAL 需配合 checkpoint；落地时排查是否有对
`-wal` / `-shm` 伴生文件的假设。
**验收**：构建期间另一连接可并发 SELECT；batched 每批 commit 延迟下降；断电恢复测试仍绿。

### 2.5 [P2] rebuild 的提前清空：避免"索引已清空"的残缺期

**动机**：`rebuild` 在 `build.py:2619-2629` 先删空索引/派生层并**提前提交**，之后才重建。
中断后 scope 处于"索引已清空"状态，查询静默返回空/残缺结果。

**做法**（二选一，待评估）：
- **A（两阶段）**：新数据写入 shadow/temp 表，全部完成后再原子交换。成本高（FK、kw_id
  intern 复用、`docs.id` 引用全部要处理）。
- **B（pending 标记）**：复用 §2.1 的标记，`rebuild` 清空时写
  `scope:{name}:rebuild_pending`，读侧据此提示"索引未完成，请重跑 rebuild"。成本低。

**倾向**：B 起步，A 仅在证据表明"rebuild 中断后必须无缝可查"时再做。
**成本**：B 低，A 高。

### 2.6 [P2] 文件级断点续跑：明确不做（与 §2.1 的关系）

**动机**：`plan-2026-07-15-build-memory-oom.md` 附了文件级断点续跑设计。上一轮核实显示
增量模式每批 commit 已天然续跑（已提交批次下次 `unchanged` 跳过），真正的缺口是 finalize
缺失（§2.1）。**完整 checkpoint 性价比低于 §2.1**。
**结论**：本文不列实施项；保留为未来词汇表占位，仅当证据表明"中断后重扫描/重读成本不可接受"
时再启动。

---

## 3. 任务分解（若启动，按依赖顺序）

1. **§2.1** 构建完成标记 + 启动检测补 finalize（含收尾事务顺序）。
2. **§2.2** 中断恢复测试（可先于 2.1 落地，作为行为基线）。
3. **§2.4** WAL + synchronous 调整（独立、低风险，可并行）。
4. **§2.3** per-batch floor 改累计 chunk 数。
5. **§2.5** rebuild pending 标记（依赖 §2.1 的标记基础设施）。

---

## 4. 契约与信号暴露检查（AGENTS.md §信号暴露原则）

- §2.1 的 `build_status` 属于"消费者绝对拿不到的内部状态"（构建中断外部不可派生）→
  **符合暴露豁免**。但暴露面克制：热路径动词（`search`/`probe`）不新增字段；告警只走
  `related`/`scope list` 等低频动词的 `warnings[]`，非 JSON 输出保持人类可读。
- `--json` 若要暴露 build_status 派生信息（如"上次构建未完成"），按"加字段 MINOR"走，
  落地前在契约文档登记。
- 不新增全局 schema 版本：§2.1 只用 `meta` 表，§2.4 是 PRAGMA 非 schema。

---

## 5. 何时重开 / 需要证据才做的项

- **跨批次计数丢失根治**（§2.3 注）：有跨仓库证据表明 batched 结果与单趟差异在真实语料上
  造成可感知的图质量下降时，做"全部 triples 落 TEMP 表 + 单次全局 self-join"。
- **rebuild 两阶段**（§2.5 A）：有证据表明 rebuild 中断后"必须无缝可查"时。
- **文件级断点续跑**（§2.6）：有证据表明中断后重扫描成本不可接受时。
- **不要**为"体系完整"提前实现：当前行为正确，本文改进的是可观测性、写放大与并发，
  每一项都以真实痛点/证据为触发条件。

---

## 6. 变更清单（若实施）

- 新增：本文。
- 修改：`src/grga/graph/build.py`（标记 + floor + WAL 相关）、`src/grga/graph/schema.py`
  （`connect()` WAL）、`src/grga/graph/related.py`（读侧 warning，如做 §2.1 第 4 条）、
  `src/grga/cli.py`（`--json` 字段若加）。
- 追加：`docs/design/decisions.md`（逐项决策留痕）。
- 更新：`docs/design/README.md` 或 `docs/navigation.md` 索引（按当时约定）。
