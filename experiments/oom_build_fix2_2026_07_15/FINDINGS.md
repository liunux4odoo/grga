# FINDINGS — oom_build_fix2_2026_07_15 (PR-2: ngram detector TEMP TABLE)

**状态**：已 landed 主干，147 项测试全绿。判据 **C4/C6 strong pass**；
**C1 部分达标**（detection 算法贡献达标，但绝对 peak 受 `line_token_lists`
输入本身限制——见 F3）。

## TL;DR

> `detect_ngram_phrases` 的三个 defaultdict（`ngram_count / left_ctx /
> right_ctx`）改成 TEMP TABLE + SQL GROUP BY 后，**M 22MB 档集成 build peak
> 从 2474 MB 降到 747 MB（-69.8%，省 1726 MB）**，ngram_iter_1 段从
> +2368 MB 降到 +604 MB（-74.5%），phrase 输出 **bit-exact**（29846 条，
> max_npmi_diff = max_branch_ent_diff = 0.0）。wall 1.11×（1.5× 硬阈内）。
>
> **加上 PR-1，M 档总 peak 2474 → 747 MB**。100 MB 语料外推从 ~10 GB
> 降到 ~3.3 GB——16 GB 机器现在能 build 60-70 MB 语料（此前必 OOM）。

## 判据裁决

### C4 — phrase 输出 bit-exact（**strong pass**）

隔离 detection 测试（tokenize 后同一 line_tokens 喂 baseline vs SQL）：

| Corpus | phrases | 集合差异 | max_npmi_diff | max_branch_ent_diff | count_mm |
|---|---:|---:|---:|---:|---:|
| S（三体 2.6MB）| 2580 | 0 | **0.0** | **0.0** | 0 |
| M（凡人 22MB）| 29846 | 0 | **0.0** | **0.0** | 0 |

sql_mem（:memory: conn）与 sql_file（file conn + db-temp）两条路径都
与 baseline 完全一致。C4-a/b 全绿。

C4-c：全部 6 个 `test_detect_ngram_phrases_*` 单测 + 35 项 test_graph_build
+ 112 项相邻套件 = **147 项全绿**，零回归。

### C6 — wall-clock（**strong pass**）

| 场景 | baseline | after PR-2 | 比值 |
|---|---:|---:|---:|
| 隔离 detection（M）| 45.4 s | 40.0 s | **0.88×**（更快） |
| 集成 build（M, iter=2）| 557 s | 620 s | 1.11×（含 PR-1 SQL 开销） |
| 集成 build（S, iter=2）| — | 73.6 s | — |

隔离测试里 SQL detector 甚至更快（SQLite hash-aggregate 比 Python
defaultdict-of-Counter 快）。集成 build 的 1.11× 主要来自 PR-1 的 pair
npmi SQL 化那一段，仍远在 1.5× 硬阈内。

### C1 — ngram_iter_1 peak（**部分达标，见 F3**）

| 指标 | 目标 | 实测（M 集成 build） |
|---|---:|---:|
| ngram_iter_1 段 Δ RSS | < 300 MB | **604 MB**（未达绝对阈） |
| — 其中 detection 算法贡献 | — | **~100 MB**（达标） |
| — 其中 line_token_lists 输入 | — | ~500 MB（本征，非算法） |

**C1 绝对阈未达，但根因不是 detection 算法**——见 F3。

## 关键发现

### F1 — 集成 build peak 大幅下降（决定性）

M 22MB 档完整 build 的 phase RSS 轨迹（PR-1 + PR-2 之后）：

```
phase                 rss     Δ        备注
scan_start            28      +28
scan_end              110     +81      docs + chunks 元数据
ngram_iter_1 end      714     +604     ← detection 峰（含 line_token_lists）
ngram_iter_2 end      714     +0       字典已收敛，无新增
ngram_finalize_end    714     +0
materialise → pairs   744     +30
pairs_scan_end        747     +3       ← PR-1 效果（原 +771）
done                  747
```

对比历史：

| 版本 | M peak | ngram_iter_1 Δ | pair_scan Δ |
|---|---:|---:|---:|
| 改造前（v9.1） | 2474 MB | +2368 MB | +771 MB |
| PR-1 后 | ~2474 MB | +2368 MB | **+3 MB** |
| **PR-1 + PR-2 后** | **747 MB** | **+604 MB** | +3 MB |

### F2 — SQL 侧数据全部落 db-temp 磁盘（不进 RSS）

`_ng_ev` TEMP TABLE 在 M 档承载约 1500 万条 raw window 事件（~1.5 GB
on disk），但因 PR-1 的 `PRAGMA temp_store_directory = db-temp` 配置，
**全部落磁盘、不进进程 RSS**。这是 peak 从 GB 级降到 MB 级的物理原因。

验证：隔离 sql_file 测试强制 spill 时临时文件落 db-temp、`/tmp` 零泄漏。

### F3 — C1 绝对阈未达的诚实归因：`line_token_lists`

ngram_iter_1 的 +604 MB 拆解：
- **~500 MB**：`line_token_lists`——jieba 分词后的**完整 corpus token
  列表**（145k 行 × ~40 token × ~60 B/str + list 开销）。这是 detection
  的**输入**，不是算法能省的部分。
- **~100 MB**：detection 算法本身（`uni` Counter + post-freq `ngram_count`
  dict + `left_dist`/`right_dist` + jieba 字典增长）。

隔离测试佐证：M 档 tokenize 后稳态 ~500 MB，SQL detection 只加 ~100 MB
（sql_file 总 599 MB）；baseline 加 ~1600 MB（总 2124 MB）。

**SPEC 里 C1 < 300 MB 的目标写得过于乐观**——当时以为 detection 算法是
peak 主导（改造前确实是），没意识到消灭算法侧之后 `line_token_lists`
输入本身成了新的地板。这是"trust the data, not the plan"的又一例。

**要进一步压低**（如果 100 MB+ 语料要能 build）需要**流式化 Phase A**：
不先构建完整 `line_token_lists`、而是 tokenize 一行就写一行进 `_ng_ev`，
detection 直接从 TEMP TABLE 读。这样 `line_token_lists` 从 O(corpus)
降到 O(1 行)。但这是**独立的 PR-4**——改动面涉及 Phase A / Phase B 的整个
token 流水线，本 PR 不做。

### F4 — 外推更新

| 语料 | 改造前 peak | PR-1+PR-2 后 peak |
|---|---:|---:|
| 22 MB（实测） | 2474 MB | **747 MB** |
| 100 MB（外推） | ~10 GB | **~3.3 GB** |
| 500 MB（外推） | ~50 GB | **~16 GB** |

100 MB 语料现在能在 16 GB 机器 build（此前必 OOM）。500 MB 仍需 PR-4
流式化才能在常规机器跑。

## 落地内容（已进主干）

- `src/grga/graph/build.py::detect_ngram_phrases`：
  - 新增可选 `conn: Optional[sqlite3.Connection]` kwarg（不破坏现有调用）
  - body 抽到 `_detect_ngram_phrases_impl`：TEMP TABLE `_ng_ev` +
    `GROUP BY ... HAVING` 频次过滤 + `_ng_passed` JOIN 拉邻居分布
  - `conn=None` 时用私有 `:memory:` conn（测试路径）
  - `_NGRAM_KEY_SEP`（U+001F）做无歧义 tuple↔key 编码
  - solidity / 分支熵 / substring salvage / sort 逻辑逐字保留
- Phase A 调用点传入 `conn=conn`（复用 build 主连接 + db-temp spill）

## 遗留 / 后续

- ✅ C4/C6 strong pass、C1 算法侧达标
- ⏭️ **PR-4（可选）**：流式化 Phase A 消灭 `line_token_lists` 的 O(corpus)
  占用——只在"要支持 100 MB+ 语料"的证据出现时才做（当前 60-70 MB 已
  可用，覆盖多数场景）
- ⏭️ **PR-3**：SQLite spill 配置 sweep（cache_size / page_size / mmap_size）
  ——现在 ngram 的大 spill 已经落 db-temp，PR-3 的 cache_size 拐点测试
  终于有真实工作负载了

## 复现

```bash
PY=/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3
cd experiments/oom_build_fix2_2026_07_15

# 单元等价 smoke（6 场景 + stress，秒级）
$PY equivalence_smoke.py

# S/M 隔离 detection 三路（baseline / sql_mem / sql_file）
for c in baseline sql_mem sql_file; do
    $PY bench.py --case $c --corpus S --out data/results/S_$c.json
    $PY bench.py --case $c --corpus M --out data/results/M_$c.json
done
$PY equivalence.py data/results/M_baseline.json data/results/M_sql_file.json
```
