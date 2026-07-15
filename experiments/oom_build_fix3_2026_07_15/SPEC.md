# SPEC — oom_build_fix3_2026_07_15 (PR-4: 流式化 Phase A + Phase B)

> **状态**：drafting，PR-4 实施中。
> **设计**：`docs/design/plan-2026-07-15-build-memory-oom.md` §8 PR-4
> **前置**：PR-1 + PR-1.5 + PR-2 已 landed（`b6db151`），M 档 peak 2474 → 747 MB
> **母方法学**：`docs/design/db-memory-optimization-lessons.md`

## 0. TL;DR

**目标**：消灭 `line_token_lists`（Phase A，~360 MB）与 `chunk_tokens`
（Phase B → Pass 2，~360 MB）两处 O(corpus) 内存驻留，把 M 档 peak
从 747 MB 降到 **< 400 MB**。

**策略**（低风险路径）：
1. **Phase A 流式**：`detect_ngram_phrases` 接 `Iterable[Iterable[str]]`
   + 可选 `n_lines_hint`；Phase A 改生成器喂 tokens
2. **Phase B / Pass 2 re-tokenize**：**不改 Python 逻辑**、不改 SQL —
   把 `chunk_tokens` 换成 on-the-fly re-tokenize。语义 **bit-exact by
   construction**（同 tokenizer + 同文本 = 同 tokens，确定性）

## 1. 判据

| C | 内容 | 目标 |
|---|---|---|
| C1 | M 档总 peak RSS | 747 → **< 400 MB** |
| C4-a | keyword_pairs 表 vs baseline | **bit-exact** |
| C4-b | keyword_stats / keyword_docs / scope_stats / auto_ngram_dict 表 | **bit-exact** |
| C4-c | pytest 149 项 | **全绿** |
| C6 | wall-clock M | **≤ 1.5× baseline**（+1 tokenize pass 约 +60s，预期 ~680s vs 620s = 1.10×） |

## 2. 数据流对比

### 现状（PR-2 之后）

```
Phase A iter 1: 全语料 tokenize → List[List[str]] (360 MB) → detect_ngram → del
Phase A iter 2: same
Phase B: 全语料 tokenize → chunk_tokens: Dict (360 MB, 持有到 Pass 2 末)
Pass 1.5: _derive_scope_stopword_candidates(chunk_tokens)
Pass 2: chunk_tokens.get() 消费 → keyword_stats/docs 聚合 + _build_chunk_kws staging
```

**峰值**：Pass 2 中 chunk_tokens (360) + pass-2 结构 (200) + jieba (70) + base (90) = **~720 MB**

### PR-4 之后

```
Phase A iter 1: 生成器 tokenize → detect_ngram consume(gen)  (每次只 1 line in memory)
Phase A iter 2: same
Phase B(streaming): 生成器 tokenize → 累积 stopword-df counter (~KB) → derive stopwords
Pass 2: 生成器 tokenize (finalized dict) → keyword_stats/docs/staging（**Python 逻辑一字不改**）
```

**峰值**：Pass 2 中 pass-2 结构 (200) + jieba (70) + base (90) = **~360 MB**

## 3. 语义等价保证

- **detect_ngram_phrases** 已单遍消费 `line_tokens`，`Iterable` 替换 `List` 是 API 直落兼容
- **Phase B re-tokenize**：现在 `chunk_tokens[(doc_id, chunk_id)] = toks` 由 `tokenize_hybrid_with_pos + merge_auto_phrases` 生成；PR-4 用同一对函数、同一 `jieba_tok` + `phrase_set` 状态就地 re-generate → **确定性、bit-exact**
- **stopword derivation**：`_derive_scope_stopword_candidates(chunk_tokens)` 输入是 dict、输出是 frozenset。PR-4 拆一个 `_derive_scope_stopword_candidates_from_df(counter, n_chunks)`，老函数保留（喂 counter+n）当薄壳，测试兼容
- **Pass 2**：只把 `toks = chunk_tokens.get((doc_id, chunk.chunk_id), [])` 改成 `toks = tokenize_chunk_inline(chunk)`，其它零改动

## 4. 实现清单

1. `detect_ngram_phrases(line_tokens: Iterable[Iterable[str]], *, ..., n_lines_hint: Optional[int] = None)` — 用 hint 打进度总数；`n_lines = n_lines_hint or 0` 已够
2. Phase A 主循环重写为 `def _stream_line_tokens(): yield toks` 生成器，配合 `total_scanned_chunks` 作 hint
3. `_derive_scope_stopword_candidates_from_df(df: Counter, n_chunks: int)` 拆出来
4. Phase B 首遍：生成器 tokenize → 累积 stopword-df；不建 chunk_tokens
5. Pass 2：内嵌 `tokenize_chunk_inline` — 用同一 jieba_tok/phrase_set，逐 chunk tokenize + 逐 chunk 消费
6. `ngram_finalize_end` 后不再报 `len(chunk_tokens)`（改报 `total_scanned_chunks`）

## 5. 判据裁决路径

- **S 档 smoke**（快，约 60s）：build 输出 `.db` diff 与 baseline 比对 keyword_pairs / keyword_stats / keyword_docs / scope_stats / auto_ngram_dict 全部 bit-exact
- **M 档**（~10 分钟）：C1 peak + C6 wall
- **149 项 pytest**：全绿

## 6. 失败预案

- 若 C1 peak > 500 MB → 排查未消灭的其它 O(corpus) 结构（可能是 SQLite temp buffer 内 spill 上限、jieba 中间状态、Python 字符串缓存）
- 若 C4 任一 diff → re-tokenize 的确定性假设不成立 → 回退到"chunk_tokens dict 仅在 Pass 2 期间存在，Phase B 顺带完成 stopword" 方案（保留 dict，仅 Phase A 流式化，peak 减半而非再降）
