# report_perf — related_perf_2026_07_12

> 自动生成 by analyze.py。原始数据 data/results/*.json。

## 1. 总性能(3 档 seed 均值)

| Variant | cold 均值 (ms) | warm_p50 均值 (ms) | n_edges | n_nodes | backend |
|---|---|---|---|---|---|
| V0_baseline | 67,994.8 | 66,649.3 | 3,102,801 | 35,686 | py |
| V1_stopword | 61,023.8 | 60,907.6 | 2,788,895 | 33,295 | py |
| V2_npmi_0.1 | 38,026.6 | 38,040.9 | 1,265,431 | 32,866 | py |
| V2b_npmi_0.2 | 20,560.8 | 20,314.4 | 431,097 | 31,030 | py |
| V3_quantile_0.1 | 19,028.3 | 18,343.7 | 312,449 | 29,885 | py |
| V4_no_adj | 36,962.7 | 37,120.2 | 1,265,431 | 32,866 | py |
| V5_id_native | 35,746.5 | 35,746.9 | 1,265,431 | 32,866 | py |
| V6_numpy | 19,326.2 | 19,241.1 | 1,265,431 | 32,866 | numpy |
| VF_full | 19,215.6 | 19,331.4 | 1,265,431 | 32,866 | numpy |
| VF_npmi_0.2 | 13,892.6 | 13,821.4 | 431,097 | 31,030 | numpy |
| VF_p23 | 3,103.2 | 3,069.3 | 431,097 | 31,030 | numpy |

## 2. 单项收益归因(相邻变体差值,hub 档 warm_p50)

| 优化 | before → after | warm_p50 (ms) | Δ | 说明 |
|---|---|---|---|---|
| P1.1 stopword | V0_baseline→V1_stopword | 68,097.5 → 61,602.0 | +6,495.5 (-10%) | |
| P1.2 npmi=0.1 | V1_stopword→V2_npmi_0.1 | 61,602.0 → 38,079.3 | +23,522.8 (-38%) | |
| P2.1 no_adj | V2_npmi_0.1→V4_no_adj | 38,079.3 → 37,620.8 | +458.5 (-1%) | |
| P2.2 id_native | V4_no_adj→V5_id_native | 37,620.8 → 35,801.4 | +1,819.3 (-5%) | |
| P3 numpy | V5_id_native→V6_numpy | 35,801.4 → 19,478.1 | +16,323.4 (-46%) | |
| P2.3 doc-agg id | VF_npmi_0.2→VF_p23 | 13,915.6 → 3,074.0 | +10,841.6 (-78%) | |

## 3. 子步骤耗时对比(hub 档 cold run)

| step | V0 (ms) | VF (ms) |
|---|---|---|
| sql_fetch | 6,513.4 | 2,345.1 |
| fold_aggregate | 7,338.4 | 0.0 |
| build_indexed_graph | 3,256.7 | 1,514.0 |
| build_adjacency | 2,716.0 | 0.0 |
| ppr | 35,066.7 | 270.7 |
| doc_agg_sql | 14,635.9 | 11,310.0 |
| id_aggregate | 0.0 | 2,432.6 |
| translate_ids | 0.0 | 110.9 |
| csr_build | 0.0 | 1,210.5 |

## 4. 正确性 top-20 doc Jaccard (VF vs V0)

| 档 | Jaccard | 判据 | 结论 |
|---|---|---|---|
| hub | 1.000 | ≥0.6 | ✅ pass |
| mid | 1.000 | ≥0.8 | ✅ pass |
| rare | 1.000 | ≥0.85 | ✅ pass |

## 5. backend 等价性 top-20 keyword Jaccard (V6 numpy vs V5 py)

| 档 | keyword Jaccard | 判据 |
|---|---|---|
| hub | 1.000 | =1.0 ✅ |
| mid | 1.000 | =1.0 ✅ |
| rare | 1.000 | =1.0 ✅ |

## 6. quantile 采样成本(P1.4a, C5)

- hub: cutoff=0.2304933084700777, 采样耗时=2650.1 ms (判据 <200ms: ❌)
- mid: cutoff=0.2304933084700777, 采样耗时=530.9 ms (判据 <200ms: ❌)
- rare: cutoff=0.2304933084700777, 采样耗时=521.1 ms (判据 <200ms: ❌)
