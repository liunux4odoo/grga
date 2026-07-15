# build 内存 OOM：ngram 检测 + pair 聚合的 TEMP TABLE 化（含断点续跑未来方向）

**日期**：2026-07-15
**关联实验**：`experiments/oom_build_2026_07_14/`（peak RSS profile，判据 C1-C4 strong pass）
**方法学母文档**：`docs/design/db-memory-optimization-lessons.md`（v9.1 pair 计数优化沉淀）
**动机**：`index build` 大语料 OOM。实测 `peak_rss ≈ 100 × corpus_mb`
（R²=1.000），100 MB 语料需 ~10 GB、500 MB 需 ~50 GB，超出常规机器。

**结论**：把 auto n-gram 检测（`detect_ngram_phrases`）和 pair npmi 重算
（`_finalize_pairs_from_temp` §4）这两处**残留的 Python 全量聚合**迁到
SQLite TEMP TABLE + SQL 聚合，套用 v9.1 pair 计数已验证的方法学。目标
peak RSS 从 ~100×/MB 降到 ~10×/MB 量级。**断点续跑本轮不做**，设计留档
（§7）供未来证据触发时参考。

---

## 0. TL;DR

| 维度 | 现状 | 目标 | 依据 |
|---|---:|---:|---|
| peak_rss / corpus_mb | ~100× | **< 15×** | 实验 F1 外推 |
| `ngram_iter_1` 峰（M 22MB） | +2368 MB | **< 300 MB** | detector 三 dict → SQL |
| `pairs_scan_end` 峰（M） | +800 MB | **< 100 MB** | npmi 重算 fetchall → SQL UPDATE |
| 100 MB 语料外推 peak | ~10 GB | **< 1.5 GB** | 两处并改后 |
| phrase 集合 / npmi 值 | — | **与主干 bit-exact** | 纯数学恒等，不允许结果漂移 |

**核心决策链**（实验 `oom_build_2026_07_14` 证据）：

1. **两个独立峰，必须并改**（F2）：sampler envelope 显示 M 档
   `ngram_iter_1`（+2368 MB）与 `pairs_scan_end`（+800 MB）两个近乎等高
   的峰，中间 RSS 只回落 <900 MB。只改一处最多减半，撑不住 100 MB+。
2. **ngram_iter_1 独占 M 峰值 96%**（F4）：detector 内 `ngram_count /
   left_ctx / right_ctx` 三个 defaultdict 从零填至 vocab × max_n 规模。
   即使砍 `ngram_iters=1`，iter 1 本身无法回避 → 必须动数据结构。
3. **pair 阶段 v9.1 没覆盖到**（F5）：`_finalize_pairs_from_temp` §4
   `pair_rows = ...fetchall()` 把全部 surviving pair 拉进 list + `updates
   / drops` 双份。v9.1 只把 pair **计数** SQL 化，**npmi 重算**仍是
   Python 全量。
4. **Python heap 占总 RSS 50-70%**（F3）：ngram 阶段 Python 与 C（jieba /
   SQLite）各半；pair 阶段 Python 32%。Python 那一半是我们能动的杠杆，
   C 侧由 SQLite `PRAGMA cache_size` / `temp_store` 控制（§6）。

---

## 1. 设计原则

### 1.1 沿用的方法学（`db-memory-optimization-lessons.md`）

> **能让 SQLite 用 SQL 完成的聚合，永远不要在 Python 端 Counter/set 里做。**

本文档是这条铁律的**第二次应用**——v9.1 把 pair **计数** 从 Python
Counter 迁到 TEMP TABLE + self-join（母文档"正例"）；本次把 ngram 检测的
n-gram 计数 + pair npmi 重算迁过去。母文档 §"元教训 2"（删表=删算法的隐藏
成本）在这里反向应验：pair 计数当年迁了，**npmi 重算这段被漏下**，成了
本次的 F5。

### 1.2 沿用的硬规则（AGENTS.md）

- **HC2 无 corpus prior**：npmi / 分支熵 / NPMI 都是纯数学恒等式。SQL 化
  只改**物理执行方式**，不改**数学定义**。结果必须 bit-exact。
- **信号暴露原则**：本次不动任何 `--json` 契约、不动 `on_event` phase 词汇
  表（实验 harness 靠现有 phase 名订阅）。纯内部实现优化。
- **共用基础设施杠杆方向**（铁律 1）：内存/磁盘负担交给 SQLite 这个共用
  层（几十年调优的 spill-to-disk / 事务），而不是每次 build 的 Python 进程。

### 1.3 非目标

- **不做**断点续跑（§7 留档，证据触发再启）。
- **不改**算法语义：phrase 检测三门（频次/solidity/分支熵）+ substring
  salvage + pair 三门（独立性/min_pair_count/min_npmi）逻辑一字不改。
- **不改** `search` / `expand` / `related` 读路径（不碰这两处聚合）。
- **不引入** numpy / pandas 依赖（SQL 能表达的用 SQL）。

---

## 2. 现状热点定位（实验数据）

### 2.1 `detect_ngram_phrases` 内部（`build.py::detect_ngram_phrases`）

```python
ngram_count: Counter = Counter()                       # ← key = vocab × max_n 个 tuple
left_ctx:  defaultdict[tuple, Counter] = defaultdict(Counter)   # ← 每个 window 一个邻居分布
right_ctx: defaultdict[tuple, Counter] = defaultdict(Counter)
for toks in line_token_lists:            # 全语料 tokens 已在内存
    for i in range(len(toks)):
        for n in range(2, max_n + 1):
            win = tuple(toks[i:i+n])     # ← 大量 tuple 分配
            ngram_count[win] += 1
            left_ctx[win][...] += 1
            right_ctx[win][...] += 1
```

M 档 33977 phrases，但 `ngram_count` 的 key 是**过滤前**的全部窗口
（远大于 phrase 数）——三个 dict 合计 +2.3 GB。

### 2.2 `_finalize_pairs_from_temp` §4（npmi 重算）

```python
pair_rows = conn.execute("SELECT ... FROM keyword_pairs ...").fetchall()  # 293k 行
updates, drops = [], []
for row in pair_rows:                    # 全量 Python 循环
    npmi = _npmi(co, df_a, df_b, n_chunks)
    (updates if keep else drops).append(...)
```

三重 tuple list（`pair_rows` + `updates` + `drops`）≈ 800 MB on M。

---

## 3. 方案 A：ngram 检测 TEMP TABLE 化

### 3.1 n-gram 频次：事件流 → SQL GROUP BY

```python
# build-local TEMP TABLE，只承载 raw n-gram 窗口事件（不聚合）
conn.execute("""
    CREATE TEMP TABLE _ngram_events (
        phrase   TEXT NOT NULL,     -- rejoin 后的窗口文本（_join_by_pair_rule）
        n        INTEGER NOT NULL,
        left_tok TEXT,              -- 左邻（NULL = 行首/被停用词边界截断）
        right_tok TEXT              -- 右邻
    )
""")
# Python 只做事件写入：list buffer + 每 200k 行 executemany（母文档骨架）
batch = []
for toks in stream_line_tokens():        # 流式，不必全语料同时在内存
    for i, win, left, right in _iter_windows(toks, max_n, stop_boundaries):
        batch.append((_join_by_pair_rule(win), len(win), left, right))
        if len(batch) >= 200_000:
            conn.executemany("INSERT INTO _ngram_events VALUES (?,?,?,?)", batch)
            batch.clear()
_flush(batch)

# 频次 + HAVING 过滤在物化前（低于 min_count 的窗口根本不进结果集）
conn.execute("CREATE INDEX _idx_ng ON _ngram_events(phrase)")
freq_rows = conn.execute("""
    SELECT phrase, n, COUNT(*) AS c
    FROM _ngram_events
    GROUP BY phrase, n
    HAVING COUNT(*) >= ?
""", (min_count,))                        # ← 只有过关窗口回 Python
```

### 3.2 分支熵：只对过关 phrase 算邻居分布

频次门槛通常把窗口砍掉 90%+。**只对过关的 phrase** 拉左右邻居分布：

```sql
-- 左邻分布（每个过关 phrase 的邻居计数），SQL 侧聚合
SELECT e.phrase, e.left_tok, COUNT(*) AS c
FROM _ngram_events e
JOIN _passed_phrases p ON e.phrase = p.phrase   -- 过关集合先落一张小 TEMP
WHERE e.left_tok IS NOT NULL
GROUP BY e.phrase, e.left_tok
```

Python 侧只对**过关 phrase 的邻居分布**（数量级更小）算
`_entropy()`——分支熵是 `-Σ p log p`，SQL 表达不优雅，留在 Python，但
输入集已从"全窗口"缩到"过关 phrase 的邻居"。

### 3.3 unigram + NPMI solidity

- unigram 计数：`SELECT token, COUNT(*) FROM _token_events GROUP BY token`
  （或复用 3.1 的 n=1 事件）。`_log_uni` / `_log_c` 的 lru_cache 保留。
- solidity（`_npmi_pair`）：纯算术，输入是已在内存的过关 phrase 的
  count + 其 split 的 count。**留在 Python**——它消费的是过关集合，不是
  全窗口，规模已受控。

### 3.4 substring salvage 不变

`entropy_rejects` 的 salvage（`by_first_char` 索引 + 独立性 margin）逻辑
一字不改——它的输入是"过关 results + entropy_rejects"，两者都已是过滤后
的小集合，不是内存热点。

### 3.5 等价性契约（**硬判据**）

SQL 化后 `detect_ngram_phrases` 返回的 phrase dict list 必须与主干版本：

- **phrase 集合 Jaccard = 1.0**（严等价）
- 每个 phrase 的 `count / npmi / branch_ent / n` **bit-exact**
  （浮点容忍 `abs(a-b) < 1e-9`，仅限 log 求和顺序差异）

不达标 = 方案否决。golden 用三体现成的
`experiments/autongram_2026_07_11/data/golden_santi.json`。

---

## 4. 方案 B：pair npmi 重算 SQL 化

### 4.1 优先：纯 SQL UPDATE（若 SQLite 有 math 函数）

npmi 是 `co / n_chunks` 等的**自然对数**恒等式（`_npmi`）。SQLite 的
`ln(x)` 是自然对数、`log(x)` 是**十进制对数**（易混淆），本方案需要的
是前者。这些函数属于**编译期可选模块**（`-DSQLITE_ENABLE_MATH_FUNCTIONS`,
SQLite ≥ 3.35），并非所有 Python 发行版都开启，因此 §4.2 兜底必需。

若 `ln()` 可用，整个 Python 循环变一条：

```sql
-- Gate 1（glued: df_a==df_b==co）+ min_pair_count + min_npmi 都进 SQL
WITH df AS (SELECT kw_id, chunk_df FROM keyword_stats WHERE scope_id = :sid)
UPDATE keyword_pairs AS kp
SET npmi = (
    -- Bouma npmi = ln(p_ab/(p_a*p_b)) / -ln(p_ab)，p_* = df/n_chunks
    ... ln() 表达式 ...
)
WHERE kp.scope_id = :sid AND kp.source = 'chunk_cooccur';

-- 退化行删除（glued / npmi<min / npmi is NULL）单独一条 DELETE
DELETE FROM keyword_pairs
WHERE scope_id = :sid AND source = 'chunk_cooccur'
  AND (<glued 条件> OR npmi IS NULL OR (:min_npmi > 0 AND npmi < :min_npmi));
```

### 4.2 兜底：分批游标（若 math 函数不可用 / 表达不便）

若目标环境 SQLite 无 `ln()`（编译期 `-DSQLITE_ENABLE_MATH_FUNCTIONS`
未开——注意此模块**可选**、非所有 Python 发行版都启用），退回分批：
`SELECT ... LIMIT :batch OFFSET :off` 或 rowid 分页，每批过 Python
`_npmi` + `executemany` UPDATE。peak 绑到 batch size（如 50k 行
≈ 数 MB）而非全表。

**启动时探测**能力（探测**函数存在与否**，不是探测返回值——`log(X)`
是 base-10，用它验数值会被误导）：

```python
try:
    conn.execute("SELECT ln(1.0)")   # 缺失时 sqlite3.OperationalError
    has_math = True
except sqlite3.OperationalError:
    has_math = False
```

结果记进 build 日志，决定走 §4.1 还是 §4.2。

### 4.3 等价性契约（**硬判据**）

- 所有 surviving pair 的 npmi 值与主干 `abs(a-b) < 1e-9`
- Gate 1 glued 判定（`df_a == df_b == co` 三等）**整数严等**，不允许
  浮点化——它是 int 比较，SQL 里也用 int 比较

---

## 5. 实验验证（先于主干）

新建 `experiments/oom_build_fix_2026_07_15/`，复用
`oom_build_2026_07_14` 的 bench/analyze harness（sampler + ru_maxrss，
关 tracemalloc）。判据：

| C | 内容 | 阈值 |
|---|---|---|
| C1 | ngram_iter_1 Python heap（M 档） | ~2368 MB → **< 300 MB** |
| C2 | pairs_scan_end 峰（M 档） | ~800 MB → **< 100 MB** |
| C3 | peak_rss / corpus_mb | ~100× → **< 15×** |
| C4 | phrase 集合等价 | Jaccard **= 1.0** |
| C5 | npmi 值等价 | **abs < 1e-9** |
| C6 | wall-clock | **≤ 主干 1.5×**（SQL round-trip 可容忍） |

C4/C5 任一不过 = 否决（结果正确性优先于内存）。

---

## 6. SQLite spill 配置（C 侧那一半）

方案 A/B 把 Python heap 压下去后，C 侧（SQLite temp store / page cache）
成为新的可控项。这一节的**核心澄清**是三个 PRAGMA 各管什么、哪个是天
花板、哪个是权衡旋钮、哪个默认不动。

### 6.1 三个 PRAGMA 的职责（先厘清）

| PRAGMA | 语义 | 定位 | 本文档处理 |
|---|---|---|---|
| `temp_store` | TEMP 表 / sorter / 大 join 中间态放**磁盘 (FILE)** 还是**内存 (MEMORY)** | **真正的 OOM 天花板** | 强制 `FILE`，非旋钮 |
| `cache_size` | 已打开的持久页保留多少在 RAM（负值=字节预算） | **速度 / RSS 权衡旋钮** | 默认待实验拐点确定，env var 可调 |
| `page_size` | 单页字节数（影响 B-tree 扇出、bulk I/O 粒度） | 正交旋钮 | **默认不动**（迁移成本高） |
| `mmap_size` | mmap 读的字节数（跟 anonymous 页竞争 RSS） | 正交旋钮 | 默认不动，可选进实验 sweep |

**关键澄清**：**是 `temp_store=FILE` 兜住 OOM，不是 `cache_size`**。
`cache_size` 只决定"落磁盘的 temp 页有多少能同时驻 RAM"，是速度 vs
RSS 的旋钮，不是硬上限。这一点在旧版 §6 里表述不准，本次改正。

### 6.2 必设项：`temp_store = FILE`（本文档不做实验）

```python
conn.execute("PRAGMA temp_store = FILE")   # 大 self-join / sorter 主动 spill 磁盘
# 可选：把 temp file 放到指定盘（避免和源语料 I/O 抢同一磁盘）
# conn.execute(f"PRAGMA temp_store_directory = '{spill_dir}'")
```

**为什么主动 spill 而不是靠系统 swap**（AGENTS.md 铁律 1 + 用户第一轮
"和虚拟内存冲突"的问题）：

- OS swap 是**无界被动**的——RSS 超物理内存 → 内核把 anonymous page 写
  swap → 全机 I/O 排队卡死。
- SQLite `temp_store=FILE` 是**有界主动**的——超 `cache_size` 阈值就
  顺序写 temp file，事务提交后释放；崩溃有 journal 兜底，不拖垮 OS。
- 两者**不冲突**，而是要主动用前者绕开后者。

`temp_store` 是无实验空间的开关——不设 = 走系统 swap，设 = 走 SQLite
自己管理。选定即结束。

### 6.3 待实验：`cache_size` 拐点

**当前占位值 256 MB（`-262144`）是拍脑袋的圆整数**，不是实验推导。
它只在两个软约束之间：

- 下界：远高于默认 2 MB，避免母文档**反模式 3**（"看起来在动、吞吐掉
  到几十行/秒"）——大 self-join / GROUP BY 反复 re-read temp 页的场景。
- 上界：远低于要避免的 GB 级 peak，否则 cache 吃满等于没优化。

**真正的默认值应由 `oom_build_fix_2026_07_15` 实验的 sweep 维度决定**：

```
cache_size ∈ {64, 128, 256, 512} MB   # 4 档
每档测：peak_rss / wall_clock / spill file size
```

预期看到"低于某阈值 wall_clock 陡升（thrash）、高于该阈值 wall_clock
持平但 peak_rss 上升"的拐点。默认值取拐点处 + 一档安全余量。

env var `GRGA_BUILD_CACHE_MB` 覆盖默认，写进 `docs/env-vars.md`。
负值语义比正值好——正值 `cache_size` 是页数，会跟 `page_size` 耦合，
换 page_size 时行为漂移；负值直接锚字节。

### 6.4 待实验：`page_size` 是否值得动

**默认 4096，本文档 default 保持 4096**。理由：

- SQLite 默认页大小自 3.12.0 起就是 4K，绝大多数负载已充分调优。
- 改 page_size **只能在建库前设，或对已有库 VACUUM**——`deep_keywords.db`
  是**累积增长的共享生产库**，改它是 migration 事项，成本高。
- 收益未验证。bulk-insert + 大扫描理论上 8K–16K 能减 B-tree 深度、提
  IO 吞吐；但 pair 行是 4 个 int，用大页可能浪费 slack space。

**可选**：在 fix 实验用**独立临时 DB**（每 run tempfile 新建）扫
`page_size ∈ {4096, 8192, 16384}`。数据显著（比如 wall_clock 减 20%+ 且
peak_rss 不升）才考虑主干迁移，且要单独设计 migration 路径（新 DB 用
大页、老库继续 4K 或提供 `grga index vacuum --page-size` 一次性升级）。

不显著就归档"实测无用"，避免"预留即污染"。

### 6.5 待实验：`mmap_size` 是否关掉

- 默认（SQLite 编译期决定，通常 0 或几百 MB）会把部分持久 DB 页 mmap
  进 RSS——跟本方案的 anonymous 页 + temp file cache 竞争同一份物理内存。
- 设 `PRAGMA mmap_size = 0` **完全关闭 mmap**，让所有 IO 走 pager cache
  （受 `cache_size` 直接管辖，行为可控）。

预期 build 场景（大量顺序扫 + 写）关 mmap 收益 > 保留。但**没有实测
就是猜**。同样进 fix 实验 sweep（`{0, default, 512MB}` 三档足够）。

### 6.6 实验落到哪里

`experiments/oom_build_fix_2026_07_15/` 的 config 矩阵扩一维：

```python
class BenchConfig:
    # 已有：方案 A/B 各开关
    variant: str          # 'baseline' | 'method_A' | 'method_B' | 'method_AB'
    # 新增（本节）：SQLite 侧旋钮
    cache_mb: int = 256   # 64 / 128 / 256 / 512
    page_size: int = 4096 # 4096 / 8192 / 16384，仅独立 DB 场景测
    mmap_size: str = "default"  # 'off' | 'default' | '512MB'
```

不必全笛卡尔（太慢），主线跑 `variant=method_AB` 下的 cache_mb sweep
（4 点），page_size / mmap_size 各挑 method_AB × cache_mb=默认 分别扫。
共约 4+3+3 = 10 个 config × 3 语料 × 3 runs ≈ 90 次 build，M 档估算
1.5 小时/次全走完不现实——用 S 档做拐点定位，M 档只验证拐点值稳定。

### 6.7 本节的落地判据（写进 fix 实验 SPEC）

| C | 内容 |
|---|---|
| C-spill-1 | `cache_size` 拐点存在且可复现（wall_clock 曲线有明显拐） |
| C-spill-2 | 默认 `cache_size` 取拐点 + 一档安全余量后，M 档 peak_rss 不比方案 AB 单独时高 >10% |
| C-spill-3 | `page_size` 改动的收益 <15% wall / <10% peak_rss → **归档不动** |
| C-spill-4 | `mmap_size = 0` 若收益 >10% peak_rss → 默认开启，否则维持 default |

**优先级**：C-spill-1/2 必须过（决定 default `cache_size`）；C-spill-3/4
未过 = 默认不动、实验数据归档，不是失败。

---

## 7. 未来方向：文件级断点续跑（留档，本轮不做）

> **状态**：技术可行、需求待证据触发。本节记录 2026-07-15 讨论的完整设计，
> 供未来"真实大语料反复崩溃"证据出现时直接落地，不必重新推导。

### 7.1 为什么本轮不做

- **正交问题**：TEMP TABLE 解决 peak RSS（能不能跑），断点续跑解决崩溃后
  重跑成本（崩了少跑一点）。两者独立，复杂度预算分开算。
- **ROI 依赖崩溃频率**：peak 降到 ~10×/MB 后 OOM 大幅减少，崩溃本身变
  稀少，续跑价值随之下降。
- **铁律 1（杠杆方向）**：文件级 staging + 状态表是加在共用层的复杂度，
  理由必须是**多 caller 反复踩坑的证据**。TEMP TABLE 有实验 F1-F5 作
  证据；续跑目前没有"用户在真实语料跑到一半反复崩"的案例。
- **决策**：TEMP TABLE 落地 → 真实 100/500 MB 语料测崩溃频次 → 若仍
  可观（如 100 MB+ 语料进程中途挂概率 >10%）再启
  `experiments/oom_build_resume_202X/`；否则归档为"可行但需求不足"。

### 7.2 前提：TEMP → 持久 staging 表

`CREATE TEMP TABLE` 生命周期绑 connection，进程崩溃即销毁。要跨崩溃
续跑，staging 必须持久化——把方案 A/B 的 TEMP 表改成 `_stage_*` 前缀的
**普通表**（build 完成时统一 DROP，不进稳定 schema 的 migration 承诺）。

### 7.3 三层 checkpoint 语义边界

build 依赖图：
```
scan → [iter1: 全语料 tokenize+detect] → auto_ngram_dict 落盘 ★
     → [iter2: 同上] → auto_ngram_dict 落盘 ★
     → [Phase B: 全语料 tokenize(最终 dict)] → keyword_stats + _stage_chunk_kws
     → [pair 聚合: 一条 INSERT-SELECT] → keyword_pairs ★
     → [npmi 重算: 一条 UPDATE] → 更新 npmi ★
```

| 粒度 | 可行 | 说明 |
|---|---|---|
| **文件级 staging** | ✅ | tokenize 一个 doc 写一批到 `_stage_chunk_kws`，per-file 提交；崩溃后按"已提交文件集"跳过 |
| **iter 级 checkpoint** | ✅（已天然存在） | iter N 结束 → `auto_ngram_dict` UPSERT 落盘；崩在 iter N 中间 → iter N 从头，但 iter N-1 字典已在库 |
| **聚合中断续跑** | ❌ 不需要 | 聚合是单条 SQL，事务原子性保证"全成或全无"；崩溃 → rollback → 重跑同一条 SQL 幂等 |

**关键：不要对聚合做"进度%"续跑**。聚合是一条语句，事务回滚就是最好的
断点。只需保证 staging 表在聚合前完整、聚合后正确清理。

### 7.4 状态表设计

```sql
CREATE TABLE IF NOT EXISTS _build_state (
    build_id      INTEGER PRIMARY KEY,
    scope_id      INTEGER NOT NULL,
    mode          TEXT NOT NULL,      -- 'rebuild' | 'build'
    refresh_ngram TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    phase         TEXT NOT NULL,      -- staging_iter1|staging_iter2|staging_final|pair_agg|npmi
    iter_num      INTEGER,            -- 仅 staging_iter* 有意义
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _build_file_state (
    build_id     INTEGER NOT NULL,
    doc_id       INTEGER NOT NULL,
    phase        TEXT NOT NULL,
    content_hash TEXT NOT NULL,       -- 恢复时校验文件未变
    completed_at TEXT NOT NULL,
    PRIMARY KEY (build_id, doc_id, phase)
);
-- staging 表从 TEMP 改普通命名
CREATE TABLE IF NOT EXISTS _stage_chunk_kws (
    build_id INTEGER NOT NULL, doc_id INTEGER NOT NULL,
    chunk_id INTEGER NOT NULL, kw_id INTEGER NOT NULL
);
```

### 7.5 落盘协议（安全性唯一依据）

每个文件跨过 staging 必须**在一个事务里同时**写数据 + 写完成标记：

```python
with conn:                                       # BEGIN … COMMIT 原子
    conn.executemany("INSERT INTO _stage_chunk_kws VALUES (?,?,?,?)", rows)
    conn.execute("INSERT INTO _build_file_state VALUES (?,?,?,?,?)",
                 (build_id, doc_id, phase, content_hash, now))
```

**要么都进要么都不进**。只写数据后补标记 → 崩在中间 = 孤儿数据行；
反之 = 假恢复标记。SQLite 事务原子性是这里的地基。

### 7.6 恢复分派

启动读 `_build_state`：
- 无行 → 全新 build。
- `mode` 变了（rebuild↔build）→ 拒绝续跑，清残留，重开。
- 否则按 `phase` 分派：
  - `staging_iter*` / `staging_final`：读 `_build_file_state` 得"已完成
    doc 集"，跳过；对已完成 doc 校验 `content_hash` 与磁盘一致（不一致
    清掉重跑）。剩余文件继续。
  - `pair_agg`：重跑 INSERT-SELECT（见 7.7 幂等）。
  - `npmi`：重跑 UPDATE（全表覆盖，天然幂等）。
- 成功完成统一清理：`DROP _stage_chunk_kws; DELETE _build_state;
  DELETE _build_file_state`。

### 7.7 UPSERT-add 幂等陷阱

`_finalize_pairs_from_temp` 的 `cooccur_count = cooccur_count +
excluded.cooccur_count` **在续跑下不幂等**。两个应对：

1. **rebuild 模式**：聚合前 `DELETE FROM keyword_pairs WHERE scope_id=?
   AND source='chunk_cooccur'`，UPSERT-add 退化为 INSERT，幂等。
2. **build（增量）模式**：phase marker update 与聚合 SQL 放**同一事务**。
   恢复看到 phase 仍在 `pair_agg` = 数据没进 = 安全重跑。

### 7.8 收益上界（诚实标注）

文件级 staging 只能省 **staging 那部分 wall-clock**（tokenize + 写库，
约占总 build 的 30-50%）；聚合阶段崩溃仍靠事务回滚重跑整条 SQL。所以
续跑最多回收 ~40% 时间——这也是 §7.1"ROI 依赖崩溃频率"的量化依据。

---

## 8. 落地顺序（分 PR）

1. **PR-1 方案 B（pair npmi SQL 化）**：✅ **已 landed**（2026-07-15）。
   experiments/oom_build_fix_2026_07_15 判据 C2/C5/C6 全绿：pair 段 Δ RSS
   从 +771 MB → +3 MB（-99.6%），bit-exact（max_diff=0.0，293k 对全等），
   wall 1.16× SQL / 1.00× batch。
2. **PR-1.5 db-temp 隔离**：✅ **已 landed**（2026-07-15）。SQLite 临时
   文件重定向到 `<main_db>/../db-temp/build-<uniq>/`，规避 tmpfs 陷阱，
   成功 build 后自动清理。
3. **PR-2 方案 A（ngram detector TEMP TABLE）**：✅ **已 landed**
   （2026-07-15）。experiments/oom_build_fix2_2026_07_15 判据 C4/C6
   strong pass、C1 算法侧达标：**M 22MB 集成 build peak 2474 → 747 MB
   (-69.8%)**，ngram_iter_1 段 +2368 → +604 MB (-74.5%)，phrase 输出
   bit-exact（29846 条严等），wall 1.11×。C1 绝对阈未达（604 vs 300 MB
   目标）根因是 `line_token_lists` 输入本身 ~500 MB——见 F3 → PR-4 候选。
4. **PR-3 SQLite spill 配置 + env var**：⏭️ **待办**。必设
   `temp_store=FILE`（PR-1.5 已经做了）；`cache_size` 默认值**取 §6.3
   实验拐点**（不是硬编码 256）；`page_size` / `mmap_size` 仅在 §6.4/6.5
   实验数据显著时才动，否则维持默认。附 §6.7 的 C-spill-* 判据数据。
   更新 `docs/env-vars.md`（`GRGA_BUILD_CACHE_MB`）。
   **PR-2 后此实验才有真实大 spill 工作负载**，拐点信号才可信。
5. **PR-4（可选）流式化 Phase A**：⏭️ **仅在 100 MB+ 语料的证据出现时做**。
   PR-2 之后 `line_token_lists` 成为新的 O(corpus) 内存地板（500 MB / 22MB
   ≈ 23×）。要突破需把 Phase A 从"先全量 tokenize、再喂 detector"改成
   "tokenize 一行 → 写一行进 `_ng_ev` → detector 直接从 TEMP TABLE 读"。
   涉及 Phase A / Phase B 整个 token 流水线改造，改动面大。当前 PR-2 后
   60-70 MB 语料已可在 16 GB 机器 build，覆盖多数用户场景。
6. **文档同步**：`docs/design/db-memory-optimization-lessons.md` 加一条
   "ngram 检测也是本方法学的适用对象"；`docs/navigation.md` 文档地图加
   本文件。

---

## 9. 一句话给未来读者

**本轮把 v9.1 pair 计数优化"漏下的另外两处 Python 全量聚合"补齐——
ngram 检测和 pair npmi 重算——让 peak RSS 从 100×/MB 掉到 ~10×/MB。
断点续跑技术上完全可行（§7 有完整设计），但它解决的是正交问题（崩溃成本
而非内存上限），ROI 依赖 OOM 修复后的真实崩溃频率，故留档待证据触发。**
