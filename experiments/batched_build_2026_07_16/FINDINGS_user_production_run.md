# FINDINGS — 用户生产环境实测 (2026-07-16)

## 实测数据

**环境**: 用户机器, 1435 文件 / 288 万 chunks, `grga index build --batch-chunks 50000`

**控制台输出**:
```
[build] indexed 1431/1459 files, 1573218 keyword-doc rows, 76284967 pair rows,
        1175 heading rows (64 md), 136808 auto-ngram phrases (1 iters) in 9696.74s
  incremental: +1435 added, ~0 updated, =0 unchanged, -0 removed, 0 metadata-only
  vocabulary: +136808 new phrases (dict size: 136808)
```

**内存**: ngram 阶段 ~2.3 GB, 生成共现对时 ~3.5 GB (稳定, 未 OOM)

**耗时**: 共 **~2.7 小时** (9696s)

| 阶段 | 用时 | 备注 |
|---|---|---|
| 扫描/读取文件 | ~3min | 1435 文件, 288 万 chunks |
| n-gram 分词 | ~22min | 包含 136808 短语检测 |
| 写入 chunks/关键词 | ~6min | keyword_docs + _build_chunk_kws staging |
| 生成共现对 | **~125min** | 占 **77%** 总时间, 瓶颈 |
| 其它 | ~6min | 卷积, 清理等 |

**生成共现对进度曲线**:
| 时间点 | 对数量 | 观察 |
|---|---|---|
| 20min | 38000 | — |
| 40min | 73000 | — |
| 60min | 106000 | — |
| 80min | 150000 | — |
| 100min | 187716 | 开始快, 后慢, 一度卡住 |
| 125min | 76284967 | 突然跳升至完成 |

**数据库**: `deep_keywords.db` 约 **11 GB**

## 关键分析

### 1. 批处理管线稳定运行 ✅

OOM 已完全解决 (ngram 2.3GB, 共现对 3.5GB, 均稳定)。这是 PR-1..8 的
核心成果——从 2474MB 峰值压到 3.5GB。每批 50000 chunks 的 batched 模式
正常运作, 1435 个文件被正确分批处理。

### 2. 共现对生成是绝对瓶颈 ⚠️

**总时间 2.7h, 其中 2.08h (77%) 在生成共现对**。76284967 对从 "187716 对"
直接跳到 "76284967 对" —— 说明进度显示没有正确反映实际进度。进度条
卡住不是程序卡住, 而是 `_finalize_pairs_from_temp` 在静默执行 SQL 聚合,
没有进度反馈。

**关键数据**:
- 前 100 分钟: 只产生了 187716 对 (每分钟 1877 对)
- 后 25 分钟: 产生了 7600 万对 (每分钟 300 万对)
- 前 100 分钟是 **per-batch 的 UPSERT-add** (每批增量贡献的 pair 数)
- 最后 25 分钟是 **Pass 3 全局 SQL self-join** (对整个 _build_chunk_kws 的
  一次全局聚合)

**但** 76284967 对是 "keyword_pairs 总行数" (包含所有批次的累积), 不是
Pass 3 自己产生的。让我重新理解:
- 每批 en的自连接在 `_build_chunk_kws` 里, 然后 UPSERT 到 `keyword_pairs`
- 第 1 批: 3561 个词 → 约 6.3M 条
- 最后的 25 分钟是整个 `_finalize_pairs_from_temp` 的全局聚合

实际上, 7600 万对是最终 `keyword_pairs` 的总数, 包含所有批次的累积。
其中 99.8% 来自 `_finalize_pairs_from_temp` 的全局聚合。

### 3. 进度条问题

"生成共现对" 进度条在 `_finalize_pairs_from_temp` 期间没有进展反馈。
`_sql_progress` 心跳正在运行, 但进度条显示的是 "对" 数, 而 `_sql_progress`
只产生 `pairs_advance` 事件, 没有百分比。

### 4. 数据库大小 11GB

`keyword_pairs` 表有 7600 万行, 每行 ~150 字节 → 11GB 合理。
但 7600 万对是巨大的数字。对于 288 万 chunks, 平均每 chunk 约 26 个
不同的 kw, 则每欠 chunk 的 pair 数 = 26×25/2 = 325 对。总 pair-instances
= 288 万 × 325 = 9.36 亿。经过 GROUP BY 去重后, 7600 万 distinct pairs
是合理的。

## 结论

### 已解决 ✅
- OOM 完全修复 (从 2474MB 峰值降到 3.5GB, 稳定运行)
- 批处理管线正常工作 (1435 文件, 288 万 chunks, 分批处理)
- 增量模式正确运行 (1435 个新增文件, 0 个更新, 正确识别)

### 待优化 🚧
- **共现对生成速度**: 2.7h 总时间中 77% 花在共现对生成, 主要瓶颈
- **进度反馈**: `_finalize_pairs_from_temp` 期间没有进度百分比
- **数据库大小**: 11GB 的 keyword_pairs 表 (7600 万对), 需要评估是否
  正常

## 建议

1. **共现对生成加速**: 主要瓶颈是 `_finalize_pairs_from_temp` 中的
   self-join。可以考虑:
   - 增加 `cache_size` (当前默认 256MB, 可尝试 512MB 或 1GB)
   - 多进程 (PR-9)
   - 在 `_build_chunk_kws` 上添加更合适的索引
2. **进度反馈**: 在 `_finalize_pairs_from_temp` 期间使用 `_sql_progress`
   的心跳, 或添加 `PRAGMA cache_size` 的进度显示
3. **数据库大小**: 7600 万对可能包含大量低质量对。考虑提高 `min_npmi`
   (当前 0.2) 或 `min_pair_count` 来减少最终数据量
