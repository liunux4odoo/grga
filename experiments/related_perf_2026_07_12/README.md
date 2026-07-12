# related_perf_2026_07_12

`related --mode graph` 性能优化实验。**contract → `SPEC.md`,决策冻结 → `QUESTIONS.md`。**

## 一句话结论

> **✅ 完整通过**——7 项优化叠加(含实验中途发现的 **P2.3 doc-agg id-native**)把冷启动从 **68s → 3.1s(-95.5%)**,PPR 35s→96ms、doc_agg 11s→53ms,top-20 结果 Jaccard=1.0 完美保序,backend 等价性 1.0。剩余 3s 全在 graph load+build,生产 `adjacency_cache` 命中后热启动预期 **~200ms**(<500ms 目标)。
>
> **最大单项收益是 P2.3(-78%)**,而它不在原方案里——是 v2 阶段 PPR 加速后 F1 暴露的新瓶颈催生的。`min_npmi` 默认定 0.2(F4:质量+性能双赢)。详见 `FINDINGS.md`。
>
> 演进:v1 首跑 → v2 code review 修正 6 处测量偏差重跑 → v3 加 P2.3 验证达标。

## 背景

上一轮诊断:`related --mode graph` 冷启动 109.7s、热启动 48.1s。瓶颈在**图规模爆炸(310 万边)+ 全图纯 Python PPR + 一处死代码 `build_adjacency`**。本实验验证 6 项独立优化叠加能否达到"冷 <3s / 热 <500ms",且不引入语料先验、不破坏 `--json` 契约、不新增必需依赖。

## 优化清单(可独立开关)

| ID | 名称 | 层次 |
|---|---|---|
| P1.1 | stopword ∪ core_boundaries(模拟) | 建图 |
| P1.2 | `min_npmi` 阈值过滤 | 加载边 |
| P1.4a | `edge_quantile` 采样分位点 → cutoff | 加载边入口糖 |
| P2.1 | 删热路径的 `build_adjacency`(死代码) | adjacency_cache |
| P2.2 | id-native 图构建(跳过 JOIN + casefold) | 加载边 + 建图 |
| P3 | numpy CSR SpMV backend(无 scipy) | PPR |

## 文件

| 文件 | 内容 |
|---|---|
| `SPEC.md` | 实验规范:变量矩阵、判据 C1-C5、决策后动作 |
| `QUESTIONS.md` | Q1-Q10 决策冻结 |
| `bench.py` | 自包含的 load→build→PPR→doc-agg 流水线,6 项优化作为 `BenchConfig` 开关 |
| `variants.py` | 变体注册表 V0..VF(累积叠加)+ 单项归因对 |
| `run_all.py` | 一键跑全矩阵,输出 `data/results/*.json` |
| `analyze.py` | 读结果生成 `report_perf.md` + 判据裁决 |
| `data/seeds.json` | 三档 seed(hub / mid / rare),已验证库里存活 |

## 复现

```bash
PY=/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3

# 跑全部变体(V0 冷启动 ~110s,全矩阵约 15-20 分钟)
$PY run_all.py

# 只跑快变体
$PY run_all.py VF_full V6_numpy V5_id_native

# 减少 warm 重复(默认 2)
BENCH_WARM_RUNS=1 $PY run_all.py

# 生成对比报告
$PY analyze.py
```

**环境变量**:
- `GRGA_BENCH_DB`:数据库路径(默认 `~/.config/grga/deep_keywords.db`)
- `GRGA_BENCH_SCOPE`:scope(默认 `/media/lqw/DATA/sync/temp`)
- `GRGA_PPR_NUMPY_MIN_EDGES`:auto backend 阈值(默认 50000)

## 注意

- **只读**打开生产库(`mode=ro`),不修改任何数据
- P1.1 用"加载后 SQL WHERE NOT IN"**模拟** stopword 重建效果(见 SPEC §2.1 / Q1);真实 rebuild 的间接收益留到主干落地验证
- 本实验**不复用**分词 build.py,只借用 experiments 的组织格式
