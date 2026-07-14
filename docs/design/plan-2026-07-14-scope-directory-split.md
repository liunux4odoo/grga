# scope / directory 分野：知识可迁移化（schema v10）

**日期**：2026-07-14
**关联**：`docs/design/plan-2026-07-12-keyword-primary.md`（v8/v9 keyword-primary，§8 "未来方向"
里预留的"corpus-agnostic pair 合并 / pair 表导入导出"在此正式立项）
**动机**：现在 `keyword_pairs` 等知识资产的 shard 键 `dir_id` 是**绝对路径**
（`resolve_scope_key` → `Path(...).resolve()`）。这把"**知识**（关键字共现字典）"和
"**证据**（哪台机器的哪个目录/文件）"在 schema 层绑死，导致学到的字典与共现对**不可跨机器迁移**：
换一台机器（或搬一个目录）后，读侧一旦带 `--directory`，`directories` 表里查不到那个绝对路径
就直接返回空。

**结论**：把 shard 键从"物理目录绝对路径"升级为"**逻辑 scope 名**"，把物理目录降级为
**可清空的旁路元数据**。知识天然可移植，证据天然可丢弃。

**兼容立场**：工具刚发布、无历史用户，**不承担向后兼容负担**。`resolve_scope_key` 直接改语义、
**不做** `--directory` → scope 的 fallback、**不做** v9→v10 数据保迁移路径（见 §7）。

---

## 0. TL;DR

| 维度 | 现状（v9） | 新方案（v10） |
|---|---|---|
| 资产 shard 键 | `directories.id`（绝对路径） | `scopes.id`（逻辑名，如 `physics-cn`） |
| 物理目录 | 就是 shard 键本身 | `scope_directories` 旁路表，**不进任何 PK** |
| 跨机器迁移 | 带 `--directory` 即失效 | 知识包 import/export，物理层重建 |
| 读侧 scope 选择 | 单 scope（`--directory`）或全表 union | **多选** `--scope a --scope b`（`IN (...)`） |
| `resolve_scope_key` | 解析成绝对路径 | 解析成 scope 名（不 resolve 路径） |
| npmi / cooccur | per-scope，进 PK | **不变**（per-scope 统计，仍进 PK） |

**核心决策链**：

1. **知识 vs 证据分野是硬约束**：任何"这台机器的这个文件"的信息都不得进入知识资产表的 PK，
   也不得进入可迁移的知识包。
2. **scope 多选 ≠ 拆复合主键**：查询多选用 `WHERE scope_id IN (?,?)`，PK 保留 `scope_id`。
   一 pair 挂多 scope 标签 + 全局 npmi 的方案（下称 S3）被否决——npmi 是 scope 依赖统计量。
3. **物理目录是 caller 便利，不是知识**：`scope_directories` 存它，帮 caller 快速定位相关文档；
   迁移时允许整表清空。

---

## 1. 设计原则

### 1.1 知识 / 证据分野（本次最高优先级，硬约束）

> **知识**（可迁移、进知识包）：从语料统计学到的、与机器状态无关的东西。
> - `scopes`（逻辑域名）
> - `keywords`（casefold 全局词表）
> - `keyword_pairs`（共现 + npmi + cooccur_count + source）
> - `keyword_stats`（per-scope df / occurrences）
> - `scope_stats`（per-scope n_chunks / n_docs / vocab_size）
> - `auto_ngram_dict`（per-scope 自动分词字典）
> - `llm_extension_votes`（LLM 投票缓冲，随图走）
> - `meta`（toolchain_fingerprint 等全局标量）
>
> **证据**（机器本地、不进知识包、迁移时可清空/重建）：
> - `scope_directories`（scope → 本机物理路径绑定）
> - `docs`（scope 下的具体文件 + size/mtime/hash）
> - `keyword_docs`（doc 级倒排）
> - `heading_docs`（Markdown 章节结构）
> - `keyword_sources`（v1 deep-search 命中位置）

**分野的判据（写代码/加字段时自问）**：这条信息在另一台机器上、对另一批文件还成立吗？
- 成立 → 知识，可进 PK / 知识包。
- 不成立（依赖本机路径、本机文件 mtime、具体 doc 身份）→ 证据，进旁路表，迁移时清空。

这条原则**严格执行**：`resolve_scope_key` 不再触碰文件系统（不 `.resolve()`、不 `.expanduser()`
去解物理路径），因为一旦它 resolve 成绝对路径，知识 shard 键就重新沾上机器状态。

### 1.2 沿用的硬规则（AGENTS.md + keyword-primary）

- **HC2 无 corpus prior**：npmi 仍是纯数学恒等式。
- **信号暴露原则**：物理目录是 caller "拿不到的内部状态"（它只知道 scope 名，不知道本机绑定），
  所以 `scope_directories` 可以暴露给 caller；但**不塞进 `keyword_pairs` 返回值**——caller 想要
  时按 `scope_id` 反查，别替它 join。
- **契约冻结**：`related --json` 的候选关键字字段（related-signals-v3）**不变**；本次新增的是
  scope 管理命令与 `--scope` 多选参数（附加，非破变 related-signals，但 CLI 参数名 `--directory`
  → `--scope` 是破变，允许，因无用户）。
- **反"完备 schema"诱惑**：不预建 scope 组合表 / 全局共享 scope（`scope_id=0`）；跨 scope 查询用
  `IN` 已够，硬造组合层是把成本从 caller 转到 grga（杠杆反了）。

### 1.3 非目标

- **不做** S3（一行 pair 挂多 scope 标签 + 单一全局 npmi）——npmi 分母 N 是 per-scope 的，
  全局化需要 n_chunks 跨 scope 归一化，无干净方案，且语义变糟。
- **不做** `scope_groups` 组合层——`WHERE scope_id IN (...)` 已覆盖多选，无跨仓库重复证据。
- **不做** `--directory` → scope 的向后兼容 fallback（无用户，直接换语义）。
- **不做** 跨 scope 的 npmi 自动归一化（`n_chunks` 不可加；聚合永远在查询层做，不落聚合表）。
- **不动** `search` / `probe` / `expand` 热路径（它们走 rga / StructureDoc，不碰这些表）。

---

## 2. Schema v10

### 2.1 逻辑 scope 层（进 PK，可迁移）

```sql
-- 逻辑域名。取代 v9 的 directories 表作为资产 shard 键。
-- name 是稳定 handle：'physics-cn' / 'grga-code' / 'rfc-archive'。
-- 与文件系统解耦——同一 scope 可在多台机器、多个目录上重建。
CREATE TABLE scopes (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,       -- 逻辑域名（用户给定或从目录名派生）
    kind       TEXT,                        -- 可选：'domain' / 'project' / 'ad-hoc'
    created_at TEXT
);
```

`scopes` 完全取代 v9 `directories` 在资产表中的角色。**注意**：`directories` 表整个删除
（它的语义是"绝对路径 intern"，与知识/证据分野冲突）。

### 2.2 物理目录旁路层（不进任何 PK，可清空）

```sql
-- scope → 本机物理路径的绑定。纯 caller 便利 + build 输入定位。
-- 迁移到新机器时：整表不导出；在新机器上重新 bind 后 build。
CREATE TABLE scope_directories (
    scope_id   INTEGER NOT NULL REFERENCES scopes(id),
    directory  TEXT NOT NULL,               -- 本机绝对路径，仅在这台机器上有意义
    role       TEXT,                         -- 可选：'primary' / 'mirror'
    indexed_at TEXT,
    PRIMARY KEY (scope_id, directory)
);
```

用途：
1. **build 输入**：`grga index build --scope physics-cn --directory /home/me/corpus` 记录一条绑定。
2. **caller 定位**：读侧拿到候选关键字后想找原文，`related --json` 可以附带（或 caller 主动查）
   "physics-cn 在本机绑定的目录是 `/home/me/corpus`"，据此 ripgrep。这就是用户说的"directory
   信息对 caller 快速定位相关文档有帮助"——它以旁路形式保留，而不是钉进知识资产。
3. **迁移**：知识包不含此表；目标机器 import 知识后，`grga scope bind <name> <dir>` 重建绑定。

### 2.3 资产层（PK 里 dir_id → scope_id，其余不变）

```sql
-- 全局词表：kw_id ↔ 文本（1:1）。不变。
CREATE TABLE keywords (id INTEGER PRIMARY KEY, kw TEXT NOT NULL UNIQUE);

-- 共现知识资产。仅 dir_id → scope_id，语义/门槛/索引全部不变。
CREATE TABLE keyword_pairs (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    kw_a_id  INTEGER NOT NULL REFERENCES keywords(id),
    kw_b_id  INTEGER NOT NULL REFERENCES keywords(id),   -- 约定 kw_a_id < kw_b_id
    source   TEXT NOT NULL,                 -- chunk_cooccur | llm_promoted
    cooccur_count REAL NOT NULL DEFAULT 0,
    npmi     REAL,                           -- per-scope 统计；llm_promoted 为 NULL
    updated_at TEXT,
    PRIMARY KEY (scope_id, kw_a_id, kw_b_id, source)
) WITHOUT ROWID;
CREATE INDEX idx_kp_a_npmi ON keyword_pairs (scope_id, kw_a_id, npmi DESC);
CREATE INDEX idx_kp_b_npmi ON keyword_pairs (scope_id, kw_b_id, npmi DESC);

CREATE TABLE keyword_stats (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    kw_id    INTEGER NOT NULL REFERENCES keywords(id),
    chunk_df INTEGER NOT NULL DEFAULT 0,
    doc_df   INTEGER NOT NULL DEFAULT 0,
    total_occurrences INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (scope_id, kw_id)
) WITHOUT ROWID;

CREATE TABLE scope_stats (
    scope_id INTEGER PRIMARY KEY REFERENCES scopes(id),
    n_chunks INTEGER NOT NULL DEFAULT 0,     -- npmi 的 N，per-scope，永不跨 scope 相加
    n_docs   INTEGER NOT NULL DEFAULT 0,
    vocab_size INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE auto_ngram_dict (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    phrase   TEXT NOT NULL, n INTEGER, count INTEGER,
    npmi     REAL, branch_ent REAL, updated_at TEXT,
    PRIMARY KEY (scope_id, phrase)
);

CREATE TABLE llm_extension_votes (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    kw_a_id  INTEGER, kw_b_id INTEGER,
    vote_count REAL DEFAULT 0, last_voted TEXT, promoted_at TEXT,
    PRIMARY KEY (scope_id, kw_a_id, kw_b_id)
);
```

### 2.4 证据层（scope_id 指向逻辑 scope，但整层是机器本地、不迁移）

```sql
-- 具体文件。dir_id → scope_id。迁移不导出（目标机器重建）。
CREATE TABLE docs (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    doc_path TEXT NOT NULL,                  -- 相对 scope 绑定目录
    size INTEGER, mtime REAL, content_hash TEXT,
    n_chunks_with_kw INTEGER NOT NULL DEFAULT 0,
    UNIQUE(scope_id, doc_path)
);
CREATE TABLE keyword_docs (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    kw_id INTEGER, doc_id INTEGER,
    occurrence_count INTEGER DEFAULT 0, chunk_count INTEGER DEFAULT 0,
    PRIMARY KEY (kw_id, doc_id)
) WITHOUT ROWID;
CREATE TABLE heading_docs (
    doc_id INTEGER NOT NULL REFERENCES docs(id),
    heading_path TEXT NOT NULL, chapter_id TEXT DEFAULT '',
    start_line INTEGER DEFAULT 0, end_line INTEGER DEFAULT 0, updated_at TEXT,
    PRIMARY KEY (doc_id, heading_path, chapter_id)
);
-- keyword_sources / keyword_counts（v1 deep-search）同样 dir_id → scope_id。
```

**关键点**：证据层的 `scope_id` 只是"这些证据属于哪个逻辑 scope"，绑定关系仍在
`scope_directories`。删证据不动知识（沿用 keyword-primary D6：删文件不销毁 pair）。

### 2.5 删除

```
× directories      -- 绝对路径 intern，与分野冲突，整表删（不迁移进 scopes）
```

---

## 3. `resolve_scope_key` 的新语义（破变，不兼容）

现状（v9，`graph/schema.py`）：

```python
def resolve_scope_key(directory) -> Optional[str]:
    # None → None; "" → ""; path → str(Path(path).expanduser().resolve())
```

v10：**函数职责改为"归一化逻辑 scope 名"，不碰文件系统**。

```python
def resolve_scope_name(name: Optional[str]) -> Optional[str]:
    """None → None（无 scope 过滤）；空串 → 报错（scope 名不能为空）；
    否则 → name.strip()（不 casefold，scope 名大小写敏感；不 resolve 路径）。"""
```

- **不再有** `""` 作为"unscoped 但可过滤"的 sentinel——逻辑 scope 必须有名字。`None` 仍表示
  "不按 scope 过滤"（跨 scope union）。
- **不再** `expanduser` / `resolve`——scope 名是纯字符串 handle。
- `scope_write_key` 删除（它的存在是为了把 `None`/`""` 折叠成写入 sentinel，v10 写入必须给
  真实 scope 名，无 sentinel）。

**受影响调用点**（本次需改）：
- `graph/schema.py`：`resolve_scope_key` / `scope_write_key` 重写/删除。
- `graph/votes.py`：`add_vote` / `get_votes` 的 scope 解析。
- `graph/promote.py`：promote 的 scope 过滤。
- `graph/build.py`：`build_graph_index` 的 `scope_dir_id = interner.dir_id(scope_key)`
  → `interner.scope_id(scope_name)`；同时记录 `scope_directories` 绑定。
- `graph/ids.py`：`IdInterner` 的 `dir_id` / `dir_text` 系列 → `scope_id` / `scope_name`；
  新增 `docs` 的 scope_id 关联。
- `related.py`：`related_keywords` / `related` 的 `directory=` 参数 → `scopes=`（多选）。
- `graph/inspect.py`：`docs_by_scope` 的 scope 来源改为 `scopes.name`。

---

## 4. scope 多选（S1 + S2，不拆 PK）

### 4.1 三种"多选"语义的定案

| 语义 | 说明 | PK 影响 | 采纳 |
|---|---|---|---|
| **S1 查询多选** | caller: "给我 pair 在 {physics, math} 的所有记录" | 无（`IN (?,?)`） | ✅ |
| **S2 学习多份** | pair(A,B) 在 physics npmi=0.7，在 math npmi=0.5，各一行 | 无（现有模型） | ✅ |
| **S3 一行多标签** | 一行 pair 挂 `scopes=[physics,math]` + 单一 npmi | 拆 PK / 塞 JSON | ❌ 否决 |

**S3 否决理由**：npmi = f(cooccur, df_a, df_b, **n_chunks**)，`n_chunks` 是 per-scope 的。
一行 pair 若要覆盖多 scope，要么丢掉 per-scope npmi（知识失真），要么把 npmi 塞成
`{scope: npmi}` JSON（等价于 S2 但破坏 index-only `ORDER BY npmi DESC` 早停，perf 回退到
keyword-primary 之前）。**S1+S2 已完全满足"多选"需求**，S3 是伪需求。

### 4.2 查询路径（`related_keywords` 支持多 scope）

```python
def related_keywords(
    seeds, *, limit_per_seed=30, min_npmi=None, db_path=None,
    scopes: Optional[Sequence[str]] = None,   # None → 跨全部 scope union
) -> RelatedKeywordsResult:
```

SQL 从 `WHERE dir_id = ?` 变为：

```sql
-- 单/多 scope：scope_id IN (...)；无 scope：省略该 clause（cross-scope）
WHERE scope_id IN (?, ?, ...) AND kw_a_id = ? AND source = ? AND npmi >= ?
ORDER BY npmi DESC LIMIT ?
```

**索引利用**：`idx_kp_a_npmi (scope_id, kw_a_id, npmi DESC)` 对**单个** scope_id 前缀走
index-only 早停。多 scope `IN` 时 SQLite 对每个 scope_id 各走一次索引区间再归并；对
2~3 个 scope 仍是亚毫秒。跨全部 scope（无 clause）走 `idx_kp_b_npmi` 全区间——与 v9 的
`--cross-scope` 现状同性能（~700ms warm，见 related.py 的 TODO(perf) 注释），本次不优化。

**chunk_df 聚合语义**：多 scope 查询时 `keyword_stats.chunk_df` 按 `SUM ... GROUP BY kw_id`
跨选中 scope 相加（沿用 v9 cross-scope 行为）。这是 hub-word 判别信号，不参与候选排序，
SUM 的轻微 double-count 可接受。**永不落聚合表**——聚合只在查询层临时算（§1.3）。

### 4.3 CLI

```
grga related <seeds...> --scope physics-cn --scope math-en   # 多选
grga related <seeds...>                                       # 不带 = 跨全部 scope
```

`--directory` 参数从 `related` / `promote` / `index build` 等命令移除或改名：
- `index build` / `rebuild` / `forget`：`--scope <name>`（逻辑名）+ `--directory <path>`
  （物理路径，写入 `scope_directories`）。两者语义**彻底分开**：`--scope` 给知识归属，
  `--directory` 给证据来源。build 要求两者都给（首次）；后续增量 build 可只给 `--scope`
  （目录从 `scope_directories` 查最近一次绑定）。
- `related` / `promote` / `record-vote`：`--scope <name>`（可多选，promote/vote 单选）。

---

## 5. 知识包 import / export（迁移的一等公民）

### 5.1 导出

```
grga index export --scope physics-cn --out physics-cn.grgapkg
grga index export --all --out full.grgapkg
```

打包**仅知识层**（§1.1 白名单）：`scopes`（选中的）+ 被引用的 `keywords` +
`keyword_pairs` / `keyword_stats` / `scope_stats` / `auto_ngram_dict` /
`llm_extension_votes` + `meta`。

**剥离证据层**：`scope_directories` / `docs` / `keyword_docs` / `heading_docs` /
`keyword_sources` **不进包**。

格式建议：一个 SQLite 文件（同 schema v10 但证据表为空），或 JSONL + manifest。倾向
**SQLite 文件**（直接 `ATTACH` 做 import，零解析）。

### 5.2 导入

```
grga index import physics-cn.grgapkg --as physics-cn        # 新建 scope
grga index import physics-cn.grgapkg --merge-into physics    # 合并进已有 scope（见下）
```

- `--as <name>`（**本次只实现这个**）：把包里的 scope 落成目标库的新 scope。`keywords` 做
  get-or-insert（全局词表复用），`keyword_pairs` 等按新 `scope_id` 整体 INSERT。
- `--merge-into <name>`（**本次不实现，留痕**）：UPSERT 累加 `cooccur_count`，npmi 需要
  合并策略（MAX / 以新覆旧 / 按 n_chunks 加权重算）——语义未定，禁用直到有证据需求。

### 5.3 `RelatedKeyword.scopes` 字段（related-signals v3.1，MINOR bump）

当 caller 用 `related_keywords(scopes=["a","b"])` 多选查询时，`_related_keywords_impl`
内部会通过 `WHERE scope_id IN (?, ?)` 拿到多 scope 命中的行，然后按 `(kw, source)`
去重取 npmi 最大值——**scope 维度在合并时被丢弃**。Caller 拿不到"这个候选来自哪几个
scope"，也无法通过 `graph inspect` 反向对齐（inspect 返回全局 scope 分布，不是本次查
询范围）。

按 §信号暴露原则三问检验：**不可派生**（合并逻辑吞掉了 scope）+ **是内部状态** +
**在低频动词 related 上**，都通过。

**本次新增**（MINOR，additive）：`RelatedKeyword` 加一个可选字段
`scopes: List[str]`，列出该候选在本次查询范围内的**哪几个** scope 命中。

```python
@dataclass
class RelatedKeyword:
    kw: str
    npmi: float                # 跨所选 scope 的 max（不变）
    cooccur_count: float       # 跨所选 scope 的 max 那份（不变）
    via_seed: str
    chunk_df: int
    source: str
    scopes: List[str]          # NEW：命中的 scope 名列表（v3.1）
```

**不做 `per_scope_npmi`**：想要分 scope 的度量，caller 分两次调用
`related_keywords(scopes=["a"])` 和 `related_keywords(scopes=["b"])`——语义清晰、
契约简单，避免 `--json` dict 膨胀。

**实现要点**：
- SQL 的 `SELECT` 加一列 `scope_id`（不加 JOIN，`scope_id → name` 映射已在函数入口拿到）
- `merged` 字典 value 从 `(npmi, cooccur)` 扩成 `(npmi, cooccur, scope_ids: set)`
- 最终 `RelatedKeyword` 构造时把 `scope_ids` set 反查成名字列表
- 单 scope 查询：`scopes = ["单个"]`（冗余但契约一致）
- 跨 scope（`scopes=None`）：`scopes` 应包含所有命中 scope 名（需要额外反查 `scopes` 表回名，或先 batch load）

**契约**：`schemas/related-signals-v3.json` 加可选属性 `"scopes": {"type": "array",
"items": {"type": "string"}}`；old caller 忽略新字段安全。

导入后：目标机器 `grga scope bind physics-cn /local/physics/corpus`，再
`grga index build --scope physics-cn`（可选，若想在本机语料上补证据层 + 增量 refine 知识）。

### 5.3 scope 管理命令（新增 `grga scope` 子命令组）

```
grga scope list                          # 列出所有 scope + 绑定目录 + 资产规模
grga scope bind <name> <directory>       # 绑定/追加物理目录
grga scope unbind <name> [<directory>]   # 解绑（清证据绑定，不动知识）
grga scope rename <old> <new>            # 改逻辑名
grga scope rm <name> [--knowledge]       # 删证据；--knowledge 连知识一起删
```

---

## 6. Build 语义（v10）

沿用 keyword-primary v8/v9 的增量累加（§3 of keyword-primary），仅两处变化：

1. **scope 来源**：`scope_id = interner.scope_id(scope_name)`，`scope_name` 来自 `--scope`，
   **不再** `str(Path(scope).resolve())`。
2. **目录记录**：build 开始时 `INSERT OR REPLACE INTO scope_directories(scope_id, directory,
   indexed_at)`，`directory` 来自 `--directory`。文件枚举 `iter_text_files` 走这个物理目录，
   `docs.doc_path` 存相对该目录的路径。

`auto_ngram_dict` 仍 per-scope 加载（`_load_auto_dict(conn, scope_id)`）。**新增可选**：
`--seed-ngram-from <other-scope>` 允许 build 时借用另一个 scope 的分词字典（迁移知识包后，
在本机新语料上 build 时复用领域切词表的典型场景）。默认不借用（保持 build 自足）。

---

## 7. 迁移 v9 → v10（无损保迁移 vs 直接重建）

**立场：不做数据保迁移。** 工具无用户，`directories`（绝对路径）→ `scopes`（逻辑名）的
自动映射没有干净语义（绝对路径不是好的逻辑名），且会把机器状态带进新的知识层，违反分野。

**采纳方案**：v10 的 `ensure_schema` 检测到 `user_version < 10` 且存在旧 `directories` 表时，
**报错并给出明确指引**，不自动迁移：

```
grga index: schema v9 → v10 是不兼容重构（scope/directory 分野）。
旧库的绝对路径 scope 无法自动映射为逻辑域名。请：
  1. 记下旧 scope（绝对路径）：grga scope list  # 旧版
  2. 删除旧库：rm ~/.config/grga/deep_keywords.db
  3. 用逻辑名重建：grga index build --scope <name> --directory <path>
（工具尚在早期，无历史数据兼容承诺。）
```

> 若日后有用户数据需保：单独写一个 `grga index migrate-v9 --map <path>=<scope-name> ...`
> 一次性工具，让用户**显式**提供路径→逻辑名映射。不进 `ensure_schema` 自动路径。

`ensure_schema` 对全新库直接建 v10 全表（快路径）。v10 起 `SCHEMA_VERSION = 10`。

---

## 8. 契约影响

- **related-signals-v3（`related --json`）**：候选关键字字段 `{kw, npmi, cooccur_count,
  via_seed, chunk_df, source}` + `resolved_seeds` **不变**。多 scope 是查询侧过滤，不改 emit
  结构。`schemas/related-signals-v3.json` 无需改。
- **CLI 破变（允许，无用户）**：`--directory` 在 `related`/`promote`/`record-vote` 改为
  `--scope`（含多选）；`index build` 拆成 `--scope` + `--directory` 两个正交参数。
- **新增契约**：`grga index export/import` 的知识包格式（SQLite，schema v10、证据表空）、
  `grga scope *` 子命令。知识包格式一旦发布即外部契约（同 related-signals 的冻结规则）。
- **退出码**：import 遇 scope 名冲突（`--as` 已存在）用现有"invalid usage"码；知识包 schema
  版本不匹配用现有"index/schema"码。见 `docs/exit-codes.md`（**只加不改**）。

---

## 9. 已决问题（本轮拍板结果）

### 9.1 scope 名必须显式（定案）

`index build` **强制要求** `--scope <name>`；没有从 `--directory` 派生的默认 scope 名。理由：
强制用户想清楚"这批语料的逻辑归属"，避免又滑回"目录即 scope"的老路。首次 build 啰嗦一次
可以接受，之后 `scope_directories` 已绑定，重复 build 无需再指定目录。

### 9.2 `scope_directories` 允许一 scope 多目录（定案）

表结构已支持（PK `(scope_id, directory)`，天然可多行）。build **单次一个目录**——
`--directory a --directory b` 不支持一次绑多个（避免 Pass-0 枚举语义复杂化）。用户想把
`physics/papers` + `physics/notes` 都归到 `physics-cn`：分两次 `grga index build --scope
physics-cn --directory <path>`，增量累加进同 scope。`grga scope list` 会显示一个 scope 的
所有绑定行。

### 9.3 知识包完整带 votes + llm_promoted，`promoted_at` 原样保留（定案）

`llm_extension_votes` 全部进知识包，`keyword_pairs` 里 `source='llm_promoted'` 的行也进包。
`promoted_at` 是 votes 表上的时间戳标记，作用是幂等——防止同一投票被 `promote_votes` 二次
升级到 `keyword_pairs`（见 `graph/promote.py` 顶部注释：非破坏性 + 幂等审计）。因为
llm_promoted pair 与投票一起迁移，`promoted_at` 的语义（"这条投票在源库里已升级到源库的
pair 表"）在目标库中仍自洽——投票和它的 promoted 结果配对搬走。目标机器 `--repromote`
仍可累加，跟本机行为一致。

### 9.4 迁移后 inspect 的 doc 分布空态文案（定案）

`inspect_keyword` 报 `docs_by_scope: {scope_name: count}`。scope 从绝对路径变逻辑名后展示
更可读（正面收益）。但导入知识包后 `docs` 表为空，`docs_by_scope` 会是空 dict——需在返回
体 `hint` 字段（或 stderr 提示）里给出：

> "no doc-level evidence in this scope; run `grga index build --scope <name> --directory <path>`
> to attach local corpus, or use `grga related --scope <name>` for pure knowledge-graph
> expansion (evidence not required)."

这条 hint 只在 `docs_by_scope` 为空 **且** 该 scope 在 `keyword_stats` 中有条目时触发
（区分"scope 是空 scope"和"scope 有知识但无本机证据"两种情况）。

---

## 10. 落地阶段（独立可交付、独立可回滚）

```
Phase 1  schema v10：scopes / scope_directories 表 + 资产/证据表 dir_id→scope_id
         + ensure_schema 对旧库报错（§7）+ 删 directories 表
         · 测试：全新库建 v10 全表；旧库触发报错指引
Phase 2  resolve_scope_key → resolve_scope_name（§3）+ IdInterner scope 化
         + votes/promote/build/related/inspect 全部调用点改造
         · 最小后端脚本：build 一个 scope → related_keywords(scopes=[name]) 命中
Phase 3  CLI：--scope 多选（related）、--scope + --directory 拆分（build）
         + grga scope 子命令组（list/bind/unbind/rename/rm）
Phase 4  知识包 export / import --as（§5）
         · 测试：export scope → 新库 import --as → related 命中，且证据表为空
Phase 5  可选：--seed-ngram-from（§6）+ docs/navigation.md / env-vars.md 更新
         + import --merge-into 留痕（不实现）
```

每阶段独立 PR。Phase 1 是不兼容 schema 变更（删表 + 报错路径），需最充分测试。
Phase 2 触碰面最广（所有 scope 解析调用点），需 grep 全量核对。

---

## 11. 决策记录（本线程达成）

| # | 决策 | 依据 |
|---|---|---|
| D1 | 资产 shard 键 dir_id（绝对路径）→ scope_id（逻辑名） | 知识/证据分野；绝对路径带机器状态，不可迁移 |
| D2 | 物理目录降级为 scope_directories 旁路表，不进任何 PK | 用户明确：directory 单独存放、迁移时可清空 |
| D3 | scope 多选 = S1(查询 IN) + S2(学习多行)，否决 S3 | npmi 是 per-scope 统计量，全局化无干净方案 |
| D4 | 不拆复合主键，PK 保留 scope_id | S1/S2 用 `WHERE scope_id IN (...)` 已满足 |
| D5 | resolve_scope_key 改语义、不 resolve 路径、无向后兼容 | 用户明确：无历史用户，严格分野 > 兼容 |
| D6 | v9→v10 不自动数据迁移，ensure_schema 报错指引 | 绝对路径→逻辑名无干净自动映射，且会污染知识层 |
| D7 | 不做 scope_groups / 全局共享 scope | 反"完备 schema 诱惑"，IN 已够，无重复证据 |
| D8 | 知识包只含知识层，证据层目标机器重建 | 分野的终极形态：知识可移植，证据本地重建 |
| D9 | import 仅 --as，--merge-into 留痕不实现 | npmi 合并语义未定，避免"预留即污染" |
| D10 | scope 名必须显式，不从目录名派生 | 强制用户想清楚逻辑归属，防滑回"目录即 scope" |
| D11 | build 单次绑一个目录，多目录靠多次 build 累加 | 避免 Pass-0 枚举语义复杂化，表结构已支持多行 |
| D12 | 知识包带 votes + llm_promoted，promoted_at 原样保留 | 二者配对迁移，幂等标记语义在目标库自洽 |
| D13 | inspect 遇 knowledge-only scope 发 hint（区分空 scope） | 分野必然产物：解释"有知识无证据"给 caller |
| D14 | `RelatedKeyword.scopes` 加字段（v3.1 MINOR bump） | 多选查询后合并逻辑吞掉 scope 维度，caller 不可派生；agent 下一步路由 / 跨领域 vocab 甄别需要；`per_scope_npmi` 不加（caller 分次查询更干净） |
