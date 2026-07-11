# grga 开发计划增量:doc-level → 段级共现图迁移

> **本文定位**:v2-plan.md 的**段级共现增量**,不是替换。
>
> - v2-plan.md = phase 0-6 长期路线,不变
> - plan-2026-07-11.md = 2026-07-11 上午沉淀的证据驱动 backlog
> - **本文** = 段级共现图(chunk-level co-occurrence)迁移的实施规范
>
> **写作元规则**(来自 workspace AGENTS.md `## 写开发计划的责任边界`):
>
> - 只写 WHAT + 硬约束 + 验收 shell,不写 HOW
> - 8 维边界必答清单显式枚举
> - 诊断候选可给,但明确标"不预定结果",code agent 独立诊断权保留
> - "实施细节 / 强制路径" 属于 code agent 空间,规范不越权
>
> **读者**:实施本迁移的 code agent(人或 AI)。
>
> **前置阅读**:
>
> - `v2-design.md` §7(数据模型)、§4(三层扩展)
> - `graph-l2-postmortem-2026-07-10.md`(尤其 §4.3 侦探比喻、§4.5 能力诚实)
> - `plan-2026-07-11.md`(尤其 P0 log1p、能力诚实哲学)

---

## 0. 为什么做这个迁移

### 0.1 证据链摘要

**doc-level 图的结构性缺陷**(2026-07-10 postmortem §2 全部四轮实验证实):

- 单文件大语料下,每个 keyword 在 `keyword_docs` 里只对应 1-2 doc,PPR
  图退化到几乎无信号
- 22MB `《凡人修仙传》.txt` 单文件案例:全书 17759 关键字都塌缩到一个
  `doc_path`,PPR 图**没有真正的边**(每对关键字 co-doc = 1)
- V0-V5 全部四种变量组合都无法让 hub seed 的邻居出现关联实体 ——
  在 doc-level 框架内**结构性无解**

**段级(chunk-level)共现的证据**(2026-07-10 V5_npmi 实验):

- 行级窗口 + npmi ranking,8 非 hub seed × top-5 Useful% 均值 = **87.5%**
- Caller 拿到的关键字直接可用于 L1 回喂(用户哲学:L2 是 L1 的补充层,
  产出关键字,不做独立检索)
- npmi 是**数学恒等式加工**,不注入语义先验,符合 caller-first 加工边界

### 0.2 用户在 seq 363 明确的三个额外挑战

1. **D1**:多行段边界可按 heading / 空行,默认目标 300-600 字
2. **D2**:npmi 直接落进主干,不拆两步走
3. **D3**:**数据模型统一,不无限保留历史兼容层**。允许一次性迁移废弃
   `keyword_docs` 和现有 `keyword_pairs` 的 doc-level 语义,`keyword_pairs`
   扩容承载多来源
4. **df 结论重开**:昨天 α 推演否掉的是**doc-level** df 接口的价值。
   段级 df 语义完全不同(区分度可能远高),build 阶段**顺手存 df**,
   **不做接口**,留作段级数据到位后单独重开 α 推演的原料

---

## 1. 目标(WHAT)

- 把 `related --mode graph` 的底层图从"文档级共现"迁到"段级共现"
- 用户角度**无 CLI 破坏性变更**:`related --mode graph` 名字与参数不变,
  只是内部走段级
- `index build` 增加段粒度参数,让 caller 可选行/多行
- npmi + raw count 两种客观数值信号,以 metadata 形式暴露到 `related` 输出
- 一次性废弃 `keyword_docs`,`keyword_pairs` schema 扩容承载多来源
- 段级 chunk-df 在 build 时顺手存下,**不出接口**

---

## 2. 硬约束(HC · 冻结契约)

**HC1 · CLI 契约不变**:

- `related --mode graph` 三个 mode 名(`graph` / `dir` / `heading`)不变
- `related` 的必填 / 选填参数集合不变,现有 caller skill 不需要改动
- 新增字段允许(向前兼容),**删/改**现有字段不行

**HC2 · 加工边界**:

- **不允许**引入 embedding、BM25、SLM rerank、语义嵌入
- **不允许**引入语料特定停用词表 / 白名单 / 领域字典
- **允许**数学恒等式加工(npmi / raw count / 数值阈值)
- **允许**极小的通用停用词(现有 build.py `_STOPWORDS` 规模,不扩)

**HC3 · caller-first · 抗评价性字段**:

- `related` 输出可暴露**客观数值**(npmi 值、raw cooccur count、seed 的
  chunk-df),但**禁止**任何形容/评价性字段(如 `is_hub`、`confidence`、
  `quality`、`warning`、`recommend_action`)
- 详见 `AGENTS.md ## 抗 banner 的最严边界`

**HC4 · 一次性迁移,不无限兼容**:

- `keyword_docs` 表允许**完全废弃**(通过 schema 版本升级迁移)
- `keyword_pairs` schema 允许扩容(加列 / 扩 PK)承载多来源
- 现有 `keyword_pairs` 里 LLM 生成的 pair 允许打上 `source='llm_promoted'`
  标记后**保留**;doc-level 派生的 pair 允许**清空**(段级会重算)
- **不允许**同时维护 doc-level 图和段级图两套代码路径

**HC5 · 段边界规则**:

- 默认行级(1 行 = 1 段,兼容 ripgrep 语义)
- 多行段:边界优先级 **heading > 空行 > 字数硬切**,目标 300-600 字
- 硬切时**不允许**切断单行内容
- 具体边界算法归 code agent 决定,只要**验收 shell**通过

---

## 3. 数据模型(合并方案 M · 契约层规格)

以下 schema 是**接口规格**(下游 PPR / related 依赖),code agent 允许改动
索引 / 存储细节但不允许改字段名或语义。

### 3.1 新增:chunks 表

```sql
CREATE TABLE chunks (
    scope TEXT NOT NULL DEFAULT '',
    directory TEXT NOT NULL,
    doc_path TEXT NOT NULL,
    chunk_id INTEGER NOT NULL,        -- 文档内顺序号,从 0 开始
    start_line INTEGER NOT NULL,      -- 1-based, inclusive
    end_line INTEGER NOT NULL,        -- 1-based, inclusive
    char_count INTEGER NOT NULL,
    PRIMARY KEY (directory, doc_path, chunk_id)
)
```

### 3.2 新增:keyword_chunks 表(关键字 → 段倒排)

```sql
CREATE TABLE keyword_chunks (
    directory TEXT NOT NULL,
    keyword TEXT NOT NULL,            -- casefold 后
    doc_path TEXT NOT NULL,
    chunk_id INTEGER NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (directory, keyword, doc_path, chunk_id)
)
```

**索引要求**:code agent 自行决定,但 `related --mode graph` 的 PPR 加载
路径 wall-clock 不得比 v2.2 doc-level 慢超过 3×(同等语料规模,详见 §4
验收 shell)。

### 3.3 扩容:keyword_pairs 表(承载多来源)

**破坏性变更**:PK 增加 `source` 字段,原有 doc-level 派生的 pair 一次性
清空。

```sql
CREATE TABLE keyword_pairs (
    scope TEXT NOT NULL DEFAULT '',
    directory TEXT NOT NULL,
    keyword_a TEXT NOT NULL,
    keyword_b TEXT NOT NULL,
    source TEXT NOT NULL,             -- 'chunk_cooccur' | 'llm_promoted'
    cooccur_count REAL NOT NULL,      -- 段级 raw 共现次数(chunk_cooccur);
                                      -- 或 LLM promote 的投票数(llm_promoted)
    npmi REAL,                        -- 段级 npmi(chunk_cooccur 时非 NULL;
                                      -- llm_promoted 时可为 NULL)
    updated_at TEXT,
    PRIMARY KEY (directory, keyword_a, keyword_b, source)
)
```

**约束**:

- `keyword_a < keyword_b`(canonical unordered pair,与现有一致)
- `source='chunk_cooccur'` 的行:build 阶段生成,`cooccur_count` 段级次数,
  `npmi` 非 NULL
- `source='llm_promoted'` 的行:`grga index promote` 生成,`cooccur_count`
  用投票数,`npmi` 可为 NULL 也可后续 backfill

**LLM promote 路径**:code agent 需保证 `graph/promote.py` 写入时携带
`source='llm_promoted'`。原有 doc-level 派生的 pair(现在 `keyword_pairs`
里未标 source 的 legacy 行)**一次性清空**。

### 3.4 保留不动

- `keyword_counts`(自演化关键字热度,与图无关)—— **不动**
- `heading_docs`(章节结构)—— **不动**;粒度天然与 chunk 不同层
- `llm_extension_votes`(投票缓冲)—— **不动**

### 3.5 一次性废弃

- `keyword_docs`:表可以整个 `DROP`,或改成迁移期只读兼容 view
- 现有 `keyword_pairs` 中所有未标 source 的行:一次性清空(通过 schema
  版本升级触发,单次事务)

**schema 版本**:v3 → v4。ensure_schema 检测 v3 时执行迁移事务:创建新表、
清空 `keyword_pairs` doc-level 行、DROP `keyword_docs`、user_version=4。

---

## 4. `related --mode graph` 内部改动

### 4.1 外部契约(冻结)

- **输入**:签名不变(`keywords`, `mode='graph'`, `limit`, `directory`,
  `cross_scope`, `alpha`, `iters`, `db_path`)
- **输出**:`List[Related]`,每个 `Related.to_dict()` 结构**扩容**新字段:

```json
{
  "doc_path": "...",
  "score": 0.0123,
  "mode": "graph",
  "reason": "...",
  "keywords": ["kw1", "kw2"],
  "directory": "/scope/path",
  "qualified_path": "/scope/path/...",

  "graph_signals": {
    "seed_chunk_df": {"kw1": 47007, "kw2": 8818},
    "total_chunks_in_scope": 145061,
    "neighbor_edges": [
      {"a": "kw1", "b": "neighbor_x",
       "npmi": 0.732, "cooccur_count": 128, "source": "chunk_cooccur"}
    ]
  }
}
```

**`graph_signals` 字段规格**:

- `seed_chunk_df`:每个 seed 关键字的段级 df(在该 scope 内出现的 chunk
  数);**不做归一化**(不除以 N,caller 自己算比例)
- `total_chunks_in_scope`:该 scope 下 chunk 总数(caller 自己算 df/N)
- `neighbor_edges`:PPR 打分用到的 seed → neighbor 边,携带 npmi / raw
  count / source;上限由 `--graph-signals-limit N` 控制(默认 30,`--json`
  下始终发出;非 `--json` 输出下不打印)

**caller 视角承诺**:

- 拿到 `seed_chunk_df / total_chunks_in_scope` 后,caller 可以自己算
  `df_ratio` 判断 seed 类型(hub / domain / rare / normal)
- 拿到 `neighbor_edges` 携带的 `npmi` 和 `cooccur_count` 后,caller 可以
  用 npmi 排序 top-K,或按 raw count 排,或两者组合 —— grga 不代替选
- **grga 不做**基于以上任何数值的过滤 / rerank / warning

### 4.2 PPR 图边来源

- 从 `keyword_pairs` 加载(所有 source),按 `npmi` 或 `cooccur_count`
  作为边权(具体权重函数由 code agent 决定,但要在 §5 边界表下都过关)
- 不再从 `keyword_docs` 派生任何边(表被废)
- 从 `keyword_chunks` self-join 派生边**允许但不必要**(build 阶段已把
  chunk 共现聚合到 `keyword_pairs`);若 code agent 选择保留 self-join
  fallback,`keyword_chunks` 需支持

### 4.3 seed → docs 的最后一跳解析

现在通过 `keyword_docs` 做:PPR 出来 top-K keyword → 用 `keyword_docs`
反查 doc_path。段级迁移后:

- **必须**改用 `keyword_chunks` 反查,聚合到 `doc_path` 层给 caller
- 每个 `Related.keywords` 里保留触发该 doc 的关键字集合(现有语义不变)

---

## 5. `index build` 改动

### 5.1 新增参数

- `--chunk-mode {line, chars}`:段粒度模式,默认 `line`
- `--chunk-max-chars N`:多行段目标字数上限(仅 `chars` 模式生效),默认 600
- `--chunk-min-chars M`:多行段目标字数下限,默认 300

### 5.2 段边界规则(仅 `chars` 模式)

优先级:

1. **heading 边界**(仅 Markdown):遇 heading 强制切,即使当前段字数 < min
2. **空行边界**:达到 min 后遇空行即切
3. **字数硬切**:超过 max 时按行边界硬切(**不允许**切断单行)

`line` 模式:1 行 = 1 段(空行也算,即空段。空段允许存在,但 keyword 提取
自然空,不进 `keyword_chunks`)。

### 5.3 build 产出

每次 build 一个 scope,写入:

- `chunks` 表:所有 chunk 元数据
- `keyword_chunks` 表:每个 (keyword, chunk) 的 occurrence_count
- `keyword_pairs` 表(source='chunk_cooccur'):每对 (kw_a, kw_b) 在
  同一 chunk 内共现的 raw count 和 npmi
- `heading_docs` 表:**不动**(照旧从 sidecar 读)

**顺手存 df**:段级 chunk-df 直接写在 `keyword_chunks` 里(某 keyword 在
该 scope 下的 distinct chunk_id 数 = COUNT DISTINCT chunk_id GROUP BY
keyword,不新增字段)。查询时按需 `SELECT COUNT(*) ... GROUP BY keyword`。

**不出接口**:build 只保证数据在 `keyword_chunks` 里可 SQL 派生,不新增
CLI 命令暴露 df,不新增 `graph nodes` 之类的接口。

### 5.4 min_pair_count 阈值

现有 build 有 `min_term_count=2`(单文件最低出现次数)。段级共现需要一
个 `min_pair_count`(段共现的最低次数)—— 用于稀疏化 `keyword_pairs` 避
免爆表。参考实验值:`min_pair_count=3`(见 V5_npmi build.py 常量)。
code agent 允许调,但要在 §7 验收 shell 下都过关。

---

## 6. 8 维边界必答清单(edge cases)

**code agent 必须为每条写出对应 pytest 用例。方案里的边界表是测试用例
的源头,不是形式主义。**

| 维度 | 具体边界 | 期望行为 |
|---|---|---|
| **空集** | scope 内无 chunk(空目录 / 全被 ignore) | `related` 返回空 list;exit 0 |
| **空集** | seed 在图里完全不存在 | `related` 返回空 list;exit 0(不是 error) |
| **单元素** | scope 内只有 1 个 chunk(极小文件) | 该 chunk 内 keyword 都是 seed 时,`neighbor_edges` 内可有边,PPR 正常;单 keyword 时无边,返回空 |
| **单元素** | seed 只有 1 个关键字 | PPR 从单 seed 起,正常返回 |
| **唯一性** | seed 关键字跨 scope 唯一 vs 跨 scope 都存在(`--cross-scope`) | 两种情况都不能塌缩到 `[]`;后者需要 scope 前缀消歧(参考 §4.3 或用 qualified_path)。参见 v2.2 迁移里 `_related_heading` 的教训 |
| **冲突** | 同名 keyword 在两个 scope 的 df 分布不同(如"韩立"在凡人 vs 三体) | `seed_chunk_df` 按当前 scope 报告(cross-scope 时按 union scope 聚合,并在 `graph_signals` 里注明) |
| **重叠** | seed 集合 ⊆ 全书高频词集 vs seed 集合 = 全书稀有词 | 两种情况 PPR 都应稳定收敛,无 NaN/无 hang;后者不塌缩 |
| **自反** | `neighbor_edges` 里是否包含 seed 自己? | **不包含**(与现有 `personalized_pagerank_indexed` 语义一致:`seed_id_set` 排除) |
| **可结合性** | `--chunk-mode chars` + `--cross-scope` 组合 | 两参数独立,组合行为 = 各自独立行为的合成 |
| **降级路径** | `chars` 模式遇纯文本无空行 100KB 单行(极端 minified) | 硬切按 min/max 字数(不切断单行 → 该单行作为一个超大 chunk)。若单行 > max,该 chunk 允许超 max |
| **降级路径** | Markdown 有 heading 但正文一段极短(< min) | heading 优先仍切;不合并跨 heading 段 |
| **hub seed 场景** | seed = "韩立"(《凡人》主角,chunk-df 极高) | 不做任何 hub 特殊处理;邻居可能都是叙述配色词;`seed_chunk_df` 如实报告(caller 自决) |
| **domain seed 场景** | seed = "修士"(《凡人》domain 词,chunk-df 中等) | 不与 hub 区分对待;邻居可能是等级词 / 门派词(caller 想要的);npmi 自然排序 |
| **rare seed 场景** | seed = "二向箔"(《三体》,chunk-df 极低) | 正常返回 rare seed 的邻居;不因低 df 触发任何 warning / filter |
| **schema 迁移原子性** | 从 v3 库启动 grga(有旧 `keyword_docs` 数据) | 单次事务:创建新表 + 清空 doc-level pairs + DROP `keyword_docs` + user_version=4;失败回滚,不留半迁移状态 |
| **schema 迁移幂等** | 从已迁移的 v4 库重启 | ensure_schema no-op |
| **LLM promote 兼容** | v3 `keyword_pairs` 里已有 LLM promote 行(可能未标 source) | 迁移时:未标 source 的行 → 若能从 `llm_extension_votes` 反查证明来源 LLM,打 `source='llm_promoted'`;否则一律视为 doc-level 派生并清空。此边界的**具体识别策略由 code agent 决定**,但要有回归测试证明 LLM 侧数据不会被误清 |

---

## 7. 验收标准(可执行 shell)

所有以下命令必须在实施完成后**同时**通过。

### 7.1 单元 / 集成测试

```bash
cd /media/lqw/DATA/myrepo/grga
pip install -e .[dev]
pytest tests/ -x -q
# 期望:所有旧 test 保持绿(不许因迁移 skip / xfail)
# 期望:新增 tests/graph/test_chunk_migration.py 全绿,覆盖 §6 所有边界
```

### 7.2 Schema 迁移

```bash
# 准备一个 v3 数据库(有旧 keyword_docs 数据)
python -c "
from grga.graph.schema import connect, ensure_schema, read_schema_version
c = connect('/tmp/test-v3.db')
ensure_schema(c)
print('version after ensure:', read_schema_version(c))
"
# 期望输出:version after ensure: 4

# 从 v3 → v4 迁移的库,以下表存在
sqlite3 /tmp/test-v3.db "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name" \
  | grep -E "^(chunks|keyword_chunks|keyword_pairs|keyword_counts|heading_docs|llm_extension_votes)$" \
  | wc -l
# 期望输出:6

# keyword_docs 已废
sqlite3 /tmp/test-v3.db "SELECT name FROM sqlite_master WHERE name='keyword_docs'" | wc -l
# 期望输出:0

# keyword_pairs 有 source 列且已扩容
sqlite3 /tmp/test-v3.db "PRAGMA table_info(keyword_pairs)" | grep -E "\|source\|" | wc -l
# 期望输出:1

# 迁移后 doc-level 派生的 pair 已清空,仅剩 llm_promoted 行(如有)
sqlite3 /tmp/test-v3.db "SELECT DISTINCT source FROM keyword_pairs"
# 期望输出:只有 'llm_promoted' 或空(禁止出现无 source 的 legacy 行)
```

### 7.3 Build 段级共现

```bash
# 用小语料 build 一次(准备一个 100 行左右的小 markdown 语料)
grga index build --scope /tmp/mini-corpus --chunk-mode line
# 期望:exit 0

# chunks 表非空
sqlite3 <db> "SELECT COUNT(*) FROM chunks"
# 期望输出:>= 语料的行数(空行是否入表由 code agent 决定,但 non-empty)

# keyword_chunks 表非空
sqlite3 <db> "SELECT COUNT(*) FROM keyword_chunks"
# 期望输出:> 0

# keyword_pairs 里有 chunk_cooccur source 且带 npmi
sqlite3 <db> "SELECT COUNT(*) FROM keyword_pairs WHERE source='chunk_cooccur' AND npmi IS NOT NULL"
# 期望输出:> 0

# chars 模式测试
grga index build --scope /tmp/mini-corpus --chunk-mode chars --chunk-max-chars 600 --chunk-min-chars 300
# 期望:exit 0;chunks 表里 char_count 分布在合理区间
sqlite3 <db> "SELECT MIN(char_count), MAX(char_count), AVG(char_count) FROM chunks WHERE char_count > 0"
# 期望:MAX 通常 ≤ 800(允许 min-max 之间浮动 + heading/超长行例外);AVG 在 300-600 附近
```

### 7.4 related 输出契约

```bash
# 基础 smoke:CLI 契约不破
grga related --directory /tmp/mini-corpus --mode graph --json foo bar | jq -e '.results[0] | has("doc_path") and has("score") and has("mode") and has("keywords")'
# 期望:exit 0(json 结构保留旧字段)

# 新增 graph_signals 字段存在
grga related --directory /tmp/mini-corpus --mode graph --json foo bar | jq -e '.results[0].graph_signals | has("seed_chunk_df") and has("total_chunks_in_scope") and has("neighbor_edges")'
# 期望:exit 0

# neighbor_edges 每条带 npmi / cooccur_count / source
grga related --directory /tmp/mini-corpus --mode graph --json foo bar | jq -e '.results[0].graph_signals.neighbor_edges[0] | has("a") and has("b") and has("npmi") and has("cooccur_count") and has("source")'
# 期望:exit 0(允许 npmi=null 当且仅当 source='llm_promoted')

# 契约禁止字段(caller 反评价性)
grga related --directory /tmp/mini-corpus --mode graph --json foo bar | jq -e '.results[0] | (has("is_hub") or has("confidence") or has("quality") or has("warning") or has("recommend_action")) | not'
# 期望:exit 0(以上评价字段一个都不允许出现)

# --graph-signals-limit 生效
grga related --directory /tmp/mini-corpus --mode graph --graph-signals-limit 5 --json foo bar | jq -e '.results[0].graph_signals.neighbor_edges | length <= 5'
# 期望:exit 0

# 非 --json 输出不打印 graph_signals(人可读输出保持简洁)
grga related --directory /tmp/mini-corpus --mode graph foo bar 2>&1 | grep -c "graph_signals"
# 期望输出:0
```

### 7.5 性能

```bash
# 用 22MB 单文件语料 build(《凡人修仙传》测试语料位置见 experiments/)
time grga index build --scope <corpus> --chunk-mode line
# 期望:wall-clock ≤ 5 分钟

# related 查询延迟
time grga related --directory <corpus> --mode graph --json 韩立 > /dev/null
# 期望:wall-clock ≤ v2.2 doc-level 同查询 × 3
```

### 7.6 caller-first 反例(手工判断)

```bash
# 复现 V5_npmi 已证的 87.5% Useful% 场景:8 非 hub seed × top-5
# 该判断需人工判定 Useful,不做全自动断言,但要求 code agent 在 PR 描述里
# 附上 8 seed × top-5 邻居列表,由 reviewer 目视确认 ≥ 60% Useful%
grga related --directory <凡人语料> --mode graph --limit 5 --json 墨大夫
grga related --directory <凡人语料> --mode graph --limit 5 --json 极阴祖师
# ... 共 8 seed(见 experiments/cooccur_2026_07_10_v3/golden_set.json)
# 期望:PR 描述里附完整表格,reviewer 手判 Useful% ≥ 60%
```

---

## 8. 诊断候选(不预定结果)

以下是**可能的实现起点**,code agent 允许独立诊断出更好的方案,只要通过
§6 边界表 + §7 验收 shell:

- **段化实现**:参考 `experiments/cooccur_2026_07_10_v3/build.py` 的
  chunk 切分逻辑(注意:实验代码只是起点,不是强制路径)
- **npmi 公式**:`npmi(a,b) = log(p(a,b) / (p(a)*p(b))) / -log(p(a,b))`,
  其中 p 用 chunk-df 归一化(N = total_chunks_in_scope)
- **PPR 权重函数**:候选包括 (a) npmi 直用、(b) `max(npmi, 0)` 截断负值、
  (c) `cooccur_count` 直用、(d) npmi × log(cooccur_count) 组合。**code
  agent 独立选**,验收 shell 通过即可
- **稀疏化策略**:`min_pair_count` 之外还可考虑 top-K neighbor per keyword
  (类似 KNN 图)。允许探索,不强制
- **schema 迁移事务**:候选包括 (a) `ATTACH` 临时库拷贝,(b) 原地 `DROP` +
  `CREATE`。code agent 选,只要迁移原子性 pytest 通过

**明确标注:不预定诊断结果**。以上任何一项都可以被 code agent 换成更好的
实现,只要满足 §2 硬约束和 §7 验收。

---

## 9. Out of Scope(本次不做)

以下明确排除,防止 scope creep:

- ❌ **独立的 `graph nodes` / `graph df` 接口**:df 数据通过 `related` 的
  `graph_signals` 元数据暴露,不新增独立 CLI 命令。理由见
  `graph-l2-postmortem-2026-07-10.md` §4.2 α 推演否证
- ❌ **hub 特殊处理 / hub 判定字段**:任何 `is_hub` / `hub_score` /
  hub 相关 warning 都在 HC3 明令禁止。caller 拿 df 自己判
- ❌ **stopword 扩表**:不因 hub 邻居退化而扩停用词,详见
  postmortem §4.4 三条 caller-first 铁律
- ❌ **embedding / rerank / SLM**:HC2 明令禁止,不在段级迁移里偷偷加
- ❌ **`--mode dir` / `--mode heading` 内部改造**:本次只改 `--mode
  graph`,另外两个 mode 保持 v2.2 语义
- ❌ **在线增量 build**:build 仍是"重跑一整个 scope"语义,增量留给未
  来 phase
- ❌ **UI / 人可读的 graph_signals 打印**:非 --json 输出保持简洁,不
  向人展示元数据(caller 是 agent,不是人)

---

## 10. 决策留痕

**为什么 doc-level 图不救,直接迁段级**:

- 22MB 单文件语料下 doc-level 图退化为无边(每对 kw co-doc = 1)
- postmortem §2 四轮实验(V0-V5)在 doc-level 框架下**全部失败**
- V5_npmi 用行级窗口 + npmi 在**非 hub seed**上已证 87.5% Useful%
- 结论:doc-level 不是"参数还没调好",是**框架能力不够**

**为什么废 `keyword_docs` 而不是双写并存**:

- 用户 seq 363 明确:"数据模型统一,不无限保留历史兼容层"
- 双写并存 = 两套代码路径 = 双倍测试 = 长期负债
- 迁移一次痛,好过永远两套

**为什么 df 通过 `graph_signals` 暴露而不做独立 `graph nodes` 接口**:

- postmortem §4.2 α 推演已证 doc-level df 独立接口净收益仅 7.7%,domain
  词误伤 -2000 是方向性错误
- 段级 df 语义不同,可能更有区分度,但**先不重复"独立接口"陷阱**
- graph_signals 作为 `related` 的元数据 = 侦探拿到放大镜时**顺便带上刻度**,
  不需要额外掏一个新工具
- 若未来段级 df 数据出来后 α 推演证明有独立接口价值,可再重开

**为什么 npmi 直接进主干,不拆两步走**:

- 用户 seq 363 明确 D2:npmi 已证价值,不拆
- 拆两步 = 主干先落 raw count,再补 npmi = 两次 schema 变更 = 两次
  keyword_pairs 迁移。合并做更省

**为什么保留 raw `cooccur_count`**:

- 数学恒等式加工原则(HC2):caller 应该能自己派生任何数值信号。只留
  npmi 而不留 raw count 相当于**替 caller 选了 ranking key**,越权
- 存储成本(每 pair 多一个 REAL 列)微不足道

---

## 11. 附录:验收 checklist(code agent 交付前自查)

- [ ] 所有 §7 shell 本地跑通
- [ ] `tests/graph/test_chunk_migration.py` 覆盖 §6 全部 15 行边界
- [ ] PR 描述附 8 seed × top-5 手工 Useful% 表格(§7.6)
- [ ] `CHANGELOG.md` 添加 v0.3.0 段级迁移条目,明列破坏性变更
  (`keyword_docs` 废、`keyword_pairs` schema 变)
- [ ] `README.md` 若有引用 `keyword_docs` 的地方一并更新
- [ ] 无 legacy 代码残留(grep `keyword_docs` 应只出现在 CHANGELOG /
  migration 代码,不在主干逻辑)
- [ ] `graph/promote.py` LLM promote 路径写入 `source='llm_promoted'`
- [ ] 迁移事务原子性:失败回滚测试通过
