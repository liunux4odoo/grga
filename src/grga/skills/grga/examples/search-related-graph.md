# Pattern: search → related graph（候选关键字）→ ripgrep

**When to use.** 用户有一个主题关键字，想找到**语料里还有哪些相关概念**，
而不仅仅是它字面出现的位置。v3 起 `related --mode graph` 返回的是**候选
关键字 + 关联证据**（不是文档）——它基于离线 L2 共现资产（`keyword_pairs`
带 `npmi`）计算，**不调用 LLM**。拿到候选关键字后，用 `rga` / `grga
search` 找原文片段是 caller 的职责。

> v3 破变：graph 模式过去返回"相关文档 + PPR 元数据"，现在返回候选关键字。
> 从关键字到片段用 ripgrep。见 `docs/design/related-signals-v3.md`。

## Storyboard

1. Prep once: 为语料建离线共现索引。
2. User: *"哪些文件提到 brew？还有什么和它相关？"*
3. Agent 跑 `grga search` 拿字面命中 + matched keywords。
4. Agent 从 top 命中里抽一小撮关键字种子。
5. Agent 调 `grga related --mode graph` 拿**候选关键字**（`kw` + `npmi` +
   `chunk_df` + `via_seed`）。
6. Agent 把 seed + 候选关键字拼进 `rga -e`（或 `grga search` 的 OR 查询）
   找原文片段。
7. 如需按结构维度补充，另调 `--mode heading` 拿共享 Markdown 章节的文档。

## Commands

```bash
# 0) One-time (or on-repo-change) offline build.
grga index build \
    --scope tests/fixtures/cross_heading \
    --db /tmp/grga.db \
    --json

# 1) Literal search for the seed term.
grga search "brew" \
    --directory tests/fixtures/cross_heading \
    --json > /tmp/hits.json

# 2) Extract matched terms (keyword seeds) from the top few hits.
jq -r '.matches
       | map(select(.is_context == false))
       | .[0:5]
       | map(.matched_terms // [])
       | flatten | unique
       | .[]' /tmp/hits.json
# -> brew

# 3) 候选关键字（over keyword co-occurrence，返回 kw 不是 doc）。
grga related "brew" \
    --mode graph \
    --db /tmp/grga.db \
    --json > /tmp/related.json

# 4) 用 seed + 候选关键字拼 ripgrep 找真正的原文片段（caller 的活）。
jq -r '.results[0:5][].kw' /tmp/related.json    # -> linux / macos / homebrew-only ...
rga -i -e brew -e linux -e macos tests/fixtures/cross_heading

# 5) Optional: cross-file chapter neighbours ("safe alternative view").
grga related "macOS" \
    --mode heading \
    --db /tmp/grga.db \
    --json
```

## Expected shape of `related --mode graph`（v3）

```json
{
  "results": [
    {"kw": "linux", "npmi": 0.62, "cooccur_count": 14.0,
     "via_seed": "brew", "chunk_df": 21, "source": "chunk_cooccur"},
    {"kw": "macos", "npmi": 0.58, "cooccur_count": 11.0,
     "via_seed": "brew", "chunk_df": 18, "source": "chunk_cooccur"}
  ]
}
```

Interpretation:

- `results[]` 是**候选关键字**，每条带 `npmi`（关联强度）、`cooccur_count`
  （原始共现次数）、`via_seed`（从哪个 seed 扩展来）、`chunk_df`（该候选
  词自身的 chunk 频次，hub 词判别信号）、`source`。
- 排序为 `(via_seed, -npmi, kw)`。取前几个高 `npmi` 候选拼进 ripgrep。
- 想剔除 hub 词：过滤掉 `chunk_df` 过高的候选（阈值由 caller 定）。
- 空 `{"results": []}` 意味着索引缺失，或 seed 无共现信号（例如 hub seed
  的所有 pair 在写入期被 npmi 门槛过滤）。此时重建索引，或改用
  `--mode heading`。

## When *not* to use this pattern

- **The user gave a natural-language question, not a keyword.** Run
  `grga suggest --intent reformulate` first to convert it to concrete
  keywords, then use those with `related`.
- **The index has not been built.** `related --mode graph` will return
  `{"results": []}`. Build the index, or改用 `--mode heading`（走
  `heading_docs`，仍返回文档）。
- **You want a specific *chapter* across files** — that is
  `--mode heading` (see the `heading` example in the sidebar file
  `../SKILL.md`).
- **You want the list of directories a seed hits** — `--mode dir` 已在
  v3 移除。改用 `grga search "<seed>" --json`，caller 侧按目录聚合命中
  路径（Unix pipeline）。

## Tuning the read-side npmi floor（defaults are usually right）

写入期资产已固化下界 `npmi >= 0.2`（build 门槛，见
`docs/design/plan-2026-07-12-keyword-primary.md` §3.3）。`related --mode
graph` 的 `--min-npmi` 是**读侧地板**：只能**收紧**（如 `0.3` 只保留强
关联），**不能放松到 0.2 以下**（低于 0.2 的 pair 根本没被记入资产）。

- 小型合成语料上候选很稀 → 写入期门槛 0.2 已经过滤掉了噪声边；重建索引
  时可用 `grga index build --min-npmi-build 0`（写入期放宽下界），而不是
  在读侧调 `--min-npmi`。
- 想只看强关联 → 抬高 `--min-npmi`（如 `0.3`）。

## Composition tip

把候选关键字喂回 ripgrep 或 `grga search`，再用 `expand` 展开片段：

```python
related = json.loads(run("grga related 'brew' --mode graph --db /tmp/grga.db --json"))
seeds = ["brew"] + [r["kw"] for r in related["results"][:5]]
# 用候选关键字拼 OR 查询找原文，再 expand top 命中
hits = json.loads(run(
    "grga search " + " ".join(f"'{s}'" for s in seeds) +
    " --implicit-or -d tests/fixtures/cross_heading --json"))
top = hits["matches"][0]
ctx = json.loads(run(
    f"grga expand '{top['path']}:{top['line_number']}' "
    f"-d tests/fixtures/cross_heading --level chapter --json"))
print(ctx["text"])
```
