# QUESTIONS — related_perf_2026_07_12

> 关键决策与冻结值。落地实验时**不再回问用户**,除非发现契约冲突。

---

## Q1 — P1.1 stopword union 走 rebuild 还是模拟?

**冻结:模拟(SQL WHERE NOT IN)**

**理由**:
- 生产库 478MB / 310 万边,rebuild 一次几十分钟,不适合实验回合
- 模拟捕捉了 P1.1 对**读路径**的所有性能影响(节点数、边数)
- 主干落地时才需要 rebuild(那时是 build 侧改动 + 一次性重建)

**局限**:
- 无法测 rebuild 后 `keyword_chunks` 少了 hub token,`recompute_pairs` 内 self-join 结果的间接变化
- 这属于建图链路的收益,与本实验(读路径优化)正交,不影响判据

---

## Q2 — min_npmi 默认值?

**冻结:实验中同时测 0.1(主推)与 0.2(激进对照)**

**理由**:
- 上一轮实测数据:npmi p90=0.2322,p50=0.086
- 阈值 0.1 保留 43.5% 边(1.35M),阈值 0.2 保留 14.1% 边(438k)
- 默认走 0.1 更保守,避免默认值过激导致 recall 下降;强化用户是显式选 0.2
- 两个都跑,让 FINDINGS 说话

---

## Q3 — numpy backend 的启用阈值?

**冻结:`GRGA_PPR_NUMPY_MIN_EDGES=50000`,实验中额外强制两种 backend 做对照**

**理由**:
- 边数 < 50k 时纯 Python 已经足够快(< 100ms),numpy 的 array 分配 + 类型转换是净开销
- 50000 是经验值,可在 bench 里调
- 变体 V6 强制 numpy,V5 强制 py;VF 走 auto(实测这个语料下必然选 numpy)

---

## Q4 — 是否测 Forward Local Push(P4)?

**冻结:❌ 不测**

**理由**:
- 上一轮已达成共识:P1+P2+P3 落地后 PPR 应 < 1s,不需要算法级改造
- P4 有语义微妙点(top-k 排序稳定性 vs eps 阈值),需要独立实验和 golden test
- **本实验若 VF 全过 → P4 归档**,不落主干

---

## Q5 — quantile 与 min_npmi 是否互斥?

**冻结:CLI 层互斥,同时给报 usage error(exit 2)**

**理由**:
- 两者内部同一条通路(都换算为 min_npmi 数值走 WHERE)
- 同时给会产生歧义:"用户给了 quantile,内部换算出的 cutoff 又低于显式 min_npmi,该听谁?"
- 明确互斥消除歧义

---

## Q6 — bench 用 wall clock 还是 perf_counter?

**冻结:`time.perf_counter()`**

**理由**:
- wall clock 受系统时钟跳变影响
- perf_counter 是单调递增高精度时钟,专门做基准测量用
- 与 grga 主干里(`deep_search.py`)已有的 profile 逻辑保持一致

---

## Q7 — cold vs warm 如何定义?

**冻结**:
- **cold**:每次 run 前清 `adjacency_cache`,清 `functools.lru_cache`,不清 SQLite page cache(OS 级别不可控,且生产也不可控)
- **warm**:同一 process 内第二次以后的调用

**理由**:
- SQLite page cache 是内核控制,清了在生产没意义
- adjacency_cache 是应用层缓存,清与不清对应"首次调用 vs 后续调用"两种真实场景

---

## Q8 — 结果对比 Jaccard 用 doc_path 还是 (directory, doc_path)?

**冻结**:`qualified_path`(即 `Related.qualified_path` 属性,directory + doc_path 拼接)

**理由**:
- 语料里只有 1 个 scope,当前二者等价
- 但契约上 `qualified_path` 是跨 scope 稳定的唯一标识,写死用它避免未来跨 scope 时出问题

---

## Q9 — 允许 numpy 缺失时 V6 跳过吗?

**冻结:✅ 允许,report 里明确标记 SKIPPED**

**理由**:
- CI / 用户机上 numpy 可能不装
- V6 单纯是 backend 加速,缺失时的 fallback 就是 V5,不影响其他变体验证
- 主干落地时 backend 检测本就是可选依赖模式

---

## Q10 — 若 VF 未达标怎么办?

**冻结:先分档定位**

- 冷启动未达标 → 看 `load_edges + build_indexed_graph`(P2.2)
- 热启动未达标 → 看 PPR 单步(P3 numpy)或 doc 聚合 SQL
- top-20 Jaccard 未达标 → **调整默认 min_npmi 到 0.05**,重跑;仍不过则**移除默认过滤**,只作 opt-in
- 三项都过但 backend 等价性失败 → 严重问题,numpy 实现有 bug,不落 P3

**不做**:自适应默认值(会引入语料先验,违反 HC2)
