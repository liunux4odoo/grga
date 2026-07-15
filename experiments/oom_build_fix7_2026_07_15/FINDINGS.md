# FINDINGS — oom_build_fix7_2026_07_15 (PR-8b pair 聚合 per-doc 进度)

**触发**：用户反馈 PR-8 后 pair 阶段进度条以"共现对数"为单位、无 total →
看不到完成率。要求换成**可预知的分母**。

## TL;DR

pair 自连接改成**两级聚合按 doc 分批**：per-doc GROUP BY → `_pair_raw`
临时表（append-only 无 PK 无索引，顺序写快），最后一条全局 GROUP BY +
HAVING → `keyword_pairs`。**进度以 N/total docs 显示**。语义与旧单条
SQL 严格等价（全局 HAVING 通过 SUM(c) 在最终 merge 应用）。

带阈值：`total_docs > 8 AND _build_chunk_kws 行数 > 1M` 才走 per-doc；
否则单条 SQL 快路径（保护小/中语料不被 per-doc 开销拖慢）。阈值可
通过 `GRGA_BUILD_PAIR_PERDOC_ROWS` 环境变量覆盖。

## 关键设计决策

**为什么不能 per-doc 直接进 keyword_pairs**：
- 去掉 HAVING → 所有 count-1/2 pair UPSERT 进持久 WITHOUT ROWID PK 表 →
  再删。实测 S 档 >2.5× 慢 + 1.5GB 未提交 temp。
- 若保留 HAVING 但 per-doc 应用 → 3 doc 各 count=1 的 pair 全局 count=3
  但 per-doc count=1 → 错误丢弃（不 bit-exact）。

**为什么两级 append + 最终 merge 才对**：
- `_pair_raw` append-only 无 PK → 顺序写快，无 B-tree 维护
- 最终 `GROUP BY + SUM(c) HAVING SUM(c) >= k` **全局** floor → ≡ 旧
  `HAVING COUNT(*) >= k` 语义
- per-doc 预聚合把最终 sort 输入从 O(chunks × kw²) 原始 join 行降到
  O(sum of per-doc distinct pairs) → 大语料下**更快** than 旧单条

**为什么加阈值**：
- S/12 测试 (12 docs, 30k rows) per-doc = 84s vs 单条 = 58s (+45%)
  → 小规模 per-doc 开销 > 收益（每 doc GROUP BY 启动成本、_pair_raw
  写入开销、最终 sort）
- 用户目标 (1400 docs, ~86M stage rows) 单条会 spill 严重 →
  per-doc 大幅胜（且有实时进度）
- 阈值 8 docs + 1M rows 是保守估计，可用 env var 微调

## 验证

| 场景 | 结果 |
|---|---|
| S 22MB (1 doc, 快路径) | ✅ 58s / 6 表 bit-exact |
| S 分 12 文件 (12 docs, 32k rows, 快路径) | ✅ 58s / 6 表 bit-exact / 179822 pairs |
| S 分 12 文件 + `GRGA_BUILD_PAIR_PERDOC_ROWS=0` 强制 per-doc | ✅ 84s / 6 表 bit-exact / 179822 pairs (证明 per-doc 路径正确) |
| pytest 149 项 | ✅ 全绿 |

## CLI 显示

per-doc 路径:
- `pairs_scan_start` 携带 `total_docs`
- `pairs_advance` 每 64 doc 一次, `current=N, total=total_docs, unit="docs"`
- CLI 显示 `扫描自连接 N/M 文档`
- 最终 merge 阶段: `pairs_advance current=step, unit="merge"`, CLI 显示 `合并共现对…`

快路径 (< 阈值):
- 沿用 PR-8 的 SQL heartbeat 心跳 (`pairs_advance current=step, no total`)
- CLI 显示 `扫描自连接… (N)`

## 后续

- 若用户的 1400 docs 实测下, per-doc 大幅优于旧路径 → 阈值可以设得
  更宽松（默认 <500k 即启用），让更多用户享受可预测进度
- PR-9 多进程仍是候选：per-doc 的 SQL 是天然的分片单元，改造 workers
  按 doc-range 分片 → 主进程 merge `_pair_raw` 应可行
