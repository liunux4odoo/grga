# Schema v5 — 关键字/目录/文档规范化

**日期**：2026-07-11
**动机**：v4 库在 6 万级 chunk / 60 万级关键字规模下达到 327 MB，其中 82%
浪费在 `keyword_chunks` 四列 TEXT 复合主键（`directory, keyword, doc_path,
chunk_id`）在数据本体和 3 张索引中的重复存储。对同一批数据做规范化实测：

| 存储对象 | v4 | v5 (实测) | Δ |
|---|---:|---:|---:|
| `keyword_chunks` 本体 | 87.8 MB | 11.4 MB | -87% |
| `keyword_chunks` 3 索引 | 226 MB | 22 MB | -90% |
| `chunks` + 索引 | 11.6 MB | ~2 MB | -83% |
| 库文件总大小 | **327 MB** | **~87 MB** | **-73%** |

真正的重复不是"关键字对存储原始文本"（`keyword_pairs` 只有 260 KB），而是
`keyword_chunks` 每行都携带 4 列 TEXT，其中 `directory` 全库仅 2 个 distinct 值、
`doc_path` 仅 4 个 distinct 值。

---

## 1. 设计约束

**契约冻结点**（重构后必须字节级一致）：

* `related --json` 的每个字段（`doc_path, keywords[], directory,
  qualified_path, graph_signals{seed_chunk_df, total_chunks_in_scope,
  neighbor_edges[{a,b,npmi,cooccur_count,source}]}`）。
* `related --json crossed_scopes_preview` 的 `top_other_scopes[].scope`、
  `top_bridge_keywords[].keyword`。
* `graph inspect --json` 的 `docs_by_scope{scope}`、
  `top_cooccurring_keywords[].shared_scopes[]` 与 `keyword` 字段。
* `index promote --json` 的 `pairs[].{directory, keyword_a, keyword_b}`。
* trace-signals v1 里 `encode_keyword(kw)` / `encode_file(path)`
  的输入字符串。
* `deep_search --json` 里 `history[].keywords`。

**允许改动的范围**：所有内部 SQL 查询与内部 API 参数。CLI 命令签名不变。

**非目标**：修剪长关键字噪声（"一下从原地消失横着出现..."之类）；这是抽取
器职责，本次不动。

---

## 2. 表结构 (v5)

### 2.1 新增 intern 表

```sql
CREATE TABLE directories (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE          -- '' 代表未 scope，与 v4 语义一致
);

CREATE TABLE keywords (
    id INTEGER PRIMARY KEY,
    kw TEXT NOT NULL UNIQUE            -- 已 casefold + strip
);

CREATE TABLE docs (
    id INTEGER PRIMARY KEY,
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    doc_path TEXT NOT NULL,            -- 相对 scope 根目录
    UNIQUE(dir_id, doc_path)
);
CREATE INDEX idx_docs_dir ON docs(dir_id);
```

`directories.path` 存储的是 v4 里的 `directory` 列值（`resolve_scope_key`
输出，`''` 或绝对路径）。`docs.doc_path` 存储的是 v4 里的相对路径。

### 2.2 迁移后的业务表

```sql
CREATE TABLE keyword_chunks (
    kw_id INTEGER NOT NULL REFERENCES keywords(id),
    doc_id INTEGER NOT NULL REFERENCES docs(id),
    chunk_id INTEGER NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (kw_id, doc_id, chunk_id)
) WITHOUT ROWID;
CREATE INDEX idx_kc_doc ON keyword_chunks(doc_id, chunk_id);

CREATE TABLE chunks (
    doc_id INTEGER NOT NULL REFERENCES docs(id),
    chunk_id INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    PRIMARY KEY (doc_id, chunk_id)
) WITHOUT ROWID;

CREATE TABLE keyword_pairs (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_a_id INTEGER NOT NULL REFERENCES keywords(id),
    kw_b_id INTEGER NOT NULL REFERENCES keywords(id),
    source TEXT NOT NULL,              -- 'chunk_cooccur' | 'llm_promoted'
    cooccur_count REAL NOT NULL DEFAULT 0,
    npmi REAL,
    updated_at TEXT,
    PRIMARY KEY (dir_id, kw_a_id, kw_b_id, source)
) WITHOUT ROWID;
CREATE INDEX idx_kp_a ON keyword_pairs(dir_id, kw_a_id);
CREATE INDEX idx_kp_b ON keyword_pairs(dir_id, kw_b_id);

CREATE TABLE heading_docs (
    doc_id INTEGER NOT NULL REFERENCES docs(id),
    heading_path TEXT NOT NULL,        -- 保持 TEXT（章节路径字面量本身唯一，
    chapter_id TEXT NOT NULL DEFAULT '',   --  不足以摊薄成 intern；行数极少）
    start_line INTEGER NOT NULL DEFAULT 0,
    end_line INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (doc_id, heading_path, chapter_id)
);
CREATE INDEX idx_hd_head ON heading_docs(heading_path);

CREATE TABLE llm_extension_votes (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_a_id INTEGER NOT NULL REFERENCES keywords(id),
    kw_b_id INTEGER NOT NULL REFERENCES keywords(id),
    vote_count REAL NOT NULL DEFAULT 0,
    last_voted TEXT,
    promoted_at TEXT,                  -- 之前由 promote.py 动态 ALTER，v5 直接建入
    PRIMARY KEY (dir_id, kw_a_id, kw_b_id)
);
CREATE INDEX idx_votes_dir ON llm_extension_votes(dir_id, vote_count DESC);

CREATE TABLE keyword_counts (
    scope TEXT NOT NULL DEFAULT '',    -- deep_search 意图上下文，保留
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_id INTEGER NOT NULL REFERENCES keywords(id),
    count REAL NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (scope, dir_id, kw_id)
);
CREATE INDEX idx_kc_count ON keyword_counts(dir_id, count DESC);

CREATE TABLE keyword_sources (
    dir_id INTEGER NOT NULL REFERENCES directories(id),
    kw_id INTEGER NOT NULL REFERENCES keywords(id),
    path TEXT NOT NULL,                -- 全路径命中，独立于 docs 命名空间
    line_number INTEGER NOT NULL DEFAULT 0,
    count REAL NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (dir_id, kw_id, path, line_number)
);
```

### 2.3 v4 → v5 差异记录

| v4 | v5 | 备注 |
|---|---|---|
| `chunks.scope`, `chunks.directory` | 折叠为 `docs.dir_id` (via `doc_id`) | `scope` 一直是 `''` 死列 |
| `keyword_pairs.scope` | 移除 | 同上 |
| `keyword_chunks.directory/doc_path/keyword` | → `kw_id, doc_id` | 主要收益 |
| `keyword_counts.term` | → `kw_id` | 语义等价 |
| `keyword_counts.directory` | → `dir_id` | 语义等价 |
| `heading_docs.directory/doc_path` | → `doc_id` | 章节名保持 TEXT |
| `llm_extension_votes.directory/keyword_a/keyword_b` | → `dir_id/kw_a_id/kw_b_id` | + 静态 `promoted_at` |
| `keyword_sources.directory/keyword` | → `dir_id/kw_id`；`path` 保留 | 全路径命中位置独立 |

---

## 3. 迁移策略

**原子性**：v4 → v5 在**单一 `BEGIN IMMEDIATE` 事务**内完成，参照现有
`_migrate_to_v4` 的模式（含 caller-owned txn 兼容 + TOCTOU 复查）。任何
步骤失败一律 ROLLBACK。

**迁移步骤**（一次事务）：

1. **快照旧数据**：`SELECT DISTINCT` 从每张旧表抽 `directory / keyword`
   全集到内存 dict（60 万关键字 × 平均 10 字节 ≈ 6 MB，可控）。
2. **建 v5 表**（用 `CREATE TABLE v5__...` 避免与旧表冲突）。
3. **INSERT** `directories`, `keywords`, `docs`，同时构造 Python 侧
   `dir_map / kw_map / doc_map`。
4. **INSERT ... SELECT ... JOIN** 从旧表批量灌入新表，每次一张表。
5. **DROP** 每张旧表；**ALTER TABLE v5__... RENAME TO ...**。
6. **CREATE INDEX** 全部索引。
7. `PRAGMA user_version = 5`。
8. `VACUUM`（在事务外，回收 240 MB freelist；在函数返回前调用）。

**幂等性**：`ensure_schema` 快速路径识别 `user_version >= 5` 直接返回。
在事务内二次读版本号（TOCTOU 复查）。

**跨版本升级**：v1/v3 → v5 走两跳（v1/v3 先升到 v4 内存态，再升 v5）。
简单起见：先跑 `_migrate_to_v4` 建 v4 中间态，同事务里立刻调
`_migrate_v4_to_v5`。

**预计耗时**：327 MB 库实测 6.3 秒（不含 VACUUM）；加 VACUUM 约 12 秒。
**磁盘峰值**：迁移期最多需要 ~500 MB 空间（旧表 + 新表并存 + rollback
journal），使用者要有 400 MB 富余。

---

## 4. Python 侧适配

### 4.1 新模块 `graph/ids.py`

小巧的 ID intern helper，供每次会话缓存 `(dir_path -> id)` /
`(keyword -> id)` / `(dir_id, doc_path -> id)`：

```python
class IdInterner:
    def __init__(self, conn): ...
    def dir_id(self, path: str) -> int: ...          # get-or-insert
    def dir_id_ro(self, path: str) -> Optional[int]: ...  # 只读
    def kw_id(self, kw: str) -> int: ...
    def kw_id_ro(self, kw: str) -> Optional[int]: ...
    def kw_ids_ro(self, kws: Sequence[str]) -> Dict[str, int]: ...  # 批量
    def doc_id(self, dir_id: int, doc_path: str) -> int: ...
    # 反查（出口 boundary）
    def kw_text(self, kw_id: int) -> str: ...
    def dir_text(self, dir_id: int) -> str: ...
    def doc_row(self, doc_id: int) -> Tuple[str, str]: ...  # (dir, doc_path)
```

大多数消费者只需要"批量把一堆 keyword 字符串换成 id 列表"和"把结果里
的 id 列换回字符串"。

### 4.2 出口 boundary（**契约守护点**）

在每个 CLI JSON 出口处，用 `IdInterner` 或 SQL join 反查回字符串：

* `related.py::Related.to_dict` — `directory`, `keywords`, `doc_path` 从
  `dir_id`/`kw_id`/`doc_id` 反查后填入。
* `related.py::_neighbor_edges_for_seeds` — 结果 `a, b` 反查关键字文本。
* `related.py::_preview_from_kw_score` — `top_other_scopes[].scope`,
  `top_bridge_keywords[].keyword` 反查。
* `graph/inspect.py::_inspect` — `docs_by_scope` 键名反查为路径；
  `top_cooccurring_keywords[].{keyword, shared_scopes}` 反查。
* `graph/promote.py::PromotedPair` — `directory`, `keyword_a`, `keyword_b`
  反查。
* `graph/votes.py::get_votes` — 同上。
* `deep_search.py::KeywordLibrary.history` — `keywords[]` 反查。
* `trace._build_graph_trace` — `encode_keyword` / `encode_file` 传入的字符串
  由 caller 反查后再传入。

### 4.3 内部 SQL 重写

所有 `WHERE directory = ?` 改为先 `dir_id = interner.dir_id_ro(scope_key)`，
再 `WHERE dir_id = ?`。空结果直接返回，避免下游 SELECT。

所有 `WHERE keyword IN (...)` 改为先批量 `kw_id_ro`，再
`WHERE kw_id IN (...)`（int 列表参数化更快）。

---

## 5. 测试计划

**新增**：`tests/test_schema_v5_migration.py`

* `test_v4_to_v5_preserves_row_counts`（用真实 v4 fixture 灌几百行，
  确认迁移后 keyword_chunks 行数不变、SELECT JOIN 回原字符串等价）。
* `test_v1_to_v5_two_hop`（跳过中间 v4 校验点，直接跳到 v5）。
* `test_v3_to_v5`（`keyword_docs` drop + salvage 直至 v5）。
* `test_v5_idempotent`（连调两次 `ensure_schema` no-op）。
* `test_migration_within_caller_txn`（复用 v4 已验的模式）。
* `test_intern_uniqueness`（同字符串两次 intern 得同 id）。

**更新**：
* `tests/test_graph_schema.py` — 断言 `SCHEMA_VERSION==5`, `GRAPH_TABLES`
  扩展含 `directories, keywords, docs`。原有 INSERT 断言改用新列名或
  改成通过 API 灌数据。
* `tests/test_graph_build.py` — 大量 `SELECT keyword FROM ...` 改为
  `SELECT keywords.kw FROM keyword_chunks JOIN keywords ...`。
* `tests/test_promote.py`, `tests/test_votes.py` — 同上。
* `tests/graph/test_chunk_migration.py` — 更新 fixture 至 v5 期望。

**回归**：
* `tests/test_related.py` / `tests/graph/test_inspect.py` /
  `tests/graph/test_cross_scope.py` / `tests/graph/test_ppr_regression.py`
  ——全走 API，不需大改，但需要确认输出 dict 的字段名/值字节级一致。
* `tests/test_cli_json.py` — 计数字段名不变，应通过。

**性能**：`bench/perf_baseline.py` 里 `SELECT COUNT(*) FROM keyword_docs`
是死代码（v3 遗物），顺手清掉。

---

## 6. 落地顺序

1. `graph/schema.py`：加 v5 常量、`_migrate_v4_to_v5`；旧 `_migrate_to_v4`
   保留（v1/v3 → v5 走两跳）。
2. `graph/ids.py`：新增 `IdInterner`。
3. `graph/build.py`：写入路径改 doc_id/kw_id。
4. `graph/ppr.py`：`load_edges` 直接读 `keyword_pairs`（kw_id）+
   `JOIN keywords` 反查（Edge.a/b 仍是字符串，保 PPR 语义不变）。
5. `related.py`：所有查询改 id，Related.to_dict 出口反查。
6. `graph/inspect.py`：同 5。
7. `graph/promote.py`：同 5，移除 `_ensure_promoted_marker`（v5 静态建入）。
8. `graph/votes.py`：同 5。
9. `deep_search.py::KeywordLibrary`：改为通过 `IdInterner`；`history()`
   出口反查。
10. 补 `tests/test_schema_v5_migration.py`；更新其它测试。
11. 更新 `docs/navigation.md`, `AGENTS.md`（如果提到 v4）, 本文件收尾章。
12. 用 `/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3` 做
    py_compile 和 `pytest tests/test_graph_schema.py tests/test_graph_build.py
    tests/test_promote.py tests/test_votes.py tests/test_related.py`。
13. 在开发机 327 MB 库上做端到端演练：备份 → ensure_schema → VACUUM →
    比对 CLI JSON 与备份的等价性 → 记录耗时和最终文件大小。

---

## 7. 后续（不在本次范围）

* 长关键字噪声修剪（"一一的将..."之类 8-20 字符全中文串）—— 抽取器职责。
* `heading_docs.heading_path` intern —— 当章节数量 > 万级再考虑。
* 未来 `related.py` cross-scope 热路径的进一步 pushdown（本次 join
  在 100K 级 doc 内足够）。
