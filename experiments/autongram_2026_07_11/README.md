# autongram_2026_07_11 —— 无监督自动词典分词实验

**状态**：done. 判据 C1/C2/C3 **strong pass**，C4 阈值口径需修订（见 FINDINGS §F8）。建议进入主干 opt-in 阶段。

**修订记录**：2026-07-11 v2 —— NQ2（`面壁者`/`破壁人` 未召回）根因定位为 detection 停用词硬边界过激；NQ5（ASCII 拼接）实现 bug 一并修正。所有报告基于修复后重跑。

## 一句话结论

**用"jieba + 频次+NPMI+分支熵 迭代式短语发现"替代手工 user_dict 是可行的**：三体语料上迭代 2 轮让 seed 整词保留率从 56.25%（裸 jieba）升到 **100%**（16/16），把 baseline_regex 20.74% 的长句碎片污染降为 0.00%，纯数学判据 + 26 字核心虚词集（无语义先验）。

## 产物索引

| 文件 | 内容 |
|---|---|
| [REPORT.md](REPORT.md) | **过程/关键发现/结论综述（推荐先读）** |
| [SPEC.md](SPEC.md) | 实验规范（含 Q1-Q5 冻结的默认阈值） |
| [FINDINGS.md](FINDINGS.md) | F1-F7 发现 + C1-C4 判据裁定 + 主干迁移建议 |
| [seed_integrity.md](seed_integrity.md) | 8 golden seed 整词保留率对比（regex vs naive vs auto iter1-3） |
| [length_distribution.md](length_distribution.md) | 关键词长度分布（长句碎片消除证据） |
| [topk_usability_santi.md](topk_usability_santi.md) | 7 seed × top-5 手工判定 |
| [build.py](build.py) | Tracer bullet 实现 |
| data/golden_santi.json | 三体 8 seed + 17 phrase_check 词表 |
| data/auto_dict.jsonl | iter3 最终字典（767 短语） |
| results_baseline_regex.json | 主干正则方案结果（对照） |
| results_naive_jieba.json | 裸 jieba 方案结果（对照 B） |
| results_auto_ngram_iter{1,2,3}.json | 主变量 A 三轮迭代结果 |

## 如何复跑

```bash
cd experiments/autongram_2026_07_11

# 三组对照
python build.py --mode baseline_regex
python build.py --mode naive_jieba

# 迭代式 auto_ngram（rebuild 会清空 auto_dict）
python build.py --mode auto_ngram --rebuild --out-suffix _iter1
python build.py --mode auto_ngram --out-suffix _iter2
python build.py --mode auto_ngram --out-suffix _iter3
```

Python 路径按 AGENTS.md 硬规则用 `/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3`。

## 关键数据速查

- 语料：三体三部曲 2.7MB / 12,059 行
- baseline_regex terms=72,478  |  jieba modes terms≈30,000（-58%，主要是消除长句碎片）
- iter1 建 2124 短语（含 章北海/云天明/面壁者/破壁人/万有引力号/自然选择号/希恩斯 等实体）
- iter2 词典累计 2615，phrase_check **16/16 = 100%**，8 seed 全部入图
- iter3 累计 2683，新增 68 词，收敛
- 单趟耗时：jieba 5.5s（含 0.9s 冷启 + 1.8s detection），regex 0.5s
- C1 ≥ 80% ✅ **100%**  |  C2 ≥ 60% ✅ **87.5%**（宽判据）  |  C3 < 5% ✅ **0.00%**
- NQ2/NQ5 已修复；detection 用 26 字核心虚词集，图构建用 746 词表（两阶段解耦）

## 下一步

见 FINDINGS §"下一步（若进主干）"。**当前工作不主动进主干**——需要用户确认后启动 PR。
