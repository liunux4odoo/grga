# QUESTIONS.md — v2 实验的疑问与 assumption

> 沟通规约：契约疑虑先做（`assumption:`），等 review。

---

## Q1. 章节切分正则的调整（无调整，规范原始正则可用）

**规范原文** (§2.3):
```
^\s*第[〇零一二三四五六七八九十百千万\d]+章[\s\u3000　]
```

**实测**：全书 145061 行，正则匹配到 **2004** 个章节标题；抽样前 5 章、后 3 章全部命中中文数字章号。

**assumption**: 直接采用规范正则，未做修改。

---

## Q2. 自定义词典的扩展

**规范原文** (§2.1): 至少 12 词，"加多不加少"。

**assumption**: 词典共 19 词。规范强制 12 词 + 补充 7 词（`银月`、`李化元`、`韩立`、`曲魂`、`墨彩环`、`严氏`、`关门弟子`），补充理由：

- `银月` 在 jieba 精确模式下会被切成 `银/月`（单字被 valid_token 丢弃），如不加则 seed miss，V2_retok / V4 里 `银月` 都进不了图。这是与 §2.1 "让 seed 不再被切碎" 精神一致的补充。
- `李化元`、`韩立`、`曲魂` 已经能被 jieba 正确整词切出（实测），加进词典是防御性冗余，避免不同 jieba 版本行为漂移。
- `墨彩环`、`严氏`、`关门弟子` 是"墨大夫"的 golden expected，属于人物专名，加词典能减少 golden 命中被切碎的次生 miss。

**未加**：其他人物专名（如"李厚有""厉飞雨""紫灵"等）——本轮只关心 golden 里出现的 seed 与 expected 词汇。

---

## Q3. V2_retok 的输出 schema

**规范原文** (§3.2): `results_V2_retok.json` schema 同前作 §4.1。

**assumption**: V2_retok 的 `corpus_stats` **不含** `total_chapters` / `effective_chapters`（这两个字段是 V4 章节级独有的），保持与前作 V2 完全同 schema。V4 才附加两个新字段。

---

## Q4. V2_retok 的 DF 自适应剪枝行为

**规范原文** (§2.4): V2 用新 tokenizer 重跑一次。

**assumption**: V2_retok 的 DF-adaptive 逻辑与前作 V2 完全一致（`AUTO_DROP_TOP_N=30`，即前作 QUESTIONS.md Q1 中已 assumption 的语义）。这样 V4 vs V2_retok 的差异只有"窗口定义"这一个变量，符合规范"纯粹的窗口对比"意图。

V4 内部**不启用 DF-adaptive**（规范 §2.3 明文），因此 V4 的 `auto_dropped_terms=[]`。

---

## Q5. V4 单章 token set 去重规则

**规范原文** (§2.3): "每章内所有非空行的 valid tokens 合并去重成一个 token 集合"

**assumption**:
- 合并去重发生在 **stopword / user-dict / valid_token 过滤之后**
- 章节内 token set 是纯集合语义，无频次信息（"一个章节 = 一次共现窗口"，pair 权重是"该 pair 出现在多少章"）
- 章节标题行本身**不参与共现**（规避标题词与正文的无意义 pair）—— 若规范作者本意包含标题，我可以放开，但集合去重后标题词权重最多也就 +2004 章。目前采取保守做法：**排除标题行**（更符合"标题是分界符不是内容"的直觉）

---

## Q6. 单章 token 数过多的截断

**规范原文** (§4): "> 10000 token 时可 top-500 by count 截断，必须在 report notes 里说明发生了几次截断"。

**assumption**: 采取阈值 **> 10000 token 时按全局 DF 降序保留 top-500**（与前作 V3 top-K 一致的截断 key）。实测：凡人绝大多数章节 token set 都远 < 10000，**仅最末章（第一千九百九十九章「黑日」，27769 token，含大量结局长文）触发 1 次截断**，notes 里记为 `"chapter_truncations: 1"`。

**补充 assumption（review 后追加）**：截断排序用的 `term_ch_df` 是在 pass-1 里**流式累加**的，被截断章节看到的是"到当前章节为止的部分累计 DF"，而非全书 DF。因此截断结果**依赖章节迭代顺序**（非幂等）：若超长章出现在开头，其 DF 排序会退化为近似字典序。本次凡人仅最末章（i=2004）触发 1 次截断，此时 `term_ch_df` 已累加前 2004 章 ≈ 全量，无实际影响。若下一轮需要稳定/幂等的截断，应先做一次完整 DF pass 再排序。

---

## Q7. min_pair_count=3 对稀疏图的影响

V4 里 pair 权重的量纲从"行数"变成"章数"（2004 章 vs 145k 行）。规范 §4 边界维度已经预留了"图崩塌 total_edges < 100 提示放宽 min_pair_count"的处置，无需契约层修改。

**assumption**: 严格按 min_pair_count=3。若崩塌就在 notes 里如实提示（不擅自放宽）。

---

## Q8. V0 数据来源

**规范原文** (§3.3): "V0 数据直接从前作 `results_V0.json` 读取"。

**问题**：前作 `results_V0.json` 的 queries 里**没有"韩立"**（前作 golden 没把"韩立"当 seed）。前作在 `hanli_probe.json` 里单独探测过韩立在 V0/V1/V2/V3 的 top-30。

**assumption**: `hub_diff.md` 里的 V0 部分**直接引用 `../cooccur_2026_07_10/hanli_probe.json` 里的 V0.neighbors[:30]**，不重跑 V0。
