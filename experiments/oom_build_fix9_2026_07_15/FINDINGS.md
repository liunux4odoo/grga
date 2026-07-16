# FINDINGS — numpy 能否给共现对聚合提速？(实验)

**问题**：用户问 self-join 产 ~12.5 亿 pair-instance 的聚合能否用 numpy 提速。

## 实验：4 条聚合路径 × 多规模

| 规模 | A) SQLite GROUP BY | B) numpy 全局 unique | E) numpy 批+numpy merge | E peak RSS |
|---|---|---|---|---|
| 1.2M rows | 13.4s | 3.6s (3.7×) | 4.7s (2.9×) | 1.2 GB |
| 3.8M rows | 43.4s | 10.8s (4.0×) | 14.4s (3.0×) | 2.9 GB |
| 10M rows | — | — | **OOM killed** | >mem |
| 20M rows | — | — | **OOM killed** | — |

所有 numpy 路径 bit-exact vs SQLite（同 (a,b) 键 int64 编码 + np.unique）。
中间尝试的 C) Python dict merge / D) SQL UPSERT merge 都 ≈ SQL 速度或更慢
（增量 merge 是瓶颈）。

## 根本矛盾（无免费午餐）

聚合 N 个 pair-instance → distinct pair，三选一：

| 方式 | 速度 | 内存 |
|---|---|---|
| numpy 全部进 RAM (B) | 快 4× | ❌ O(pair-instances)：用户 12.5 亿 → 10GB keys + 2× 排序缓冲 = 20-30GB |
| numpy 批 + numpy merge (E) | 快 3× | ❌ merge 要 concat 所有 partial 做 np.unique(return_inverse)，3.8M 时 partials 已 694MB / peak 2.9GB，用户规模 OOM |
| numpy 批 + 增量 merge (C/D) | ✗ ≈ SQL | ✅ 有界 |
| **SQLite GROUP BY（现状）** | 慢（基准） | ✅ 外部排序 spill，cache_size 有界 |

**numpy 快是因为全在 RAM，一旦超 RAM 就崩。用户 300 万 chunks 正好超 RAM。**
SQLite GROUP BY 慢但内存最优（外部归并排序，永不 OOM）。

E 方案的 peak 拆解（3.8M rows）：
- max 单批 keys array = 60MB（有界，好）
- **partials concat = 694MB**（随批数增长）← 瓶颈
- np.unique(return_inverse) 对 partials ≈ 3-4× 输入 → 2.9GB peak
- merge 要么全 partial 进 RAM（快但 OOM），要么增量（有界但慢）

## 结论

- numpy 能让**中小语料**（工作集 < 可用 RAM）的共现聚合快 3-4×
- **救不了大语料**：用户规模的 pair 工作集超内存，numpy 全局 OOM、批+merge 也 OOM
- 真正兼顾快+有界只有两条：
  1. **PR-9 多进程**：self-join 按 doc-range 切给 N worker，每 worker 工作集
     小（装得进 RAM，可内部用 numpy 快 4×），写 partial，主进程流式 merge。
     N 核 → ~N× 加速 + 内存有界。**根本解**。
  2. **自适应**：估算 pair 工作集，装得下用 numpy 全局（中小语料快 4×），
     否则退回 SQLite GROUP BY（大语料稳但慢）。

## 复现

```bash
PY=/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3
DOCS=300 CHUNKS=500 KW=25 $PY /tmp/kilo/numpy_pair_bench.py    # A/B/C/D/E 对比
DOCS=500 CHUNKS=800 KW=25 BATCH_DOCS=50 $PY /tmp/kilo/e_only.py  # E 隔离 peak
```
