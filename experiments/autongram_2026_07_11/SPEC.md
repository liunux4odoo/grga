# SPEC — autongram_2026_07_11

> **状态**: 实验规范，实现进行中。
> **目标读者**: 未来落地此实验的开发者（人或 agent）。
> **契约来源**: 2026-07-11 与用户的四轮讨论。
> **决策路径**: 先在 experiments 里出 tracer bullet → 数据过关再评估是否进 grga 主干 `src/grga/graph/build.py`。

## 0. TL;DR

**要验证的核心假设**：在 build index 时，用**当前 auto_dict 加载后的 jieba** 对文档做切分 → 用**频次 + NPMI + 左右分支熵**做无监督多元短语发现 → 把新发现的短语**追加**到 auto_dict → 用户下次执行 build/rebuild 会用更完善的字典。以此**替代手工维护词典**，同时避免 v3 build.py `_TOKEN_RE` 造成的"长句碎片污染 keywords 表"（当前主干 deep_keywords.db 646,411 词条中 len=20 的整句片段占 24,322 条）。

**判据方向**：验证在同一份《三体》语料上，auto-dict 方案相比裸 jieba 和当前正则方案：
- seed 整词保留率不下降
- top-K Useful% 不逊于 v3（8 seed 均值 ≥82.5%，v3 baseline 87.5%）
- 消灭 len≥15 的整句碎片（当前正则方案占比约 23%）

**决策门**：C1-C4 判据同时过 → 建议进主干；有一项劣化 → 归档为负面结果。

---

## 1. 背景与动机

### 1.1 触发问题

当前主干 `src/grga/graph/build.py` 用正则 `_TOKEN_RE = re.compile(r"[A-Za-z]...|[\u4e00-\u9fff]{2,20}")` 做纯 py 分词。实测发现：
- 长句碎片污染：deep_keywords.db 的 646k 条 kw 里，len=20 的截断整句 24,322 条（尾部堆积异常）。
- seed 保留靠运气：`墨大夫` / `极阴祖师` / `筑基丹` / `大衍神君` 虽都能 exact hit=1，但只在恰好被标点/ASCII 边界围住时才独立成 token；连续汉字串会被贪婪合成 20 字整块。

### 1.2 前作教训（不重蹈的坑）

| 来源 | 教训 |
|---|---|
| `experiments/cooccur_2026_07_10/FINDINGS.md` F2 | 裸 jieba 精确模式会把 `墨大夫→墨/大夫` 切碎，seed miss |
| `experiments/cooccur_2026_07_10_v2/QUESTIONS.md` Q2 | 加 19 词 user_dict 可修复，但**需要人工维护词典** |
| `docs/design/graph-l2-postmortem-2026-07-10.md` §4.4 铁律 2 | **数学恒等式加工允许，语义先验判断禁止** |

### 1.3 本实验与 postmortem 的关系（务必澄清）

postmortem 的"不做"结论**只针对 hub ranking 和 df 接口**，不针对分词质量。postmortem §4.1 明确"V2_retok 分词修正生效，P@5 从 0.05→0.15（3×）"。本实验属于**被实验背书的方向**，与"不救 hub"不冲突。

---

## 2. 语料与 golden

### 2.1 语料

- 主：`/media/lqw/DATA/sync/temp/三体.txt`（2.7MB, 12059 行）
- 备（如首轮通过再跑）：《凡人修仙传》—— 用于验证大语料下的算法稳定性

先用《三体》一份走通判据，避免与 postmortem "12x 规模稀释"现象混在一起。

### 2.2 Golden（新建三体版）

`data/golden_santi.json`：新增 5-8 个三体 seed 及其 golden 邻居实体：

```
罗辑    -> 面壁者 / 黑暗森林 / 三体 / 咒语 / 章北海
汪淼    -> 大史 / 三体游戏 / 科学边界 / 纳米 / 申玉菲
叶文洁  -> 三体 / 红岸 / 伊文斯 / 齐家屯 / 杨冬
二向箔  -> 歌者 / 太阳系 / 二维 / 打击 / 光粒
智子    -> 三体 / 质子 / 监视 / 展开 / 云天明
```

（seed 列表由 tracer bullet 作者据语料实况调整，golden 词最终以 topk_usability_santi.md 手工判定为准。）

**auto-dict 自评估 golden**：`v3/data/user_dict.txt` 19 词里没有三体词，此维度用**三体 seed 本身**做替代——检查三体 seed 是否被 auto_dict 自动发现。

---

## 3. 单变量隔离原则（对比矩阵）

沿用项目实验传统，一次只改一个变量。baseline = v3 build.py 逻辑，本实验变量：

| 变量 | v3 baseline | 本实验 A（auto-dict） | 本实验 B（对照） |
|---|---|---|---|
| tokenizer | jieba + 手工 user_dict(19 词) | jieba + **迭代式 auto_dict**（首轮空） | 裸 jieba（无字典） |
| valid_token 过滤 | `len<2` 丢单字 | **移除**（jieba 天然多字，不需要长度过滤） | 移除 |
| 短语来源 | 手工维护 | 频次+NPMI+分支熵 自动发现 | 无 |
| 其他（ranking / 窗口 / min_pair_count） | 全部 frozen | 全部 frozen | 全部 frozen |

**Frozen 项**（禁止在本实验里同时改动）：line 窗口、NPMI ranking、min_pair_count=3、v2 保守停用词表、Bouma 2009 npmi 公式、tie-break 规则。

---

## 4. 算法规范

### 4.1 Build/rebuild 迭代顺序（用户 2026-07-11 打断修正）

**每次执行 `build` 或 `rebuild` 的核心流程**：

```
Step 1. 加载旧 auto_dict（若存在）→ jieba.add_word() 全部载入
Step 2. jieba.cut(precise mode) 对文档做切分  ← 切分基于本次加载的字典
Step 3. 从 Step 2 的 token 流上做 ngram 短语检测（频次+NPMI+分支熵）
Step 4. 新发现的短语 append 到 auto_dict（数据库表 upsert）
Step 5. 用 Step 2 的 tokenization 建 keyword_chunks / keyword_pairs 索引
```

**为什么是这个顺序**：
- 迭代收敛：首次 build 时 auto_dict 空，走裸 jieba；发现基础短语后写回。第二次 build 加载基础短语（例如 `罗辑`），tokenization 已经不会再把 `罗辑` 切碎，此时 detection 能发现**更高阶**的组合（如 `面壁者罗辑`）。
- Detection 期间**不反复重载词典**：Step 2 一次性切完整个 scope，Step 3 在这个稳定的 token 流上做统计。这样 detection 结果与文档处理顺序无关（用户第 3 条原则）。
- `build` 与 `rebuild` 的区别：
  - `build`：非破坏，追加式。keyword_chunks 和 auto_dict 都是 upsert。
  - `rebuild`：先清空该 scope 的 auto_dict 与索引，然后从头走一遍上述 5 步。用于用户想丢掉历史词典重新收敛。

### 4.2 Stage 2：切分（jieba）

```python
if auto_dict_exists(scope):
    for phrase in load_auto_dict(scope):
        jieba.add_word(phrase)

for doc in corpus_docs:
    for line in doc:
        raw_tokens = list(jieba.cut(line, cut_all=False))
        # 不做任何 len<2 过滤 —— 用户明确指出这是正则时代的遗产
        yield doc_id, raw_tokens
```

### 4.3 Stage 3：多元 ngram 候选生成（用户第 4 条）

参数 `--max-ngram-n`（默认 **4**，2026-07-11 实测调优——见 FINDINGS §F10）。对每个 doc 独立生成候选（用户第 3 条 "以文档为单位"）：

```python
def ngram_candidates(tokens: list[str], max_n: int, stop_boundaries: set[str]):
    for i in range(len(tokens)):
        for n in range(2, max_n + 1):
            if i + n > len(tokens):
                break
            window = tokens[i:i+n]
            if any(t in stop_boundaries for t in window):
                continue  # 停用词硬边界
            yield tuple(window)
```

**停用词硬边界**（数学允许，非语义先验）：使用 v2 stopwords 已冻结的保守表；不扩展。目的是防止 `罗辑说道` 这种被 stopword `说` 隔开的组合污染候选。

### 4.4 Stage 3.5：数学判据过滤（用户第 2 条）

三层门槛，全部**纯数学**，无语义规则：

**判据 1：频次门槛**
```
count(ngram) ≥ min_count（默认 3，沿用 min_pair_count）
```

**判据 2：NPMI 凝固度（Bouma 2009，用户 Q4 冻结用 min-split 版本）**

对 n≥2 的 ngram，用所有二切分中最小 NPMI 作凝固度（最弱链）：

```
NPMI(A, B) = log(P(A,B) / (P(A) * P(B))) / -log(P(A,B))

对于 phrase w = t_1 t_2 ... t_n：
solidity(w) = min over splits {NPMI(t_1..t_k, t_{k+1}..t_n)}   for k in 1..n-1
门槛：solidity ≥ npmi_threshold（默认 0.3）
```

n=2 时 solidity = NPMI(A, B) 本身。n=3 时评估两种切分（A|BC 和 AB|C）取最小。

**判据 3：分支熵（Matrix67 新词发现，用户 Q1 冻结用 1.0 nat）**

对候选 phrase `w`，收集其在整个 scope 语料中的左/右邻词分布：
```
H_L(w) = -Σ p(l) * log p(l)   # l 遍历所有出现在 w 左侧的 token
H_R(w) = -Σ p(r) * log p(r)
branch_entropy(w) = min(H_L(w), H_R(w))
门槛：branch_entropy ≥ ent_threshold（默认 1.0 nat）
```

**为什么这三条能过滤 `韩立说道` 这类无组合意义高频串**：
- 频次 ✓（能过）
- NPMI：`韩立` df≈46k, `说道` 也高频，`说道` 与 `韩立` 联合频次相对 marginals 的比值不高 → NPMI 偏低 → 门槛砍掉
- 分支熵：即使前两条勉强过，`韩立说道` 的右邻会集中在 `：` 或 `，` → H_R 低 → 砍掉

**性能约束**（用户第 2 条）：所有 log、H_L/H_R 计算用 `functools.lru_cache(maxsize=None)`，参考 v3 build.py 做法。

### 4.5 Stage 4：auto_dict 持久化

新增表（先在 experiment 里用 sqlite 单文件跑，如进主干再合到 schema.py）：

```sql
CREATE TABLE IF NOT EXISTS auto_ngram_dict (
    directory   TEXT NOT NULL,   -- scope，per-scope 维护词典
    phrase      TEXT NOT NULL,
    n           INTEGER NOT NULL,
    count       INTEGER NOT NULL,
    npmi        REAL NOT NULL,
    branch_ent  REAL NOT NULL,
    detected_at REAL NOT NULL,
    PRIMARY KEY (directory, phrase)
);
```

**Upsert 策略**：`build` 用 `INSERT OR REPLACE`，让新一轮 detection 的统计值覆盖旧值（新一轮的 count/npmi 更准，因为字典迭代过一次后 token 边界更干净）。

**Per-scope 而非全局**（用户 Q3 确认）：postmortem 已证"hub 是 sub-corpus 属性"（罗辑 case）；词典也 per-scope 存，避免跨语料词典串味。**参数、文档和 skill 中需明确说明 scope 同时决定"搜索范围"和"自定义词典"**。

### 4.6 Stage 5：keyword_chunks 建索引

沿用 v3 build.py 的 `filter_dedup` → `combinations` → npmi 逻辑，不动。唯一变化：`filter_dedup` 内 `len<2` 过滤**移除**。

### 4.7 CLI 参数（本实验专属）

```
--max-ngram-n INT       # 默认 4（覆盖长复合专名）
--min-count INT         # 默认 3
--npmi-threshold FLOAT  # 默认 0.3
--ent-threshold FLOAT   # 默认 1.0
--rebuild               # 清空该 scope 已有的 auto_dict 与结果，从头开始
```

---

## 5. 实验目录布局

```
experiments/autongram_2026_07_11/
├── SPEC.md                         # 本文件
├── README.md                       # 实验产物索引 + 一句话结论
├── FINDINGS.md                     # 关键发现（正/负面均记）
├── QUESTIONS.md                    # 契约疑虑（Q1-Q5 结果冻结）
├── build.py                        # tracer bullet，不改主干
├── data/
│   ├── stopwords_v2.txt            # 从 v3 copy
│   ├── auto_dict.jsonl             # detect 输出（含 count/npmi/branch_ent）
│   ├── auto_dict_iter1.jsonl       # 第一轮 detection 结果（可选，做收敛观察）
│   ├── auto_dict_iter2.jsonl       # 第二轮 detection 结果
│   ├── golden_santi.json           # 三体 5-8 seed 手工 golden
│   └── line_tokens.pkl             # jieba 切分缓存（沿用 v3 mtime 门控）
├── results_baseline_v3.json        # baseline = v3 直接跑三体
├── results_naive_jieba.json        # 裸 jieba 无字典（对照 B）
├── results_auto_ngram_iter1.json   # 本实验主变量 A，第一轮
├── results_auto_ngram_iter2.json   # 本实验主变量 A，第二轮
├── seed_integrity.md               # seed 整词保留率对比表
├── topk_usability_santi.md         # 8 seed × top-30 手工判定
└── length_distribution.md          # kw 长度分布对比（消除长句碎片是否成功）
```

---

## 6. 判据（Acceptance Criteria）

**必须全部通过**，否则不进主干、只写 FINDINGS：

**C1 — Seed 整词保留率**
- 三体 5-8 手工 seed（罗辑/汪淼/叶文洁/二向箔/智子/面壁者/黑暗森林），auto_ngram_dict **在 iter2 内**覆盖率 ≥ 80%。
- 允许 iter1 覆盖率较低，观察收敛过程（首轮字典为空，靠 ngram 合出所有 seed）。

**C2 — top-K Useful% 不劣化**
- iter2 结果的三体 seed × top-5 Useful% 均值 ≥ 60%（新语料无 v3 baseline，用绝对值判据）。
- 若严格 ≥ 80% 且 seed 保留率 ≥ 90%，标记为 **strong pass**。

**C3 — 长句碎片消除**
- keywords 表中 len ≥ 15 的条数占比 < 5%（当前正则方案 646k 里 len≥15 约 15 万条，占 23%）。
- 数据方式验证：对同一语料，跑 baseline_v3 vs auto_ngram 输出长度分布直方图对比。

**C4 — 性能可接受**
- iter1 单趟耗时 ≤ 2× v3 baseline build 时间。
- iter2 单趟耗时 ≤ iter1 * 1.2（字典大小增加带来的额外开销可接受）。

---

## 7. QUESTIONS 冻结（2026-07-11 用户已答）

| Q | 内容 | 冻结值 |
|---|---|---|
| Q1 | 分支熵阈值 | 1.0 nat（Matrix67 经验值起步，实测后可调） |
| Q2 | 是否单独 detect-ngrams 动词 | ❌ 不做。build/rebuild 内部自动 detect（§4.1 顺序） |
| Q3 | Per-scope 词典 | ✅ 采纳。参数/文档/skill 说明 scope 决定"搜索范围+词典" |
| Q4 | n>2 solidity 公式 | ✅ min-split NPMI（更严） |
| Q5 | 跨平台 | ✅ 本实验先验证算法，跨平台性能基线留到进主干阶段 |

---

## 8. 决策后动作

**Pass（C1-C4 全过）**：
- 起草主干迁移 PR：在 `src/grga/graph/build.py` 加 `--tokenizer=jieba+auto` opt-in（**先不改默认**，保留正则默认一版本观察 caller 反馈）。
- 同步更新 `docs/design/tokenization.md`（新建）记录判据、阈值、per-scope 词典契约。
- 更新 skill 中"scope 同时决定搜索范围+词典"的说明（Q3 承诺）。

**Partial pass（C1/C2 过，C3/C4 有一项劣化）**：
- 保留在 experiments 作参考实现，不进主干。
- FINDINGS 记录劣化根因。

**Fail（C1 或 C2 劣化）**：
- 归档为负面结果。主干继续用正则。

---

## 9. 一句话给未来读者

**这个实验要证的是"无监督数学判据 + 迭代式字典 能替代手工词典"，不是"jieba 一定比正则好"。判据挂在 seed 整词保留 + top-K Useful + 长句消除 三个可量化指标上，不挂在直觉上。**
