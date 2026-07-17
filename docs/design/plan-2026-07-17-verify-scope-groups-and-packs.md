# verify + scope-groups + baseline packs 设计规范

**日期**：2026-07-17
**状态**：设计定稿，待 code agent 认领 P1 / P2 / P3。
**关联**：
- `docs/design/plan-2026-07-14-scope-directory-split.md`（scope / directory 分野；
  本方案沿用"知识 vs 证据"分层，并把 scope_groups / pack 归入知识层）
- `docs/design/plan-2026-07-11-build-vs-rebuild.md`（`--seed-ngram-from` 冷启动
  机制，本方案在此基础上增加 `--seed-ngram-from-pack`）
- `docs/build-tuning.md` §2.7（长期存储治理靠拆 scope + 归档 grgapkg，本方案是
  该方向的能力配套）

**动机**：v0.3.0 后 grga 的搜索、性能、skill 指导性都到可用程度。剩余痛点集中在
三个方向：

1. **DB 里到底有什么"看不见"**——用户主观判断"存了太多无效短语和共现对"，但
   grga 只提供 `graph inspect`（单 seed 视图），缺一份**全库分布 + 短语治理面板**。
2. **多 scope 场景打字负担**——真实场景一个用户往往有多个相关 scope（工作年份、
   项目类别），每次 `--scope A --scope B --scope C` 是重复劳动。
3. **未来更大语料 / 新机器冷启动**——目前 `--seed-ngram-from` 只能借用**本机**
   已有 scope 的词典；跨机器复用需要一个可分发的知识包生态。

**兼容立场**：三项功能全部 **additive**——现有 `grga index export/import` 契约
0 破坏，所有新能力走新命令、新表、新参数。用户已确认可以承受本机数据结构的
schema 增量（新增表）。

---

## 0. TL;DR

三项功能按物理依赖顺序（P2 是 P3 的可选内容项，P1 独立）：

| 项 | 交付形态 | 核心承诺 | 依赖 |
|---|---|---|---|
| **P1 · verify** | `grga index verify [phrases|pairs]` | 只读全库审计 + 短语治理面板；只暴露事实，不评价 | 无 |
| **P2 · scope groups** | `grga scope group ...` + `--scope-group X` | scope 的一等公民别名集合；本身进知识包 | 无 |
| **P3 · baseline packs** | `grga pack build/install/list/inspect/uninstall` + `--seed-ngram-from-pack` | 内容菜单化的知识包分发（默认只带词典，可选带 pair/votes/groups） | P2 完成后才能"带 groups" |

**共同哲学**：三项都是**能力新增**，不做 caller / 用户可以自己做的判断。

- verify **不给**"这条短语该被删"的建议——只列客观分桶，删动作走已有的 `grga
  index forget --phrase`
- scope groups **不承载**除成员集合外的语义（不带描述、不带默认参数、不带 tag
  绑定）——薄集合
- pack **不预设**"什么内容组合是正确的"——contents 清单让 sender 完全掌控，
  receiver 装机时看得见 sender 做了什么裁剪

**caller-first 铁律回顾**（三项都受此约束）：

1. **视角换位**：verify / pack inspect 的输出格式默认人类可读（markdown table），
   `--json` 才结构化——跟已有动词一致
2. **加工边界**：verify 不做 rerank / 权重合成；pack 不做"质量分"标注
3. **暴露判据**：verify 的每一个桶都必须附带**可复验的数字判据**（例：`极低使用
   桶 (chunk_df ≤ 2 且 pair 数 ≤ 1)`），禁止用形容词命名桶

---

## 1. 设计原则

### 1.1 知识 vs 证据分野的延伸（沿用 plan-2026-07-14）

判据不变："这条信息在另一台机器上、对另一批文件还成立吗？成立 = 知识（可进 PK
/ 知识包）；不成立 = 证据（旁路表 / 迁移时清空）。"

本方案新增三张表，分层如下：

| 表 | 分层 | 理由 |
|---|---|---|
| `scope_groups`（id, name, created_at） | **知识** | group 定义**跟 scope 名一起可移植**——用户在 A 机器建的 `work` group（含 `工作-2024, 工作-2025`）迁到 B 机器，只要 B 上有对应 scope，group 就该复原 |
| `scope_group_members`（group_id, scope_id） | **知识** | 与 group 定义强绑，是 group 语义的一部分 |
| `installed_packs`（pack_name, version, installed_at, manifest_json, content_hash） | **证据** | "这台机器装过哪些包"是本机状态，不跨机器 |

**pack manifest 表**（`pack_manifest`）位于 pack 文件内部，不在主 DB。见 §4.4。

### 1.2 Additive-only 硬约束

- 现有 `grga index export` / `grga index import` **行为不变**——是完整备份/整体
  导入 scope 的动词，continue to work as-is
- 现有 `grga index build --seed-ngram-from Y` **行为不变**——本机 scope 间借词典
  的通道保留
- 新加的 `grga pack build ...` 与 `--seed-ngram-from-pack P` 是**并行姿势**，
  不覆盖不替换
- 主 DB schema 版本推进到 **v12**（scope_groups 相关表 + installed_packs 表加入）
- 新 CLI 顶层命令：**`grga pack`**（子命令族）；新 `grga scope` 子命令：`group`
  子子命令族

### 1.3 契约冻结的边界

**外部接口（不可自由改）**：
- 所有 `--json` 输出的字段名 / 结构（发布即冻结）
- CLI 命令名 / 参数名 / 参数语义
- pack manifest JSON schema（跨版本兼容底线）
- 退出码语义（沿用 `exit_codes.py`）

**内部实现（code agent 自由）**：
- 具体表列名、索引选择、SQL 语句形式
- Python 模块划分、函数命名
- 桶阈值判断的算法（只要输出满足契约）
- pack 文件内部 SQLite 页组织

---

## 2. P1 · `grga index verify`

### 2.1 目标

给用户一份**全库分布 + 短语治理候选**的只读报告，回应"DB 里存了什么"的透明度
诉求。**不删任何东西，不建议删任何东西**——删除动作走已有的 `grga index forget`。

### 2.2 硬约束

1. **全程只读**：整个 verify 执行周期持有 SQLite `PRAGMA query_only=1`，任何写
   路径视为 bug
2. **不评价**：字段命名严格中立
   - ✅ 允许：`low_usage_bucket`（附带客观阈值 `chunk_df ≤ 2 且 pair 数 ≤ 1`）
   - ❌ 禁止：`noisy_phrases`、`should_be_removed`、`quality_score`、`confidence`
3. **每个分桶必须附带数值判据**：human-readable 输出与 `--json` 输出都要在字段
   或列头里明写判据（`(chunk_df ≤ 2)`），caller 读一眼即知是什么标准
4. **默认输出 human-readable**，`--json` 才给结构化。与现有动词（related /
   search）保持默认格式一致性
5. **不 crash**：DB 缺表 / schema 版本不匹配 / 空 scope，明确指出缺什么、按已有
   部分报，退出码见 §2.6

### 2.3 CLI 契约（外部接口，冻结）

三个子命令：

```
grga index verify [--scope S ...] [--tag T ...] [--json]
grga index verify phrases [--scope S ...] [--top-k N] [--json]
grga index verify pairs [--scope S ...] [--top-k N] [--json]
```

**`grga index verify`**（全库整体审计）返回四段内容：

1. **Structural integrity**
   - SQLite `PRAGMA integrity_check` 结果
   - 孤儿行检测：`keyword_docs → docs`、`keyword_stats → keywords`、
     `scope_group_members → scopes`、`scope_group_members → scope_groups`——每一
     类给出计数（应恒为 0）
   - `scope_stats` 元数据 vs 实际重计数偏差（`n_chunks`、`n_docs`、`vocab_size`）
   - `auto_ngram_dict.count` 与 detection round 分布的单调性检查

2. **Storage breakdown**（per scope + total 两张表）
   - 每张主表的 row count / disk size / avg row bytes
   - 每个索引的 disk size
   - "pair 三件套"合计占比（对齐 `build-tuning.md §2.1`）

3. **Value distribution**（per scope）
   - `keyword_pairs` 的 npmi 直方图（默认桶 `[0.2, 0.3), [0.3, 0.4), ... [0.9, 1.0]`）
   - `keyword_pairs` 的 cooccur_count 直方图（对数桶）
   - `auto_ngram_dict` 的 ngram_n 分布
   - `keyword_stats` 的 chunk_df 直方图（对数桶）

4. **Summary**：一行 "N scopes / M pairs / K phrases / D GB"

**`grga index verify phrases`**（短语治理面板）—— 回应"太多无效短语"痛点。列出
若干候选桶，每桶输出前 `--top-k` 条（默认 20），用户读了拿去 `grga index forget
--phrase X`：

- **极低使用桶**：`chunk_df ≤ 2 且 参与 pair 数 ≤ 1`
- **极高使用桶**：`chunk_df ≥ scope_total × 90%`（疑似停用词漏网）
- **独立性可疑桶**：所有 pair 都命中 Gate 1 边缘（`df_a == df_b == cooccur`，
  即使已被 build 过滤，若发现残留说明索引不一致）
- **单迭代 + 低使用桶**：`detection_round == 1 且 chunk_df < 中位数 × 0.5`

**`grga index verify pairs`**（pair 分布 + 边缘识别）：

- **绑死 pair 桶**：`高 npmi (≥0.9) 且 低 cooccur (≤3)`——疑似 tokenizer 副作用
- **泛词共现桶**：`高 cooccur (top-1%) 且 低 npmi (<0.3)`
- **hub 关键字**：连接 pair 数最多的 top-K keyword

### 2.4 输出格式契约

**human-readable**（默认）：markdown table，每个桶前带一行"桶名 (判据)"标注。
建议但不强制使用 `rich` 类库渲染——纯文本输出也接受。

**`--json`**：envelope 长这样（字段名冻结，加字段是 MINOR，删/改是 MAJOR）：

```json
{
  "verify_type": "overall" | "phrases" | "pairs",
  "scopes": ["工作", "小说"],
  "generated_at": "2026-07-17T...",
  "integrity": {
    "sqlite_integrity_check": "ok",
    "orphan_rows": {
      "keyword_docs_orphan": 0,
      "keyword_stats_orphan": 0,
      "scope_group_members_orphan_group": 0,
      "scope_group_members_orphan_scope": 0
    },
    "scope_stats_drift": [
      {"scope": "工作", "metric": "n_chunks", "recorded": 2880000, "actual": 2880003, "delta": 3}
    ]
  },
  "storage": {
    "per_scope": [{"scope": "工作", "tables": {...}, "indexes": {...}}],
    "total": {...}
  },
  "distribution": {
    "per_scope": [{
      "scope": "工作",
      "pair_npmi_histogram": [
        {"bucket": "[0.2, 0.3)", "count": 12345, "criterion": "npmi >= 0.2 AND npmi < 0.3"}
      ],
      ...
    }]
  },
  "buckets": [  // phrases / pairs 子命令输出
    {
      "bucket_name": "low_usage_bucket",
      "criterion": "chunk_df <= 2 AND pair_count <= 1",
      "total_count": 12345,
      "top_k": [{"phrase": "...", "chunk_df": 1, "pair_count": 0}]
    }
  ]
}
```

**"未来词汇表"**（保留 key，实现里不允许出现）：`quality_score`、`recommendation`、
`_confidence`、`suggested_action`——这些名字是评价性的，未来若需暴露只允许作为
更细粒度的**客观事实**扩展（例：`chunk_df` 已经是这类事实），不允许加评价字段。

### 2.5 8 维边界（每一条 code agent 必须写对应测试）

| 维度 | 期望行为 |
|---|---|
| **空集** | 空 scope / 空 DB：返回结构合法的空报告，各表 count=0，不 crash；exit 0 |
| **单元素** | 只有一个 scope：per_scope 数组长度 1，total 与 per_scope[0] 数值一致 |
| **唯一性** | keyword 只在一个 scope 存在 vs 多个 scope 存在：storage / distribution 独立统计，不合并 |
| **冲突** | 元数据偏差（`scope_stats_drift` 非空）：仍完整报告 + exit 2（不自动修复） |
| **重叠** | phrase 同时命中多个候选桶：全部列出，桶间**不去重** |
| **自反** | verify 前后 `sha256(main.db)` 恒等；`PRAGMA query_only=1` 全程有效 |
| **可结合性** | `--scope A --tag T` 组合：与其他动词 `--scope/--tag` 语义一致（交集） |
| **降级路径** | DB 缺 `scope_groups` 表（旧版本 DB）：跳过 group 相关 orphan 检查 + stderr warning + 其他部分正常报；exit 0（缺表非错） |

### 2.6 退出码（沿用 `exit_codes.py` 契约）

- `0` — verify ok，integrity ok，无 drift
- `2` — integrity 失败 / orphan 非零 / scope_stats_drift 非零；**报告仍完整输出**
  （不要让 exit != 0 就 abort，用户需要看到具体哪里不对）
- 其他退出码沿用现有约定（参数错误 / IO 错误等）

### 2.7 验收命令（可执行 shell，code agent 交付时必过）

```bash
# 1. 空 DB 不 crash
grga index verify --db-path /tmp/empty.db --json | jq '.integrity.sqlite_integrity_check' | grep -q ok

# 2. verify 后 DB hash 不变（只读性）
sha256sum <db> > /tmp/before
grga index verify --db-path <db> --json > /dev/null
sha256sum <db> > /tmp/after
diff /tmp/before /tmp/after

# 3. 桶输出必须携带 criterion 字段
grga index verify phrases --json | jq -e '.buckets[] | .criterion' | grep -qE '(chunk_df|pair_count|npmi)'

# 4. 禁词字段不允许出现
grga index verify --json | jq -r '..|objects|keys[]?' | \n  grep -Ev '(quality_score|recommendation|_confidence|suggested_action)$' | wc -l  # 应等于原 key 数

# 5. 一致性偏差非零时退出码 2
# （构造一个 scope_stats 偏差的 fixture 后）
grga index verify --db-path <fixture.db>; test $? -eq 2

# 6. --scope 过滤缩小报告范围
grga index verify --scope 工作 --json | jq '.scopes | length' | grep -q '^1$'
```

### 2.8 决策留痕（本次拍板 + 拒绝的候选）

**拒绝**：
- `verify` 自动删低使用短语——违反"能力诚实"，用户可能有语料语义我们不知
- `_quality_score: 0.34` / `_confidence: low` 字段——banner 化陷阱（
  `memory/projects/grga.md §3` 的铁律 3）
- 一键 `verify --fix` 修 integrity drift——修复语义各异，交给用户/后续动词

**采纳**：
- 桶的**初始阈值**（`chunk_df ≤ 2`、`≥ 90%` 等）是初始 default，用户跑一次 verify
  拿到真实分布后可能提议调整；不做参数化 flag，改就改代码里的常量
- MVP 只做 `verify` 顶层 + `phrases` 子命令，`pairs` 子命令可稍后交付（三个子命令
  之间无循环依赖）

---

## 3. P2 · scope groups

### 3.1 目标

给"想一次查多个 scope"的用户少打字，且 group 定义可跨机器迁移（进知识包）。

### 3.2 硬约束

1. **薄成员集合**：group 只有 `(name, members)` 两个语义字段；**禁止**在 group
   上挂 description / default-tag / default-query-params 等——违反奥卡姆剃刀
2. **禁止嵌套**：`group.members` 只能包含 scope，不允许 group。避免嵌套展开时的
   循环依赖 + 心智模型复杂度
3. **不改任何 query 语义**：`--scope-group X` 严格是 `--scope A --scope B ...`
   的**语法糖**——展开发生在参数解析阶段，展开后调用的 query path 与直接指定多
   `--scope` 完全一致
4. **group 定义变更不需要 rebuild**：纯读侧便利
5. **group 名与 scope 名共享同一命名空间**：`grga scope group create 工作 ...`
   如果本机已有名为 `工作` 的 scope，拒绝创建（exit 非 0）

### 3.3 Schema（外部契约，字段名冻结）

主 DB 新增两张知识层表：

- **`scope_groups`**：group 定义主表，键为逻辑 group 名（跨机器可移植）
- **`scope_group_members`**：group ↔ scope 多对多关系

**冻结的字段语义**（列名不冻结，语义冻结）：

| 语义 | 说明 |
|---|---|
| group 唯一标识 | 逻辑 group 名（字符串），跨机器可复现 |
| group 创建时间 | ISO8601，本机时间戳（非核心，但便于审计） |
| 成员 scope 引用 | 通过 scope 名（不是 scope_id）——因为要跨机器 remap |
| 成员写入顺序 | 不保证。查询侧任何依赖成员顺序的行为视为 bug |

主 DB schema 版本推进到 **v12**。schema 迁移只做 additive `CREATE TABLE IF NOT
EXISTS`，不改动任何现有表。

### 3.4 CLI 契约（外部接口，冻结）

**管理命令**（与现有 `grga scope` 子命令族并列）：

```
grga scope group list [--json]
grga scope group show <name> [--json]
grga scope group create <name> <scope> [<scope> ...]
grga scope group add <name> <scope> [<scope> ...]
grga scope group remove <name> <scope> [<scope> ...]
grga scope group rename <old> <new>
grga scope group rm <name>
```

**使用侧**：给现有所有接受 `--scope` 的动词加 `--scope-group X`（可重复）：

```
grga related "苍坤上人" --scope-group novels
grga search "OKR" --scope-group work --scope 私活-2025
grga probe ...  --scope-group novels
grga expand ... --scope-group ...  # 仅在动词本身接受 --scope 时可用
grga suggest ... --scope-group ...
```

**语义**：`--scope-group X` 在参数解析阶段展开为 X 的所有成员 scope，然后与其他
`--scope`（直接指定）取并集，再经过 `--tag` / `--tag-logic` 交集过滤（沿用现有
tag 语义）。

### 3.5 8 维边界（每一条都要有对应测试）

| 维度 | 期望行为 |
|---|---|
| **空集** | 空 group（0 成员）：`--scope-group empty` 报错 `EXIT_USAGE`，明确 "scope group 'empty' has no members"。`show` 该 group 正常输出 members=[] |
| **单元素** | group 只有一个 scope：`--scope-group X` 等价于 `--scope X`，查询路径完全一致 |
| **唯一性** | group 名与 scope 名共用命名空间：`create` 冲突时 `EXIT_USAGE`，明确报是跟 scope 冲突还是跟 group 冲突 |
| **冲突** | `--scope A --scope-group G`（G 含 A）：**取并集去重**——同一个 scope 只作用一次，不重复计入 |
| **重叠** | `create work 工作 工作`：内部去重后写入，不报错 |
| **自反** | `create work` 后 `rm work` 后 `create work` 应能成功（rm 完全清理成员表） |
| **可结合性** | `--scope-group G --tag T`：与 `--scope A --scope B --tag T` 完全等价（交集语义一致） |
| **降级路径** | group 引用的 scope 已被 `scope rm` 删掉：**读侧跳过缺失成员 + stderr warning + 不 fail**（继续用剩余成员）；`show` 会明确列出 `missing_members: ["工作-老"]` 字段 |

### 3.6 验收命令

```bash
# 1. 空 group 报 EXIT_USAGE
grga scope group create empty_group_test  # 建一个 0 成员 group（如果 create 允许）
grga related "test" --scope-group empty_group_test; test $? -eq 2

# 2. group 展开等价于多个 --scope
diff \n  <(grga related "seed" --scope-group work --json) \n  <(grga related "seed" --scope 工作-2024 --scope 工作-2025 --json)

# 3. group 与 scope 命名冲突拒绝
grga scope group create 工作-2024 ... ; test $? -ne 0

# 4. 缺失成员 warning 但不 fail
grga scope rm 工作-老 --purge-knowledge
grga related "seed" --scope-group work 2>&1 | grep -q "missing scope"
grga related "seed" --scope-group work; test $? -eq 0

# 5. group 数据可 export/import 到另一个 DB（进知识包见 §4）
```

### 3.7 决策留痕

**拒绝**：
- **嵌套 group**（`group_of_groups`）——展开算法要处理循环依赖，收益远不抵复杂度
- **给 group 挂 description / default query params**——薄集合原则；未来若真需要
  再走"未来词汇表"升级路径
- **group 独立命名空间**（跟 scope 名不冲突）——两个命名空间会让 caller 犹豫是
  `--scope X` 还是 `--scope-group X`，共享命名空间反而更清晰

**采纳**：
- `--scope-group` 可重复（`--scope-group work --scope-group novels`），语义为并集
- `show` 输出 `missing_members` 字段——诚实暴露，caller 需要知道 group 不完整

---

## 4. P3 · baseline packs

### 4.1 目标

给"跨机器复用知识资产"和"新语料冷启动"提供内容菜单化的知识包分发机制。**默认
只带词典**（最安全），sender 显式选择才带 pair / votes / groups。receiver 装机
时看得见 sender 做了哪些裁剪。

### 4.2 与现有 `grga index export/import` 的关系

| 动词 | 语义 | 内容 | 用户故事 |
|---|---|---|---|
| `grga index export`（现有） | **完整备份** | 所有知识层（词典 + pair + votes + stats + tags），无过滤 | "我要把这个 scope 整体搬到 B 机器" |
| `grga index import`（现有） | **整体导入 scope** | 从任何 .grgapkg 装成一个 scope，pair 融入主 DB `keyword_pairs`（`scope_id=新 scope`） | "我要在 B 机器上让 X scope 立即可查" |
| `grga pack build`（新增） | **分发型打包** | 内容菜单化（`--dict-only` 默认，`--with-pairs`, `--with-votes`, `--with-groups` 显式） | "我要发一份基线包给别人用" |
| `grga pack install`（新增） | **安装为 scope**（复用 import 语义） | 由 pack manifest 决定 receiver 侧行为 | "我拿到别人的基线包想装" |
| `--seed-ngram-from-pack P`（新增） | **词典冷启动** | 只借词典，不带 pair/votes | "我要在新语料上 build，借 P 的词典热身" |

**核心不变量**：**`export` 和 `pack build` 是两个独立姿势，不合并、不覆盖**。
现有 `export` 契约 0 破坏。

### 4.3 硬约束

1. **默认只带词典**（`--dict-only` 隐含在无 `--with-*` flag 时）——安全默认
2. **`--with-pairs` 允许 `--min-npmi` 过滤**——sender 选择打包成本 vs 完整性
3. **`--with-groups` 需要 `--include-scopes` 展开**：若 group 引用的 scope 不在
   本次 pack 内，group 定义**保留完整引用 + 装机时 warn**（软引用，跟 §3 边界一致）
4. **不做质量分**：pack manifest 不带任何评价字段（`quality`, `rating`,
   `recommended_for` 等一律禁止）
5. **content_hash 强制校验**：install 时算 sha256 与 manifest 里的 `content_hash`
   比对，不匹配 refuse install（`EXIT_VERIFY_FAIL`）
6. **manifest 缺失兼容**：无 `pack_manifest` 表的老 .grgapkg 视为 legacy pack，
   允许 install（走现有 `grga index import` 语义）+ stderr warning
7. **install 前 dry-run 语义**：`grga pack install --dry-run` 只读 manifest 输出
   计划，不写主 DB
8. **不评价 pack 内容**：`grga pack list` / `pack inspect` 输出中立事实（行数、
   大小、hash、min_npmi_filter），无好坏判断

### 4.4 pack manifest schema（外部契约，冻结）

pack 是 SQLite 单文件（沿用 `.grgapkg` 扩展名）。**内部新增一张 `pack_manifest`
表，单行**，冻结 JSON schema：

```json
{
  "manifest_version": 1,
  "name": "chinese-fiction-v1",
  "version": "1.2.0",
  "built_at": "2026-07-17T14:30:00+08:00",
  "built_from": "自然语言描述（22MB 凡人语料 + 12MB 三体语料）",
  "license": "SPDX-id-or-empty",
  "maintainer": "邮箱或 URL",
  "grga_schema_version": 12,
  "content_hash": "sha256:...",
  "contents": {
    "auto_ngram_dict": {"phrase_count": 136808, "max_ngram_n": 4},
    "keyword_pairs": {"row_count": 65065461, "min_npmi_filter": 0.4},
    "llm_extension_votes": null,
    "scope_stats": {"scope_count": 3},
    "scope_tags": {"tag_count": 12},
    "scope_groups": {"group_count": 2, "member_count": 5}
  },
  "included_scopes": ["工作-2024", "工作-2025"]
}
```

**冻结字段语义**：

- `contents.*` 的每一项：`null` 表示不带；对象表示带（附摘要数字）
- `contents.keyword_pairs.min_npmi_filter`：**sender 打包时的 npmi 过滤下限**；
  null 表示无过滤（`--min-npmi` 未指定）。receiver 用 `related --min-npmi X` 若
  `X < min_npmi_filter`，pack 内容无法满足，`related` 结果携带 knowledge_ceiling
  提示（见 §4.7）
- `included_scopes`：pack 里带的 scope 名列表（用于 receiver `install --as` 重命名
  映射的目标数）
- `content_hash`：**排除 manifest 自身**，只 hash 内容表——避免 hash 循环依赖

**"未来词汇表"**（保留 key，实现里不允许出现）：
- `contents.quality_score` / `contents.rating`
- `recommended_for` / `warning`
- `contents.embeddings`（未来若接 embedding 层再启用）

### 4.5 CLI 契约（外部接口，冻结）

**打包侧**（sender 用）：

```
grga pack build [--scope S | --group G | --all] --out PACK.grgapkg
                [--dict-only]                        # 默认（隐含）
                [--with-pairs [--min-npmi N]]        # 可选 pair，可过滤
                [--with-votes]                       # 可选 llm votes
                [--with-groups]                      # 可选 group 定义
                [--with-tags]                        # 可选 scope_tags
                [--include-scopes S1 S2 ...]         # --group 时手动扩展
                [--maintainer M] [--license L]
                [--version V]                        # semver
                [--json]                             # 输出结构化打包报告
```

**读机器侧**（receiver 用）：

```
grga pack inspect PACK.grgapkg [--json]              # 只读 manifest，不装
grga pack install PACK.grgapkg
                [--as SCOPE_MAP]                     # 重命名 scope（例：orig1=new1,orig2=new2）
                [--group-conflict skip|rename|merge] # 默认 skip
                [--dry-run]                          # 只输出计划不写
                [--verify-hash]                      # 默认开
                [--json]
grga pack list [--json]                              # 本机装了哪些包
grga pack uninstall PACK_NAME[@VERSION]              # 只清 installed_packs 记录，
                                                     #  不 rollback scope（因为已融入）
```

**新参数**（给 build/rebuild）：

```
grga index build --scope X --directory D --seed-ngram-from-pack P
```

语义：仅使用 pack 内的 `auto_ngram_dict` 作为词典 seed，忽略 pack 里的 pair /
votes / groups 内容——严格是"词典冷启动"通道。

### 4.6 8 维边界

| 维度 | 期望行为 |
|---|---|
| **空集** | 空 pack（0 phrase / 0 pair）：build 允许 + warn；install 无操作 + exit 0 |
| **单元素** | 只带 1 个 scope 的 pack：install 后本机多一个 scope，行为等同 `grga index import` |
| **唯一性** | pack 里的 group 引用的 scope 有的 receiver 本机存在有的不存在：软引用，装机 warn 具体缺哪些 |
| **冲突** | (a) scope 名冲突：`--as` 强制 remap；无 `--as` 且冲突时 `EXIT_USAGE`。(b) group 名冲突：走 `--group-conflict` 默认 skip 保留本机 |
| **重叠** | pack 里的 phrase 与本机同 scope 已有 phrase 重叠：union，chunk_df 取 max（跟现有 `--seed-ngram-from` 一致） |
| **自反** | `pack build --scope X` 出的 pack `pack install` 到同一 DB（scope 已存在）需要 `--as X2` 或 `--group-conflict rename`；不做隐式覆盖 |
| **可结合性** | `--with-pairs --min-npmi 0.4 --with-groups`：三个 flag 独立正交，任意组合合法 |
| **降级路径** | (a) manifest 缺失：走 legacy import + warn。(b) hash 不匹配：refuse install（`EXIT_VERIFY_FAIL`）。(c) `grga_schema_version` 不匹配：refuse + 明确提示哪个版本 mismatch |

### 4.7 receiver 侧 knowledge_ceiling 探针（新契约字段）

**动机**：pack 里 pair 若被 `--min-npmi 0.4` 过滤过，receiver 用 `related
--min-npmi 0.2` 查询时得到的结果不完整——但 receiver 不知道。

**契约**：`related` 的 `--json` envelope 加**可选**字段：

```json
{
  "results": [...],
  "resolved_seeds": [...],
  "knowledge_ceiling": {
    "affected_scopes": ["工作"],
    "min_npmi_filter": 0.4,
    "source": "pack:chinese-fiction-v1@1.2.0"
  }
}
```

**触发条件**：查询涉及的 scope 中，任一 scope 的 pair 来源含被过滤过的 pack 且
`query.min_npmi < pack.min_npmi_filter`。若无过滤过的 pack 参与，**该字段完全省略**
（不 emit `null`，避免噪声）。

**caller 姿态**：这是 **L1 客观形状**（不评价，只报事实：`0.4 是天花板`），不是
banner——数字化、多维度、可复验。

### 4.8 验收命令

```bash
# 1. 默认只带词典
grga pack build --scope 工作 --out /tmp/p1.grgapkg
grga pack inspect /tmp/p1.grgapkg --json | jq -e '.contents.keyword_pairs == null'
grga pack inspect /tmp/p1.grgapkg --json | jq -e '.contents.auto_ngram_dict != null'

# 2. --with-pairs 带 pair
grga pack build --scope 工作 --with-pairs --out /tmp/p2.grgapkg
grga pack inspect /tmp/p2.grgapkg --json | jq -e '.contents.keyword_pairs.row_count > 0'

# 3. --min-npmi 记录到 manifest
grga pack build --scope 工作 --with-pairs --min-npmi 0.4 --out /tmp/p3.grgapkg
grga pack inspect /tmp/p3.grgapkg --json | jq -e '.contents.keyword_pairs.min_npmi_filter == 0.4'

# 4. content_hash 校验，篡改 refuse
grga pack build --scope 工作 --out /tmp/p4.grgapkg
# 篡改 pack 内容（写入某字节）
printf '\x00' | dd of=/tmp/p4.grgapkg bs=1 seek=1000 count=1 conv=notrunc
grga pack install /tmp/p4.grgapkg; test $? -eq 8   # EXIT_VERIFY_FAIL

# 5. 装机后 knowledge_ceiling 探针（若过滤过）
grga pack install /tmp/p3.grgapkg --as 工作-imported
grga related "seed" --scope 工作-imported --min-npmi 0.2 --json | \n  jq -e '.knowledge_ceiling.min_npmi_filter == 0.4'

# 6. 无过滤时 knowledge_ceiling 字段不出现
grga pack install /tmp/p2.grgapkg --as 工作-full
grga related "seed" --scope 工作-full --min-npmi 0.2 --json | \n  jq -e 'has("knowledge_ceiling") == false'

# 7. --with-groups 带 group 定义
grga scope group create work 工作 工作-2025
grga pack build --group work --with-groups --out /tmp/p5.grgapkg
grga pack inspect /tmp/p5.grgapkg --json | jq -e '.contents.scope_groups.group_count == 1'

# 8. group 软引用：装机时缺成员 warn 但成功
grga pack install /tmp/p5.grgapkg --as work
grga scope rm 工作-2025 --purge-knowledge
grga related "seed" --scope-group work 2>&1 | grep -q "missing scope"
grga related "seed" --scope-group work; test $? -eq 0

# 9. --seed-ngram-from-pack 只借词典，忽略 pack 里的 pair / votes / groups
grga pack build --scope 工作 --with-pairs --with-votes --out /tmp/p6.grgapkg
grga index build --scope 新语料 --directory /path/to/new-corpus \
  --seed-ngram-from-pack /tmp/p6.grgapkg
# 新 scope 的 keyword_pairs 只应含"新语料"自己 build 出的 pair
grga related "seed" --scope 新语料 --json | \
  jq -e '.results[].source | select(. == "chunk_cooccur")' | wc -l  # 全部本机来源

# 10. --dry-run 不写主 DB
sha256sum ~/.config/grga/deep_keywords.db > /tmp/before
grga pack install /tmp/p2.grgapkg --dry-run --json | jq -e '.would_install == true'
sha256sum ~/.config/grga/deep_keywords.db > /tmp/after
diff /tmp/before /tmp/after   # 恒等
```

### 4.9 决策留痕（本次拍板 + 拒绝的候选）

**拒绝**：

- **修改 `grga index export` 默认行为**（"默认只带词典"）——现有 export
  契约是 v0.2 起就发布的完整备份语义，改默认 = breaking。新增 `grga pack
  build` 走并行姿势解决同一诉求，代价可控
- **pack pair 存旁路表 + 查询时按 `--include-pack P` 合并**——引入新的
  查询语义、新参数、新代码路径，且违反"一个 scope = 一份可信知识"的隔离
  假设；复用 import 语义把 pair 融入 `keyword_pairs` 更简洁
- **强制 receiver 最低 `--min-npmi ≥ pack.min_npmi_filter`**——违反"能力
  诚实 + 不评价"哲学。改成 `knowledge_ceiling` 探针字段，让 caller 自己
  判断
- **pack manifest 的 `kind` 枚举**（`baseline-dict | evidence-full`）——
  组合爆炸不易穷举，改成 `contents` 清单式表达
- **给 group 挂 description / default query params**——薄集合原则；未来
  若真需要走"未来词汇表"升级路径
- **pack 的 quality_score / recommended_for 评价字段**——banner 化陷阱
  （`memory/projects/grga.md §3` 铁律 3）
- **pack registry / 远程下载**（`grga pack install --url ...`）——延后到
  P3+，等生态雏形出现再谈；本次只做本地文件路径

**采纳**：

- pack 与 export 并行姿势，双动词服务两种用户故事，互不覆盖
- `contents` 清单支持 `null` 表示未带、对象表示带（附摘要）
- `min_npmi_filter` 记入 manifest，装机后经 `knowledge_ceiling` 探针传递
  给 caller
- group 名与 scope 名共享命名空间，`show` 输出 `missing_members` 字段
- `--group-conflict skip|rename|merge`，默认 skip 保留本机
- content_hash 强制校验，篡改 refuse install
- `--dry-run` 语义：只读 manifest 输出计划，不写主 DB

---

## 5. 交付顺序与 MVP 拆分

### 5.1 物理依赖

```
P1 verify        ⊥  独立
P2 scope groups  ⊥  独立
P3 packs (dict + pairs + votes + tags)   ⊥  独立（不含 --with-groups）
P3 packs (--with-groups)                 ← P2
knowledge_ceiling 字段扩展 related-signals-v3  ← P3 且仅在 pack 携带过滤时触发
```

### 5.2 建议的落地顺序

按"单次交付面积最小 + 独立验收"排：

| 阶段 | 内容 | 独立验收命令 |
|---|---|---|
| **1. P1 MVP** | `grga index verify` 顶层 + `phrases` 子命令；只读 + integrity + storage + distribution + 治理面板前 4 桶 | §2.7 前 6 条 shell 全过 |
| **2. P2 全套** | `scope_groups` / `scope_group_members` 表 + `grga scope group` 子命令族 + 所有接受 `--scope` 的动词加 `--scope-group` 展开 + schema 推 v12 | §3.6 全部 shell 过 |
| **3. P3 MVP** | `grga pack build/install/inspect/list/uninstall` + `--dict-only` / `--with-pairs` / `--with-votes` / `--with-tags` + `--seed-ngram-from-pack` + `knowledge_ceiling` 探针；**暂不含 `--with-groups`** | §4.8 shell #1-#6 + #9 + #10 |
| **4. P3 补** | `--with-groups` + group 软引用软解析 + `--group-conflict` 三选一 | §4.8 shell #7 + #8 |
| **5. P1 v3** | `verify pairs` 子命令（绑死 pair / 泛词共现 / hub 关键字） | §2.7 剩余断言 |

**独立验收**：每一阶段完成后运行**全量 pytest + 阶段验收 shell**，无绿灯不进入下一阶段。

### 5.3 硬约束（跨阶段）

1. **现有 141 项 pytest 全程保持全绿**——任何 phase 引入回归即视为交付失败
2. **现有 `grga index export/import` shell 契约**（build-tuning.md / plan-2026-07-14
   里列出的所有已发布命令行）**行为逐字节等价**
3. **schema 迁移是单向 additive**：老 DB 打开新版 grga 后加表；新 DB 反向兼容
   老版 grga 时**允许缺表 stderr warning**，不 crash
4. **`related --json` 已发布字段全部保留**——`knowledge_ceiling` 是 MINOR
   additive，不动 `results` / `resolved_seeds` 等既有字段

### 5.4 每一阶段的 8 维边界表 → pytest fixture 对齐

`dev-methodology.md §4` 铁律的复用：**规格里的 8 维边界表 = pytest 用例的枚举源
头**。code agent 交付时对每个 phase 的边界表逐行做成测试用例，避免"只测乐观路径"。

### 5.5 诊断候选（不预定诊断结果）

以下候选**仅供参考，允许 code agent 独立诊断出不在候选内的病因或方案**：

- P1 verify 的孤儿检测可能命中意料之外的表关联（例：`keyword_stats` 里
  发现引用了已被 `forget` 清掉的 keyword_id）——不预设行为，看到再决定
- P3 pack build 的 SQL 侧过滤（`WHERE npmi >= ?`）在 76M 行 `keyword_pairs`
  上的性能可能需要临时索引或改走 CTAS
- group 软引用的"missing scope"检测顺序：查询前预检 vs 查询中懒失败——
  两种都合契约，让实现方决定

---

## 6. 相关文档 & 元数据

### 6.1 关联文档

- **依赖**：
  - `docs/design/plan-2026-07-14-scope-directory-split.md`（scope / directory
    分野，知识 vs 证据分层判据）
  - `docs/design/plan-2026-07-11-build-vs-rebuild.md`（`--seed-ngram-from`
    冷启动机制，本方案在此扩展 `--seed-ngram-from-pack`）
  - `docs/design/related-signals-v3.md`（`related --json` envelope 契约，
    本方案对其做 MINOR additive 扩展：加 `knowledge_ceiling` 字段）
  - `docs/build-tuning.md` §2.7（长期存储治理靠拆 scope + 归档 grgapkg，
    本方案是该方向的能力配套）

- **元哲学**：
  - `memory/projects/grga.md §3` caller-first 三条铁律
    （视角换位 / 加工边界 / 暴露判据）—— 本方案三处硬约束（不评价、
    不 rerank、桶必带判据）的元层依据
  - `memory/dev-methodology.md §2 / §3` 规格分工与五类内容分级 —— 本方案
    自身的写作规范
  - `memory/dev-methodology.md §7` 契约冻结与信号暴露 —— pack manifest
    "未来词汇表" 章节的方法学出处

- **上游代码入口**：`docs/navigation.md` 30 秒代码导航索引；相关 CLI
  文件：`src/grga/cli.py`；schema 迁移：`src/grga/graph/schema.py`；
  scope 管理：`src/grga/graph/scope.py`。**具体模块划分由 code agent
  自决**——本方案不预定。

### 6.2 元数据

- **作者**：Ada（AI 开发搭子），与用户 co-design
- **日期**：2026-07-17
- **schema 版本**：本方案完成后主 DB schema 推进到 **v12**
- **状态**：设计定稿，等 code agent 认领 phase-1（P1 MVP）
- **哲学落点**：三项均为 caller-first 铁律的自然外延，"新增能力而不做
  判断"是共同姿态
- **兼容承诺**：所有改动 additive；老 DB 开新版 grga 自动 upgrade；新 DB
  用老版 grga 允许缺表 warning；现有 `grga index export/import` 契约 0 破坏
- **未做的三项**（明确 defer）：
  - pack registry / 远程下载 —— 生态雏形出现再谈
  - `verify --fix` 自动修 integrity —— 修复语义各异，交给用户 / 后续动词
  - 语料专有停用词自动学习 —— 违反"能力诚实 + 不评价"哲学

---

## 附录 A · 未来词汇表（保留 key，实现里不允许出现）

跨全方案的评价性词汇表——任何后续 PR 若引入以下字段名一律 refuse review：

| 字段 | 出现在 | 拒绝理由 |
|---|---|---|
| `quality_score` / `rating` | 任意 `--json` 输出 | 评价字段 = banner 化陷阱 |
| `recommended_for` / `warning` | pack manifest | 评价字段 |
| `_confidence` / `_certainty` | 任意 | 单维度处方性总结 = banner |
| `suggested_action` / `recommendation` | verify / pack inspect | 越权做判断 |
| `should_be_removed` / `is_noise` | verify phrases | 评价 |
| `is_hub` / `is_stopword` | verify | 评价（哪怕基于客观 df 阈值也不允许，用 `chunk_df` 数值本身） |

允许的字段命名模式：**客观事实 + 附加判据**（例：`low_usage_bucket`
带 `criterion: "chunk_df <= 2"` 字段；`high_df_keywords` 带 `criterion:
"chunk_df >= 0.9 * scope_total"`）。

---

**END OF SPEC**
