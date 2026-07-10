# Graph L2 co-occurrence expansion: 2026-07-10 post-mortem

> **Status**: Design retrospective, not a plan.
> **Audience**: 未来任何想在 grga L2 层做"共现图 hub 处理"、"df 元数据接口"、
> "关键字图排序改进"的开发者(人或 agent)。
> **Bottom line**: 一整天四轮实验(V0/V1-3, V4, V5_npmi, α 手工推演) + 三次
> 哲学重定位得到的结论是**在共现关系图 + 单一 ranking 的框架下,hub seed
> 结构性无法救**;`graph nodes` df 接口净收益 7.7% 不足以证明引入合理。
> **本次立场:不做**。若未来重启这个话题,先读本文档 §7"重启前置条件"。

---

## 0. TL;DR

| 维度 | 结论 |
|---|---|
| 非 hub seed 的 L2 邻居质量 | ✅ **成立**(V5_npmi top-5 Useful% 均值 87.5%) |
| hub seed 的 L2 邻居质量 | ❌ **结构性失败**(V0-V5 四种变量组合都无法救) |
| DF-adaptive 剪枝救 hub | ❌ 反而伤害配角实体(F1 冲突) |
| 章节窗口救 hub | ❌ 退化**更严重**(F2 数学证明) |
| npmi ranking 救 hub | ❌ 奖励"独占叙述词",吊打关联实体 |
| 暴露 per-scope df 让 caller 自决 | ❌ α 推演净收益 7.7%,domain 词误伤 -2000 |
| **最终决策** | **方案 γ:不做 df 接口,不在共现图框架内继续救 hub** |

---

## 1. 背景与起点

### 1.1 触发问题

Caller agent 用 grga 检索"《凡人修仙传》里韩立有几个师傅"时,LLM 首轮直答含
事实错误(编造第三位师傅)。用户希望通过 grga 的 L2 关键字图扩展来复核和补
充关键字线索。测试发现:**用"韩立"作为 seed 走 `related`,返回的邻居质量
极差,基本是虚词和叙述配色词,没有配角实体**。

### 1.2 语料

主语料 `《凡人修仙传》（精校全本）.txt`:22MB,145061 行,单文件超过 grga
默认 `--max-file-kb=512`。晚间加入 `三体.txt`(2.7MB, 12059 行)测试跨语料
稳健性。

### 1.3 用户的产品哲学(讨论前提)

- **轻依赖**:不要 embedding、不要向量库;关键字 + 简单图就够
- **Agent-Native**:目标消费者是 agent 不是人;接口 caller-first
- **智能反思**:多轮迭代 + 反思逼近,不追求一次到位
- **抗先验判断**:grga 是原材料提供者,caller 是决策者;停用词表保守
- **命中扩展分层**:L1(物理邻近,免费)→ L2(图,离线预建)→ L3(LLM,贵)

**L2 定位(用户明确)**:L2 是 **L1 的补充层**,不是独立检索层。产出 = 关键字
集合,价值 = 跨文本联想。Caller 拿关键字回喂 L1。

---

## 2. 四轮实验时间线与关键数据

四轮实验均放在 `experiments/cooccur_2026_07_10*/`,每轮独立 tracer bullet
实现,不改 grga 主干。

### 2.1 V0-V3:行窗口 + 变量对比(cooccur_2026_07_10/)

**目的**:先建立 baseline,对比 raw-count / DF-cutoff / DF-adaptive 剪枝在
段级(逐行)共现下的效果。

**结果**:

| 变体 | Mean P@5 | 备注 |
|---|---:|---|
| V0(raw count) | 0.05 | 韩立邻居全高频虚词 |
| V1(DF > cutoff 剪枝) | 0.10 | 略好,但配角"李化元"被剪 |
| V2(DF-adaptive) | 0.05 | 剪多了,配角丢 |
| V3(more aggressive) | 0.00 | 图过稀,seed 都进不去 |

**关键发现 F1(记录于原 FINDINGS.md)**:golden set 的"韩立"覆盖与 DF 剪枝
**结构性冲突** —— DF 剪枝角度看韩立是噪声,但从 QA 角度"李化元关联谁"里
韩立就是信号。**传统 IR 停用词剪枝的直觉不适用图扩展。**

### 2.2 V2_retok + V4:章节窗口(cooccur_2026_07_10_v2/)

**目的**:换更大的共现窗口(章节),期望让配角实体被拉近韩立。同时 retok
把 jieba 精度提高到能捕获合成词。

**结果**:

- V2_retok: Mean P@5 = **0.15**(vs V0 的 0.05,3× 提升)—— 分词修正生效
- V4(章节窗口): 韩立 top-30 邻居 = **0 实体**,**比 V0 更差**

**关键发现 F2(数学证明)**:

```
w(韩立, X) ≈ #chapters_containing_both ≈ min(DF_chapter(韩立), DF_chapter(X))

∵ 韩立几乎每章都出现(DF_chapter(韩立) ≈ N_chapter)
∴ w(韩立, X) ≈ DF_chapter(X)

⟹ 章节窗口下韩立的邻居排序 = 全书 chapter-DF 排序
⟹ 高频虚词吊打关联实体
```

**技术副产物**: pair 空间 OOM(6.94 亿 pair 事件),用 `M.T @ M` 稀疏矩阵乘
法绕过。这是章节窗口相比行窗口的隐藏放大效应。

### 2.3 用户重定位(V4 后)

用户看到 V4 失败,明确提出:

> "L2 不用做独立检索。L2 是 L1 的补充,产出是关键字。Caller 拿关键字回喂 L1。"

这一句改变了整个问题空间:
- 之前的隐含目标:"L2 一次给出答案"(top-K 就是最终结果)
- 重定位后:"L2 给 L1 加词"(top-K 是下一步 L1 输入)

评估维度也随之改变 —— 从 P@K vs golden 变成 **top-K Useful%(手工判定)**。

### 2.4 V5_npmi:归一化 + 去 DF-adaptive(cooccur_2026_07_10_v3/)

**目的**:单变量测试 npmi ranking。用户产品哲学重定位后,判据也变:
- 主判据:**hub seed(韩立) top-30 by npmi 里是否有 entity**
- 副判据:**8 非 hub seed top-5 Useful% ≥ 60%**

**结果**:

- 副判据大胜:8 seed top-5 Useful% 均值 = **87.5%** ≫ 60%
- 主判据惨败:韩立 top-30 entity 命中 = **0**

**关键发现 F3(数学根因)**:

npmi 的隐性偏好是"奖励最纯粹共现"(co/df→1)。对 seed 类型的分化:

- **非 hub seed(墨大夫、极阴祖师…)**:最纯粹共现的邻居往往是配套语域词
  (如墨大夫 → 医术/把脉/妻室 co/df≈0.6-0.9),**正是 caller 想要的**
- **hub seed(韩立)**:最纯粹共现的邻居是**主角独占叙述词**(韩某/眉梢/微微
  一笑 co/df≈0.85-0.88),**不是关联实体**

配角实体在韩立的完整邻居排序中的位置:

| Entity | co-count | npmi rank / 17759 |
|---|---:|---:|
| 墨大夫 | 高 | 208 |
| 李化元 | 中 | 1247 |
| 极阴祖师 | 中 | ~2000 |
| ... | | 直到 12612 |

**结论**:npmi 数学性质 + hub seed 结构性能让**关联实体永远排在独占叙述词
之后**。这不是参数问题,是 ranking 结构性错误。

### 2.5 α 手工推演:df 接口价值(未做实验目录,只推演)

**背景**:V5 结果出来后,用户提出"不由 grga 判 hub,只暴露 df 数据(build 时
存 df 到图节点,caller 分页读取自决)"。

**加入三体后暴露的盲点**:

| Seed | 合并 df/N | 仅 sub-corpus df/N |
|---|---:|---:|
| 韩立 | 0.299 | 0.324(凡人内) |
| 罗辑 | **0.006** | **0.083**(三体内) |

**hub 是 sub-corpus 属性,不是全 corpus 属性**。12x 规模差把三体 hub 稀释
到看不出。方案改为 per-scope df。

**推演 6 seed × 3 archetype**(No df / Naive df / Aware per-scope df),token
成本估算:

| Seed | Type | No df | Aware(per-scope) | Aware 净收益 |
|---|---|---:|---:|---:|
| 韩立 | 极端 hub | 3000 | 1500 | +1500 |
| 罗辑 | 三体 sub-hub | 2000 | 1700 | +300 |
| **修士** | **凡人 domain 词** | 1500 | 3500 | **-2000** |
| 墨大夫 | 明确非 hub | 1500 | 1700 | -200 |
| 汪淼 | 三体 sub-hub 边界 | 2000 | 1900 | +100 |
| 二向箔 | rare(df=4) | 3000 | 1700 | +1300 |
| **总和** | | **13000** | **12000** | **+1000 (7.7%)** |

**核心 counter-example(修士 -2000)**:

- Caller 任务:"修士都有哪些类型/等级" → 想要修真语域词
- L2 会给出:结丹/元婴/掌门/前辈/道友 —— **正是 caller 想要的**
- Naive/Aware caller 用 df_ratio > 0.05 判 hub → 修士 0.056 触发 → skip
  L2 → rga 返回 8818 行 → token 爆炸
- **根因**:df 高有两种可能 —— hub(主角) vs domain(语域词)。df 单维度
  **结构性无法区分**

---

## 3. 决策路径

```
起点: "韩立师傅"问题 → L2 邻居质量差
  │
  ├─ V0-V3(行窗口 + DF 变量)
  │    └─ hub 邻居退化为高频虚词
  │    └─ F1: DF 剪枝与图扩展任务哲学冲突
  │
  ├─ V4(章节窗口 - 单变量)
  │    └─ hub 退化更严重(F2 数学证明)
  │
  ├─ 【用户重定位 1】L2 是 L1 补充层,产出关键字
  │    └─ 评估维度: P@K → top-K Useful%
  │
  ├─ V5_npmi(归一化 + 去 DF-adaptive - 单变量)
  │    └─ 非 hub 大胜(87.5%),hub 结构性失败(0)
  │    └─ F3: npmi 奖励独占叙述词,不是关联实体
  │
  ├─ 【用户提议】build 存 df,caller 分页读取自决 hub
  │
  ├─ 【三体加入】暴露 df 是 sub-corpus 属性
  │    └─ 罗辑 df_ratio 从 0.083 稀释到 0.006
  │    └─ 方案改为 per-scope df
  │
  ├─ α 手工推演(6 seed × 3 archetype)
  │    └─ 净收益 7.7%(太小)
  │    └─ Counter-example: 修士 domain 词误伤 -2000
  │    └─ df 单维度无法区分 hub vs domain
  │
  └─ 【最终决策】方案 γ: 不做 df 接口
       └─ 元层教训: "暴露数据" ≠ caller-first 的充分条件
```

---

## 4. 最终结论

### 4.1 什么被证实

**V5_npmi 在非 hub seed 上是 caller-first 的**:

- 8 seed × top-5 Useful% 均值 = 87.5%
- Caller 拿到关键字直接可用于 L1 回喂
- npmi 是数学恒等式加工,不注入语义偏见 —— 符合"抗先验"哲学

**这一部分可以进入主干**(但独立于本 post-mortem 的 hub 讨论)。

### 4.2 什么被证否

**在共现关系图 + 单一 ranking key 的框架下,hub seed 结构性无法救**:

- V0(raw)、V1-V3(DF 剪枝)、V4(章节窗口)、V5(npmi)四种变量组合都无法让 hub
  邻居出现关联实体
- 这不是"再试一种组合就能救"的问题,是**框架能力边界**

**df 元数据接口(A/B/C/D 方案)不做**:

- α 推演净收益 7.7% 太小
- Domain 词 counter-example(-2000)是**方向性错误**,不是量级问题
- 甜蜜点场景(极端 hub / rare seed)caller 用现有工具就能识别

### 4.3 产品层根因:侦探与放大镜(2026-07-10 会话末沉淀)

以上所有失败的**产品层根因**——不只是数学层根因——用一个心智模型收束:

> **Caller 是侦探福尔摩斯,grga 是放大镜。**
> **grga 的目标不是"把沙子吹开只留下钻石",是放大侦探视野让他自己判断。**

**"什么是钻石"是 caller-任务-相对的**:

| 侦探任务 | 韩立是什么 | 眉梢/心中是什么 | 修士是什么 |
|---|---|---|---|
| 找人物关系 | 沙(hub 主角) | 沙(叙述配色) | 钻石(等级词) |
| 找叙事风格 | 沙(主角背景) | **钻石(风格特征)** | 沙 |
| 找情节推进节点 | **钻石(主线信号)** | 沙 | 沙 |
| 找术语体系 | 沙 | 沙 | **钻石(术语)** |

**任何 grga 侧对"什么是 hub / 什么是噪声"的默认判断,都会剥夺至少一类侦探的能力**。这不是"df 单维度不够"的量级问题,是"'什么该被过滤'本身不属于 grga 的决策空间"的**方向性问题**。

**推论**:今天所有"救 hub"方案的失败,本质是**这些方案都在替侦探吹沙**:

- V0-V4 用不同权重函数 → 试图让"关联实体"自动浮起来 → 假设侦探要找的是关联实体(不一定)
- df 接口 A/B/C/D → 试图让 caller 一眼识别 hub → 假设 hub 是"坏东西"(对某些任务反而是好东西)
- 二分聚类 / hub whitelist → 试图定义 hub → 假设 hub 有客观定义(没有)

**正确的方向**是让 caller **看得更清楚**(暴露完整数据、多种 ranking 供选、多种粒度扩展),而不是**看到更少**(帮 caller 过滤)。

### 4.4 元层教训(caller-first 三条铁律)

沉淀在 workspace `AGENTS.md` 中,层次递进,均可从 §4.3 侦探比喻推出:

1. **视角换位**:设计 agent 接口时切到 caller 立场,不用人类审读者指标评估
   → 侦探视角 ≠ 审读者视角
2. **加工边界**:抗**语义**判断(如扩停用词),允许**数学恒等式**加工(如 npmi)
   → 磨镜片可以,遮挡区域不行
3. **暴露判据**:**暴露 raw 数据 ≠ caller-first**。真正判据是"caller 拿到能否
   做出比 baseline 更好的决策"。若不能,不做才是真 caller-first
   → 给侦探不会用的仪器不是尊重,是负担

---

## 5. 数学与结构性性质速查

### 5.1 hub 在四种窗口/ranking 下的退化机制

| 方案 | Hub 退化到 | 数学根因 |
|---|---|---|
| V0 raw count 行窗口 | 高频虚词 | w(hub, X) ≈ DF(X) |
| V1-V3 DF-adaptive | 大部分邻居被剪,配角误伤 | 剪枝阈值无法区分 domain vs stopword |
| V4 章节窗口 | 更严重的高频虚词 | w ≈ min(DF_ch(hub), DF_ch(X)) ≈ DF_ch(X) |
| V5 npmi | 独占叙述词(韩某/眉梢) | npmi 奖励 co/df→1,hub 上 = 主角配色 |

**共同结构性瓶颈**:seed 是 hub 时,seed 一列的信号被 dominant 项(seed 自身
的高 DF)吞噬,ranking 塌缩到"跟 seed 关联度弱的相关信号"。

### 5.2 npmi 的双面性

- 对**非 hub seed**:奖励"最纯粹共现" ≈ 奖励语域配套词 ✓
- 对 **hub seed**:奖励"最纯粹共现" ≈ 奖励独占叙述词 ✗

**推广元规则**:任何"数学恒等式加工"都要过"这个加工的隐性偏好是否与目标
一致"的自检。npmi 好用不等于处处好用。

### 5.3 df 单维度的分类失效

高 df 词有**至少两种类型**,df 无法区分:

| 类型 | 例 | df | Caller 应对 |
|---|---|---:|---|
| Hub(主角) | 韩立 | 47007 | Skip L2 |
| Domain 词 | 修士 | 8818 | **走 L2 拿语域** |
| 通用虚词 | 心中 | 5672 | 视 caller 任务 |

若要真做 df 接口,必须多维度(df + degree + avg_neighbor_npmi),复杂度飙升
到不值得。

---

## 6. 拒绝的候选方案与理由

| 方案 | 描述 | 拒绝理由 |
|---|---|---|
| Hub whitelist | 手动列 hub 名单绕过图查询 | 违反"抗先验"哲学;不 scalable |
| 二分聚类分流 | k-means 把词分 hub/normal 两类 | 凡人 df 分布是"1 outlier + Zipf 长尾",单峰非双峰;要么误伤 domain 要么等价手挑 |
| 扩停用词表(200-500 词) | 更激进过滤 | 违反"抗先验",注入语义偏见 |
| df 接口 A(全 corpus df) | build 时存 df,caller 分页读 | 混合语料下 sub-corpus hub 被稀释(罗辑 case) |
| df 接口 B(per-scope df) | 每 scope 一份 df | 净收益 7.7% + domain 词误伤 -2000 |
| df 接口 C(max_local_ratio) | 单一 df + max_scope_ratio | 丢信息;仍有 domain 词误伤 |
| df 接口 D(强制 scope 查询) | caller 必须指定 scope | 接口最重;caller 大多数场景不知选哪个 scope |
| 多维度 β | df + degree + avg_npmi | 能区分 domain vs hub,但成本飙升,当前投入产出比不划算 |
| 端到端 γ agent 实验 | 真跑 LLM caller 校准 α 推演 | 推演已有 -2000 counter-example,边际证据递减 |

---

## 7. 重启前置条件

如果未来有人想重启"共现图 hub 处理"这个话题,先满足以下**至少一条**再开工:

### 7.1 框架层面出现新维度

只在共现关系图 + 单一 ranking 框架内调参,预期是**继续失败**(V0-V5 已证)。
必须至少引入一个新维度,例如:

- **子图切割**(seed 附近的 dense subgraph 提取,而非 top-K)
- **hierarchical clustering**(先聚簇,再从簇内选代表)
- **Multi-hop propagation**(PPR / random walk with restart)
- **多信号融合**(df + degree + avg_neighbor_npmi + betweenness 等)

### 7.2 实测发现 caller 需求改变

本次结论"L2 = L1 补充层"下,hub seed 走 L1 已足够。若未来发现:

- **Caller 无法从 seed 名判 hub**(反例:领域外 caller 不知"韩立"是主角)
- **rare seed 不显式 empty**(caller 用 L2 拿不到 empty 反馈,浪费轮次)
- **Domain 词自动判定需求真实存在**且 baseline 已失败

那么可以重新评估 df/多维度接口。

### 7.3 语料形态变化

本次结论基于"文学长篇 + 12x 规模差混合"。若语料变成:

- **短文档为主**(每篇 <100 行) → 章节/文档窗口的退化机制不同,需重测
- **多 scope 均衡**(N 个大小相近 sub-corpus) → per-scope df 价值可能上升
- **技术文档**(代码 + Markdown) → hub 定义完全不同,可能不存在文学 hub 现象

### 7.4 元层校准

重启前必读 workspace `AGENTS.md` 的三节:
- "为 agent 设计接口时,先切换到 caller 视角"
- "抗先验 ≠ 什么加工都不做"
- "暴露数据 ≠ caller-first"

以及本文档 §4.3 三条铁律。避免重复今天早上"暴露 df 就是 caller-first"的
认知路径。

---

## 8. 附:实验目录一览与产物索引

### 8.1 目录

| 目录 | 内容 | 状态 |
|---|---|---|
| `experiments/cooccur_2026_07_10/` | V0-V3 raw/DF 行窗口 | done(accept 全过) |
| `experiments/cooccur_2026_07_10_v2/` | V2_retok + V4 章节窗口 | done(9/9 accept) |
| `experiments/cooccur_2026_07_10_v3/` | V5_npmi 归一化 | done(10/10 accept) |

### 8.2 关键产物

- `experiments/cooccur_2026_07_10/FINDINGS.md` — F1 DF 剪枝冲突
- `experiments/cooccur_2026_07_10_v2/FINDINGS.md` — F1 OOM 绕过 + F2 章节窗口退化
- `experiments/cooccur_2026_07_10_v3/FINDINGS.md` — npmi 数学退化证据表
- `experiments/cooccur_2026_07_10_v3/hub_diff_v3.md` — hub 邻居对比手工分析
- `experiments/cooccur_2026_07_10_v3/topk_usability_v3.md` — 8 seed × top-30 手工判定
- `experiments/cooccur_2026_07_10_v3/hanli_golden_entity_ranks.json` — 6 golden 实体 npmi 排位

### 8.3 α 推演报告

不在 grga repo 内,在 caller workspace:
- `~/qwenpaw-data/workspaces/ai-developer/memory/2026-07-10-df-interface-caller-simulation.md`

### 8.4 关键数据速查

- **语料 N**:凡人 145061 行 / 三体 12059 行 / 合并 157120 行
- **V5 图规模**:total_terms=76478, total_edges=1204783, build_time=25.73s
- **韩立 df 断层**:df=46252(合并 0.299),第二名"心中" df=5571(0.040),**8.3× 断层**
- **8 非 hub seed top-5 Useful% 均值**:87.5%(V5_npmi)
- **韩立 6 golden 实体 npmi 排位范围**:208-12612 / 17759
- **α 推演 token 总节省**:7.7%(1000/13000);修士 counter-example -2000

---

## 9. 一句话给未来读者

**如果你在 grga 里遇到 hub 问题觉得"再改一版共现图 ranking 应该能救",
先读本文档 §7"重启前置条件"。**四轮实验的分类性证据说明这不是参数问题,
是框架能力边界。真要救 hub,该换框架(§7.1),不该继续调参。

*完。*
