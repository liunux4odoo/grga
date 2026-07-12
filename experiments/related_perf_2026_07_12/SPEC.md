# SPEC — related_perf_2026_07_12

> **状态**: 实验规范,tracer bullet 实现进行中。
> **目标读者**: 未来落地此实验的开发者(人或 agent)。
> **契约来源**: 2026-07-12 与用户的六轮讨论 (`docs/design/plan-2026-XX-related-perf.md` 待写)。
> **决策路径**: experiments 里跑通所有变体的**组合叠加对比** → 数据过关再进主干 `src/grga/graph/*`。

## 0. TL;DR

**要验证的核心命题**:`related --mode graph` 的性能可以通过 **6 项独立优化的组合叠加**从
"冷启动 109.7s / 热启动 48.1s"降到"冷 <3s / 热 <500ms",同时**不引入语料先验、不破坏 --json 契约、不新增必需依赖**。

**优化清单**(各自可开关、可叠加):

| ID | 名称 | 层次 | 预期收益 |
|---|---|---|---|
| P1.1 | stopword ∪ core_boundaries | 建图阶段(模拟) | hub 边少 |
| P1.2 | `min_npmi` 阈值过滤 | 加载边 | 边数砍到 14-44% |
| P1.4a | `edge_quantile` 采样分位点 → cutoff | 加载边入口糖 | 与 P1.2 同路径 |
| P2.1 | 删热路径的 `build_adjacency` | adjacency_cache | 冷启动省 ~10s |
| P2.2 | id-native 图构建 | 加载边 + 建图 | load 与 build_indexed_graph 大幅省 |
| **P2.3** | **doc 聚合 id-native**(v3 新增) | **doc 聚合** | **doc_agg 11s→53ms,F1 催生** |
| P3 | numpy CSR SpMV backend | PPR 幂迭代 | 34s → <1s |

**判据方向**:同一份 seed 集合、同一数据库(`/home/lqw/.config/grga/deep_keywords.db`, 478 MB, 310 万边)下:

1. **性能**:冷启动 <3s、热启动 <500ms(见 §6 详细目标)
2. **正确性**:top-20 结果与 baseline 的 Jaccard 相似度 ≥ 0.85(P1 阶段允许显著变化,因为语义门槛真的提高了)
3. **backend 等价性**:numpy 版 top-20 keyword 集合与 py 版严格相等
4. **契约稳定**:`graph_signals` / `graph_actions` schema 不变

**决策门**:P1.2 + P2.1 + P2.2 + P3 全过 → 主干迁移;任一劣化 → 归档为负面结果或推迟。

---

## 1. 背景与动机

### 1.1 触发问题

上一轮性能诊断实测:
- 冷启动 `related --mode graph` = 109.7s
- 热启动(adjacency 缓存命中)仍 = 48.1s
- 瓶颈拆解:
  - `load_edges` (含 JOIN + casefold) = 34.7s
  - `build_adjacency` (**从不使用**) = 9.8s
  - `build_indexed_graph` = 13.9s
  - PPR 幂迭代 = 34.0s

规模:9.4 万 keyword 节点、310 万 keyword_pairs 边、157k chunks。

### 1.2 根本原因(六轮讨论共识)

- `ppr.py` 顶部注释假设"图很小(几千节点)",实际大了近 1000 倍
- 建图时索引层 stopword 覆盖度停留在 v1 时代(14 个中文虚词),没跟上 chunk 迁移
- 存的是 id,加载时 JOIN 回文本、Python 里 casefold 620 万次、再 intern 回 id——**id → 文本 → id 的一整圈无用功**
- `adjacency_cache` 无条件构建 dict adjacency,但热路径全走 indexed 版
- PPR 是手写 Python 三重循环,在 310 万边上做 20 轮 = 1.24 亿次浮点操作

### 1.3 与既有决策的关系

- **AGENTS.md §信号暴露原则**:所有优化都在**读路径**,不动 grga 的 `--json` 契约字段
- **AGENTS.md §HC2 无语料先验**:P1.1 只是让**已经存在**的 `_CORE_STOP_BOUNDARIES`(26 字结构虚词,detection 层原本就有)服务于索引层;不新增词表
- **plan-2026-07-11-schema-v5-normalization.md**:v5 已把 keyword 存成 id,本实验的 P2.2 兑现"存 id 就直接用 id"的隐含语义
- **trace-signals-v1**:P1.2 后 `neighbor_edges` 语义微变(门槛从"任何非零权重"提高到"npmi≥cutoff"),需在 trace-signals.md 补版本说明

---

## 2. 语料与 seed

### 2.1 数据库

- 生产库:`/home/lqw/.config/grga/deep_keywords.db`(478 MB)
- 只读打开(`file:...?mode=ro`),**不修改**
- scope:`/media/lqw/DATA/sync/temp`(单一 dir,3,102,801 边)

**P1.1 stopword union 的模拟策略**:
生产库已建好,直接测 P1.1 需要 rebuild(几十分钟)。本实验用**加载后过滤**模拟:
- 拉边前先取 `_INDEX_STOPWORDS ∪ _CORE_STOP_BOUNDARIES` 里所有词的 kw_id
- 加载边时 SQL WHERE 加 `AND kw_a_id NOT IN (...) AND kw_b_id NOT IN (...)`
- 结果对性能的影响近似 rebuild(边数变化 + Python 侧节点/边少)
- **不能测**:重建后 keyword_chunks / npmi 重算带来的间接影响。这一项在主干落地时再验证。

### 2.2 seed 集合

三档,共 3 组 × 5 seed(§6 判据按每档独立评估):

**A. hub 档(高频虚词/常用词)**:`韩立`、`元婴`、`修士`、`法宝`、`掐诀`
- 度数排名靠前的语义词。测试"扩散到全图"的最差情况。

**B. mid 档(语料专有词,中频)**:`罗辑`、`面壁者`、`章北海`、`叶文洁`、`智子`
- 三体系列词,在语料中确实存在但不是 hub。测试正常检索场景。

**C. rare 档(低频专有名词)**:`寒骊上人`、`赫尔辛根默斯肯`、`元婴后期修士`、`韩立目光闪动几下`、`一击没有得手`
- 从 auto_ngram_dict 里 count < 100 的 4-gram 抽的。测试"边少 + 邻域窄"场景。

seed 存活性在启动时校验:seed 若不在 `keywords` 表(intern 后)则该组跳过并 warn,不影响其他组。

---

## 3. 单变量隔离原则(配置矩阵)

沿用项目实验传统——每个变量独立开关,通过组合叠加暴露单项收益。

### 3.1 配置维度

```python
@dataclass
class BenchConfig:
    # P1.1 stopword ∪ core_boundaries(模拟)
    apply_stopword_union: bool = False
    # P1.2 npmi 阈值(0 = 不过滤)
    min_npmi: float = 0.0
    # P1.4a quantile 便捷入口(与 min_npmi 互斥)
    edge_quantile: Optional[float] = None
    # P2.1 跳过 build_adjacency
    skip_build_adjacency: bool = False
    # P2.2 id-native 图加载
    id_native: bool = False
    # P3 PPR backend
    ppr_backend: str = "py"  # py | numpy
```

### 3.2 变体清单(累积叠加)

| Variant | 配置 | 目的 |
|---|---|---|
| V0 baseline | 全默认(纯复刻 main 行为) | 性能基线 |
| V1 stopword | + P1.1 | 单项测:stopword 剪枝效果 |
| V2 npmi_0.1 | + P1.2 (min_npmi=0.1) | 单项测:npmi 剪枝(默认阈值) |
| V2b npmi_0.2 | + P1.2 (min_npmi=0.2) | 单项测:更激进阈值 |
| V3 quantile_0.1 | + P1.4a (edge_quantile=0.1) | 单项测:quantile 便捷入口 |
| V4 no_adj | + P2.1 | 单项测:删死代码收益 |
| V5 id_native | + P2.2 | 单项测:id-native 收益 |
| V6 numpy | + P3 | 单项测:numpy backend |
| **VF full stack** | 全开(推荐默认:min_npmi=0.1, id_native, no_adj, numpy) | 目标验证 |

**Frozen 项**(禁止在本实验里同时改动):
- `iters=20`, `alpha=0.15`
- limit=20, graph_signals_limit=30
- 数据库文件本身
- keyword_pairs 的 edge_weight 公式(V0 保留 log1p 地板;V2 起去掉地板,遵循方案 §P1.3)

---

## 4. 算法规范

### 4.1 公共基线(V0)

严格复刻 `src/grga/graph/{ppr,adjacency_cache,related}.py` 现状:
- `load_edges`: 拉 keyword_pairs JOIN keywords 回文本,`_fold(casefold+strip)`
- `edge_weight`: chunk_cooccur = `max(npmi,0) + 0.01*log1p(count)`,含地板
- adjacency_cache: 无条件 `build_adjacency(edges)`
- PPR: 纯 Python 幂迭代 20 轮

### 4.2 P1.1 stopword union(模拟)

```python
STOPWORD_TEXT = _STOPWORDS ∪ _CORE_STOP_BOUNDARIES  # 60+26=~86 词
stopword_ids = SELECT id FROM keywords WHERE kw IN (STOPWORD_TEXT)
edges_sql WHERE = "... AND kw_a_id NOT IN (stopword_ids) AND kw_b_id NOT IN (stopword_ids)"
```

### 4.3 P1.2 min_npmi

```python
edges_sql WHERE = "... AND (source != 'chunk_cooccur' OR npmi >= :min_npmi)"
edge_weight:
    chunk_cooccur -> max(npmi, 0)  # 去掉 0.01*log1p(count) 地板
    llm_promoted  -> log1p(count)  # 保留(npmi 常 NULL)
```

### 4.4 P1.4a quantile → cutoff(采样)

```python
def _resolve_quantile_cutoff(conn, scope_dir_id, quantile, sample_size=100_000):
    n = SELECT COUNT(*) FROM keyword_pairs WHERE dir_id=?
    if n <= sample_size:
        # 全量精确
        cutoff = SELECT npmi FROM keyword_pairs WHERE dir_id=?
                 ORDER BY npmi DESC LIMIT 1 OFFSET n*quantile
    else:
        # 采样估算
        step = n // sample_size
        vals = SELECT npmi FROM keyword_pairs WHERE dir_id=? AND (rowid % step) = 0
        vals.sort(reverse=True)
        cutoff = vals[int(len(vals) * quantile)]
    return cutoff
```

### 4.5 P2.1 skip build_adjacency

adjacency_cache 层只构建 `IndexedGraph`,返回值三元组的 dict 位改 `None`。

### 4.6 P2.2 id-native

```python
def load_edges_indexed(conn, *, directory, min_npmi):
    """直接 SELECT kw_a_id, kw_b_id, source, cooccur_count, npmi
    (无 JOIN),Python 侧用 int 键聚合。"""
    ...
    # 收集所有出现的 kw_id,批量 SELECT id, kw FROM keywords WHERE id IN (...)
    # 用这份 id→text 反查表构建 IndexedGraph.node_ids(str 数组)
    #   → PPR 内部仍用 int 索引,但对外仍以 keyword 字符串暴露
```

**关键**:`IndexedGraph.node_ids` 保持 str 数组(方案 2a),trace/signals/输出零改动。

### 4.7 P3 numpy CSR backend

```python
@dataclass
class CSRGraph:
    n: int
    indptr: np.ndarray  # int32, (n+1,)
    indices: np.ndarray  # int32, (nnz,)
    data: np.ndarray   # float64, (nnz,)
    out_degree: np.ndarray  # float64, (n,)

def _ppr_numpy(seeds_ids, csr, *, alpha, iters):
    # SpMV 幂迭代:np.add.at + np.where dangling leak
    ...
```

无 scipy 依赖。backend 选择:`--backend py|numpy|auto`,auto 阈值 `n_edges >= 50_000`。

---

## 5. 实验目录布局

```
experiments/related_perf_2026_07_12/
├── SPEC.md                         # 本文件
├── README.md                       # 一句话结论 + 使用说明
├── FINDINGS.md                     # 关键发现(正/负面均记)
├── QUESTIONS.md                    # 决策冻结
├── bench.py                        # 公共基准框架 + 变体实现
├── run_all.py                      # 一键跑所有变体
├── analyze.py                      # 生成对比表格
├── data/
│   ├── seeds.json                  # 三档 seed 集合
│   └── results/                    # 每次 run 的原始 json
│       ├── V0_baseline.json
│       ├── V1_stopword.json
│       ├── V2_npmi_0.1.json
│       ├── V2b_npmi_0.2.json
│       ├── V3_quantile_0.1.json
│       ├── V4_no_adj.json
│       ├── V5_id_native.json
│       ├── V6_numpy.json
│       └── VF_full.json
└── report_perf.md                  # 详细性能对比(总/分档/子步骤)
```

**results/*.json schema**:
```json
{
  "variant": "V2_npmi_0.1",
  "config": { ... BenchConfig dict ... },
  "seeds_group": "hub",
  "seed_list": ["韩立", ...],
  "runs": [
    {
      "run_id": 0,
      "cold_or_warm": "cold",
      "total_ms": 3421.5,
      "step_ms": {
        "load_edges": 1234.0,
        "build_indexed_graph": 456.0,
        "ppr": 890.0,
        "doc_agg_sql": 234.0,
        "neighbor_edges": 45.0
      },
      "n_edges_loaded": 438949,
      "n_nodes": 34210,
      "ppr_backend_used": "numpy"
    },
    { "run_id": 1, "cold_or_warm": "warm", ... }
  ],
  "top_20_keywords": ["法宝", "元婴", ...],
  "top_20_docs": [{"doc_path": "...", "score": 0.123}, ...]
}
```

---

## 6. 判据(Acceptance Criteria)

### C1 — 性能目标

按 VF full stack 的**热启动 5 次运行 p50** 判定:

| 指标 | 目标 | 严阈值(strong pass) |
|---|---|---|
| 冷启动 total(3 seed 组均值) | < 3000ms | < 1500ms |
| 热启动 total(3 seed 组均值) | < 500ms | < 200ms |
| PPR 单步耗时(numpy backend) | < 1000ms | < 300ms |
| load_edges + build_indexed_graph(id-native) | < 3000ms | < 1500ms |

### C2 — 单项收益可归因

每个变体相对 V0 的收益要**方向正确**:

| 变体 | 预期 |
|---|---|
| V1 stopword | 节点数 -↓、边数 -↓、总耗时 -↓ |
| V2 npmi_0.1 | 边数 -50%+、PPR 耗时 -30%+ |
| V4 no_adj | 冷启动 -8s+ |
| V5 id_native | load_edges -80%+、build_indexed_graph -80%+ |
| V6 numpy | PPR 耗时 -80%+ (仅在 n_edges>50k 时) |

### C3 — 正确性(top-K 稳定性)

对每档 seed 组,top-20 doc 列表的 Jaccard(V0, VF):

| 档 | 最低要求 | 说明 |
|---|---|---|
| hub | ≥ 0.60 | hub 邻域大,门槛提高会显著换血,允许较大变化 |
| mid | ≥ 0.80 | 主要场景,不该显著劣化 |
| rare | ≥ 0.85 | 稀有词邻域窄,基本应保持稳定 |

**backend 等价性**(V5 vs V6):top-20 keyword 集合(不算 doc) Jaccard = 1.0(严等价,只允许浮点误差导致的最后一位并列翻转)。

### C4 — 契约不变

- `graph_signals` 字段名/类型/schema 100% 一致
- `neighbor_edges` 语义微变(门槛提升),但结构不变
- `graph_actions` 词汇表未使用新 op / 新 node 前缀

### C5 — quantile 便捷入口的成本

P1.4a `_resolve_quantile_cutoff` 采样版单次调用 < 200ms(scope 3M 边)。若 ≥ 500ms 说明采样策略要优化。

---

## 7. QUESTIONS 冻结(见 QUESTIONS.md)

见 `QUESTIONS.md`,列表:

| Q | 内容 | 冻结值 |
|---|---|---|
| Q1 | P1.1 走 rebuild 还是模拟 | ✅ 模拟(SQL WHERE NOT IN);主干落地时才 rebuild |
| Q2 | min_npmi 默认值 | 0.1(实验中同时测 0.2 作激进版本对照) |
| Q3 | numpy backend 阈值 | 50000 边;实验里同时测强制两种 backend |
| Q4 | 是否测 Forward Push (P4) | ❌ 本实验不测;VF 全过则归档,不进 P4 |
| Q5 | quantile / min_npmi 是否互斥 | ✅ 互斥(CLI 层拒绝同时给) |

---

## 8. 决策后动作

**Pass (C1-C4 全过)**:
- 起草主干迁移 PR。分阶段:P1 → P2 → P3 各独立 PR
- 更新 `docs/design/plan-2026-XX-related-perf.md`(把本 SPEC 提炼成设计文档)
- 更新 `docs/design/trace-signals.md`:补 neighbor_edges 语义微变说明
- 更新 `docs/navigation.md`:P3 引入 backend 层入口点

**Partial pass**:
- FINDINGS 记录劣化根因;保留 experiments 作参考实现;主干只落 pass 的部分

**Fail**:
- FINDINGS 记录、归档,不进主干。方案设计假设需要重审

---

## 9. 一句话给未来读者

**这个实验要证的是"6 项独立优化叠加后能同时达成 30-100× 性能提升与 top-20 结果稳定",不是"某一项单独就够"。判据挂在性能、正确性、契约稳定三条量化指标上,每一项都可以独立开关、独立回滚。**
