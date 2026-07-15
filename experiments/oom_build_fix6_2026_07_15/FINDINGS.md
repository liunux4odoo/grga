# FINDINGS — oom_build_fix6_2026_07_15 (PR-8 build 性能快赢包)

**触发**：用户 287万 chunks 实测（PR-6/PR-7 之后）—— OOM 已解、salvage 已快，
但四个新慢点：
1. 第一轮枚举 n-gram 慢（8 min），开头特慢（10% 用 2 min）后来快
2. **n-gram 结束 / 过实心门结束后进度条卡几分钟无动作**
3. 最终分词慢，同样开头特慢
4. 自连接扫描慢，问能否多进程

## 根因

**全项目未设 `PRAGMA cache_size`** —— SQLite 默认页缓存仅 2 MB。300万
chunks 时三个大 SQL（Stage 2 `_ng_freq` GROUP BY ~1亿行、Stage 4 邻居
UNION、pair 自连接）在 2 MB 缓存里翻搅 → 疯狂 spill db-temp 磁盘页。
这是现象①②③④里**三个的共同放大器**。

现象②的另一半：Stage 2 / Stage 4 是**单条大 SQL，无进度反馈** → 进度条
"卡住"实为在跑但不动。

## PR-8 快赢包（不含多进程）

| 修复 | 手法 | 影响 |
|---|---|---|
| **cache_size** | `PRAGMA cache_size = -(GRGA_BUILD_CACHE_MB×1024)`，默认 256 MB | 三大 SQL 提速；S 档 wall 71→58s (-18%) |
| **synchronous** | `PRAGMA synchronous = NORMAL`（build 期临时数据 + 单次 commit，per-COMMIT fsync 是纯开销） | 降 fsync 开销 |
| **进度反馈** | `_sql_progress` 上下文管理器：`conn.set_progress_handler(cb, 1M steps)`，每百万 VM 指令 emit `<phase>_advance` 心跳 | 现象②"卡几分钟"→ spinner 持续动 |

包住的三条大 SQL：
- Stage 2 `INSERT INTO _ng_freq ... GROUP BY ... HAVING` → phase `agg_freq`
- Stage 4 邻居 UNION-scan（ORDER BY 强制全物化）→ phase `agg_neighbors`
- pair 自连接 `INSERT INTO keyword_pairs SELECT ... JOIN ... GROUP BY` → phase `pairs_join`

CLI 加 `agg_freq`="聚合频次" / `agg_neighbors`="聚合邻居" label。
env var `GRGA_BUILD_CACHE_MB`（默认 256，300万+ 建议 512）。

## 验证

| 项 | 结果 |
|---|---|
| S 22MB 6 表 bit-exact vs baseline | ✅ 全等（245k+ 行） |
| S wall | 71 → 58s (-18%，仅 cache+sync) |
| pytest 149 项 | ✅ 全绿 |
| set_progress_handler API sanity | ✅ 48 ticks fired on 200k-row GROUP BY |

## 现象①③（开头特慢）的残留

cache_size 改善 SQLite 侧，但"开头特慢后来快"还有**磁盘冷启动**成分：
- 首次读 1400 语料文件，OS page cache 冷 → 慢；读过一遍后缓存热 → 快
- `_ng_ev` temp 文件从零增长，初期页分配开销
这部分**无代码解**（属 OS/磁盘 IO 特性）。用户可预热（`cat 语料 > /dev/null`）
或用更快的盘放 db-temp。

## 现象④（自连接）后续

PR-8 的 cache_size 预期让自连接提速数倍。**若用户实测后仍是瓶颈**，
再启 PR-9 多进程：`_build_chunk_kws` 落 db-temp 持久文件 → N worker 按
doc_id 分片算局部 pair count → 主进程 merge。工程量大、需先测 cache_size
后的真实耗时才值得做。

## 复现

```bash
GRGA_BUILD_CACHE_MB=512 grga index rebuild --scope X --directory /corpus
# 观察: agg_freq / agg_neighbors / pairs_join 阶段 spinner 是否持续动
```
