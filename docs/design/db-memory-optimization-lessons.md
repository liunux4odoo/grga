# DB / 内存优化经验沉淀（v9 → v9.1 pair 聚合重构）

> **场景**：`index build` 在 22 MB 中文小说（157k chunks, 29k 短语）
> 上从"崩溃 + OOM"变到"3 分钟完成、内存平稳"。这份文档抽取**可复用
> 的方法学**，供后续任何"Python 端做批量聚合 + 落 SQLite"的模块直接引用。
>
> 详细决策见 `docs/design/decisions.md` 2026-07-13 条目。

---

## 一句话结论

> **能让 SQLite 用 SQL 完成的聚合，永远不要在 Python 端 Counter/set 里做。**

具体到本项目：**per-chunk 关键字对 (pair) 计数**这类"raw 事件 → 分组聚合
→ 阈值过滤 → 落盘"的流水线，把 raw 事件写进 TEMP TABLE，用一条
`INSERT ... SELECT ... GROUP BY ... HAVING ... ON CONFLICT` 一次搞定，
Python 只做**数据搬运**、不做**聚合状态维护**。

---

## 三个反模式（v9 撞过的）

### 反模式 1：Python 端聚合大 Counter，等末尾一次性 flush

```python
# ❌ 这种代码在小语料上跑得飞快，大语料上一定 OOM
pair_delta: Counter = Counter()
for chunk in all_chunks:
    for a, b in itertools.combinations(chunk.kw_ids, 2):
        pair_delta[(a, b)] += 1
# ... 最后 conn.executemany(..., pair_delta.items())
```

**问题**：Counter 的 key 数是**整个 scope 的 distinct pair 数**——对
词汇丰富的语料（如小说、技术文档大集合）能爆到 **10M–30M 条**，
Python tuple + dict 单条 ~80–100 B → 2–3 GB Python 堆。**Counter/set 增长
和 GC 都是 CPython 大对象堆最贵的操作之一**。

**症状**：进程 RSS 冲上系统上限被 OOM killer 直接 SIGKILL，或者机器
swap 打满卡死。

### 反模式 2：过滤时机放在"落盘之后"而不是"落盘之前"

```python
# ❌ 先把所有 pair 灌进磁盘，再 DELETE 掉低于阈值的
conn.executemany("INSERT INTO pairs UPSERT-add ...", all_pairs)   # 30M 行
conn.execute("DELETE FROM pairs WHERE cooccur_count < 5")         # 剩 500k
```

**问题**：SQLite 的 WITHOUT ROWID 表 + secondary index 是每行 3 次
B-tree 操作，30M UPSERT 会：

1. 让 DB 文件在**未提交事务里**膨胀 GB 级（rollback journal 也跟着膨胀）
2. 后续 DELETE 扫全表触发 tombstone 页，DB 大小回不到理论最小值
3. 事务失败 / kill 后留下"1.9 GB 文件 + 38 页有效数据"的诡异现场

**症状**：DB 文件大小和实际有效数据行数**严重不成比例**；进程 kill 后
`.db-journal` 大文件残留；VACUUM 才能缩回来。

### 反模式 3：在写入循环里频繁 flush 有 secondary index 的大表

```python
# ❌ 看似分批降内存，实际让每次 UPSERT 都打一次索引维护的钉子
for chunk in chunks:
    for a, b in pairs:
        pair_delta[(a, b)] += 1
    if len(pair_delta) >= 100_000:
        conn.executemany("INSERT ... UPSERT-add ...", pair_delta.items())
        pair_delta.clear()
```

**问题**：flush 频率越高，`keyword_pairs` 增长越快，**每次 flush 都要
维护随行数增长的 secondary index**——退化为 O(N²) 总写入代价。批量
INSERT 的核心优势（延迟到最后一次索引重建）被 flush 抵消掉了。

**症状**：进度条卡在某个文档不动、CPU 占用低 iowait 高、`.db-journal`
持续增长。**"看起来在正常写"，实际吞吐已经掉到几十行/秒**。

---

## 正确模式：TEMP TABLE + 一条 INSERT-SELECT

### 三步骨架

```python
# 1. 建 build-local TEMP TABLE，只承载 raw 事件（不做任何聚合）
conn.execute("""
    CREATE TEMP TABLE IF NOT EXISTS _build_events (
        group_key1 INTEGER NOT NULL,
        group_key2 INTEGER NOT NULL,
        item_id    INTEGER NOT NULL
    )
""")
conn.execute("DELETE FROM _build_events")   # idempotent cleanup

# 2. Python 侧只做"事件写入"，用 list buffer + 定期 executemany 落进 TEMP
batch: list = []
FLUSH = 200_000
for event in stream_events():
    batch.append(event)
    if len(batch) >= FLUSH:
        conn.executemany("INSERT INTO _build_events VALUES (?, ?, ?)", batch)
        batch.clear()
if batch:  # 尾巴别忘
    conn.executemany("INSERT INTO _build_events VALUES (?, ?, ?)", batch)

# 3. 一条 SQL 完成"聚合 + 阈值过滤 + UPSERT-add + 累积语义"
conn.execute("""
    CREATE INDEX IF NOT EXISTS _idx_events_group
        ON _build_events (group_key1, group_key2)
""")
conn.execute("""
    INSERT INTO permanent_table (a, b, count, ...)
    SELECT x.item_id, y.item_id, COUNT(*), ...
    FROM _build_events x JOIN _build_events y
      ON x.group_key1 = y.group_key1 AND x.group_key2 = y.group_key2
     AND x.item_id < y.item_id                    -- 每对只算一次
    GROUP BY x.item_id, y.item_id
    HAVING COUNT(*) >= ?                          -- 关键：落盘前过滤
    ON CONFLICT(...) DO UPDATE SET
        count = count + excluded.count            -- grow-only 累积
""", (min_threshold,))

conn.execute("DROP TABLE IF EXISTS _build_events")
```

### 为什么这套一定行

- **RAM 交给 SQLite**：hash-aggregate 是 C 层 hash table，每 entry ~30% Python
  对应对象的大小；超出 `PRAGMA cache_size` 后**自动 spill 到磁盘 temp
  file**（可控、不 OOM）。
- **HAVING 是"过滤在物化前"的原语**：低于阈值的聚合结果**从不出现在
  结果集里**——不是先算完再删，是**根本不算**。
- **一条 INSERT-SELECT 只走一次事务提交路径**：中间 index 维护交给
  SQLite 的 batch insert 优化，跟"手动分批 UPSERT"完全不同的性能曲线。
- **TEMP TABLE 不动持久 schema**：`CREATE TEMP TABLE` 只存在于当前
  connection，连接关闭自动消失；不进 `sqlite_master`，不影响 migration、
  不影响 `--json` 契约、不影响读侧。

---

## 什么时候不该用 TEMP TABLE

- **事件流本身就很小**（< 1M 行 raw、< 100k 聚合）：直接 Python dict
  聚合再一次性 executemany 就够了，TEMP TABLE 反而多两次 disk I/O。
- **需要多次访问聚合中间态**（增量 online 算法）：TEMP TABLE 每次要重扫，
  不如维护 Python 数据结构 + 定期 checkpoint。
- **聚合逻辑 SQL 表达不了**（例如复杂 pandas / numpy 向量运算）：
  这种情况先考虑能不能拆成"raw 存 TEMP + SQL 做能做的 + 剩下的 Python
  再算"。

**判断标准**（一句话）：

> raw 事件数 × 单事件字节 > 500 MB，且 Python 聚合状态会随 corpus 大小
> 超线性增长 → 立刻上 TEMP TABLE。

---

## 观测 & 验证清单

给未来的 code agent：改任何"Python 聚合 → 落 SQLite"的模块前，先跑
这几个检查。

### 落盘前

- [ ] Python 端聚合状态（Counter / dict / set）的 key 数上界是多少？
      随 corpus 大小是**线性**还是**超线性**（如 pair 数是 vocab²）？
      如果超线性，直接看 TEMP TABLE 方案。
- [ ] 目标表是 `WITHOUT ROWID` 吗？secondary index 有几个？
      每行 UPSERT 的 amortized 代价是 O(1) 还是 O(log N)？
- [ ] 阈值过滤（min_count / TF-IDF 下限之类）能不能表达成 `HAVING`？
      能的话，一定放 SQL 里做。
- [ ] `min_threshold` 变更时，历史行需要**重新过滤**吗？如果需要，
      SQL 里加一条 `DELETE ... WHERE value < ?` 兜底。

### 落盘后（现场诊断）

- [ ] `sqlite3 db.file "PRAGMA page_count; PRAGMA page_size"`：算出
      **理论 DB 大小**，对比文件大小。差 10× 以上说明有未提交事务或
      dead pages。
- [ ] `.db-journal` / `.db-wal` 存在且大 → 有事务卡住或崩溃。
- [ ] `readonly` 打开看 `COUNT(*)` 全 0 但文件 GB 级 → **rollback journal
      场景**，rm 掉 `.db*` 是安全的（本来就没提交）。
- [ ] `EXPLAIN QUERY PLAN` 那条聚合 SELECT：确认走了 hash agg 或 sort
      而不是 nested-loop-per-row。

---

## 参照实现

- **正例**：`src/grga/graph/build.py::_finalize_pairs_from_temp`
  （v9.1，2026-07-13）
- **反例（已修）**：v9 的 `pair_delta` + `touched_pair_keys` 双结构，
  见 git log `5b9c358..HEAD -- src/grga/graph/build.py`
- **历史 pre-v9 正例（永久 `keyword_chunks` 表）**：`git show b70ba2d^:src/grga/graph/build.py`
  的 `recompute_pairs`——用永久表实现同样的 SQL self-join，v9.1 是它的
  "不落持久 schema"版本

---

## 元教训

1. **共用基础设施的杠杆方向**（AGENTS.md 铁律）：把内存 / 磁盘负担
   放在 SQLite 这个共用层，而不是放在每次 build 的 Python 进程里。
   SQLite 的内存管理、spill-to-disk、事务边界都是**几十年调优的成果**，
   Python 手撸的 Counter + set 是每次都从零开始。
2. **"删表 = 删算法"是隐藏的重构成本**：v9 把 `keyword_chunks` 从
   持久 schema 移出（合理，为了瘦身），但**没同时把消费它的 SQL 算法
   迁到 TEMP TABLE 上**——直接改写成 Python 循环，等于把 SQLite 的数学
   基础设施丢在了 refactor 现场。以后再做类似简化，schema 层删表 =
   算法层同步补 TEMP TABLE 替身。
3. **进度条不是性能指标**：F3-corrected 时进度条更平滑了（每 chunk
   flush 一次），实际吞吐比"每 doc flush 一次"慢 100 倍。**"看起来在
   动"和"实际在做有效工作"不是一回事**——benchmark 上真语料再说。
