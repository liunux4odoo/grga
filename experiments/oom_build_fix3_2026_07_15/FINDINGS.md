# FINDINGS — oom_build_fix3_2026_07_15 (PR-4)

**状态**：已 landed 主干，判据 C1/C4/C6 全绿。

## TL;DR

> Phase A 流式化 + Phase B/Pass 2 re-tokenize（去 `chunk_tokens` dict） →
> **M 22MB 集成 build peak 从 745 MB 降到 517 MB (-30.6%)**，全部持久表
> **bit-exact vs baseline**（S: 6 表严等；M: 6 表严等，共 480k+ 行），
> wall 1.07× (686/643 s)。综合 PR-1/1.5/2/4：**2474 → 517 MB (-79.1%)**。

## 判据裁决

### C4 — 所有持久表 bit-exact

M 22MB 档 6 张持久表逐行严等（`updated_at` 除外）：

| 表 | 行数 | 差异 |
|---|---:|---:|
| keyword_pairs | 293824 | 0 |
| keyword_stats | 105038 | 0 |
| keyword_docs | 105038 | 0 |
| auto_ngram_dict | 39052 | 0 |
| scope_stats | 1 | 0 |
| docs | 1 | 0 |

**S 22MB 同样 bit-exact**（179k+32k+32k+2.8k+1+1 行全等）。

C4-c pytest 149 项全绿（含 test_derive_scope_stopword_candidates、
detect_ngram_phrases 相关 + 全部端到端 build tests）。

### C1 — M 档总 peak RSS

| 版本 | peak | Δ | 累计 |
|---|---:|---:|---:|
| pre-optimisation | 2474 MB | — | — |
| PR-1 后 (pair npmi SQL) | 2471 MB | -3 | -0.1% |
| PR-2 后 (ngram TEMP TABLE) | 747 MB | -1724 | -69.8% |
| **PR-4 后 (streaming) 【本轮】** | **517 MB** | **-230** | **-79.1%** |

*(PR-1.5 db-temp 是 fstype safety fix，不直接降 peak)*

### C6 — wall-clock

- baseline (PR-2 之后)：643 s
- PR-4：686 s
- 比值 **1.07×**（远低于 1.5× 硬阈）

+43 s 来自 Pass 2 从 `chunk_tokens.get()` 变成 on-the-fly re-tokenize
（约 +1 tokenization pass over corpus，与预期一致）。

## 关键工程

### F1 — 归因修正

PR-2 的 FINDINGS 猜"line_token_lists ~500MB 是 Phase A 新地板"是**错的**。
实测发现：
- token list 只占 ~360 MB（55 B/tok × 459k tok on S 外推）
- 真正 M 档 peak (745 MB) 主导者是 **Pass 2 期间 chunk_tokens (~360 MB)
  与 pass-2 结构 (~200 MB) 叠加**
- Phase A iter 1 峰 710 MB ≠ 整体 peak；整体 peak 在 materialise/pairs 阶段

sampler envelope 揭示（PR-2 前）:
```
Phase                min..max MB
ngram_iter_1 (A)     93 → 710
ngram_iter_2 (A)     452 → 592
ngram_finalize (B)   512 (chunk_tokens 稳态)
materialise (P2)     512 → 744    ← 真峰
pairs_scan           737 → 747
```

### F2 — re-tokenize 是"bit-exact by construction"

Pass 2 从"读 `chunk_tokens[(doc_id, chunk_id)]`"改成"用同一 `jieba_tok`
+ `phrase_set` 重新 tokenize"，**语义完全等价**：jieba 与 regex 都是确定
性函数，同输入 + 同状态 = 同输出。这是本 PR 敢做的核心依据 —— 避开 SQL
3b DELETE 那条复杂路径。

代价：+1 tokenization pass over corpus（M 档 ~43 s / +7%）。

### F3 — 测试参数陷阱

早期用 `min_pair_count=1, min_npmi=0.0` 测试时 build 无限爆炸（DB 到
1.5 GB 仍未结束），一度以为是 PR-4 回归。实际是**参数陷阱**：
- `min_pair_count=1` 关闭 write-time 严过滤
- 真实语料下每 chunk 有 100+ 关键词，pair 空间指数爆炸
- baseline 同参数**也**爆炸（验证过）

**教训**：等价性测试务必用**生产默认参数**（自适应 `min_pair_count`），
才是真实场景。

## 落地内容

`src/grga/graph/build.py`:
- `detect_ngram_phrases`：签名 `Iterable[Iterable[str]]` + 可选
  `n_lines_hint`；`len()` 用 try/except fallback（generator 兼容）
- Phase A 主循环：line-list → 生成器 `_stream_iter_tokens()`
- Phase B：`chunk_tokens: Dict` → 流式 `stopword_chunk_df: Counter`
- Pass 2：`chunk_tokens.get()` → 内联 re-tokenize
- 新函数 `_derive_scope_stopword_candidates_from_df(df, n_chunks)`;
  老函数保留为薄壳（测试兼容）

## 遗留

- ✅ M 档 peak 517 MB，100 MB 语料外推 ~2.3 GB，300 MB 语料 ~7 GB
- ⏭️ 500 MB+ 语料仍需 PR-5（如需）：可能路径是 Pass 2 也流式化（现在
  Pass 2 尚有 ~200 MB pass-2 结构 + jieba 70 MB + base 100 MB），
  或迁 jieba 到子进程完全释放
- ⏭️ PR-3（SQLite spill sweep）：peak 已不是 spill 主导，收益进一步下降

## 复现

```bash
PY=/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3
cd experiments/oom_build_fix3_2026_07_15

# 1) 用 /tmp/kilo/build_m.py 构建 baseline + PR-4 的 M 档 db
# 2) 等价性 diff
$PY equivalence_check.py /path/base_m.db /path/pr4_m.db
# 期望：6 表全 PASS
```
