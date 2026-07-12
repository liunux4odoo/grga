# keyword-primary 架构：pair 字典化 + 删除 keyword_chunks（schema v8）

**日期**：2026-07-12
**关联实验**：`experiments/subgraph_ppr_2026_07_12/`（局部 PPR 否决 + npmi 现算/存储成本 + pair 表规模外推）
**动机**：生产实测 452 MB / **2 个文件**。`keyword_chunks`（chunk 级倒排）随语料**线性**膨胀，
外推 1000 文件 ≈ **45 GB**，不可扩展。`keyword_pairs` 在写入期严过滤后只有 ~16 万行、~10 MB，
且随 vocab² + top-M 边**次线性饱和**。

**结论**：把 `keyword_pairs` 提升为**核心知识资产**（像字典一样只 grow-and-refine），
**删除 `keyword_chunks` / `chunks`**，`related --mode graph` 从"返回相关文档"改为
"返回候选关键词 + 关联度量"。从 keyword 找原文片段是 caller + ripgrep 的职责。

---

## 0. TL;DR

| 维度 | 现状（v7） | 新方案（v8） | 1000 文件外推 |
|---|---:|---:|---:|
| DB 总大小（2 文件） | 452 MB | **~100 MB** | v7≈45 GB / **v8≈0.7–1 GB** |
| `keyword_chunks` | 93 MB（线性） | **删除** | v7≈45 GB |
| `keyword_pairs`（表+索引） | 329 MB | ~30 MB（严过滤+存 npmi） | ~150 MB |
| `related --mode graph` 冷启动 | 3s（需建 adjacency_cache） | **亚毫秒**（无 PPR、无全图加载） | 亚毫秒~数 ms |
| PPR | 全图幂迭代 | **移除** | — |
| `related --mode graph` 返回 | `List[Related]`（文档） | `List[RelatedKeyword]`（关键词） | — |

**核心决策链**（本线程验证过的实验证据）：

1. **局部 PPR 否决**：oracle 诱导子图 PPR 只有 0.65 top-30 一致率 —— PPR 抗截断能力差，
   局部 PPR ≈ 1 跳加权求和（Jaccard 0.5），算力白花。
2. **1 跳直接共现足够**：caller 天然多轮，多跳桥接移到 caller 侧（pipe ripgrep）。
3. **npmi 存储**：fanout=1498 时索引 `ORDER BY npmi DESC LIMIT` = 0.076ms vs 现算 2.889ms（38×），
   随 fanout 线性发散 → **存 npmi**。
4. **pair 字典化**：写入期严过滤（co≥5, npmi≥0.2）把 279 万 → 16.7 万行，随语料饱和。

---

## 1. 设计原则

### 1.1 知识资产原则（本次新增，最高优先级）

> build 阶段从语料库学到的**字典（keywords / auto_ngram_dict）**和**共现知识（keyword_pairs）**
> 是核心知识资产。它：
> - **随语料更新/增加而 refine**（grow-and-refine）
> - **不随语料删除而销毁**（删除源文件不校准已记忆的共现）
> - **可跨语料库迁移复用**（一个语料学到的 pair 字典能无缝服务另一个语料）

这条原则直接推翻 v7 的"派生层"心智模型：v7 里 `keyword_pairs` 是 `keyword_chunks` 的派生物，
每次 build `recompute_pairs` 全量重算。v8 里 **pair 是第一性资产**，`keyword_chunks` 反而消失。

**推论**：
- 删除源文件 → **不动 pair 表**（知识保留）。仅当用户显式 `grga index forget` 才移除。
- pair 表的 `dir_id` 从"隔离屏障"降级为"来源标签"；未来可支持跨 scope 合并（§8 未来方向）。

### 1.2 沿用的硬规则（AGENTS.md）

- **HC2 无 corpus prior**：npmi 是纯数学恒等式，不引入语义先验。
- **信号暴露原则**：caller 能自己派生的不塞进返回值；只暴露 caller 拿不到的内部状态
  （npmi / cooccur_count 是内部计算，暴露；doc 映射 caller 用 ripgrep 自取，不暴露）。
- **契约冻结**：`--json` 字段一旦发布即外部契约。本次是 MAJOR 破变（related-signals v3）。

### 1.3 非目标

- **不做**多跳图扩展（K≥2）—— 实验证明局部 PPR 无价值；多跳是 caller 的多轮职责。
- **不做** PPR / 任何全图 rank 算法 —— 移除。
- **不做** pair 表的跨 scope 自动合并（§8 记为未来方向，本次保留 `dir_id`）。
- **不动** `search` / `expand` 的核心路径（它们走 rga / StructureDoc，不碰这两张表）。

---

## 2. Schema v8

### 2.1 资产层（saturating growth）

```sql
-- 全局字典：kw_id ↔ 文本（1:1，跨 scope 共享）。不变。
CREATE TABLE keywords (
    id INTEGER PRIMARY KEY,
    kw TEXT NOT NULL UNIQUE
);

-- 按 scope 的词频统计。npmi 分母原料。NEW。
-- 与 keywords 分离的理由：df 是 per-(dir,kw)，keywords 是全局 1:1。
-- 合并会强制复制 kw 文本，反 v5 归一化。
CREATE TABLE keyword_stats (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_id INTEGER NOT NULL REFERENCES keywords(id),
    chunk_df INTEGER NOT NULL DEFAULT 0,   -- 出现的独立 chunk 数（build 时算好）
    doc_df INTEGER NOT NULL DEFAULT 0,     -- 出现的独立 doc 数
    total_occurrences INTEGER NOT NULL DEFAULT 0,  -- 总频次
    updated_at TEXT,
    PRIMARY KEY (dir_id, kw_id)
) WITHOUT ROWID;

-- 共现知识资产。第一性资产（不再派生）。
CREATE TABLE keyword_pairs (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_a_id INTEGER NOT NULL REFERENCES keywords(id),
    kw_b_id INTEGER NOT NULL REFERENCES keywords(id),  -- 约定 kw_a_id < kw_b_id
    source TEXT NOT NULL,                  -- chunk_cooccur | llm_promoted
    cooccur_count REAL NOT NULL DEFAULT 0,
    npmi REAL,                             -- 存储（决策 1）；llm_promoted 为 NULL
    updated_at TEXT,
    PRIMARY KEY (dir_id, kw_a_id, kw_b_id, source)
) WITHOUT ROWID;

-- 索引：让 ORDER BY npmi DESC LIMIT 走 index-only + 早停（决策 3 验证）
CREATE INDEX idx_kp_a_npmi ON keyword_pairs (dir_id, kw_a_id, npmi DESC);
CREATE INDEX idx_kp_b_npmi ON keyword_pairs (dir_id, kw_b_id, npmi DESC);

-- scope 级 scalar：npmi 分母 N + inspect 展示用。NEW。
CREATE TABLE scope_stats (
    dir_id INTEGER PRIMARY KEY REFERENCES directories(id),
    n_chunks INTEGER NOT NULL DEFAULT 0,   -- npmi 的 N
    n_docs INTEGER NOT NULL DEFAULT 0,
    vocab_size INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
```

### 2.2 派生层 / 元数据层（不变）

```sql
directories   (id, path)                          -- 不变
docs          (id, dir_id, doc_path, size, mtime, content_hash)  -- 增量判定，不再喂 pair
heading_docs  (doc_id, heading_path, chapter_id, start_line, end_line, updated_at)  -- expand 用
auto_ngram_dict (dir_id, phrase, n, count, npmi, branch_ent, updated_at)  -- tokenizer 用
meta          (key, value)                        -- toolchain_fingerprint 等
```

### 2.3 删除

```
× chunks          -- 唯一读者是 related._total_chunks_in_scope（npmi 分母），
                  --   改为读 scope_stats.n_chunks 后即可删
× keyword_chunks  -- 45 GB 怪兽。build 时 chunk 只在 RAM 存活
```

**注意**：`docs.content_hash` 保留 —— 增量 build 仍需三级变更检测（size/mtime/hash）判断
哪些文件要重新 tokenize。但 docs 不再与 pair 表有外键关系（删 doc 不 cascade pair）。

---

## 3. Build 语义（v8）

### 3.1 pair 是累积资产，不是每次重算

```
mode="build"（增量，默认新语义）：
  对每个 added / updated 文件：
    1. 读文件 → split_chunks（RAM only，不落 chunks 表）
    2. 每个 chunk：tokenize → 关键词集
    3. 生成 C(k,2) pair，累加进 pair_delta（RAM Counter）
    4. 累加 kw 的 chunk_df / doc_df / occurrences 进 stats_delta
  批量 UPSERT：
    - keyword_pairs.cooccur_count += pair_delta（ON CONFLICT DO UPDATE SET += ）
    - keyword_stats.chunk_df += ...
  更新 scope_stats.n_chunks += 新增 chunk 数
  重算受影响 pair 的 npmi（见 §3.2）

mode="rebuild"（全清重建）：
  DELETE 该 scope 的 keyword_pairs / keyword_stats / scope_stats
  然后等价于对所有文件跑一次 build
```

**关键差异 vs v7**：v7 每次 build 都 `recompute_pairs`（从 keyword_chunks 全量 self-join 重算）。
v8 是**增量累加**——新文件的 pair 增量 UPSERT 进已有资产，旧文件的贡献**不重算**（已在资产里）。

### 3.2 npmi 更新策略（存储，但增量重算）

npmi = f(cooccur, chunk_df_a, chunk_df_b, n_chunks)。增量 build 后 **n_chunks 变了 → 所有 npmi 漂移**。

两种应对，按 scope 规模选：

- **增量 UPDATE（默认）**：build 末尾对**本次触碰过的 pair**（pair_delta 的 key）重算 npmi。
  未触碰的 pair 的 npmi 用旧 n_chunks —— **这正是"知识资产带时间戳"的语义**：老知识保留其
  学到时的度量，除非新语料重新提及它。`updated_at` 记录漂移时点。
- **全量 refresh（opt-in `--refresh-npmi`）**：`UPDATE keyword_pairs SET npmi = <recompute>`
  全表重算。千级文件 ~16万行的 UPDATE 预计 <1s。用户想要"全局一致 npmi"时手动触发。

> 决策依据：知识资产原则允许 npmi 轻微时间漂移（老 pair 保留老度量）。强一致不是默认，
> 是 opt-in。这与"删文件不校准"一脉相承——都接受资产带历史痕迹。

### 3.3 写入期准入门槛

```python
BUILD_MIN_PAIR_COUNT = 5      # 单 scope 内 cooccur < 5 的 pair 不记入资产
BUILD_MIN_NPMI = 0.2          # npmi < 0.2 的 pair 不记入资产
```

准入门槛固化了资产的**下界**：caller query 时可用**更严**阈值现算过滤（如 min_npmi≥0.3），
但**不能更松**（低于 0.2 的 pair 根本没被记忆）。这是刻意的存储契约（省 20× 空间）。

`llm_promoted` 的 pair 不受 npmi 门槛约束（它们 npmi 为 NULL，走 vote→promote 流程）。

---

## 4. Query 语义（related --mode graph 破变）

### 4.1 新返回类型

```python
@dataclass
class RelatedKeyword:
    """graph 模式的候选关键词 + 关联度量。"""
    kw: str                    # 候选关键词（casefold）
    npmi: float                # 与 via_seed 的关联强度（存储值，或 caller 现算过滤后）
    cooccur_count: float       # 原始共现次数
    via_seed: str              # 这个候选是从哪个 seed 扩展来的（决策 4：不融合）
    chunk_df: int              # 候选词自身的 chunk_df（hub 词判别信号）
    source: str                # chunk_cooccur | llm_promoted

def related_keywords(
    seeds: Sequence[str], *,
    limit_per_seed: int = 30,
    min_npmi: Optional[float] = None,   # None → 用 build 门槛 0.2；可传更严值
    db_path=None, directory=None,
) -> List[RelatedKeyword]:
    """每个 seed 独立展开 top-M 邻居，不融合分数（决策 4）。
    caller 拿到候选集 C 后自行 ripgrep 找原文。"""
```

### 4.2 查询路径（无 PPR、无全图）

```sql
-- 每个 seed 一次（双向 UNION，走 idx_kp_a_npmi / idx_kp_b_npmi）
SELECT kw_b_id AS other, npmi, cooccur_count, source
FROM keyword_pairs
WHERE dir_id=? AND kw_a_id=? AND npmi>=?
ORDER BY npmi DESC LIMIT ?
UNION ALL
SELECT kw_a_id AS other, npmi, cooccur_count, source
FROM keyword_pairs
WHERE dir_id=? AND kw_b_id=? AND npmi>=?
ORDER BY npmi DESC LIMIT ?;
-- 合并、按 npmi 排序、取 top-M、JOIN keywords + keyword_stats 补文本/chunk_df
```

### 4.3 mode=dir / mode=heading 保留

- `mode=dir`：seed 直接命中的**目录**聚合 —— 但 v8 没了 keyword_chunks，无法知道 seed 命中哪些 doc。
  **改为 caller 用 `search` 拿命中 doc**，或 `mode=dir` 降级/移除（§7 待定）。
- `mode=heading`：走 `heading_docs`，不依赖 keyword_chunks，**保留不变**。

> ⚠️ **未决**：`mode=dir` 依赖 doc 映射。删 keyword_chunks 后它失去数据源。选项见 §7。

---

## 5. 契约破变（related-signals v3）

`docs/design/related-signals-v2.md` → v3。破变清单：

| v2 字段 | v3 变化 |
|---|---|
| `related --mode graph` 返回 `doc_path` 系列 | ❌ 改为 `kw` 系列（RelatedKeyword） |
| `expansion.keyword_neighbors[].ppr_score` | ❌ 删除（无 PPR） |
| `expansion.keyword_neighbors[].hops_from_seed` | ❌ 删除（恒为 1） |
| `expansion.keyword_neighbors[].chunk_df` | ✅ 保留（来自 keyword_stats） |
| `keyword_contributions`（per-doc） | ❌ 删除（无 doc 映射） |
| `edges_to_seeds[].npmi / cooccur_count` | ✅ 保留（现在是主返回字段） |
| `via_seeds` | ✅ 改为 `via_seed`（单数，不融合） |

新 schema：`schemas/related-signals-v3.json`。热路径动词（search/probe/expand/deep-search）
仍不 emit 任何相关字段（不变）。

---

## 6. 迁移 v7 → v8

```
_migrate_v7_to_v8(conn):
  1. 建 v8 新表（keyword_stats, scope_stats）+ 新索引
  2. 从 keyword_chunks 回填 keyword_stats：
       INSERT INTO keyword_stats(dir_id, kw_id, chunk_df, doc_df, total_occurrences)
       SELECT d.dir_id, kc.kw_id, COUNT(*),
              COUNT(DISTINCT kc.doc_id), SUM(kc.occurrence_count)
       FROM keyword_chunks kc JOIN docs d ON d.id=kc.doc_id
       GROUP BY d.dir_id, kc.kw_id
  3. 从 chunks 回填 scope_stats.n_chunks（per dir_id）
  4. keyword_pairs：保留 co>=5 AND npmi>=0.2 的行，DELETE 其余
       （现有 keyword_pairs 已有 npmi 列，v8 只是加索引 + 清低分行）
  5. DROP TABLE keyword_chunks; DROP TABLE chunks;
  6. PRAGMA user_version = 8
  （事务内原子完成，参照 v4→v5 迁移的 BEGIN IMMEDIATE 模式）
```

迁移后建议 `grga index vacuum` 回收 keyword_chunks 释放的空间。

**注意**：v8 迁移**有损** —— 删掉的 keyword_chunks 无法逆向重建 chunk 级信息。用户若需
chunk 级定位，迁移后靠 ripgrep 现取。迁移前 stderr 警告一次。

---

## 7. 未决问题（实施前需拍板）

### 7.1 `mode=dir` 的去向

删 keyword_chunks 后 `mode=dir`（列出 seed 命中的目录/文档）失去数据源。选项：

- **A. 移除 `mode=dir`**：让 caller 用 `search --json | 提取路径`。最符合 Unix 哲学。
- **B. 保留但改数据源**：内部调 `search` 拿命中 doc，再按目录聚合。多一层 rga 调用。
- **C. 保留 doc_df，返回目录列表**：从 keyword_stats 只能给"seed 出现在 N 个 doc"，
  给不出具体路径。语义降级。

**倾向 A**（最干净），但要确认没有 skill/GUI 强依赖 `mode=dir` 的文档列表。

### 7.2 npmi 时间漂移可接受度

§3.2 默认"增量 build 只更新触碰过的 pair 的 npmi"，老 pair 保留旧 n_chunks 下的 npmi。
这会让同一张表里的 npmi 来自不同 n_chunks 基准。**可接受吗？** 还是坚持每次 build 都
全量 refresh npmi（简单但增量 build 变慢）？

### 7.3 跨 scope 知识复用的接口（§8 未来方向的当下占位）

知识资产原则说"pair 可跨语料复用"。本次是否需要预留接口（如 `--import-pairs <other_db>`），
还是纯记文档、实现留待后续？**倾向纯记文档**，避免"预留即污染"（AGENTS.md 契约冻结条）。

---

## 8. 未来方向（本次不实现，仅留痕）

- **corpus-agnostic pair 合并**：`keyword_pairs.dir_id` 从隔离屏障演化为来源标签，
  支持 `SELECT ... WHERE dir_id IN (scopeA, scopeB)` 合并多语料知识，或一张 `dir_id=0`
  的"全局共享知识"表。需要 npmi 的跨 scope 归一化方案（n_chunks 不可加）。
- **pair 表导入/导出**：把一个语料学到的 pair 字典打包，`grga index import-knowledge`
  注入另一个库。知识资产原则的终极形态。
- **K≥2 `--deep` opt-in**：若实践中发现 1 跳不够，用 LinearRAG 式衰减剪枝（seed×npmi 沿边
  衰减、每跳 top-M、阈值截断）做受控多跳。实验已备好骨架（`experiments/subgraph_ppr_2026_07_12`）。

---

## 9. 落地阶段（独立可交付、独立可回滚）

```
Phase 1  schema v8 + 迁移（keyword_stats / scope_stats / 索引 / v7→v8 / DROP 两表）
         · 迁移测试：v7 库迁移后 keyword_stats 回填正确、pair 低分行清除、vacuum 生效
Phase 2  build 改增量累加（pair 为资产，chunk 仅 RAM，npmi 增量 UPDATE）
         · 补最小后端脚本验证 search_directory 兼容（AGENTS.md 要求）
Phase 3  related --mode graph → related_keywords（新返回类型 + 查询路径）
         · 移除 PPR / adjacency_cache / load_edges_indexed 热路径
Phase 4  契约 v3：related-signals-v3.json + CLI --json 输出 + mode=dir 定夺（§7.1）
Phase 5  清理：删 ppr.py 全图路径、subgraph 实验归档、docs/navigation.md 更新
```

每阶段独立 PR。Phase 1 迁移最高风险（有损、删表），需最充分测试 + stderr 警告。

---

## 10. 决策记录（本线程达成）

| # | 决策 | 依据 |
|---|---|---|
| D1 | 存 npmi，不现算 | fanout=1498 时 0.076ms vs 2.889ms（38×），随 fanout 发散 |
| D2 | graph 模式只返候选关键词 + 度量 | 用户明确：从 kw 找原文是 ripgrep 的活 |
| D3 | keywords / keyword_stats 不合并 | 全局 1:1 vs per-scope N:M，合并反 v5 归一化 |
| D4 | 多 seed 不融合，各带 via_seed | 用户明确：交给 caller 判断方向 |
| D5 | 删 keyword_chunks + chunks | keyword_chunks 45GB 线性怪兽；chunks 唯一读者可用 scope_stats 替代 |
| D6 | pair 为累积知识资产 | 用户核心原则：随更新 refine，不随删除销毁，可跨语料迁移 |
| D7 | 局部 PPR 否决、移除全图 PPR | oracle 子图 PPR 仅 0.65 一致率，局部 PPR≈1跳加权求和 |
