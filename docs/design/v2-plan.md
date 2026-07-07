# grga v2 开发计划

> 版本：v0.1（2026-07-07）
> 依据：`docs/design/v2-design.md`
> 目标仓库：`/media/lqw/DATA/myrepo/grga`（src/grga/*.py，4498 行）
> Python：`/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3`
> 计划来源：Ada 通过 make_plan skill 咨询 default agent 生成，Ada 已 review

## 全局原则

1. **Layer 1 是旁路增强层，不阻塞 Layer 0**：ripgrep-all 主链路保持原样，结构/图能力全部走"离线预建索引 + 在线 O(log N) 查询"。
2. **保护 v1 自用能力**：所有新代码走独立模块（`meta.py` / `structure/` / `graph/` / `expand.py` / `related.py` / `suggest.py`），`search.py` 和 `deep_search.py` 只允许"非侵入式扩展"（新增字段、新增可选参数），不重构。
3. **每个 Agent API 动词幂等 + 可预测**：`search / expand / related / suggest` 独立、无隐藏状态、无内部反思循环；LLM 只出现在 `suggest`。
4. **结构感知全走 sidecar**：结构索引写到 `.grga/structure/*.json` 和 SQLite 库 `.grga/grga.db`，仓库本身不写侵入文件。
5. **先功能后 Skill**：Phase 0–5 只做纯 Python 库 + CLI 子命令，Phase 6 才封 Skill；不早于此写 SKILL.md。
6. **退化可预测**：任何结构相关能力都必须给出 `chapter → paragraph → lines(±3) → lines(±0)` 的兜底路径，返回值带 `level` 字段明示实际粒度。

## 依赖关系总览

- **强串行**：Phase 0 → Phase 1 → Phase 2 → Phase 3；Phase 2 是 Phase 3 的 `chapter/document` 分支和 Phase 4 的 `heading_docs` 表的前置。
- **可并行**：Phase 4（图扩展）和 Phase 3（L1 规则扩展）在 Phase 2 完成后可并行；Phase 5（L3 + 反哺）依赖 Phase 4 的 `keyword_pairs` 写入接口，但 `suggest()` 本身可以在 Phase 4 未完成时先出接口 stub。
- **末端串行**：Phase 6（Skill 化）依赖 Phase 3、4 至少可用，Phase 5 可选。

```
Phase 0 ─▶ Phase 1 ─▶ Phase 2 ─┬─▶ Phase 3 ─┐
                                └─▶ Phase 4 ─┼─▶ Phase 5 ─▶ Phase 6
                                             │
                                     (Phase 3/4 可并行)
```

---

## Phase 0 · 基础扎实（1.5 天）

**目标**：为现有 4498 行代码建立测试防线，避免 v2 改动打穿 v1 自用能力。

### 0.1 建立 pytest 骨架
- 产出物：`tests/` 目录、`tests/conftest.py`、`pyproject.toml` 增加 `[tool.pytest.ini_options]`、`tests/fixtures/` 存放小型 md/代码语料
- 完成标志：`python -m pytest -q` 空跑通过（0 failed）
- 前置依赖：无
- 工作量：半天

### 0.2 为 `search.py` 查询表达式引擎加回归测试
- 产出物：`tests/test_search_expr.py`，覆盖词法、parser、AST、布尔 PCRE2 lookahead 生成
- 完成标志：≥ 20 个 case 通过，覆盖 AND/OR/NOT/短语/嵌套括号；`pytest --cov=grga.search` ≥ 70%
- 前置依赖：0.1
- 工作量：一天

### 0.3 为 `deep_search.py` 关键纯函数加测试
- 产出物：`tests/test_deep_search_pure.py`，锁定合并去重、分层排序、早停这三类纯函数
- 完成标志：≥ 10 个 case 通过，无需真起 LLM（用 fake planner）
- 前置依赖：0.1
- 工作量：半天

### 0.4 冒烟测试 v1 CLI 主用例
- 产出物：`tests/test_cli_smoke.py`，跑 `grga search "xxx" tests/fixtures/`
- 完成标志：exit code 0，输出条目数 > 0，字段完整
- 前置依赖：0.1
- 工作量：半天

**Phase 0 检查点**：向用户演示 `pytest -q` 全绿 + `pytest --cov` 报告；证明"改 v2 不打穿 v1"的护栏已经在。

---

## Phase 1 · 命中元数据（1 天）

**目标**：在 `search()` 结果每条 Hit 上补齐 §6.1 中"位置元数据"四个字段。

### 1.1 定义 `Hit` dataclass
- 产出物：`src/grga/models.py`，`class Hit`（含 `occurrence_index / total_occurrences / line_ratio / nearby_hits / matched_terms / is_llm_generated`，结构字段先留 `None`）
- 完成标志：`from grga.models import Hit` 可用；`tests/test_models.py` 通过
- 前置依赖：0.1
- 工作量：半天

### 1.2 元数据计算函数
- 产出物：`src/grga/meta.py`，函数 `annotate_hits(raw_hits, file_line_counts) -> list[Hit]`
- 完成标志：`tests/test_meta.py`，给定 10 条构造 hit 能算出正确 index/total/ratio/nearby
- 前置依赖：1.1
- 工作量:半天

### 1.3 接入 `deep_search.py` 输出管线
- 产出物：`deep_search.py` 末端加一个 `annotate_hits` 调用（非侵入，走可选参数 `with_metadata=True`，默认开）
- 完成标志：CLI 输出多出 4 个字段；v1 用法 `--no-metadata` 仍可关闭；0.4 冒烟测试仍绿
- 前置依赖：1.2, 0.4
- 工作量：半天

**Phase 1 检查点**：CLI 上跑一次搜索，用户能看到每条命中带 `[3/12] ratio=0.42 nearby=2` 这类元数据。

---

## Phase 2 · Markdown 结构感知(2.5 天)

**目标**：产出 sidecar 结构索引，为 `heading_path / chapter_id / chapter_range` 供数。

### 2.1 完成 Markdown 解析工具选型（见文末专节）
- 产出物：`docs/adr/0001-md-parser.md`，选型结论 + 试用脚本 `scripts/eval_md_parser.py`
- 完成标志：给定 5 份含代码块/引用/嵌套列表的 md，代码块内 `# xxx` 不被识别为 heading；所有 heading 行号正确
- 前置依赖：0.1
- 工作量：半天

### 2.2 `structure/md.py` 实现 md → 结构 JSON
- 产出物：`src/grga/structure/md.py`，函数 `parse_markdown(path) -> StructureDoc`
- 完成标志：`tests/test_structure_md.py` ≥ 8 个 case 通过，含边界（无 heading / 只有 h1 / 深层嵌套 / 代码块伪装）
- 前置依赖：2.1
- 工作量：一天

### 2.3 Sidecar 读写 + mtime 懒重建
- 产出物：`src/grga/structure/sidecar.py`，`load_or_build(path) -> StructureDoc`；sidecar 落到 `.grga/structure/<hashed_path>.json`
- 完成标志：`tests/test_sidecar.py` 覆盖首建/命中缓存/mtime 变更重建；两次调用第二次 < 5ms
- 前置依赖：2.2
- 工作量：半天

### 2.4 结构字段回填 Hit
- 产出物：`meta.py` 增加 `annotate_structure(hits) -> None`，把 `heading_path / chapter_id / chapter_range` 写回
- 完成标志：md 命中输出带 `heading_path=["安装","macOS"]`；非 md 保持 `None` 不炸
- 前置依赖：2.3, 1.3
- 工作量：半天

**Phase 2 检查点**：对 `docs/` 目录搜索一次，md 命中带完整 heading_path；给用户跑一遍代码块伪装 heading 的 case，确认不误识别。

---

## Phase 3 · L1 规则扩展（1.5 天）

**目标**：实现 `expand(hit, level)` 四档粒度 + 退化路径。

### 3.1 `expand.py` 骨架 + Context 模型
- 产出物：`src/grga/models.py` 加 `class Context`；`src/grga/expand.py` 定义 `expand(hit, level, size=3) -> Context`
- 完成标志：`level="lines"` 分支跑通（内部调 `rg -B/-A` 或 Python 切片）；`tests/test_expand_lines.py` 通过
- 前置依赖：1.1
- 工作量：半天

### 3.2 paragraph / document 分支
- 产出物：`expand.py` 的 `_expand_paragraph`（找前后空行）、`_expand_document`（读整文件 + 截断保护）
- 完成标志：`tests/test_expand_para_doc.py` 通过；document 分支对 > 1MB 文件返回 `truncated=True`
- 前置依赖：3.1
- 工作量：半天

### 3.3 chapter 分支 + 退化链
- 产出物：`_expand_chapter` 依赖 Phase 2 sidecar；`_degrade(hit, level)` 实现 chapter→paragraph→lines(±3)→lines(±0)
- 完成标志：非 md 文件请求 `level="chapter"` 自动退化为 paragraph，返回 `Context.level="paragraph"`
- 前置依赖：3.2, 2.3
- 工作量：半天

### 3.4 CLI 暴露 `grga expand`
- 产出物：`cli.py` 增加 `expand` 子命令：`grga expand <path>:<line> --level chapter`
- 完成标志：能对上一步 `grga search` 的输出手动展开一条命中
- 前置依赖：3.3
- 工作量：半天

**Phase 3 检查点**：演示 caller 模拟——先 `grga search foo`，再对第一条 hit `grga expand --level chapter`，看到完整章节文本；对非 md 场景演示退化。

---

## Phase 4 · L2 图扩展（2.5 天）

**目标**：新增两张表 + PPR + `related()` 三种 mode，可与 Phase 3 并行。

### 4.1 SQLite schema 迁移
- 产出物：`src/grga/graph/schema.py`，`ensure_schema(conn)` 建 `keyword_docs / heading_docs / llm_extension_votes` 表；保留 `keyword_pairs / keyword_counts`
- 完成标志：`tests/test_graph_schema.py` 通过；对旧库自动 upgrade 不丢数据
- 前置依赖：0.1
- 工作量：半天

### 4.2 离线索引构建脚本
- 产出物：`src/grga/graph/build.py`，`grga index build --scope <dir>`；填充 `keyword_docs`（走关键字抽取，可先复用 v1 的 `keyword_counts` 逻辑）与 `heading_docs`（依赖 Phase 2 sidecar）
- 完成标志：对 fixtures 目录跑一次，两张表行数 > 0；`tests/test_graph_build.py` 校验倒排一致性
- 前置依赖：4.1, 2.3
- 工作量：一天

### 4.3 PPR 实现
- 产出物：`src/grga/graph/ppr.py`，`personalized_pagerank(seeds, top_k, alpha=0.15, iters=20)`；基于 `keyword_pairs` 邻接
- 完成标志：`tests/test_ppr.py` 用手工构造 6 节点小图，验证 top-k 与手算一致
- 前置依赖：4.1
- 工作量：半天

### 4.4 `related()` API 三种 mode
- 产出物：`src/grga/related.py`，实现 `mode="graph|dir|heading"`；`cli.py` 加 `grga related`
- 完成标志：`tests/test_related.py` 三种 mode 各一 case；CLI 演示"给定关键字 → 相邻文档 top-5"
- 前置依赖：4.2, 4.3
- 工作量：半天

**Phase 4 检查点**：演示 `grga index build` 建库耗时 + `grga related "安装" --mode heading` 输出跨文档同章节。

---

## Phase 5 · L3 LLM 扩展 + 反哺闭环（1.5 天）

**目标**：`suggest()` 上线，投票达阈值后沉淀到 `keyword_pairs`。

### 5.1 `suggest()` API + intent 分派
- 产出物：`src/grga/suggest.py`，`suggest(query, hits, intent="expand|rerank|reformulate")`；prompt 模板 `prompts/suggest_*.txt`；返回 `Suggestion(llm_generated=True, tokens_used=...)`
- 完成标志：fake LLM 下 `tests/test_suggest.py` 三种 intent 各一 case；真跑 openai 出结果结构合法
- 前置依赖：1.1
- 工作量：半天

### 5.2 投票表写入
- 产出物：`graph/votes.py`，`record_vote(kw1, kw2)`；`suggest()` 输出的关键字对自动记票
- 完成标志：`tests/test_votes.py` 校验累计与去重
- 前置依赖：4.1
- 工作量：半天

### 5.3 反哺离线任务
- 产出物：`cli.py` 加 `grga index promote --threshold 3`，把 `llm_extension_votes` 中 vote_count ≥ N 的对合并进 `keyword_pairs`
- 完成标志：`tests/test_promote.py` 覆盖阈值/去重/幂等
- 前置依赖：5.2, 4.2
- 工作量：半天

**Phase 5 检查点**：演示一次冷查询 → suggest 命中 → 手动 promote → 下次同查询走 L2 免费拿到，无需再调 LLM。

---

## Phase 6 · Skill 化（1 天）

**目标**：把 4 个动词包装成 QwenPaw Skill 供 caller agent 使用。

### 6.1 Skill 目录 + SKILL.md 起草
- 产出物：`skills/grga/SKILL.md`（触发条件、4 动词签名、典型组合模式）
- 完成标志：另一个 agent 读 SKILL.md 后能正确说出何时调 `search` 何时调 `related`
- 前置依赖：3.4, 4.4
- 工作量：半天

### 6.2 CLI 稳定契约 + 输出 JSON schema
- 产出物：所有子命令加 `--format json`；`docs/api.md` 声明字段
- 完成标志：`jq` 能解析所有输出；`tests/test_cli_json.py` 通过
- 前置依赖：3.4, 4.4, 5.3
- 工作量：半天

### 6.3 caller 组合示例
- 产出物：`skills/grga/examples/*.md`，至少 3 个模式（search→expand chapter / search→related graph / search→suggest→promote）
- 完成标志：用户 review 通过
- 前置依赖：6.1, 6.2
- 工作量：半天

**Phase 6 检查点**：让一个测试 agent 加载 skill，用它回答一个真实自用问题，全流程无人工干预。

---

## Markdown 解析工具选型决策方案

**推荐**：**Rust CLI `pulldown-cmark-cli`（首选）** + **Python `marko`（fallback）**。

理由：pulldown-cmark 是 CommonMark 严格实现（Rust 生态主力），代码块内 `#` 不会误识别；独立子进程调用与 ripgrep-all 同风格，符合"轻依赖"约束。marko 作为纯 Python fallback，供无 Rust 工具链环境使用。

**试用步骤**：

1. 准备评估语料 `tests/fixtures/md_eval/`：至少 5 份 md，含（a）代码块内 `# not heading` （b）四级嵌套 heading （c）heading 内含 inline code （d）无 heading 空文档 （e）BOM/CRLF 混合。
2. 写 `scripts/eval_md_parser.py`：分别调 `pulldown-cmark-cli`、`marko`、`mistune`、`markdown-it-py` 四个候选，输出统一 JSON `{path, headings: [{level, text, line}], code_blocks: [{start_line, end_line}]}`。
3. 与人工标注 golden 对拍：字段完全一致才算通过；重点检查 case (a) 的 heading 数为 0。
4. 记录冷启动耗时、单文件解析耗时、依赖大小；写入 `docs/adr/0001-md-parser.md`。

**验证清单**：

- [ ] case (a) 代码块内 heading 数为 0
- [ ] heading 行号误差 = 0
- [ ] 冷启动 < 200ms，单 md 文件 < 30ms
- [ ] 无 CPython 3.12 编译依赖（或提供预编译）
- [ ] 输出可 pipe 成 JSON，便于 sidecar 落盘
- [ ] 失败时能优雅退化到"按段落切"

## 关键风险点与兼容性防护

- **风险 1：`search.py` / `deep_search.py` 被改坏，v1 自用能力回归**
  → 应对：Phase 0 先建 pytest 防线；新能力只允许"新增可选参数 + 新增字段"，禁止改现有函数签名；`--no-metadata` 保留旧输出格式。
- **风险 2：sidecar 目录污染仓库或跨机器不一致**
  → 应对：sidecar 统一落 `.grga/structure/`（用户 home 或仓库根可配置，默认仓库根）；`.gitignore` 模板给出建议；文件名用 hash 避免 windows 路径长度问题。
- **风险 3：SQLite schema 变更打穿旧库**
  → 应对：`ensure_schema` 用 `CREATE TABLE IF NOT EXISTS` + 版本号 pragma；4.1 提供一次性 dump/load 脚本；每次 schema 变更加 alembic 风格迁移记录。
- **风险 4：Markdown 解析工具选错，Phase 2/3/4 全部返工**
  → 应对：解析器抽象在 `structure/md.py` 后一层接口（`parse_markdown(path) -> StructureDoc`），实现可替换；先验证再动 Phase 2。
- **风险 5：L3 反哺污染图，胡乱产生 keyword_pairs**
  → 应对：投票阈值默认 3，可配；`llm_extension_votes` 保留原始记录不清空，便于回滚；`promote` 命令支持 `--dry-run`。
- **风险 6：`grga index build` 全库扫描太慢，用户放弃**
  → 应对：增量走 mtime；`build --scope` 支持子目录；PPR 迭代次数默认 20 但可调；给出预估进度条。
- **风险 7：新增依赖打脏 v1 打包**
  → 应对：新依赖仅在 `pyproject.toml` 的 `[project.optional-dependencies].v2` 分组；`toolchain.py` 不动。

## 最小可行版本（3 天冲刺）

**只做**：0.1 + 0.4 + 1.1 + 1.2 + 1.3 + 2.1 + 2.2 + 2.3 + 2.4 + 3.1 + 3.3 + 3.4。

**跳过**：0.2 / 0.3 深度测试（只保留冒烟）、3.2 的 document 分支、Phase 4/5/6 全部。

**能演示的效果**：

1. `grga search "安装"` 输出每条命中带 `[3/12] ratio=0.42 heading_path=["安装","macOS"]`（Phase 1 + 2.4 联合出效果）。
2. `grga expand docs/foo.md:42 --level chapter` 拿到整章文本，非 md 文件自动退化。
3. 代码块伪装 heading 的 md 不再误识别（Phase 2.1 验证脚本可当场跑给用户看）。
4. `pytest -q` 冒烟测试全绿，证明 v1 自用能力没被打穿。

**留给下一阶段**：related() / suggest() / 反哺闭环 / Skill 化。这四项一旦 3 天验证 MVP 有效再排期，避免过早投入。

---

## Ada 的审阅意见（2026-07-07）

计划整体质量高，可直接执行。几点补充观察：

1. **总工期约 12 天**（0.5+1+2.5+1.5+2.5+1.5+1+1）单人节奏；如只用碎片时间，MVP 3 天路径最实用。
2. **Markdown 解析选型推荐 pulldown-cmark-cli 我认可**，Rust 独立进程和 grga 现有 rga 打包哲学一致。
3. **风险 4 是最大隐忧**：Phase 2.1 是所有结构感知的地基，一定要在 Phase 2.2 之前把选型固化。
4. **Phase 0.4 的冒烟测试是关键护栏**：这一步跳掉就等于裸奔。
5. **可选补强**：Phase 0 可加一个 `git commit -m "baseline"` 检查点，确保有回滚基准。
