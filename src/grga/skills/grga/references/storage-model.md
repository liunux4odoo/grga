# Storage model (deep dive)

Referenced from the "Storage model at a glance" section of `SKILL.md`.
This file holds the full detail; `SKILL.md` keeps only the summary needed
at trigger time.

## The core idea

grga is designed around a **single per-user knowledge graph**:
`~/.config/grga/deep_keywords.db` (honours `$XDG_CONFIG_HOME`). Every
`index build` writes into that shared DB using the `directory` column as
a partition key, and every query verb defaults to **filtering results by
`--directory`** — scope is a *query-time filter*, not a partition
boundary. Give `-d ./project_a` and you narrow the answer to that corpus;
omit `-d` and the whole per-user graph is in scope（跨 scope 求和）。

Why: grga's target caller is an AI agent that hops across corpora
(novels, papers, code, notes) in one session. Keeping the graph unified
turns cross-corpus co-occurrence into a *feature* (recall the concept
regardless of which corpus you were browsing yesterday) while
`--directory` still lets a query stay inside one project when needed.

Index each corpus once, then all query verbs share the graph:

```bash
grga index build -s ~/novels  --db ~/.config/grga/deep_keywords.db --json
grga index build -s ~/papers  --db ~/.config/grga/deep_keywords.db --json
grga index build -s ~/code    --db ~/.config/grga/deep_keywords.db --json
# `related "x" -d ~/papers` sees only papers; omit -d to search all three.
```

## v9 起 pair 是累积知识资产

`keyword_pairs`（+ `keyword_stats` + `scope_stats`）是 grga 从语料**学到
的知识资产**，不是可派生的中间表。它遵循 grow-and-refine 语义：

- **随语料更新/新增而 refine**：新文件的共现增量 UPSERT 进已有资产。
- **不随语料删除而销毁**：删源文件**不动** pair 表。只有显式 `grga index
  forget` 才清理某 scope 的资产。
- **可跨语料迁移复用**（未来方向）：一个语料学到的 pair 字典可服务另一
  个语料，见 `docs/design/plan-2026-07-12-keyword-primary.md` §8。

写入期存储契约（build 时固化的资产下界）：

| Flag | 默认 | 作用 |
|---|---:|---|
| `--min-pair-count` | 5 | 单 scope 内 `cooccur_count < 5` 的 pair 不入表 |
| `--min-npmi-build` | 0.2 | `npmi < 0.2` 的 pair 不入表（`llm_promoted` 不受此限） |

**门槛只能在 build 期放松，不能在读侧救回**：低于门槛的 pair 根本没被
记忆。`related --mode graph --min-npmi` 只能**收紧**读侧地板（`0.2 →
0.3`），不能低于写入期门槛。

## 关于跨 scope

- 传 `-d ./proj_a`：`related --mode graph` 只看 `dir_id=proj_a` 的
  `keyword_pairs`；`chunk_df` 来自该 scope 的 `keyword_stats`。
- 不传 `-d`：跨所有 scope 的求和视图。`chunk_df` 是 `SUM(chunk_df)`；
  `keyword_pairs` 展开时 `dir_id` 不做过滤。
- v3 起 `--cross-scope` / `--preview` / `crossed_scopes_preview` 都已
  移除——跨语料合并的显式接口留待未来（§8 of keyword-primary plan）。
  当前若需比较两个 scope 的结果，caller 各跑一次 `-d` 再合并。
- `--mode heading` 依然默认跨 scope（heading paths 是结构化过滤，抗
  同名歧义）。加 `--same-scope` 限制在 `-d`。

## Observability

- `grga graph inspect <keyword> --db <path> --json` — 关键字诊断动词：
  reports `docs_by_scope`（该 keyword 在各 scope 的 doc 分布）、
  `top_cooccurring_keywords`（top 共现邻居）、`suspected_polysemy`
  启发（不同 scope 邻居列 Jaccard < 0.2 时为 true）。当 keyword 从未
  进入索引（`total_docs == 0`）时输出 `index_hint`，`suspected_polysemy`
  / `polysemy_hint` 字段不出现（key on 字段 presence 区分两支）。
- 用它做**种子质量判断**（hub? 稀有?）、**跨 scope 分布**审计。它不
  替代 `related`——见 `references/reflection-loop.md` §"T2 是 related，
  不是 graph inspect"。

跨 scope 的结果里，每个 `Related`（heading 模式）仍带 `directory`（该
文档所在的 scope 根）和 `qualified_path`（`<directory>/<doc_path>`），
所以同名文件（例如两个 `README.md`）保持可区分。

## When to override with `--db <path>`

The shared per-user DB is the default. Pass an explicit `--db` when:

- **Multi-user machines** where per-user separation is not enough
  (each user still gets a separate `~/.config/grga/deep_keywords.db` by
  default; use `--db` only to escape that): `--db /secure/team.db`.
- **CI / test fixtures** that must not touch your personal DB:
  `--db "$TMPDIR/ci.db"`.
- **Sensitive projects** that must not co-occur with anything else:
  `--db ~/work/secret/graph.db`.

## Schema 备忘（v8）

| Table | Purpose |
|---|---|
| `directories` / `keywords` | 全局 intern 表（id ↔ 文本 1:1） |
| `docs` | 增量判定（size/mtime/hash），不再喂 pair |
| `keyword_pairs` | **核心知识资产**：(dir_id, kw_a_id, kw_b_id, source) + `cooccur_count` + `npmi` |
| `keyword_stats` | per-(dir_id, kw_id) 词频统计（chunk_df / doc_df / total_occurrences） |
| `keyword_docs` | **doc 级倒排索引**（v9），替代已删的 chunk 级 `keyword_chunks`；一行一 (kw, doc)，比旧表小 ~30×。供 `related --mode heading` / `graph inspect` 的 seed→docs 查找 |
| `scope_stats` | per-dir_id scalar：`n_chunks`（npmi 分母 N）/ `n_docs` / `vocab_size` |
| `heading_docs` | Markdown 章节表（`related --mode heading` / `expand --level chapter` 用） |
| `auto_ngram_dict` | tokenizer 学到的 n-gram 字典 |
| `llm_extension_votes` | LLM 提出的 pair 投票暂存（`grga index promote` 达阈值才升级） |

**v8/v9 已删除**：`chunks`（唯一读者是 npmi 分母，改读 `scope_stats.n_chunks`
后即可删）、`keyword_chunks`（45GB 线性怪兽；chunk 只在 build 期 RAM
存活，doc 级需求由 `keyword_docs` 承接）。旧库首次打开会走
`_migrate_v7_to_v8` / v9 迁移（有损：删掉的 chunk 级信息无法逆向重建）
——迁移前 stderr 警告一次。
