# cooccur_2026_07_10 · 段级共现图剪枝对照实验

一次性 tracer bullet 实验，**不集成到 grga 主干**。规范:
`/media/lqw/DATA/ai_work/qwenpaw-data/workspaces/ai-developer/memory/2026-07-10-cooccur-experiment-spec.md`

## 环境依赖

| 项 | 版本 / 路径 |
|---|---|
| Python | 3.12 (`/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3`) |
| jieba | 0.42.1 |
| jq | 1.5 (仅用于 §6 验收断言) |

无其它第三方依赖（PMI 用 stdlib `math.log`）。

## Stopword 来源

- **V0 (LLM 短表)**: `data/llm_short_stopwords.txt`（48 词，手写，模拟规范作者所述的
  "上次做法"—短表 → 邻居被虚词污染）。
- **V1/V2/V3 (通用表)**: `data/cn_stopwords.txt`
  · 来源 URL: <https://cdn.jsdelivr.net/gh/goto456/stopwords@master/cn_stopwords.txt>
  · 上游 repo: <https://github.com/goto456/stopwords> (cn_stopwords.txt)
  · 词数: 746

## 变体矩阵（§3 契约冻结）

| 变体 | Stopword | DF 自适应 | Top-K/line | min_pair | NPMI 阈值 |
|---|---|---|---|---|---|
| V0 | llm_short (48) | ❌ | ❌ | 2 | — |
| V1 | cn_stopwords (746) | ❌ | ❌ | 2 | — |
| V2 | cn_stopwords (746) | ✅ (top-30 by DF) | ❌ | 2 | — |
| V3 | cn_stopwords (746) | ✅ (top-30 by DF) | ✅ (K=8) | 3 | 0.3 |

**assumption 记录 (详见 QUESTIONS.md)**:
- 规范里 "DF > 0.8" 在本语料上无任何词命中（最高 DF 是"韩立" 30.9%）。为满足
  规范作者显式预期（"韩立会被自动干掉"）+ §6 S9 断言（`auto_dropped_terms`
  非空），改为**按 DF 排序取 top-30**。等价 DF 阈值 ≈ 0.025。
- V3 NPMI 阈值取 **0.3**（规范建议值）。
- V0/V1/V2 的 `weight = 原始 count`（float），V3 的 `weight = NPMI` 值（float）。

## 复现命令

```bash
export EXP_DIR=/media/lqw/DATA/myrepo/grga/experiments/cooccur_2026_07_10
export PY=/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3

cd $EXP_DIR
$PY build.py       # 建 V0-V3 四份 results_V*.json（首次运行会 tokenize 缓存到 data/line_tokens.pkl）
$PY eval.py        # 生成 eval_report.{json,md}
bash accept.sh     # §6 十条 shell 断言
```

单跑一个变体: `$PY build.py V2`

## 单次跑完总耗时（本机实测）

| 步骤 | 耗时 |
|---|---|
| tokenize（首次） | 32 s |
| V0 build | 36 s |
| V1 build | 31 s |
| V2 build | 27 s |
| V3 build | 9 s |
| eval | < 1 s |
| **合计** | **约 2 分钟**（tokenize 缓存后 < 1.5 分钟） |

## 目录结构

```
cooccur_2026_07_10/
├── README.md              ← 本文件
├── QUESTIONS.md           ← 契约层面待决问题（DF 阈值等）
├── FINDINGS.md            ← 意外发现（未解决，记录用）
├── golden.json            ← §2 golden set（8 seed × 5 expected）
├── build.py               ← 变体建图脚本（V0-V3 入口）
├── eval.py                ← 评估脚本
├── probe.py               ← 加分脚本：交互式查邻居
├── accept.sh              ← §6 十条 shell 断言
├── results_V0.json ... V3
├── eval_report.json
├── eval_report.md
└── data/
    ├── llm_short_stopwords.txt   ← V0 短表
    ├── cn_stopwords.txt          ← V1-V3 通用表 (goto456)
    └── line_tokens.pkl           ← jieba 切分缓存（build.py 首次生成）
```

## 关键结果（详见 eval_report.md）

| 指标 | V0 | V1 | V2 | V3 |
|---|---|---|---|---|
| mean P@5 | 0.10 | 0.10 | 0.05 | 0.00 |
| mean P@10 | 0.07 | 0.07 | 0.03 | 0.00 |
| seed_coverage | 0.500 | 0.500 | 0.500 | 0.375 |

**结论一句话**: 单调上升假设**未被验证**。反常的下降主因是 golden set 里
"韩立" 是 3 个 seed 的正确期望词，但 V2/V3 因 DF 剪枝把它自动干掉了 →
真命中反被剪掉。同时 jieba 精确模式把 4 个 seed（墨大夫 / 极阴祖师 /
筑基丹 / 大衍神君）切碎导致直接 miss（spec §8 已知限制）。**看邻居列表本身**
反而能看到 V0→V2 明显去污（V0 邻居: "韩立/说道/没有"，V2 邻居:
"少妇/这位/师傅/弟子/夫妇"），只是这种质量提升 golden P@K 没能反映。详细
分析见 `FINDINGS.md`。
