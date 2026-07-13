# ADR 0001 · Markdown 解析工具选型

- **状态**：已接受（2026-07-07）
- **关联**：`docs/design/v2-design.md` §9.1、`docs/design/v2-plan.md` Phase 2.1
- **决策者**：MVP 3 天冲刺执行

## 背景

v2 结构感知（`heading_path` / `chapter_range`）依赖一个能可靠解析 Markdown 的工具。
硬性要求（摘自 plan §Markdown 解析工具选型决策方案）：

1. 代码块内的 `#` 不能被误识别为 heading（v1 曾用 `markdown-to-data` v2.0.1，此处会误判）。
2. 必须能拿到 heading 的**行号**（不只是 AST 结构）。
3. 冷启动 < 200ms，单 md 文件 < 30ms。
4. 无 CPython 3.12 编译依赖（或提供预编译）。
5. 输出可序列化为 JSON，便于 sidecar 落盘。
6. 失败可优雅退化到"按段落切"。

## 对拍方法

脚本 `scripts/eval_md_parser.py` 对四个候选做统一 schema 对拍，语料
`tests/fixtures/md_eval/`（5 份）覆盖：

- (a) 代码块内 `# not heading`（fenced + 缩进两种）
- (b) 四级嵌套 heading
- (c) heading 内含 inline code
- (d) 无 heading 空文档
- (e) BOM + CRLF 混合

golden 与解析结果逐字段对拍（level / text / line），任一不一致即判 FAIL。

## 对拍结果（本机 aarch64 / CPython 3.12）

| 候选 | 通过 | 冷启动 | 单文件均值 | 行号来源 |
|---|---|---|---|---|
| **markdown-it-py 4.0.0** | 5/5 | ~43ms | ~9ms | `token.map`（原生，精确） |
| marko 2.2.3 | 5/5 | ~11ms | ~3ms | 文本回搜启发式 |
| mistune 3.3.2 | 5/5 | ~13ms | ~3ms | 文本回搜启发式 |
| pulldown-cmark-cli | 未安装 | — | — | （需 `cargo install`） |

关键结论：**三个纯 Python 候选都能正确忽略代码块伪装 heading**（case a 全部 PASS），
说明 `markdown-to-data` 的缺陷不是 CommonMark 生态通病，而是该库个例。

## 决策

**首选：`markdown-it-py`**。**fallback：`marko`**。

理由：

1. **行号最可靠**：`markdown-it-py` 的 `token.map` 直接给出 heading 起止行号，
   无需像 marko / mistune 那样用"文本回搜"启发式推断行号（启发式在重复标题、
   跨行标题场景下会失准）。
2. **CommonMark 严格**：`MarkdownIt("commonmark")` 预设，代码块处理正确。
3. **纯 Python、零编译依赖**：直接进 wheel，符合项目"内置工具、方便部署"哲学，
   且本机 env 已装（v4.0.0）。
4. **性能达标**：冷启动 43ms < 200ms，单文件 9ms < 30ms。
5. **marko 作 fallback**：同为纯 Python、CommonMark 严格，接口稳定，可在
   `markdown-it-py` 缺失时兜底。

**放弃 pulldown-cmark-cli（本阶段）**：虽然 plan 原推荐它，但它需要
`cargo install` 引入 Rust 工具链构建步骤，而纯 Python 候选已 100% 满足全部
硬性要求。保留为未来可选加速项（若出现超大仓库性能瓶颈再评估独立进程方案）。

## 后果

- Phase 2.2 `structure/md.py` 的 `parse_markdown` 以 `markdown-it-py` 为主实现，
  解析器抽象在 `parse_markdown(path) -> StructureDoc` 一层接口后，实现可替换
  （对应 plan 风险 4 的防护）。
- 新增运行期依赖 `markdown-it-py`；起初以 `[project.optional-dependencies].v2`
  分组避免污染 v1 打包（plan 风险 7），后于 2026-07-13 随 PPR/numpy 相关
  `perf` extra 一并整理时提升到核心 `dependencies`（v2 已经是既定路径，
  分组抽象不再有意义）。
- 退化路径：解析异常时 `structure/md.py` 返回空 chapters，expand 退化到段落/行。

## 复现

```bash
python scripts/eval_md_parser.py                 # 人类可读
python scripts/eval_md_parser.py --json          # CI 用，任一 available 解析器有 FAIL 则退出码 1
python scripts/eval_md_parser.py --only markdown-it-py marko
```
