# SPEC — oom_build_fix2_2026_07_15 (PR-2: ngram detector TEMP TABLE)

> **状态**：drafting，PR-2（方案 A）实施中。
> **设计文档**：`docs/design/plan-2026-07-15-build-memory-oom.md` §3
> **前置**：PR-1 已 landed（pair npmi SQL 化 + db-temp 隔离）
> **母方法学**：`docs/design/db-memory-optimization-lessons.md`

## 0. TL;DR

按设计文档 §3 把 `detect_ngram_phrases` 里的三个 defaultdict（`ngram_count /
left_ctx / right_ctx`）改造成 TEMP TABLE + SQL GROUP BY。

**核心策略**：写入 → HAVING 过滤 → 只把过关 phrase 的邻居分布拉回 Python 算
分支熵。原地保留分支熵、substring salvage 的 Python 实现（SQL 表达不便，
输入集已经缩小）。

### 判据（硬阈）

| C | 内容 | 目标 |
|---|---|---|
| C1 | ngram_iter_1 Python heap peak（M 22MB 档） | 从 ~2368 MB → **< 300 MB** |
| C4-a | phrase 集合 Jaccard vs baseline | **= 1.0**（严等） |
| C4-b | 每个 phrase 的 `count / n / npmi / branch_ent` | **bit-exact**（`abs<1e-9` for 浮点; int 严等; round(4) 结果严等） |
| C4-c | 全部 pytest test_detect_ngram_phrases_* | **全绿** |
| C6 | wall-clock（M 档 iter=1） | **≤ 1.5× baseline** |

**软判据**：
- 分支熵浮点顺序漂移（SQL GROUP BY 顺序 vs Python dict 插入顺序）
  → 通过 `ORDER BY MIN(rowid)` 匹配 Python dict 插入序
- 最终 `results.sort` 的 tiebreak（相同 npmi+count 的 phrase 排序）
  → 上游顺序保持 = tiebreak 一致

## 1. 现状热点（已在 PR-1 前置实验 oom_build_2026_07_14 确认）

`detect_ngram_phrases` 三个数据结构随语料线性/超线性膨胀：

```python
ngram_count: Counter = Counter()                      # 全窗口计数（pre-freq）
left_ctx:  defaultdict[tuple, Counter] = defaultdict(Counter)  # per-window 左邻分布
right_ctx: defaultdict[tuple, Counter] = defaultdict(Counter)  # per-window 右邻分布
```

M 22MB 档：**+2368 MB** Python heap（F4）。iter 1 独占，占 M 档 peak 96%。

## 2. 方案 A 落地设计

### 2.1 API

保持 `detect_ngram_phrases(line_tokens, *, ...)` 签名兼容，新增可选
`conn: Optional[sqlite3.Connection]` kwarg：

- `conn=None`（默认）：内部 `sqlite3.connect(":memory:")`，测试路径。小
  输入 OK，大输入会吃内存（无 db-temp）——但这个分支只给 pytest 用。
- `conn=<主 build conn>`：`build_graph_index` Phase A 传入，享受 PR-1
  之前配置的 `PRAGMA temp_store = FILE` + `temp_store_directory = db-temp`。
  大输入自动 spill 到磁盘。

**保证**：conn 是可选的、不改现有调用签名 → 现有 pytest 单测零改动。

### 2.2 数据流

```
Stage 1: 事件流
  ├── Python 一次扫 line_tokens：
  │     - 生成所有 n∈[2,max_n] 的 windows（跳过含 stop_boundary 的）
  │     - 每 200k 行 executemany 写入 TEMP TABLE _ng_ev
  │     - 顺带更新 uni: Counter（unigrams，小、留 Python）
  └── Python peak：event buffer ~10 MB + uni ~5 MB

Stage 2: 频次 + 首见位置聚合
  SELECT phrase_key, n, MIN(rowid) AS first_seen, COUNT(*) AS c
  FROM _ng_ev
  GROUP BY phrase_key, n
  HAVING c >= :min_count
  ORDER BY first_seen               -- 匹配 Python dict 插入顺序
  ├── 只有过关 phrase 出结果集
  └── 拉回 Python 建 ngram_count（post-freq）dict：~50k 条 × ~100B ≈ 5 MB

Stage 3: 内联 solidity 过滤（Python）
  for (phrase_key, n, c) in freq_rows:
      sol = _solidity(win, c)     # 用 uni + ngram_count（post-freq） 查 splits
      if sol >= npmi_threshold:
          candidates.append(...)  # 或存 entropy_rejects
  ├── **关键洞察**：任何过关 phrase 的 splits 也必然过关（每次容器出现
  │   都携带一次 split 出现）→ splits 一定在 post-freq ngram_count 里
  └── Python peak：候选集 ~30k × ~200B ≈ 6 MB

Stage 4: 过关 phrase 的邻居分布（仅对通过 freq + solidity 的）
  CREATE TEMP TABLE _passed(phrase_key TEXT PRIMARY KEY)
  -- 灌入 candidates 的 phrase_key
  SELECT e.phrase_key, e.left_tok, COUNT(*)
  FROM _ng_ev e JOIN _passed p ON e.phrase_key = p.phrase_key
  WHERE e.left_tok IS NOT NULL
  GROUP BY e.phrase_key, e.left_tok
  -- 同样一遍 right_tok
  ├── 只对过关 phrase 拉分布 → 输入缩到千级
  └── Python peak：per_phrase 邻居 dict ~30k × 10 邻居 × ~50B ≈ 15 MB

Stage 5: 分支熵（Python，输入已过滤）
  同当前实现，只是输入集小了 100×

Stage 6: substring salvage（Python，无变化）
  同当前实现，输入是 entropy_rejects + results

Stage 7: sort + return（不变）
```

### 2.3 关键 SQL Schema

```sql
CREATE TEMP TABLE _ng_ev (
    phrase_key TEXT NOT NULL,   -- \x1f-joined token tuple（无歧义）
    n INTEGER NOT NULL,         -- 2..max_n
    left_tok TEXT,              -- NULL = 行首或被 boundary 截断
    right_tok TEXT              -- NULL = 行尾或被 boundary 截断
)
-- 无索引：pass 2 只扫一次；pass 4 通过 JOIN _passed(phrase_key PK) 加速
```

`phrase_key = "\x1f".join(win)` 避免 rejoin_by_pair_rule 潜在的 tuple 歧义。
`\x1f` (U+001F Unit Separator) 在 jieba/regex token 里不出现。

### 2.4 等价性风险点

**风险 1：dict 插入顺序 → SQL 结果顺序**

Python `ngram_count` 是 insertion-ordered dict（3.7+）。SQL `GROUP BY`
默认无序。→ 用 `ORDER BY MIN(rowid)` 显式匹配（每 phrase 首次出现的
`_ng_ev.rowid`）。

**风险 2：分支熵浮点顺序**

`_entropy = -Σ p log p`——求和顺序不同 = 结果低位漂移。
但 `results` 里存的是 `round(be, 4)`——4 位小数容忍所有可能的 ULP 漂移。
**只要 `neighbors: dict` 的迭代顺序稳定即可**——用 SQL 结果的自然顺序
（`GROUP BY phrase_key, left_tok`）填 dict。

**风险 3：substring salvage 遍历顺序**

`entropy_rejects` 列表顺序 → salvage 遍历顺序 → 最终 `results` append 顺序。
只要 stage 2 用 `ORDER BY first_seen`，rejects 保持 Python-等价序。

**风险 4：final sort tiebreak**

`results.sort(key=lambda x: (-npmi, -count))` 稳定排序。相同 (npmi,count)
的 phrase 按插入序排 → 保 stage 2 的 first_seen 序即可。

### 2.5 API 契约

保持：
- 签名（`conn` 是 optional kwarg，不改现有位置参数）
- 返回值 shape：`List[dict]` with `phrase / n / count / npmi / branch_ent`
- `on_progress` callback 词汇表：`unigram_*`, `window_*`, `filter_*`,
  `salvage_*`, `sort_*`（新实现要保留这些 tick，方便进度条不空跑）

## 3. 实验目录

```
experiments/oom_build_fix2_2026_07_15/
├── SPEC.md                       # 本文件
├── FINDINGS.md                   # 判据裁决（pass 后填）
├── README.md                     # 一句话结论（pass 后填）
├── patch/
│   └── detect_ngram_v2.py        # SQL 版本的 detect_ngram_phrases 实现
├── bench.py                      # subprocess-hosted bench（复用 fix 版）
├── equivalence.py                # phrase 集合 + 属性 diff
├── run_all.py                    # 矩阵驱动
└── data/
    ├── results/                  # bench json
    └── dumps/                    # phrase list dumps (jsonl)
```

## 4. 判据顺序

**先跑 S 档 golden**（三体 iter=1，~50s）：
- C4-a/b/c 是否全绿 → 决定 SQL 实现的算法等价性
- **不绿 = 立即否决**，回炉

**再跑 M 档**（凡人 iter=2，~8-10 分钟/run）：
- C1（peak）+ C6（wall）
- 若 C1 未过（peak > 300 MB）→ 排查是否有另外的 Python-side 未消灭的
  数据结构

## 5. 落地方案

**Pass**（C1/C4/C6 全过）：
1. 把 `patch/detect_ngram_v2.py` 的实现回移替换主干 `detect_ngram_phrases`
2. `build_graph_index` Phase A 传入 `conn` kwarg
3. FINDINGS 归档
4. `pytest tests/test_graph_build.py`（含 6 个 detect_ngram_* 单测）确保零回归
5. 端到端验证：无参数 build 一次三体，确认成本合理

**Fail**：
- FINDINGS 记录根因，PR-2 推迟或调整方案（可能拆成"只优化 left_ctx/right_ctx"
  等增量步骤）
