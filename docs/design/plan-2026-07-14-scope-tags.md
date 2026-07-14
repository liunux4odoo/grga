# plan-2026-07-14 · scope tags（v11 多值标签）

> 状态：已落地（schema v11）。本文记录决策依据、契约与边界，供后续
> caller / 维护者参考。上游背景见 `plan-2026-07-14-scope-directory-split.md`
> （v10 scope/directory 分野）。

## 1. 目标

让一个 scope 可以携带多个**标签**（tag），并在查询侧用 tag 组合
（顺序无关，支持 and/or）过滤出符合条件的 scope 集合，再走现有的
`keyword_pairs.scope_id IN (...)` 过滤链路。

典型用法：

```
grga index build --scope three-body --directory ./corpus --tag 科幻 --tag 小说
grga index build --scope wukong    --directory ./corpus2 --tag 仙侠 --tag 小说

grga related 黑暗森林 --tag 科幻 --tag 小说 --tag-logic and   # 命中 three-body
grga related 修炼    --tag 小说                              # 命中 three-body + wukong (OR)
```

## 2. 决策（方案 A）

来源：与用户的三轮设计确认。

- **scope 仍是原子知识分片键**：`keyword_pairs` / `keyword_stats` /
  `scope_stats` / `auto_ngram_dict` / `llm_extension_votes` 的 shard
  关系完全不变。tag **不**成为新的 shard key（否决方案 B 的组合展开，
  避免存储放大）。
- **tag 是叠加在 scope 上的多值元数据**：many-to-many。新增一张
  `scope_tags(scope_id, tag)` 关联表，不动任何现有表。
- **CLI**：
  - build 侧 `--scope`（必填、语义不变）+ `--tag`（可多次，写入
    `scope_tags`）。
  - query 侧 `--scope`（可多次、精确 name 匹配，语义不变）+ `--tag`
    （可多次）+ `--tag-logic and|or`（默认 `or`）。
  - 独立管理：`grga scope tag add|rm|list|mv`（见 §4.1）——不必 rebuild
    就能增删改标签。
- **组合规则**（`related._related_keywords_impl`）：
  - 都不传 → cross-scope union（老行为）。
  - 只 `--scope` → 精确 name 匹配（老行为）。
  - 只 `--tag` → `scope_tags` 反查 scope_id 集合。
  - 两者都传 → **取交集**（scope 必须同时满足 name 列表与 tag 条件）。
  - 任一 filter 非空但解析为空集 → 提前返回空结果（不退化成 cross-scope）。

### 2.1 tag 管理命令（`grga scope tag`）

```
grga scope tag add  <scope> <tag>...        # 累积附加（INSERT OR IGNORE，创建 scope on demand）
grga scope tag rm   <scope> [<tag>...]      # 删标签；省略 tag = 清该 scope 全部标签
grga scope tag list <scope>                 # 列该 scope 全部标签（升序）
grga scope tag mv   <scope> <old> <new>     # 单 scope 内改名（单行 UPDATE）
```

`mv` 语义严格对齐 `grga scope rename`：

- `LookupError`（退出码 1）：scope 不存在，或该 scope 上无 `old` 标签。
- `FileExistsError`（退出码 1）：`new` 已附加在该 scope 上（拒绝静默 merge，
  错误信息指向 `scope tag rm` 让用户显式先删）。
- `ValueError`（退出码 2）：名字空 / `old == new`。
- **作用域是单个 scope**：改 `three-body` 上的 `科幻→sci-fi` 不动其它
  scope 上的同名标签（tag 语义就是"scope 的多值标签"，跨 scope 强行同步
  会破坏可组合性）。
- 只碰 `scope_tags` 一行，不触知识资产。

## 3. Schema（v11）

```sql
CREATE TABLE scope_tags (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    tag      TEXT NOT NULL,
    PRIMARY KEY (scope_id, tag)
) WITHOUT ROWID;

CREATE INDEX idx_scope_tags_tag ON scope_tags (tag, scope_id);
```

- PK `(scope_id, tag)` 覆盖"scope → tags"反查；`idx_scope_tags_tag`
  覆盖"tag → scope_id"正查（查询热路径）。
- **v10 → v11 是加表非破坏性升级**：`ensure_schema` 对 `user_version`
  落在 `[MIN_UPGRADABLE_VERSION(=10), SCHEMA_VERSION(=11)]` 的库跑
  幂等 `CREATE TABLE IF NOT EXISTS` + 重打版本戳，**不重写任何数据**。
  `user_version < 10` 仍按 `plan-2026-07-14-scope-directory-split.md`
  §7 直接报错重建。

## 4. 知识 / 证据边界

`scope_tags` 归**知识层**：tag 描述"这个 scope 是什么"，跨机器可迁移。

- `grga index export` / `import`（`graph/pack.py`）序列化 `scope_tags`：
  export 走 ATTACH `INSERT ... SELECT`（无 kw_id remap，只 scope_id）；
  import 用 `_import_tags` 只重写 scope_id。`ExportStats.n_tags` /
  `ImportStats.n_tags` 报告条数。
- `remove_scope(purge_knowledge=True)` 连 `scope_tags` 一起清
  （`RemoveStats.tags_removed`）；默认的 evidence-only 删除**不**碰 tag。

## 5. 契约冻结点

- `--tag` / `--tag-logic` 一旦发布即外部契约，**只加不改**。
- `--tag-logic` 默认值 `or` 是契约的一部分，改默认需走 deprecation。
- `scope_tags` 表结构只加列不改列不删列；改即 schema v12 破坏性升级。
- `RelatedKeyword.scopes` 字段**不变**：暴露的仍是命中的 scope **name**，
  不是 tag。caller 想知道命中哪些 tag，拿 scope name 后自己回查
  `scope_tags`（AGENTS.md §"能派生就让 caller 自己做"）。

## 6. 明确不做（边界）

- 热路径动词 `search` / `probe` / `expand` **不**接 `--tag`。用户想按
  tag 缩范围时先跑 `grga related --tag ...` 或 `grga scope list --json`
  拿到 scope name，再喂给 `search --scope <name>`
  （AGENTS.md §"契约扩展优先落在低频动词"）。
- `deep_search`（v1 legacy）、GUI 不动。
- 不支持任意布尔表达式（`(A AND B) OR C`）——只有全局 `and|or` 切换。
  未来若有 caller 侧派生笨拙的证据，再考虑升级。
