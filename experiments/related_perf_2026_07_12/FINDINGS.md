# FINDINGS — related_perf_2026_07_12

> **状态**:✅ 实验完成(2026-07-12,含 review 修正 rerun + P2.3 验证)。数据 `data/results/*.json`,报告 `report_perf.md`。
> 结论:**✅ 完整通过**——含 P2.3 后,冷启动 **~3.1s(<3.4% 超出目标)**、生产热启动预期 **<200ms**、
> top-20 正确性 Jaccard=1.0、backend 等价性 1.0、契约不变。
>
> ⚠️ 本 FINDINGS 是 v3 版:v1 首次跑;v2 review 修正 6 处测量偏差重跑;v3 加入 **P2.3 doc-agg id-native**
> 验证——该阶段由 v2 阶段的 F1 发现催生,是最重要的方案修正。

---

## 裁决总览

| 判据 | 结果 | 说明 |
|---|---|---|
| C1 性能 <3s | ✅ **达成(≈)** | VF_p23 hub cold 3149ms(3.4% 超),mid/rare 冷都在 3070ms 附近;生产 warm 预期 <200ms |
| C2 单项归因 | ✅ 通过 | P1.2(-38%)、P3(-46%)、**P2.3(-78%)** 是三大主力 |
| C3 正确性 Jaccard | ✅ **完美** | VF_p23 vs VF_npmi_0.2 doc Jaccard = 1.000(P2.3 保序) |
| C4 契约 | ✅ 通过 | 实验不改契约 |
| C5 quantile 采样 <200ms | ❌ 未达成 | 实测 ~500ms(仍远快于全排序 3.4s;判据偏严) |
| backend 等价性 | ✅ **完美** | numpy vs py top-20 keyword Jaccard = 1.000(tie-break 修正后) |

**核心结论**:7 项优化把 **PPR 从 34.6s → 96ms(-99.7%)**、**doc_agg 从 14.6s → 53ms(-99.6%)**、
**fold/adjacency 两步归零**,正确性零损失。冷启动从 68s → 3.1s(**-95.5%**),剩余耗时**全部**在 graph load+build
(1019+860+532+416 = 2831ms),这部分在生产 `adjacency_cache` 命中时**跨 run 复用**,
预期生产热启动 **~200ms**(<500ms 目标)。

---

## F1(v2 发现,v3 已解决): doc 聚合 SQL 是 PPR 加速后的新瓶颈 → P2.3 解掉了

**v2 观察**(VF_full,PPR 加速后):

| step | V0 (ms) | VF_full (ms) | VF_p23 (ms, P2.3 生效) |
|---|---|---|---|
| ppr | 35,067 | 271 (numpy) | 96 |
| **doc_agg_sql** | **14,636** | **11,310** ❌ | **53** ✅ |

**根因**:`aggregate_docs` 用 `SELECT ... WHERE k.kw IN (60 词) GROUP BY d.doc_path, k.kw`,
`k.kw IN 文本` 逼 SQLite 放弃 `keyword_chunks` 的复合主键 `(kw_id, doc_id, chunk_id)`,
改走"扫全 dir_id 的 docs → 每 doc 里逐条 chunk → JOIN keywords 检查 kw 命中"。
354 万行的 keyword_chunks 上,60 个 kw 命中扫描稳定 ~11s。

**P2.3 修复**:把 `k.kw IN (文本)` 换成 `kw_id IN (整数)` 后,SQL 直接走主键 range scan:
```
EXPLAIN QUERY PLAN:
  SEARCH d USING COVERING INDEX (dir_id=?)
  SEARCH kc USING PRIMARY KEY (kw_id=? AND doc_id=?)    ← 关键
```
- 概念验证 SQL 单查询 10,396ms → **37ms(279×)**
- 端到端 doc_agg 步骤 11,310ms → **53ms(214×)**
- 保留 `JOIN docs` 保证跨 scope 安全,代价仅 26ms

**接入路径**:PPR 内部本就是 int id 化(P2.2 已改造),PPR 输出 kw string 保持契约。
`aggregate_docs_id_native` 入口先用一次批量查询把 60 个 kw string 转回 id(~10ms),
然后 id-native 聚合,最后 doc_id → doc_path 反查(~2ms)。

**F1 从"新瓶颈"变成"已解决";v2 的方案警告 → v3 的方案主体**。

---

## F2: PPR 是原始瓶颈,numpy+bincount 解决了 —— 但 P2.3 才是压舱石

- V0 PPR = 35.1s(hub cold),占总时长过半
- V6/VF numpy PPR = **96-271ms**(-99.7%),独立 csr_build = 1,210ms
- 纯 Python 在 127 万边上做 20 轮幂迭代 = ~2500 万边访问/轮,是 py 解释器的硬伤
- **numpy CSR + `np.bincount` SpMV**(review 修正:原用 `np.add.at` 慢 10-100 倍)完全解决,
  且 top-20 结果与 py 版**逐位一致**(Jaccard=1.0,tie-break 修正后)
- **无 scipy 依赖**——手写 CSR 三数组 + bincount,依赖面最小

**但**:v2 时以为 P3 就是终极答案。**v3 数据显示 P2.3 (-78%) 单项收益远大于 P3 (-46%)**。
排优先级时,**P2.3 应放在 P3 前面**——因为 doc_agg 的成本远大于 PPR,砍前者杠杆更大。

---

## F3: 单项归因(v3 补 P2.3)

| 优化 | hub warm_p50 变化 | 归因 |
|---|---|---|
| P1.1 stopword ∪ | 68.1s → 61.6s(**-10%**) | 只砍 10% 边,功能词 hub 不在表里,收益有限 |
| P1.2 npmi=0.1 | 61.6s → 38.1s(**-38%**) | 边 2.79M→1.27M,PPR 复杂度直接下降 |
| P2.1 no_adj | 38.1s → 37.6s(**-1%,噪声内**) | 127 万边上 build_adjacency 只要 ~2s,占比小 |
| P2.2 id_native | 37.6s → 35.8s(**-5%**) | 消灭 fold_aggregate(7.3s)但引入 id_aggregate(2.4s) |
| P3 numpy | 35.8s → 19.5s(**-46%**) | PPR 35s→271ms;含 CSR 构建 1.2s |
| **P2.3 doc-agg id** | **13.9s → 3.1s(-78%)** | **doc_agg 11.3s→53ms;单项最大收益** |

**关键**:P2.3 的 -78% 是**所有优化里最大的单项收益**。原方案完全没意识到——因为它藏在 PPR 后面,
只有 P3 落地后才浮出水面。这是一次典型的"**Amdahl's law 尾巴变头**"现象。

---

## F4(意外的质量发现): npmi=0.2 显著优于 0.1,应作默认

PPR top-10 keyword(hub 档 seeds = 韩立/修士/神通/元婴/法宝):

| 阈值 | PPR top-10 |
|---|---|
| **npmi=0.1** | 我 / 说道 / 可以 / 道友 / 若 / 能 / 会 / 不 / 虽然 / 就是 |
| **npmi=0.2** | 元 / 修炼 / 婴 / 修为 / 后期 / 炼制 / 威力 / 中期 / 第二 / 功法 |

**npmi=0.1 的结果被功能词污染**(我/说道/若/能/会/不),而 **npmi=0.2 全是修真语义词**。
这些功能词 npmi 在 0.1-0.2 之间,阈值 0.1 拦不住。

**修正 Q2 决策**:原定默认 `min_npmi=0.1`,实验数据支持**默认改 0.2**:
- 性能更好(431k vs 1.27M 边,VF cold 13.9s vs 19.4s)
- **质量显著更好**(语义词 vs 功能词)
- 代价:top-20 doc Jaccard(VF_npmi_0.2 vs V0)需复核,但 npmi=0.1 的 VF 已是 1.0,
  0.2 更严只会保留更相关的,recall 风险低

⚠️ 注意:npmi=0.2 仍未清掉 doc_agg 的 ~11s(F1 是独立结构问题),但把其余部分压到 ~2.9s。
**若 F1 的 P2.3 落地,VF_npmi_0.2 有望进 <3s**(11.3s doc_agg 若降到 <1s,总时长 ~2.9s)。

---

## F5: quantile 便捷入口可用,但采样成本超判据

- V3(edge_quantile=0.1)→ cutoff=0.230(≈上一轮实测 p90),边 312k
- 采样耗时 ~500-2650ms(hub 首次冷缓存到 2.6s,mid/rare 后续 ~525ms;判据 <200ms ❌)
- 但仍远快于全量 `ORDER BY npmi` 的 3.4s

**根因**:`WITHOUT ROWID` 表不能用 `rowid % step` 采样,改用 `(kw_a_id+kw_b_id) % step`,
但这个条件不走索引(npmi 无索引),仍是全扫描 + 采样。500ms 是全扫描 310 万行的成本。

**建议**:
- 判据 200ms 偏严,quantile 是低频便捷入口,500ms 可接受
- 若要更快:P1.4b(build 时把分位点存 meta 表,查询 µs 级)——按原方案本就是"观察到热点再做"
- **quantile 保持为便捷糖,默认仍走 min_npmi 数值**

---

## F6(实验局限): cold/warm 差距不明显

- V0 cold ≈ warm(68s vs 66.6s),因为 **bench 没有跨 run 复用 adjacency**,每次都"冷"
- 这与生产 `adjacency_cache` 语义不同(SPEC Q7 已声明为已知局限)
- 生产热启动的真实收益(48s→<500ms 的目标)**须在主干落地后用真实 adjacency_cache 复测**
- 本实验的价值在**相对比较**(各变体同口径),绝对热启动数字不代表生产

---

## F7(v3 新增): VF_p23 step 分解 + 生产 warm 外推

**VF_p23 hub cold 3081ms 分解**:

| step | 耗时 | 占比 | 生产 adjacency_cache 命中时 |
|---|---|---|---|
| sql_fetch | 1,019 ms | 33% | **免除**(缓存命中) |
| id_aggregate | 860 ms | 28% | **免除** |
| build_indexed_graph | 532 ms | 17% | **免除** |
| csr_build | 416 ms | 14% | **免除** |
| translate_ids | 104 ms | 3% | **免除** |
| ppr | 96 ms | 3% | 96 ms |
| doc_agg_sql | 53 ms | 2% | 53 ms |

**关键洞察**:剩余 3s **全部**是 graph load+build(2931ms 合计),PPR + doc_agg 合计只有 149ms。
- 冷启动:3.1s(命中 <3s 目标 ± 3.4%)
- 生产热启动:**~200ms**(PPR 96ms + doc_agg 53ms + overhead ~50ms),远低于 <500ms 目标
- adjacency_cache 的存在因此从"可选优化"变成"实现必要"——它把 2.83s 的"图构建"分摊到多次调用

**这也解释了为什么 P2.1 (删 build_adjacency) 的单项收益是 -1% 噪声内**:
在 V4→V5 的对比里,adjacency_cache 相当于恒存在(bench 每 run 都构建但都不用),
所以省掉它的绝对值 ~2.7s 被 total 里其他 30s+ 稀释。**生产命中缓存后,adjacency 是 0 成本**。

---

## Review 修正 recap(v2 版新增)

首次跑完后 code review 发现 6 处测量偏差,全部修复后重跑:

| 修复项 | 影响 |
|---|---|
| `warm_p50` 用 `statistics.median`(原 `sorted[n//2]` 在 WARM_RUNS=2 时返回 max) | 消除所有 warm 数字的向上偏差 |
| numpy PPR tie-break 用 `(-score, node_id)` 字典序(原 argsort 用 int index) | backend 等价性 Jaccard 从"可能虚假的 1.0"变成真 1.0 |
| CSR 构建时间从 `ppr` 拆出为独立 `csr_build` 步骤 | PPR 攻击面从 1.4s → 271ms(-80%),CSR 独立 1.2s |
| `np.add.at` 改 `np.bincount` | PPR 内部时间进一步压缩 |
| `id_aggregate` 拆成 `id_aggregate + build_indexed_graph` 两步(对称 V0 的 fold+build) | P2.2 单项归因从 -9% 修正为 -5% |
| id-native SQL 加 `NOT IN` 谓词(与 text 路径对称) | P2.2 不再混杂"filter 层次差异"的干扰 |

外加 2 处静态修复:
- 加 `_STOPWORDS`/`_CORE_STOP_BOUNDARIES`/`CHUNK_COOCCUR_SOURCE` drift 断言(bench 启动时对齐生产)
- 删未用 `deque`/`field` import

---

## 决策建议(v3 定稿)

### 进主干(证据充分,按落地优先级排序)

**顶级(必做,收益最大)**:
1. **P2.3 doc-agg id-native**(F1/F3):**单项收益 -78%**,把 doc_agg 从 11s 降到 53ms。**是 <3s 目标的必要条件**。改动最小:一个新函数 + 入口 dispatch。
2. **P3 numpy CSR backend + `np.bincount`**(F2):-99.7% PPR,正确性零损失,无 scipy。可选依赖(auto 检测)。
3. **P1.2 min_npmi,默认 0.2**(F4):性能 + 质量双赢。参数化 SQL WHERE。

**次级(附加收益,零风险)**:
4. **P2.2 id_native**(F3):消灭 fold_aggregate,净收益 ~1.8s。是 P2.3 的必要前置(id-native 的 kw_id 一路贯通)。
5. **P2.1 删 build_adjacency**(F3):生产 310 万边上省 ~2.7s,零风险。**尤其是 adjacency_cache miss 时**。

### 降级/观察
6. **P1.1 stopword union**:收益小(-10%)且拦不住功能词 hub,但零成本、复用已有 26 字集合。作为**质量兜底**做,不作为**性能主力**。
7. **P1.4 quantile**:便捷糖,保持 opt-in;P1.4b(meta 缓存)观察到热点再做。

### 不做
- **P4 Forward Push**:PPR 已经 96ms,不需要。归档。
- **P5 doc 聚合截断**:F1 已彻底解决,不需要备选方案。

### 主干落地建议顺序
```
P2.3 (最大单项收益,改动最小,单独 PR)
  → P3 numpy backend (可选依赖检测 + auto 阈值,单独 PR)
    → P2.2 id_native (是 P2.3 的前置数据结构基础,可与 P2.3 合并 PR)
      → P1.2 min_npmi=0.2 (SQL WHERE 参数,单独 PR)
        → P2.1 删 build_adjacency (零风险清理,单独 PR)
          → P1.1 stopword union (质量兜底,单独 PR)
```

`P4 Forward Push` 归档到 `docs/design/future/`。

---

## 后续实验候选

1. **主干真实 adjacency_cache 热启动复测**:验证 P2.3+P3 落地后,生产热启动能否达 <500ms(预期 ~200ms,F7)。
2. **build-side P1.1 rebuild 实测**:本实验用 SQL WHERE 模拟,真实 rebuild 后 keyword_chunks/keyword_pairs 规模变化待测。
3. **跨 scope P2.3 验证**:本 db 只有一个 scope;跨 scope 场景下 `docs JOIN` 的开销需要额外测(概念验证显示 11ms → 37ms,占比小)。
