# scope 下多目录安全追加 + 知识资产 CLI 保留开关

**日期**：2026-09-12  
**关联**：
- `docs/design/plan-2026-07-14-scope-directory-split.md`（v10 scope/directory 分野）
- `memory/projects/grga.md` §4（07-12 keyword-primary 把 `keyword_pairs` 提升为核心知识资产；07-14 知识/证据分野）

**动机**：v10 把物理目录降级为证据层，但 `docs` 表仍未记录文件来自哪个 directory binding。当同一个 scope 先后绑定多个目录时，相对路径可能冲突，导致后一次 build 覆盖前一次数据。同时，`grga index forget --all` 会把 `keyword_pairs` 等知识资产一并清空，但用户 increasingly 把这些资产当作可累积、可迁移的对象，希望 CLI 提供显式保留开关。

---

## 0. TL;DR

| 维度 | 现状 | 新方案 |
|---|---|---|
| `docs` 唯一键 | `(scope_id, doc_path)` | `(scope_id, directory, doc_path)` |
| `docs.doc_path` 相对基准 | 本次 build 传入的 directory | 不变，但显式记录于 `docs.directory` |
| 同 scope 追加目录 | 不安全，相对路径可能冲突 | 安全，directory 参与 identity |
| `forget --all` | 必须显式传 `--all` | `--all` 变可选/废弃，默认即 wipe |
| 保留知识资产 | 不支持 | `--keep-dict`、`--keep-pairs`、`--keep-assets` |
| pair 保留后的 npmi | 无此场景 | 默认 frozen（原值保留），可选 `--recompute-pairs` |

**核心决策链**：

1. `docs` 的 identity 必须包含 `directory`，否则同 scope 多目录无法区分来源。
2. `keyword_pairs`、`complementary_pairs`、`auto_ngram_dict`、`keyword_stats`、`scope_stats` 是知识资产，CLI 清理时应允许用户显式保留。
3. 保留 pair 默认走 **frozen** 模式（原 npmi 不变），与知识包 import 语义一致；需要时再显式 `--recompute-pairs`。
4. `--all` 在存在 `--keep-*` 选项后语义冗余，改为默认行为，保留 `--all` 仅作向后兼容。

---

## 1. 设计原则

### 1.1 证据层需要记录自己的来源

v10 把 `scope_directories` 定义为证据层，但 `docs` 表没记录自己来自哪个 binding。这导致：

- 同 scope 绑定 `/a` 和 `/b` 后，`/a/x.md` 与 `/b/x.md` 共享同一个 `doc_path`。
- 没有可靠方式判断某条 `docs` 行应相对哪个 directory 解析。

因此 `docs.directory` 不是新知识，而是**证据层的自描述字段**，与 v10 的知识/证据分野不冲突。

### 1.2 知识资产默认可保留

`keyword_pairs`、`complementary_pairs`、`auto_ngram_dict`、`keyword_stats`、`scope_stats`、`llm_extension_votes` 都是知识层。清理命令不应无条件销毁它们，而应要求用户显式选择是否保留。

### 1.3 保持 caller-first 与客观 npmi

- frozen 模式下保留的 pair npmi 可能与新 docs 不一致，这是**调用者主动选择**的行为，不是 grga 替调用者做加权/遗忘。
- `--recompute-pairs` 只重算已保留 pair 的 npmi，不自动删低 npmi pair（仍由 caller 显式 `forget --phrase` 处理）。

---

## 2. Schema 变更

### 2.1 `docs` 表

```sql
ALTER TABLE docs ADD COLUMN directory TEXT NOT NULL DEFAULT '';

-- 旧约束：UNIQUE(scope_id, doc_path)
-- 新约束：UNIQUE(scope_id, directory, doc_path)
```

- `directory` 存储的是**本次 build 传入的 directory 参数原始字符串**（与 `scope_directories.directory` 一致）。
- `doc_path` 仍相对于该 `directory`。
- 已有数据迁移后 `directory = ''`（空字符串），属于 legacy 行；这些行必须通过 rebuild 重新索引才能参与同 scope 多目录场景。

### 2.2 `scope_directories` 不变

`scope_directories` 继续作为证据层绑定清单，表结构与语义不变。

---

## 3. Build / Rebuild 行为变更

### 3.1 `doc_id` identity

`IdInterner.doc_id()` 的 lookup key 从 `(scope_id, doc_path)` 改为 `(scope_id, directory, doc_path)`。

### 3.2 相对路径计算

`_rel_path_of()` 继续以**本次 build 传入的 directory** 为基准计算 `doc_path`，结果存入 `docs.directory`。

### 3.3 目录枚举范围

`iter_text_files()` 仍只枚举本次 build 传入的 directory，不自动扩展到 scope 的所有 bindings。

### 3.4 同 scope 追加目录

```bash
grga index build --scope work --directory /a
grga index build --scope work --directory /b
```

结果：scope `work` 同时包含 `/a` 和 `/b` 的文件，因为 `(work, /a, x.md)` 与 `(work, /b, x.md)` 身份不同。

### 3.5 Rebuild 语义

`rebuild` 默认仍清空该 scope 的 evidence + knowledge 后重建。新增 `--keep-*` 选项见 §4。

---

## 4. CLI 变更

### 4.1 `grga index forget`

#### 默认行为

```bash
grga index forget --scope work
```

等效于当前 `grga index forget --all --scope work`：
- 删除 `docs`、`keyword_docs`、`heading_docs`
- 删除 `keyword_pairs`（`source='chunk_cooccur'`）
- 删除 `complementary_pairs`
- 删除 `keyword_stats`、`scope_stats`
- 删除 `auto_ngram_dict`
- 保留 `llm_promoted` pair（与现状一致）
- 保留 `scope_directories` 绑定（与现状一致）

#### `--all` 选项

- 仍接受，但为 **no-op / deprecated**。
- 保留是为了不破坏现有脚本。
- 文档与 help 提示用户将来可省略。

#### `--keep-dict`

```bash
grga index forget --scope work --keep-dict
```

保留 `auto_ngram_dict`，其余与默认行为相同。

#### `--keep-pairs`

```bash
grga index forget --scope work --keep-pairs
```

保留：
- `keyword_pairs`（含 `chunk_cooccur` 与 `llm_promoted`）
- `complementary_pairs`
- `keyword_stats`
- `scope_stats`

删除 evidence 层（`docs` / `keyword_docs` / `heading_docs`）。

**npmi 语义**：frozen。原值不变。

#### `--keep-assets`

```bash
grga index forget --scope work --keep-assets
```

同时保留 `--keep-dict` + `--keep-pairs` 涉及的全部表。

#### `--recompute-pairs`

```bash
grga index forget --scope work --keep-pairs --recompute-pairs
```

仅在与 `--keep-pairs` 或 `--keep-assets` 同时使用时生效：
- 保留 pair 行
- 基于清理后剩余 evidence（此时通常为空）重算 `npmi`、`chunk_df`、`scope_stats`

> 注：`forget` 后 evidence 通常为空，因此 `--recompute-pairs` 在 `forget` 场景下主要作用是清零 npmi 或标记 stale，具体行为需在实现时定义。如果 evidence 为空导致无法重算，应报错或回退到 frozen。

### 4.2 `grga index build` / `grga index rebuild`

#### `--keep-dict`

```bash
grga index rebuild --scope work --directory /a --keep-dict
```

效果等价于 `--refresh-ngram never`：不清空 `auto_ngram_dict`，直接用已有字典。

#### `--keep-pairs`

```bash
grga index rebuild --scope work --directory /a --keep-pairs
```

- 不清空 `keyword_pairs`、`complementary_pairs`、`keyword_stats`、`scope_stats`
- 新 docs 产生的 pair 与旧 pair **合并**（UPSERT 语义：`cooccur_count` 累加或覆盖）
- 默认 npmi 为 frozen

#### `--keep-assets`

同时启用 `--keep-dict` + `--keep-pairs`。

#### `--recompute-pairs`

```bash
grga index rebuild --scope work --directory /a --keep-pairs --recompute-pairs
```

build 完成后，对 scope 内全部 pair 重新计算 npmi、chunk_df、scope_stats。用于 caller 希望保留关系但刷新统计量。

### 4.3 与 `--refresh-ngram` 的关系

- `--keep-dict` 与 `--refresh-ngram` 互斥或等价，需实现时决定。
- 建议：`--keep-dict` 是高层语义，`--refresh-ngram never` 是其内部等价物；两者同时传时以 `--keep-dict` 为准或报错。

---

## 5. Migration

### 5.1 Schema migration

```sql
ALTER TABLE docs ADD COLUMN directory TEXT NOT NULL DEFAULT '';
```

- 所有现有行 `directory = ''`。
- 不自动改 UNIQUE 约束（SQLite 有限制），需在 schema 初始化/升级时处理。

### 5.2 数据迁移策略

**不自动回填 `directory`**。原因：
- 单 binding scope 可回填，但多 binding scope 无法确定来源。
- 自动猜测会引入脏数据。

推荐流程：

```bash
# 对需要多目录能力的 scope 重建
grga index forget --scope work --keep-assets
grga index rebuild --scope work --directory /a --keep-assets
grga index build  --scope work --directory /b --keep-assets
```

### 5.3 Legacy 行处理

- `directory = ''` 的 `docs` 行仍可被查询（读侧 `directory` 通常不参与过滤）。
- 但向已有 legacy 数据的 scope 追加新目录时，legacy 行与新行 identity 不同，不会冲突。
- 建议文档说明：legacy scope 应 rebuild 以获得完整多目录能力。

---

## 6. 验收标准

### 6.1 Schema 层

- `docs` 表包含 `directory` 列，且唯一键为 `(scope_id, directory, doc_path)`。
- `directory` 默认值 `''`，旧数据不 crash。

### 6.2 Build 行为

- 同 scope 先后 build `/a` 和 `/b`，`docs` 中同时存在两行 `(work, /a, x.md)` 与 `(work, /b, x.md)`，互不影响。
- `_rel_path_of` 仍基于传入 directory 计算。

### 6.3 Forget 行为

- `grga index forget --scope work` 默认 wipe 全部（不再需要 `--all`）。
- `grga index forget --scope work --all` 行为不变，仅 deprecation warning。
- `grga index forget --scope work --keep-dict` 不清除 `auto_ngram_dict`。
- `grga index forget --scope work --keep-pairs` 不清除 `keyword_pairs`、`complementary_pairs`、`keyword_stats`、`scope_stats`。
- `grga index forget --scope work --keep-assets` 同时保留 dict + pairs + stats。

### 6.4 Rebuild 行为

- `grga index rebuild --scope work --directory /a --keep-assets` 重建 evidence 层但保留全部知识层。
- `--recompute-pairs` 在 rebuild 后刷新全部 pair 的 npmi。

### 6.5 回归

- 现有测试全绿。
- 新增测试覆盖：同 scope 多目录不冲突、`--keep-*` 各组合行为、`--recompute-pairs` 刷新 npmi。

---

## 7. 风险与开放问题

### 7.1 pair 合并语义

`--keep-pairs` 下，旧 pair 与新 pair 的 `cooccur_count` 如何处理？
- **选项 A**：新 pair 覆盖旧 pair（简单，但丢失历史计数）。
- **选项 B**：计数累加（符合"知识累积"直觉，但要求 pair identity 一致）。
- **倾向**：选项 B，但需在实现时确认 `source='chunk_cooccur'` 的 pair 在两次 build 间 identity 一致。

### 7.2 `forget --recompute-pairs` 的实用性

`forget` 后 evidence 通常为空，重算 npmi 可能无意义。是否只在 `rebuild` 场景支持 `--recompute-pairs`，`forget` 场景禁用？

### 7.3 `--keep-pairs` 与 `llm_promoted` 的边界

`llm_promoted` pair 本来就不受 `forget --all` 影响。`--keep-pairs` 是否应显式包含它们，还是默认行为已足够？
- 建议：`--keep-pairs` 显式声明保留所有 pair 来源（包括 `chunk_cooccur`），避免 caller 对来源产生歧义。

### 7.4 Schema 唯一键迁移

SQLite 对 ALTER TABLE 改 UNIQUE 约束支持有限。实现时可能需要：
- 新建带新约束的临时表
- 迁移数据
- 删旧表重命名

这是 implementation detail，计划只要求结果正确，不指定具体 SQL。

---

## 8. 不做的事

- 不改 `keyword_pairs` 的 PK 结构（仍按 `scope_id` 分区）。
- 不改 `scope_directories` 的语义。
- 不引入全局共享 scope 或 scope 组合表。
- 不自动 decay / 加权 npmi（仍由 caller 显式管理）。
- 不做 v9→v10 式的激进 breaking；本次所有 CLI 变更是 additive。
