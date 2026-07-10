# cooccur_2026_07_10_v3 · V5_npmi 单项实验

**规范**：`/media/lqw/DATA/ai_work/qwenpaw-data/workspaces/ai-developer/memory/2026-07-10-cooccur-experiment-v3-spec.md`
**前作**：`../cooccur_2026_07_10/`（V0-V3）、`../cooccur_2026_07_10_v2/`（V2_retok, V4）
**日期**：2026-07-10

## 单一验证问题

**换用 npmi 排序 + 去掉 DF-adaptive 剪枝**后，能否同时救 hub 和保住非 hub？

- 主判据（hub）：韩立 top-30 by npmi 里 entity 命中 ≥ 1
- 副判据（非 hub）：8 seed top-5 Useful% 均值 ≥ 60%

## 结论（tl;dr）

| 判据 | 结果 | 值 |
|---|---|---|
| 主判据（hub, 韩立 top-30 entity 命中） | ❌ 不成立 | **N = 0**（top-100 累计 = 0） |
| 副判据（非 hub, top-5 Useful%） | ✅ 成立 | **87.5%** ≥ 60% |
| 整体（两条同时满足） | ❌ 不成立 | 单一 ranking key 无法兼顾 hub 与非 hub |

**产品含义**：npmi 在非 hub seed 上表现极好，但 hub 无法靠单一 ranking
key 救回（数学根因见 `hub_diff_v3.md`）。下一步应走 **caller-side 按
`seed_df` 分流**（契约里已保留 `seed_df` 字段），或 §11 的其他 fallback。

## 复现

```bash
PY=/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3

# 1. 建图（含 npmi 计算，约 27s；首次 tokenize 约 +34s）
$PY build.py

# 2. 生成 hub_diff_v3.md + topk_usability_v3.md
$PY gen_reports.py

# 3. 验收 S1-S10
bash accept.sh
```

## 单变量隔离（Spec §2）

**只改两处**（相对 v2 V2_retok）:
1. `ranking key`: raw count `w` → `npmi(a, b)`（Bouma 2009）
2. `DF-adaptive pruning`: **整个删掉**

**沿用 v2 不变**:
- 窗口 = 行；tokenizer = jieba + 19 词自定义词典
- 停用词 = 保守表 746 词；`min_pair_count = 3`
- 9 个 seed / golden_v2.json

## 产物清单

- `results_V5_npmi.json` —— schema 见 spec §6.1
- `hanli_by_w_top30.json` —— 韩立按 raw w 排序 top-30（hub_diff 对照列）
- `hanli_golden_entity_ranks.json` —— 韩立与判定集合实体的 npmi 全排位置
- `hub_diff_v3.md` —— **核心产物**：3 列对比 + 数学根因 + 判定
- `topk_usability_v3.md` —— 8 个非 hub seed 逐词 E/D/F/G + Top-5/10/30 Useful% 汇总
- `QUESTIONS.md` —— 2 条 assumption + 14 个 `?` 分类不确定词
- `FINDINGS.md` —— 数学退化观察 + tokenizer 切分错误 + 性能
- `accept.sh` —— S1-S10 全过 ✓
- `build.py` / `gen_reports.py`

## 关键数字（一屏）

- 语料：total_lines=145061, effective_lines=139072, total_terms=76478,
  total_edges=1204783（co ≥ 3）
- 韩立 df=46252（0.333 × N，全语料最高频；hub 判定成立，见 QUESTIONS
  的 `assumption:hanli_df_ratio`）
- build 时间：26.2s（不含 tokenize）；全流程 27.9s（缓存命中）
- 判定集合实体在韩立 npmi 全排序中的位置：曲魂 208、李化元 566、墨大夫
  615、黄枫谷 4279、七玄门 12104、极阴祖师 12612（全部在 200+ 之外）

## 契约与 assumption

见 `QUESTIONS.md`：
- `assumption:hanli_df_ratio`（0.333 不等于字面"接近 N"，但仍是 hub）
- `assumption:top5_useful_100`（三个 seed top-5 = 100% 属 npmi 结构性优势，非 bug）
- 14 个 `?` 分类待复核（含 3 个 tokenizer 切分错误）
