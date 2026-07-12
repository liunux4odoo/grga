# subgraph PPR 可行性实验（keyword-primary 决策的证据链）

**日期**：2026-07-12
**关联决策**：`docs/design/plan-2026-07-12-keyword-primary.md`
**结论**：局部 PPR / 全图 PPR 双否决，架构应换成 keyword-primary（pair 字典化，
无 PPR，`related --mode graph` 只返回候选关键词）。

---

## 起因

`related --mode graph` 冷启动 3s，且 keyword_pairs 表在 2 个文件规模下已 329 MB，
随语料线性膨胀。两个思考方向：
1. 换用**局部 PPR**（种子 K 跳子图上跑 PPR）能否同时省时间和空间？
2. 或者干脆**移除 PPR**，把 pair 表当字典化知识资产？

---

## 脚本目录

| 文件 | 作用 | 现状 |
|---|---|---|
| `subgraph.py` | LazyKeywordSubgraph v1：BFS 展开 + top-M 前沿剪枝 | ✅ 可运行 |
| `subgraph_v2.py` | 修正版：top-M 同时限制节点入图 + 前沿传播 | ✅ 可运行 |
| `npmi_cost.py` | 存 npmi vs 现算 npmi 的成本对比（决策 D1 依据） | ✅ 可运行 |
| `run_exp.py` | 全图 PPR vs 展开子图 PPR 的 Jaccard/recall 对比 | ⚠️ **HISTORICAL** — 依赖已删除的 `grga.graph.ppr`（Phase 3b-2 commit `7a232c2`）。数据结论已量化保留在下文，如需重跑请 checkout `7a232c2^`。 |
| `run_exp_v2.py` | v2：参数扫描 + 真实实体 seed | ⚠️ **HISTORICAL** — 同上 |
| `oracle_exp.py` | **决定性实验**：全图 PPR top-60 作为"神仙 oracle 子图"再跑 PPR | ⚠️ **HISTORICAL** — 同上 |

所有脚本硬编码 `~/.config/grga/deep_keywords.db` 作为数据源。

---

## 关键实验数据

### 1. 局部 PPR 语义损失

在 `run_exp_v2.py` 最好的参数（H3/M20，子图 ~2500 节点、~15K 边）下：

| 指标 | 全图 PPR baseline | 展开子图 PPR |
|---|---:|---:|
| Jaccard@30 | 1.000 | 0.601 |
| recall@10 | 1.000 | 0.764 |
| recall@30 | 1.000 | 0.739 |
| 构建 + PPR 耗时 | 2.8s 加载 + 200ms PPR | **2.5s + 164ms** |

**没赚到时间，也损失 40% 语义**。

### 2. Oracle 天花板（`oracle_exp.py`）

取全图 PPR top-60 作为节点集（"神仙般准确的展开"），在这个诱导子图上重跑 PPR：

| 指标 | 平均 |
|---|---:|
| match@10 vs 全图 top-30 | **0.520** |
| match@30 vs 全图 top-30 | **0.650** |
| Jaccard@30 | **0.495** |

**这是理论天花板**。子图 PPR ≠ 全图 PPR，是 PPR 抗截断能力的**数学本质问题**，
不是展开规则不好。

### 3. 1 跳加权求和基线（`oracle_exp.py`）

**完全不用 PPR，直接对 seed 的 1 跳邻居按 edge_weight 求和排序**：

| 指标 | 1-hop baseline | Oracle 子图 PPR |
|---|---:|---:|
| Jaccard@30 vs 全图 | **0.509** | 0.495 |
| recall@10 | **0.560** | 0.520 |

**局部 PPR 的算力 ≈ 完全不做 PPR** —— PPR 传递的信号量白花。

### 4. SQL 硬地板（`run_exp_v2.py` 顺带诊断）

- OR 单种子：250 ms（PK 扫描，2.8M 行）
- UNION 单种子：208 ms（第二支还是 PK 扫描）
- OR 30 节点前沿：520 ms
- UNION 30 节点前沿：330 ms

`WHERE (kw_a_id IN ... OR kw_b_id IN ...)` 无法命中索引，退化为 `dir_id=?` 全扫描。
局部 PPR 每跳有 200-500ms 的硬地板，2 跳最少 400ms。

### 5. npmi 存储 vs 现算（`npmi_cost.py`）

严过滤 pair 表（312K 行）+ `(kw_id, npmi DESC)` 索引：

| fanout | STORED idx+LIMIT | COMPUTE all+sort |
|---:|---:|---:|
| 6 | 0.026 ms | 0.015 ms |
| 75 | 0.048 ms | 0.142 ms |
| 382 | 0.066 ms | 0.727 ms |
| **1498** | **0.076 ms** | **2.889 ms** |

STORED 用 index-only + 早停，恒 O(M)；COMPUTE 必须取完所有邻居 + Python 算 npmi + sort，
线性 O(fanout)。千级文件 hub seed fanout 5K-10K 时差距进一步拉开。**存 npmi**。

---

## 决策链

```
Q1: 局部 PPR 能救 keyword_pairs 存储 + PPR 冷启动吗？
    → 用 subgraph_v2.py 展开 + PPR：Jaccard 0.60（太差）+ 耗时 2.5s（没赚）
    → 用 oracle_exp.py 神仙 oracle：天花板 Jaccard 0.65（不是规则问题）
    → 用 1-hop baseline：Jaccard 0.51（局部 PPR ≈ 白花力气）
    ∴ 否决局部 PPR

Q2: 完全移除 PPR 可行吗？
    → 1-hop baseline 已经跟"神仙 oracle 子图 PPR"打平
    → caller 天然多轮，多跳桥接移到 caller pipe ripgrep
    ∴ 移除 PPR

Q3: 移除 PPR 后 keyword_pairs 用什么形态存？
    → 严过滤（co>=5, npmi>=0.2）: 279 万 → 16.7 万行（10 MB）
    → 存 npmi + 双索引: fanout 1498 时 0.076ms 查询
    ∴ 把 keyword_pairs 当字典化知识资产,keyword_chunks 可删

Q4: keyword_chunks 能删吗?
    → 唯一 doc 映射用途来自 mode=graph（返回文档）
    → 但 D2 说 mode=graph 只返 keyword,无需 doc 映射
    → chunks 表唯一读者 (npmi 分母 n_chunks) 用 scope_stats scalar 替代
    ∴ 删除 keyword_chunks + chunks
```

---

## 复现方式

```bash
# 前提：~/.config/grga/deep_keywords.db 已由 grga index build 生成
export PATH="/media/lqw/DATA/bin:$PATH"
PY=/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3
$PY experiments/subgraph_ppr_2026_07_12/run_exp_v2.py
$PY experiments/subgraph_ppr_2026_07_12/oracle_exp.py
$PY experiments/subgraph_ppr_2026_07_12/npmi_cost.py
```

**注意**：脚本硬编码开发机 DB 路径，非普遍可移植。作为决策证据保留，不作为回归测试。
