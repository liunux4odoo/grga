# QUESTIONS.md — pending clarifications for spec author

> 记录规范里没写清或与语料实测数据存在张力的点。按沟通规约，先做（`assumption: ...`），等 review。

---

## Q1. DF > 0.8 阈值与"韩立会被自动干掉"的预期不一致（**契约级张力**）

**规范原文** (§3, V2):
> DF 自适应 (DF > 0.8) + 备注："凡人里韩立应该会被自动干掉"

**实测数据** (jieba 精确切分 + 长度≥2 过滤后，无任何停用词):

```
韩立    df_all=0.3089    df_eff=0.3221    count=44811
没有    df_all=0.0846
一下    df_all=0.0783
说道    df_all=0.0760
什么    df_all=0.0755
...
(top-30 全部 df_all < 0.10)
```

**结论**: 全书 145061 行里没有任何一个 token 的 DF 超过 0.8。
即使阈值降到 0.1，也只有"韩立"一个词命中。

**与 §6 S9 断言的冲突**:
```
jq '.auto_dropped_terms | length > 0' results_V2.json | grep -q true
```
按 DF > 0.8 直译，V2 的 `auto_dropped_terms` 必然为空，S9 fail。

**assumption（等 review 时纠正）**:
我把"DF > 0.8"解读为 **"极端高频（对语料而言）的自适应剪枝"** 而不是字面
80% 行覆盖率。具体做法：**取有效行内 DF 排名 top-30 的词作为动态停用词**
（等价于阈值 ≈ DF > 0.025）。这样能满足：
1. 规范作者显式预期的"韩立会被自动干掉" — 命中
2. §6 S9 断言 — `auto_dropped_terms` 非空
3. hypothesis 精神 — V2 相比 V1 剪掉了更多语料特异的高频虚词

如果规范作者本意就是字面 0.8：请指示，我会：
- (a) 把 V2/V3 的 auto_dropped 改成空，同时 S9 需要放宽为 `>= 0`
- (b) 或降低阈值到能命中的实测值 (~0.30 干掉唯一命中"韩立")

---

## Q2. V3 NPMI 阈值

**规范原文** (§3, V3): "阈值 1.5 是纯 PMI 的等价（自己映射到 NPMI 范围），
你可以用 `NPMI > 0.3` 之类合理阈值——**在报告里说清楚用了什么阈值**"

**assumption**: 直接采用规范建议的 `NPMI > 0.3`，并在 README 里注明。

---

## Q3. 邻居"weight"字段在 PMI 变体下的语义

**规范原文** (§4.1): "weight: 图上边权重（PMI 或原始 count，看变体）"

**assumption**:
- V0/V1/V2: `weight = 原始 co-occurrence count`（int-as-float，稳定排序）
- V3: `weight = NPMI 值`（float），保留 4 位小数

这样返回结构完全一致（都是 float），但 V3 报告的 weight 语义不同，README 会说明。

---

## Q4. seed 分词与图匹配

seed 词如 "极阴祖师"、"大衍神君" 在 jieba 精确模式下会被切成两个 token，不
会作为一个整体进入词表。§8 (1) 已经说这是预期行为。

**assumption**: seed 严格按整词查图；如果整词不在图内，`seed_in_graph=false`，
不做 fallback 拼接。这与 §8 一致，报告里会指出哪些 seed miss。

---

## Q5. "effective_lines" 定义

**规范原文** (§4.1): `corpus_stats.effective_lines` 无定义。

**assumption**: 有效行 = 分词后至少有 2 个 valid token 的行（否则不产生任何
共现边，对图无贡献）。`total_lines` = 输入文件 `\n` 切分后的 raw 行数。

---

## Q6. 每行 pair 生成方式

行内 token 全组合会产生 C(n,2) 条边，对长行（p99 = 172 char，可能 ~40 token）
是 780 pair/行。全书累计粗估几千万 pair。**assumption**: 全组合无窗口限制
（"行"就是窗口）；每行 token 集合**先在过滤/停用词/auto-drop 后去重**再组合
避免自环。

V3 的 "Top-K per line" 实际做法（**规范未指定，我做了如下选择**）:
- 由于每行 token 已经去重，"行内出现次数"恒等于 1，无法作为排序 key
- 实际按**全局 DF 降序**取 top-K（K=8）：等价于"每行保留剩余词表里最常见的
  8 个词做 pair"（auto_dropped 前 30 已经去掉，所以这里是"top-31..top-N"的
  头部）。目的是**抑制长行主导**（长行本来就更容易带上稀有 token 组合成
  低频 pair 污染），而不是"保留最有信息量的低 DF 词"
- 一个合理的**相反策略**是按 DF 升序取 top-K（保留最"稀有/信息量高"的词）——
  这两种方向都合理，但会给出完全不同的图。当前实现选前者
- 排序 key: `(-df, term)`，稳定排序

如果规范作者本意是相反方向（低 DF 优先），改一个符号即可：build.py 里把
`key=lambda t: (-prelim_df[t], t)` 改成 `key=lambda t: (prelim_df[t], t)`。
