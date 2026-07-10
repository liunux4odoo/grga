# cooccur_2026_07_10_v2 · V4 章节级共现单项实验

**规范**: `/media/lqw/DATA/ai_work/qwenpaw-data/workspaces/ai-developer/memory/2026-07-10-cooccur-experiment-v2-spec.md`
**前作**: `../cooccur_2026_07_10/`（V0-V3 保留不动）
**日期**: 2026-07-10
**执行**: code agent（tracer bullet 脚本级）

## 单一变量

本次只改一个变量：**共现窗口定义**。
- V2_retok = 前作 V2 的整行窗口，但换成含自定义 jieba 词典的新 tokenizer
- V4 = 章节级窗口（第 X 章 切分，一章 = 一次共现窗口）

DF-adaptive 剪枝在 V4 中**不启用**（规范 §2.3），因此 V4 vs V2_retok 的差异**只在窗口定义**。

## 结论

**V4 top-30 里有 0 个实体** —— 位置约束假设 **不成立**（章节窗口太宽，韩立几乎全章出现，pair 权重退化为章节频次）。见 `hub_diff.md`。

## 复现

```bash
# 1. 建图（V2_retok ~24s，V4 ~86s）
python3 build.py            # 跑 V2_retok + V4
# 或 python3 build.py V4    # 只跑一个

# 2. 评估（P@5 / P@10 表）
python3 eval.py

# 3. 生成核心产物 hub_diff.md（韩立邻居对比）
python3 hub_diff.py

# 4. 验收断言
bash accept.sh
```

## 依赖

- Python 3.12（`/media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3`）
- `jieba`、`scipy`、`numpy`
- 语料：`/media/lqw/DATA/sync/temp/《凡人修仙传》（精校全本）作者：忘语.txt`

## 关键实现说明

### Tokenizer

- jieba 精确模式 + `data/user_dict.txt`（19 词，含规范强制的 12 词 + 7 词补充，见 QUESTIONS.md Q2）
- 缓存 `data/line_tokens.pkl`，version=2，`corpus_mtime + dict_mtime` 双 key 使 stale 缓存失效

### Stopword

- `data/cn_stopwords.txt`（哈工大风格 ~1200 词，与前作一致）

### V4 章节切分

- 正则：`^\s*第[〇零一二三四五六七八九十百千万\d]+章[\s\u3000　]`
- 匹配 2004 章节标题；含"章节 0"（前言）共 2005 个章节
- 章节标题行本身不参与共现（见 QUESTIONS.md Q5）
- 单章 token set 上限：10000（超过时 top-500 by DF 截断，实测触发 1 次：最末章 27769 token）

### V4 图构造（内存优化）

朴素方法 `Counter[(a,b)] += 1` 会 OOM（预估 pair 事件 ~7e8）。改用 scipy 稀疏矩阵：

1. 按 chapter-DF ≥ min_pair_count=3 预筛（27429 / 69160 terms 通过）
2. 构建 (chapter × term) 二值稀疏矩阵 `M`（nnz ≈ 1.05M）
3. 分块计算 `M.T @ M`（block=512），仅保留权重 ≥ min_pair_count 的上三角非零项写入邻接表

### V0 数据来源

`hub_diff.md` 的 V0 部分直接引用 `../cooccur_2026_07_10/hanli_probe.json`（**未重跑**，规范 §3.3）。

## 产物清单

- `results_V2_retok.json` / `results_V4.json` —— schema 同前作 §4.1，V4 附加 `total_chapters` / `effective_chapters` / `notes.chapter_truncations`
- `eval_report_v2.json` / `eval_report_v2.md`
- `hub_diff.md` —— **本实验唯一目的的可读输出**
- `QUESTIONS.md` —— assumption 记录
- `FINDINGS.md` —— 意外发现（如有）

## 契约与 assumption

见 `QUESTIONS.md`。核心 assumption 摘要：
- Q2 词典扩展到 19 词（银月 等）
- Q5 章节标题行不参与共现
- Q7 严守 min_pair_count=3，不擅自放宽
- Q8 V0 数据直接引前作 hanli_probe.json
