# 增量 build / rebuild / forget —— 知识资产分层与文件级增量

**日期**：2026-07-11
**状态**：设计规范（待落地）
**动机**：`grga index build` 当前是"清表重灌"的全量重建，没有文件级增量，
也没有 `rebuild` 与之区分。同时 `auto_ngram_dict`（分词词汇表）、
`keyword_pairs`（共现关系）等产物的生命周期含糊：它们到底是"每次重算的
派生物"，还是"逐步积累的知识资产"？本文把这两个问题合并回答，给出一套
**本体论 → 三层分工 → 动词契约 → schema 迁移 → 增量算法**的完整规范。
**讨论来源**：2026-07-11 作者与 AI 搭档的多轮对话，关键推理链沉淀在 §13。
**关联**：`docs/design/tokenization.md`（§5 已同步为三层模型）、
`experiments/autongram_2026_07_11/QUESTIONS.md`（NQ4 部分撤销）。

---

## 0. TL;DR

1. **知识 = 识别专名的能力，不是语料的一比一记忆。** 系统里唯一的
   *资产*是分词词汇表 `auto_ngram_dict`；专名之间的关系（`keyword_pairs`）
   是这个能力作用于当前语料后的**观察记录**，属于派生层。
2. 三张表分三层，生命周期不同：
   - **资产层** `auto_ngram_dict`：只增不减，只有 `forget` 能移除。
   - **索引层** `chunks / keyword_chunks / heading_docs`：与磁盘严格同步，
     文件删了对应行必须删。
   - **派生层** `keyword_pairs`：每次 build/rebuild 从索引层全量重算，
     `cooccur_count` + `npmi` 一起物化，查询直读。
3. 四个动词：
   - `build`（默认，**增量**）：只处理内容变化的文件，词汇表只增。
   - `rebuild`（校准）：保留词汇表，清索引/派生层重扫。
   - `forget --phrase X`：从词汇表遗忘短语 + 级联清理（衰减最简版）。
   - `forget --all`：遗忘全部资产（核弹，= 旧 `build --replace` 语义）。
4. 文件变化判定三级：`(size, mtime)` 快过 → rga 输出 `content_hash` 慢过
   → 变了才重分词。`docs` 表新增 `size / mtime / content_hash` 三列。
5. `npmi` 归一化基线随语料规模漂移，**不能当快照存**——所以它是派生层，
   每次 build 全量重算。

---

## 1. 本体论：知识是能力，不是记忆

做共现图的出发点不是"记住语料里每句话"，而是**从语料学到有哪些关键概念、
概念之间怎么联系**，供 agent 消费。据此精确定义：

> **知识 = 识别专名的能力（词汇表）+ 这种能力作用于当前语料的观察（关系）。**

这条定义有两层，生命周期截然不同：

- **能力持久**：一旦从语料里学到"面壁计划"是一个专名，这种识别能力就是
  资产，跨越文件增删都应保留。这是唯一进入*资产层*的东西。
- **观察当下**：面壁计划和罗辑在**当前**语料里共现了几次，是能力作用于
  当前语料的结果。语料变了、观察就跟着变——这不是失忆，是重看世界。

类比人类学习："认识面壁计划"（能力，持久）vs"数这本书里它出现了几次"
（观察，可重数）。

这条本体论的直接工程后果：**`cooccur_count` / `npmi` 不进资产层**。它们
是当前索引层的数学函数，每次 build 从当前 `keyword_chunks` 全量派生，
永远与当前语料严格一致。这顺便解决了 `npmi` 归一化基线漂移的老问题
（`npmi` 依赖 `n_chunks`，语料规模变了值就该变；把旧值当快照存下来是
"用过期世界模型判新事实"）。

**未来加衰减机制**时，衰减作用于资产层（识别能力可淡出），派生层跟随
资产层自动同步，不需要独立衰减逻辑。这层性质是本体论决策的红利，不是
巧合。

---

## 2. 三层分工

### 2.1 资产层（唯一资产，只增不减）

| 表 | 键 | 更新语义 |
|---|---|---|
| `auto_ngram_dict` | `(dir_id, phrase)` | build 时对内容变化文件的 token 跑 n-gram 检测，新短语 union 进来；已有短语的 `count/npmi/branch_ent` 可刷新（`INSERT OR REPLACE`）。**只有 `forget` 能删除。** |

**级联规则**（`forget` 或未来自动衰减触发）：从 `auto_ngram_dict` 删掉短语
`X` 时，同事务内清理：

```sql
-- 1. 该 scope 下 kw = X 的所有 keyword_chunks 行（幽灵倒排）
DELETE FROM keyword_chunks
WHERE kw_id = (SELECT id FROM keywords WHERE kw = ?)
  AND doc_id IN (SELECT id FROM docs WHERE dir_id = ?);

-- 2. 该 scope 下含 X 的所有 chunk_cooccur 边
DELETE FROM keyword_pairs
WHERE dir_id = ? AND source = 'chunk_cooccur'
  AND (kw_a_id = (SELECT id FROM keywords WHERE kw = ?)
    OR kw_b_id = (SELECT id FROM keywords WHERE kw = ?));

-- 3. 资产层本身
DELETE FROM auto_ngram_dict WHERE dir_id = ? AND phrase = ?;
```

**注意**：`llm_promoted` 来源的 pair **不级联**，那是 `grga index promote`
单独维护的另一类资产。`keywords` intern 表也不动（跨 scope 共享）。

### 2.2 索引层（与磁盘严格同步）

| 表 | 键 | 更新语义 |
|---|---|---|
| `chunks` | `(doc_id, chunk_id)` | 文件增/改 → 替换该 doc 全部行；文件删 → 删该 doc 全部行。 |
| `keyword_chunks` | `(kw_id, doc_id, chunk_id)` | 同上。 |
| `heading_docs` | `(doc_id, heading_path, chapter_id)` | 同上。 |

索引层的每一行**必须**对应磁盘上仍存在的文件的当前内容。任何"文件删除但
索引层保留"的策略都会让下游查询返回幽灵引用，禁止。

### 2.3 派生层（当前索引层的物化函数）

| 表 | 键 | 更新语义 |
|---|---|---|
| `keyword_pairs` (`source='chunk_cooccur'`) | `(dir_id, kw_a_id, kw_b_id, source)` | 每次 build/rebuild **全量重算**，`cooccur_count + npmi` 一起物化。查询直读。 |

**为什么必须全量重算**：`npmi(A,B) = f(cooccur(A,B), df(A), df(B), n_chunks)`，
三个输入都是全 scope 聚合量。任何 chunk 增删（哪怕不涉及 A 或 B）都会
让 `n_chunks` 变，所有 pair 的 npmi 都要重算。这是数学约束，不是工程选择。

**为什么便宜**：全量重算 = 一次 SQL GROUP BY 聚合（从 `keyword_chunks` 求
`chunk_df` 和 `pair_count`）+ 批量 npmi 算术 + 一次批量 `INSERT OR REPLACE`。
**不涉及重新分词**——分词才是真正贵的那步。对 10M `keyword_chunks` 行的
scope，这步在秒级。

---

## 3. 四动词契约

### 3.1 `grga index build` —— 增量（默认）

```
grga index build --scope PATH [--refresh-ngram append|never] [--json]
```

**语义**：扫描 scope，只处理内容变化的文件；词汇表**只增**；派生层全量重算。

**流程**：

1. **扫描 + 判定**：遍历 scope 下所有可索引文件，对比 `docs` 表中已有记录
   的 `(size, mtime, content_hash)`，把文件分成五类：
   - `unchanged`：`(size, mtime)` 与库中一致 → 跳过所有后续步骤。
   - `updated`：`(size, mtime)` 变了，且 rga 输出 `content_hash` 变了 →
     进入重分词分支。
   - `metadata_only`：`(size, mtime)` 变了但 `content_hash` 未变
     （touch / 元数据改动 / rga 输出稳定）→ 只更新 `docs.mtime`。
   - `added`：`docs` 表中无记录 → 走 `updated` 同一路径。
   - `removed`：`docs` 表中有记录，但磁盘不再存在 → 从 `docs` 及其索引层
     三张表删除所有对应行。

2. **对 `updated + added` 集合**：
   - 用**当前** `auto_ngram_dict` 分词（不迭代 n-gram 检测）。
   - 替换该 doc 的 `chunks / keyword_chunks / heading_docs`。
   - 收集所有 token 序列，用于步骤 3 的检测。

3. **词汇表增量**（当 `updated + added` 非空时）：
   - 对步骤 2 收集的 token 序列跑一次 `detect_ngram_phrases`（不迭代，
     单轮）。
   - `--refresh-ngram=append`（默认）：新短语 `INSERT OR REPLACE` 进
     `auto_ngram_dict`，已有短语的统计量刷新。**从不删除现有短语。**
   - `--refresh-ngram=never`：完全跳过检测，词汇表保持不变。适合
     "只想同步索引层，不想学新词"的场景。

4. **派生层全量重算**（当索引层有任何变动时）：
   - `keyword_pairs` where `source='chunk_cooccur'` 全部清掉。
   - 从当前 `keyword_chunks` 聚合 `chunk_df / pair_count / n_chunks`。
   - 按 `min_pair_count` 过滤后计算 npmi，批量插入 `keyword_pairs`。
   - `llm_promoted` 行不动。

5. **收尾**：更新 `docs` 表的 `(size, mtime, content_hash)`；对比 meta 表
   中的 `toolchain_fingerprint`，变化则 emit warning（**不做自动重验**，
   见 §7）。

**代价 A：增量检测样本量退化**。步骤 3 只对*变化文件*的 token 跑检测，
不是全 scope 聚合。小文件更新时可能漏检某些新短语（样本量不足过不了
`min_count` / `branch_entropy` 门槛）。**但因为词汇表只增不减，漏检 =
"这次没加"，不会破坏已有的好词**；下次 `rebuild --refresh-ngram=append`
会用全 scope 聚合重跑一遍，兜底补上。词汇表质量单调不降，这是设计的
数学基础。

**代价 B：词汇表与未变文件的分词漂移**。词汇表加入新短语后，*变化文件*
用新词典分词，*未变文件*的 `keyword_chunks` 仍是旧词典时期的产物。若新
短语恰好也在旧文件里出现，图边分布会有轻微不一致。危害局部、单调；
`rebuild` 兜底。

### 3.2 `grga index rebuild` —— 校准

```
grga index rebuild --scope PATH [--refresh-ngram never|append|replace] [--json]
```

**语义**：**保留词汇表**（唯一资产），清索引层 + 派生层，用当前词汇表
重扫整个 scope。这是"用现有识别能力重新看一遍世界"。

**流程**：

1. `--refresh-ngram=replace` 时先清空 `auto_ngram_dict`（等价 `forget --all`
   随后 `build`）。其他情况保留。
2. 清空该 scope 下的 `chunks / keyword_chunks / heading_docs` 和
   `keyword_pairs where source='chunk_cooccur'`。`llm_promoted` 不动。
3. 遍历磁盘上 scope 下所有可索引文件（不看 `docs.mtime`，全部重扫）。
4. `--refresh-ngram={append|replace}`：跑完整的多轮 n-gram 检测
   （`ngram_iters` 默认 2，与全量重扫语义一致）；`--refresh-ngram=never`：
   跳过检测，纯用当前词汇表分词。
5. 派生层全量重算（同 `build` 步骤 4）。
6. 更新 `docs` 表的 `(size, mtime, content_hash)` 和
   `toolchain_fingerprint`。

**默认 `--refresh-ngram=append`**：rebuild 时若正好带进新文件却不学词，
会造成"这次 rebuild 后紧接一次 build 才补上词"的别扭时序，`append` 消除
这个缝隙。它对旧短语只刷新统计不删除，语义仍是"校准"而非"重学"。

### 3.3 `grga index forget --phrase X [--phrase Y ...]` —— 手动衰减

```
grga index forget --scope PATH --phrase X [--phrase Y ...] [--json]
```

**语义**：从词汇表遗忘一个或多个短语 + 级联清理索引/派生层（见 §2.1
级联规则）。这是**衰减机制的最简版本**：手动、显式、逐个/批量删除。

**流程**（单事务 `BEGIN IMMEDIATE`）：

1. 对每个 `X`，查 `keywords` 表得到 `kw_id`（不存在则跳过并 warn）。
2. 执行 §2.1 的三条 DELETE。
3. 从当前索引层重算 `keyword_pairs`（这一步是必要的：清掉的短语可能不是
   pair 边的端点，但清 `keyword_chunks` 行会改变 `chunk_df / n_chunks`，
   触发 npmi 全量刷新）。
4. 从 `auto_ngram_dict` 删除对应短语行。

**未来自动衰减**（不在本次实现）：复用同一路径，仅触发条件变（例如
"最近 90 天未在 related 查询中命中"、"scope 内 df 低于阈值"）。作用点、
级联规则、事务性都不变。

### 3.4 `grga index forget --all` —— 核弹

```
grga index forget --scope PATH --all [--json]
```

**语义**：遗忘该 scope 的**所有**资产 = 清空 `auto_ngram_dict` + 索引层
三表 + `keyword_pairs.chunk_cooccur`。等价旧 `build --replace` 语义。
**不清 `llm_promoted`**（那是独立资产）。

紧接着的 `build` 会从零开始学词、建索引，成本 = 首次全量 build。

### 3.5 `--append` 与 `--replace` 的迁移

旧 `build --append` 语义（保留字典，累加索引）→ 由新 `build`（默认增量）
天然覆盖，flag 弃用（保留过渡期别名 + deprecation warning）。

旧 `build --replace` 语义（清一切重来）→ 分解为 `forget --all` + `build`
两步；可提供过渡期 `build --replace` 别名映射到该组合，附 deprecation
warning。

---

## 4. `docs` 表 schema 迁移（v6 → v7）

### 4.1 新增列

```sql
ALTER TABLE docs ADD COLUMN size INTEGER;
ALTER TABLE docs ADD COLUMN mtime REAL;
ALTER TABLE docs ADD COLUMN content_hash TEXT;
```

- `size` (INTEGER)：原文件 `st_size`。快路径判定第一道，最便宜。
- `mtime` (REAL)：**原文件** `st_mtime`，`os.stat` 直接读，不触发 rga。
- `content_hash` (TEXT)：**rga 输出文本**的 hash（BLAKE2b-128 或 SHA1，
  用 `hashlib` 内置足够），慢路径验证用。

**为什么 mtime 以原文件为准、hash 以 rga 输出为准**：索引层真正消费的是
`read_file_lines_ex()` 返回的 rga 输出（`build.py:823`），不是原文件字节。
"内容是否变化"的判定基准必须是 rga 输出。这样 PDF 元数据变了但抽取文本
没变的场景能正确跳过。反之，若把 hash 也算在原文件字节上，PDF 改元数据
会误伤成"内容变了"，白重建。

**迁移**：老库这三列初始为 NULL。首次 build 时所有 doc 都走"无记录 →
全量处理"分支，同时把三列填上。之后转入增量模式。`SCHEMA_VERSION` 6 → 7，
走既有的 `ensure_schema` 一次性迁移路径（幂等）。

### 4.2 全局工具链指纹（可选、单行）

在 meta 表或 `directories` 表加一列 `toolchain_fingerprint TEXT`：

```
toolchain_fingerprint = sha1(
  rga --version + pandoc --version + pdftotext -v + rga-preproc --version
)
```

**仅作提醒**：build 结束时对比库中记录，变化则 emit warning：

```
warning: toolchain fingerprint changed since last build; if rga-processed
files show stale extractions, run `grga index rebuild`.
```

`--json` 下作为 `stats.warnings[]` 字段暴露。**不修改判定流程**，用户
自行决定是否 rebuild。理由见 §7。

---

## 5. 三级判定算法

```python
def classify_doc(path, doc_row):
    """Return one of {unchanged, metadata_only, updated, added}."""
    if doc_row is None:
        return "added"
    st = path.stat()
    # Level 1: cheapest. size + mtime match -> skip everything.
    if (doc_row["size"] == st.st_size
            and doc_row["mtime"] == st.st_mtime):
        return "unchanged"
    # Level 2: metadata changed -> read via rga, hash the OUTPUT.
    read_result = read_file_lines_ex(path)  # invokes rga if needed
    if read_result.source == ReadSource.FAILED:
        return "read_error"
    new_hash = blake2b_16("\n".join(read_result.lines))
    if new_hash == doc_row["content_hash"]:
        # Level 2 hit: touch / metadata change / stable rga output.
        return "metadata_only"
    # Level 3: real content change.
    return ("updated", read_result, new_hash)
```

**性能特征**：

- 大多数未变文件命中 Level 1，一次 `os.stat`，纳秒级，不调 rga。
- 仅少数元数据变过的文件命中 Level 2，需要跑 rga（rga 类文件贵，纯文本
  便宜）。**注意 hash 无法省掉 rga 调用**——要算 rga 输出的 hash 就得先
  跑 rga；它省的是"重分词 + 重建索引"这步（分词才是真正贵的）。
- 真正内容变化的文件才进入 Level 3，重分词 + 索引层重建。

**删除文件**：单独一步。遍历 `docs` 表中该 scope 的所有 `doc_path`，与
本次扫描的实际路径集合对比，差集即 `removed`。

---

## 6. `keyword_pairs` 全量重算 SQL

概念性伪代码（实际实现在 Python 端组织事务，`INSERT OR REPLACE` 批量执行）：

```sql
-- 1. 清 chunk_cooccur 行（llm_promoted 保留）
DELETE FROM keyword_pairs
WHERE dir_id = ? AND source = 'chunk_cooccur';

-- 2. 聚合 chunk_df（该 scope 下每个 kw 出现在多少 chunk）
SELECT kc.kw_id, COUNT(*) AS df
FROM keyword_chunks kc
JOIN docs d ON d.id = kc.doc_id
WHERE d.dir_id = ?
GROUP BY kc.kw_id;

-- 3. 聚合 n_chunks
SELECT COUNT(*) FROM chunks c
JOIN docs d ON d.id = c.doc_id
WHERE d.dir_id = ?;

-- 4. 聚合 pair_count（同一 chunk 内两两 keyword 共现）
SELECT a.kw_id AS kw_a_id, b.kw_id AS kw_b_id, COUNT(*) AS cooccur
FROM keyword_chunks a
JOIN keyword_chunks b
  ON a.doc_id = b.doc_id
 AND a.chunk_id = b.chunk_id
 AND a.kw_id < b.kw_id
JOIN docs d ON d.id = a.doc_id
WHERE d.dir_id = ?
GROUP BY a.kw_id, b.kw_id
HAVING COUNT(*) >= ?;  -- min_pair_count

-- 5. Python 端算 npmi、批量插回 keyword_pairs
```

第 4 步的自连接对大表可能吃内存/时间；实现时可加临时索引或分批处理，
具体调优留给落地时的 profiling。注意 `keyword_chunks` 已按
`(kw_id, doc_id, chunk_id)` 建主键，`chunk_df` 直接 `COUNT(*)` 即可（每行
就是一个 distinct chunk 里的一个 kw）。

---

## 7. 工具链变化不做自动重验

**决策**：`toolchain_fingerprint` 只作提醒，不作判定输入。

**理由（成本/收益杠杆）**：

- **强制重验的成本**（确定发生）：工具链升级 → 全部 rga 类 doc 重跑 rga
  + 重算 hash + 可能触发全量重分词。含大量 PDF/Office 的 scope 是分钟到
  小时级。
- **强制重验的收益**（概率性）：仅当 rga 输出真的变了且用户继续查旧索引
  时才有价值。小版本升级输出往往稳定；大版本升级用户通常知情。
- **杠杆方向**：为一个"用户自己知道且低频"的场景让所有升级路径吃确定
  成本，杠杆反了（对应 AGENTS.md §信号暴露"不要替 caller 省事"）。

用户升级完工具链后，看到 warning 自行决定是否 `rebuild`，是**更精准的
兜底**，也避免了"原文件没变 + 工具链升级 + rga 输出其实没变"却触发全量
重建这一巨大且概率高的误伤。

---

## 8. 可观测量（`--json` 输出）

`BuildStats` 扩展新字段，遵循信号暴露原则（AGENTS.md §设计哲学）：**内部
状态、消费者拿不到**才暴露；非 JSON 输出保持人类可读。

### 8.1 增量分类计数（`build` 独有）

```json
{
  "docs_added": 3,
  "docs_updated": 12,
  "docs_metadata_only": 1,
  "docs_removed": 2,
  "docs_unchanged": 4837
}
```

### 8.2 词汇表健康度

```json
{
  "ngram_dict_size": 2618,
  "ngram_dict_added": 4,
  "ngram_dict_removed": 0
}
```

- `ngram_dict_size`：当前词汇表总条数，观察膨胀趋势。
- `ngram_dict_added`：本次新增短语数（rebuild=append 时为本轮检测新增）。
- `ngram_dict_removed`：本次 `forget` 移除的短语数（build 恒为 0）。

### 8.3 提醒

```json
{
  "warnings": [
    "toolchain fingerprint changed since last build; ..."
  ]
}
```

**不暴露**能从上述字段派生的量（如"变化比例"= updated/unchanged，由 caller
自算）。

---

## 9. 落地任务分解

按依赖顺序：

### Phase 1 — schema v7 迁移（`graph/schema.py`）

1. `SCHEMA_VERSION` 6 → 7。
2. `docs` 表加 `size / mtime / content_hash` 三列（`ALTER TABLE`，幂等）。
3. meta/`directories` 加 `toolchain_fingerprint`。
4. 迁移测试：v6 库升级后三列为 NULL，且 `ensure_schema` 幂等。

### Phase 2 — 增量核心（`graph/build.py`）

1. `classify_doc` 三级判定 + `blake2b_16` helper。
2. Pass 0 改为按分类只读取 `updated + added` 文件；`removed` 走删除分支。
3. Pass 1（n-gram 检测）：`build` 单轮、局部 token；`rebuild` 多轮、全 scope。
4. Pass 2（索引层）：per-doc 替换（已有 `ON CONFLICT` upsert，补 `removed`
   的 DELETE）。
5. `keyword_pairs` 抽出独立函数 `recompute_pairs(conn, dir_id, min_pair_count)`，
   build/rebuild/forget 共用。
6. `forget(conn, dir_id, phrases=None, all=False)` 级联函数。

### Phase 3 — CLI（`cli.py`）

1. `build` 默认增量，加 `--refresh-ngram`（默认 append）。
2. 新增 `rebuild` 子命令（`--refresh-ngram` 默认 append，可 never/replace）。
3. 新增 `forget` 子命令（`--phrase`（多值）/ `--all` 互斥）。
4. `--append` / `--replace` 过渡期别名 + deprecation warning。

### Phase 4 — BuildStats + 测试 + 文档

1. `BuildStats` 加 §8 字段。
2. 单元：三级判定四类分支、级联删除、pair 重算幂等。
3. 集成：改一个文件 → 只该 doc 重建 + 全量 pair 一致；删文件 → 幽灵行清除；
   `forget --phrase` → 级联干净。
4. `docs/exit-codes.md` / CHANGELOG 若涉及退出码或契约变化同步更新。

---

## 10. 契约冻结点

一旦发布，以下是外部契约（加字段 MINOR，删/改 MAJOR）：

- `build --json` 的 §8 字段名与语义。
- `rebuild` / `forget` 子命令签名与 `--refresh-ngram` 取值集合。
- `keyword_pairs` 的"每次全量重算、cooccur+npmi 同步物化"性质（下游
  `related` / `suggest` 依赖它与索引层一致）。
- `auto_ngram_dict` 的"只增不减、仅 forget 可删"生命周期。

---

## 11. 何时重开此决策

仅在以下之一出现时重开：

- 有跨仓库证据表明 caller 反复需要 pair 的**独立生命周期**（关系强度随
  时间/使用衰减，而非跟随索引层）——届时给 `keyword_pairs` 加独立字段，
  把它从纯派生层部分升格。
- 实验证明 per-doc union 检测捕获的"局部专名"语义有明确 caller 价值，
  且能找到通用门槛（需重跑 `autongram_2026_07_11` 评测）。见 §13 决策链。
- `keyword_pairs` 全量重算成为实测瓶颈——届时做"n_chunks 变化率 < 阈值
  时只重算受影响 pair"的近似增量（受控漂移）。

**不要**为"体系完整""关系也是知识所以要独立存"这类完备感重开。C4 立场
（词汇表是唯一资产）已足够，且心智模型最简。

---

## 12. 未来接口占位（不实现）

- **自动衰减机制**：作用于资产层，复用 §3.3 的 `forget` 级联路径。触发
  条件（时间 / 使用频次 / df 阈值）待产品语义明确后定义。
- **关系强度独立衰减**：若 §11 第一条触发，`keyword_pairs` 加
  `last_hit_at` / `hit_count` 等字段，做数值衰减。届时它部分脱离派生层，
  需重新评估 C4 立场。

这两项按 AGENTS.md"未来词汇表"约定放这里作占位，**实现里不允许出现**。

---

## 13. 决策推理链（重要）

本节沉淀讨论过程中被推翻或修正的推理，防止未来重开讨论时重复弯路。

### 13.1 曾拒绝 per-doc 检测的理由三条，两条不成立

初次拒绝 per-doc 检测（相较全 scope 聚合）时给了三条理由：

1. **~~`min_count=3` 在单文件里过严~~** —— **不成立**。这是绝对计数门槛
   本身的问题，全 scope 聚合下同样没有通用值（`min_count=3` 对 5KB scope
   和 500MB scope 意义天差地别）。不能作为 per-doc 的专有缺陷。
2. **~~单文件样本量导致分支熵坍缩~~** —— **前提错误**。示例把"三体拆到
   单章"当反例，但三体在系统里是**单文件**（`doc_id` = 文件），per-doc
   检测在此等价于全 scope 检测。真正差异只在"多文件 scope"出现。
3. **NPMI 在单文件里方差大** —— **成立但收窄**。仅在"大量小文件"目录里
   为真；"少量大文件"（三体式）目录 per-doc ≈ per-scope，无损。

**最终立场**：per-doc 检测在"大量小文件"目录会掉统计质量，但这不是
"应该保持全 scope 聚合"的充分理由——目录形态无法预判，任何**固定**的
检测单位都会在某种形态上吃亏（无解）。

### 13.2 无解通过"解耦"绕过

关键洞察：**检测单位和存储单位可以解耦**。

- 检测单位始终为全 scope 聚合（保证样本量与统计质量）。
- 存储单位可以是 per-doc（保证增量能力）。

理论上完美方案（曾称"方案 C"）：`counter` 按 doc 分桶存储，检测时
$\text{dict} = \text{detect}(\sum_d \text{stats}_d)$，数学等价于现状且天然
支持增量。

**代价**：新增 `doc_ngram_stats` 表，10k 文档 × 平均 500 unique n-grams
≈ 5M 行，存储放大 5-10 倍。

### 13.3 用户提出更轻方案（C4 立场，本设计采用）

用户重定位问题：**auto n-gram 的目的是得到一个高质量分词器（jieba
用户字典），字典本身是核心资产；共现关系是这个能力作用于当前语料的
观察记录，不必存为资产**。

据此：

- 字典**只增不减**（用户手动 `forget` 兜底噪声）。
- 共现 pair 每次从索引层重算，无独立生命周期。
- 增量检测退回 per-doc 局部（接受代价 A / 代价 B），因为**词汇表只增
  的性质吸收了漏检**——最多"这次没加"，不会破坏已有好词，`rebuild` 兜底。

**存储代价**：几乎为零（`docs` 表加三列而已，不需要 counter 桶）。
**心智模型**：只有一个资产（词汇表），其他都是当前状态的派生。

### 13.4 `npmi` 归一化基线漂移

讨论中曾考虑给 `npmi` 加陈旧标记缓存或衰减保留旧值，都不成立。C4 下的
正确做法是：`keyword_pairs` 整表是"当前 build/rebuild 完成时刻的物化
派生结果"，`cooccur_count` 与 `npmi` 同步覆盖，查询直读——数学一致、
消费者零现算成本、无陈旧字段问题。这与现有代码路径（`build.py:1052-1066`）
一致，无需改动，只需把这条**语义**写入契约。

### 13.5 工具链指纹从"强制重验"退化为"仅提醒"

初始方案给 rga 类 doc 加"工具链变化时强制重验"路径。用户指出：**追求
完备性收益不高，反而带来"原文件没变、工具链升级、rga 输出实际未变"
却触发全量重建的巨大误伤**。

**修正**：`toolchain_fingerprint` 只作提醒，不改判定流程。用户自己知道
何时升级过工具链，手动 rebuild 是更精准的兜底。

### 13.6 rebuild 的三种立场（选定 C4）

- **A（严格资产主义）**：rebuild 只重扫索引层，知识层永不清除。缺点：
  衰减机制未做时噪声无兜底。
- **B（rebuild 即校准）**：rebuild 从零重刷所有产物。缺点：违背"知识是
  积累的资产"哲学。
- **C（分级动词）**：三动词分工，`forget` 是显式逃生阀。**采用**。
  - C1（保留旧字典 + 加当前语料）：老文件被算两次，错。
  - C2（per-doc 存历史贡献，减去再加）：回到 counter 桶，被否决。
  - C3（保留旧字典 + 只重算当前语料，历史遗产按值保留）：简单，但混入
    幽灵证据。
  - **C4（只保留词汇表，其他都重做）**：**采用**。心智模型最简，语义
    最清晰，`cooccur` 从零重算避免所有幽灵证据问题。

### 13.7 NQ4 的部分撤销

`experiments/autongram_2026_07_11/QUESTIONS.md` NQ4 结论"保持全 scope
聚合"在**全量重扫**语义下依然正确，但**增量 build 引入了新语义**：
`build` 时 detection 只对变化文件的 token 跑（局部聚合）。这不是推翻
NQ4，而是限定它的适用范围——**`rebuild --refresh-ngram=append`（或
`replace`）仍是全 scope 聚合**，增量 build 是新引入的次级路径。QUESTIONS.md
已同步追加更新说明。

---

## 14. 变更清单（本文档生效带来的具体改动）

- 新增：本文（`docs/design/plan-2026-07-11-build-vs-rebuild.md`）。
- 修改：`docs/design/tokenization.md` §5 → 三层模型（资产/索引/派生）。
- 追加：`docs/design/decisions.md` 2026-07-11 决策条目。
- 追加：`experiments/autongram_2026_07_11/QUESTIONS.md` NQ4 更新说明。
- 更新：`docs/design/README.md` 索引表加入本文。
- 代码层：`src/grga/graph/schema.py`、`src/grga/graph/build.py`、
  `src/grga/cli.py`（按 §9 分阶段落地）。



