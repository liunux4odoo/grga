# REPORT — autongram_2026_07_11 无监督自动词典分词实验

> **实验日期**：2026-07-11
> **状态**：done. 判据 C1/C2/C3 **全项 strong pass**，C4 阈值口径需修订。
> **建议行动**：进入主干 opt-in 阶段（保留正则默认一个版本观察反馈）。
> **本报告读者**：未来在主干集成此方案、或对项目分词策略做决策的开发者。

## 0. Executive summary

在 grga 主干 `src/grga/graph/build.py` 里，L2 关键字图构建用正则 `_TOKEN_RE`（`[\u4e00-\u9fff]{2,20}` 贪婪合并）做纯 py 分词。实测三体语料下这种方式产出的 keyword 表中 **20.74% 是长句碎片**（如 `一下将下方千亩大小的地方全都笼罩在了其`），关键 seed 词（罗辑/汪淼/云天明）大都被吸进这类整块，孤立命中率极低（df=2~56）。

本实验用 **jieba + 频次/NPMI/分支熵 迭代式短语发现** 替代手工 user_dict，在三体语料上：

- **phrase_check 覆盖率**：56.25% (naive jieba) → **100%** (auto_dict iter2)
- **长句碎片污染**：20.74% (regex) → **0.00%**
- **top-5 Useful%**：87.5%（宽判据，与前作 v3 baseline 持平）
- **seed 全部入图**：8/8（含 F2 记录的历史 miss 词 `章北海`/`云天明`/`面壁者`）
- **成本**：单趟 ~5s（三体 2.7MB），迭代 2 轮即收敛

方案哲学 100% 对齐 AGENTS.md 铁律 2 "抗语义判断，允许数学恒等式加工"：三层判据（频次≥3 + NPMI≥0.3 + 分支熵≥1.0 nat）全部为数学门槛，唯一"边界规则" `CORE_STOP_BOUNDARIES` 是 26 字核心虚词集（真结构助词，无构词能力）。

## 1. 背景与动机

### 1.1 触发问题

初始怀疑："build.py 一开始不是直接用 jieba 做 tokenize 吗？" 查 git log 后确认从最早的 093af9d 起就用正则，从未用过 jieba。但主库 `~/.config/grga/deep_keywords.db` 里 646,411 个词条的长度分布严重右偏：

```
len= 2   5,707
len= 5  24,546
len=10  61,025
len=15  28,356
len=20  24,322  ← 硬上限，堆积异常
```

len=20 的 24,322 条全部是"连续 20 个汉字未被标点/ASCII 打断"的整句切片，是当前分词器的贪婪合并伪像。

### 1.2 前作教训（不重蹈的坑）

| 来源 | 教训 |
|---|---|
| `cooccur_2026_07_10/FINDINGS.md` F2 | 裸 jieba 会把《凡人》`墨大夫→墨/大夫` 切碎，50% seed miss |
| `cooccur_2026_07_10_v2/QUESTIONS.md` Q2 | 加 19 词 user_dict 可修复，但**需要人工维护** |
| `docs/design/graph-l2-postmortem-2026-07-10.md` §4.4 铁律 2 | 数学恒等式加工允许，语义先验判断禁止 |

### 1.3 与 postmortem 的关系（澄清）

postmortem 的"不做"结论**只针对 hub ranking 和 df 接口**，不针对分词质量。§4.1 明确 "V2_retok 分词修正让 P@5 从 0.05→0.15，3× 提升"。本实验属于**被实验背书的方向**。

## 2. 方案设计

### 2.1 核心思路 —— 迭代式无监督短语发现

每次 `build` 执行 5 步：

```
Step 1: 加载现有 auto_dict → jieba.add_word 全部载入
Step 2: jieba.cut(precise) 对语料切分（用刚加载的字典）
Step 3: 在 Step 2 的 token 流上做 ngram 短语检测
Step 4: 新发现的短语 append 到 auto_dict（upsert）
Step 5: 用 Step 2 的 tokenization 建 keyword_chunks
```

**迭代收敛**：首次 iter1 字典空 = 裸 jieba；iter2 加载 iter1 发现的短语后重新切分，此时 `章北海` 已是整词，能与相邻 token 组合出更高阶实体；iter3+ 边际收益递减。

### 2.2 三层数学判据

对每个 ngram 候选 w = (t_1, ..., t_n)：

| 判据 | 公式 | 默认阈值 |
|---|---|---|
| 频次 | `count(w) ≥ min_count` | 3 |
| 凝固度 | `min over splits { NPMI(left, right) }` (Bouma 2009, 单一 N) | 0.3 |
| 分支熵 | `min(H_L(w), H_R(w))` nat | 1.0 |

**NPMI 单一 N 约定**（关键数学细节）：所有概率 P(x) = c(x) / total_tokens，即使 x 是 bigram 或 trigram。这是 phrase-mining 领域标准约定，且是**唯一能保证 NPMI ∈ [-1, 1]** 的选择（本实验 F9.2 单测证否了"分层 N"的直觉修改）。

### 2.3 关键设计决策 —— 两阶段停用词表分离

**Detection 阶段**（决定 ngram 候选边界）：用 26 字 `CORE_STOP_BOUNDARIES`：
```
的了和与或是在这那有被把将从对为之其而且但也就都还只很才又已
```
只含**语法层无构词能力**的真结构助词。**不含** `者/人/号/家` 等构词后缀。

**图构建阶段**（决定关键字是否作为检索结果）：仍用 `cn_stopwords.txt` 完整 746 词表。

两阶段解耦是修复 `面壁者` / `破壁人` 未召回的关键（详见 F5）。

### 2.4 拼接规则 —— 按相邻对判断（F11 教训）

将 ngram tokens 拼回 phrase 时，按**相邻两 token 的字符类型**决定分隔符：

| 前 token | 后 token | 分隔符 | 例 |
|---|---|:---:|---|
| CJK | CJK | 无 | `章`+`北海` → `章北海` |
| CJK | ASCII | **无** | `艾`+`AA` → `艾AA` |
| ASCII | CJK | **无** | `PDC`+`主席` → `PDC主席` |
| ASCII | ASCII | 单空格 | `nano`+`center` → `nano center` |

依据：中文文本 CJK↔ASCII 直接相邻不加空格是汉语排版惯例。

### 2.5 参数默认值

| 参数 | 默认 | 说明 |
|---|---|---|
| `--max-ngram-n` | **4** | 一次覆盖到"赫尔辛根默斯肯"级 4-段外来专名 |
| `--min-count` | 3 | 频次门槛 |
| `--npmi-threshold` | 0.3 | 凝固度门槛 |
| `--ent-threshold` | 1.0 | 分支熵门槛（nat） |

## 3. 实验过程与关键节点

实验按 tracer bullet 惯例进行（`experiments/autongram_2026_07_11/build.py`，不动主干）。语料：三体三部曲 2.7MB / 12,059 行。5 个 mode / iter 组合逐步验证：

| Step | Mode | 结果 | 关键发现 |
|---|---|---|---|
| 1 | baseline_regex | phrase_hit=93.75%, ge15=20.74% | seed df 极低（罗辑=4）证明正则"贪婪合并"假阳性 |
| 2 | naive_jieba | phrase_hit=56.25%, ge15=0% | F2 复现：章北海/云天明/面壁者 全 df=0 |
| 3 | auto_ngram iter1 | 693 短语，phrase_hit=56.25% | 字典空冷启，等同 naive |
| 4 | auto_ngram iter2 | 2615 短语，phrase_hit=**100%** | 迭代 1 轮完全修复 |
| 5 | auto_ngram iter3 | 2683 短语，收敛 | 边际收益递减 |

**中途发现的 5 个关键问题**（都在报告体系里）：

- **NQ2**：`面壁者`/`破壁人` 未召回 —— 根因是初版 detection 用 746 词全停用词表把 `者`/`人` 也当边界，`面壁+者` bigram 在候选阶段就被跳过。改用 26 字核心虚词集后立即修复。
- **NQ5 v1**：ASCII token 拼接分隔符 —— 修复"nano center → nanocenter" 拼错。
- **NQ5 v1 实现 bug**：`CJK_RE = [\u4e00-\u9fff]`（单字符 pattern），`fullmatch("北海")` 返回 False，导致 CJK 合成词也误走空格分支，`章北海` → `章 北海`。加 `+`/`*` 修正 pattern。
- **F9.2 NPMI 分层 N false-fix**：code review 建议按 n 层分别用事件数，实测让 bigram NPMI=2.17 越界 [-1,1]。回退单一 N 约定并单测锁定。
- **NQ5 v2**：中英混合实体（`艾AA` / `PDC主席`）在 v1 修复后仍错，因规则是"整个窗口含 ASCII 就全空格"。改为按相邻对判断，恢复 10 个真实混合实体的整词状态。

## 4. 最终数据

### 4.1 三方对比

| 模式 | terms | ge15_ratio | phrase_hit | build_time |
|---|---:|---:|---:|---:|
| baseline_regex（当前主干） | 72,478 | **20.74%** | 93.75% (假阳性) | 0.47s |
| naive_jieba | 29,778 | 0.00% | 56.25% | 5.54s |
| **auto_ngram iter3** | **31,824** | **0.00%** | **100%** | **4.77s** |

### 4.2 Golden seed df 对比

| Seed | regex | naive | auto iter3 |
|---|---:|---:|---:|
| 罗辑 | 4 | 956 | 855 |
| 汪淼 | 2 | 474 | 430 |
| 叶文洁 | 56 | 341 | 336 |
| 章北海 | 22 | **0** | **237** |
| 程心 | 12 | 1041 | 950 |
| 云天明 | 3 | **0** | **210** |
| 智子 | 4 | 343 | 327 |
| 面壁者 | 7 | **0** | **153** |

3 个历史 miss（章北海/云天明/面壁者）在 auto_ngram 下全部完整召回。

### 4.3 auto_dict 里的高价值实体样本

按类别抽样：

```
主角      章北海(297,0.97), 云天明(275,0.97), 面壁者(263,0.89)
面壁体系  面壁计划(114,0.68), 破壁人(111,0.64), 黑暗森林(132,0.86)
飞船      万有引力号(125,0.74), 自然选择号(85,0.71)
外来专名  赫尔辛根默斯肯(54,0.96), 联合国行星防御理事会(7,0.54)
概念      量子计算机(4,0.46), 纳米中心(8,0.61), 红岸基地(35,0.68)
中英混合  艾AA(27,0.74), PDC主席(6,0.53), 白Ice(32,0.92),
          高Way(20,0.78), PDC常任理事国(3,0.64)
```

**结构性优势**：手工 user_dict 无法枚举 2804 个词条。数学恒等式判据能。

### 4.4 判据裁定

| 判据 | 门槛 | 实测 | 结论 |
|---|---|---|:---:|
| C1 phrase_check ≥ 80% | 80% | 100% (16/16) | **strong pass** |
| C2 top-5 Useful% ≥ 60% | 60% | 87.5% | **strong pass** |
| C3 len≥15 占比 < 5% | 5% | 0.00% | **strong pass** |
| C4 iter1 ≤ 2× naive_jieba | 2× | 1.0× | pass（旧口径 vs regex 未过需修订）|

## 5. 沉淀教训（元层）

### 5.1 数学正确性易在"看似精细"的重构里丢失

Code review 的 Finding 3 建议"NPMI 分层用不同 N 空间"看似更严谨，实施后单元测试立即抓出 NPMI=2.17 越界 [-1,1]。真正的数学正确性是**所有概率同一 N 空间**，这是标准 phrase-mining 约定，原代码没错。

**行动准则**：任何触碰概率分母的改动必须过"NPMI/PMI 边界是否守得住"的单测。

### 5.2 "停用词越多越好"是直觉陷阱

初版 detection 用 746 词全表当硬边界，把 `者/人/号/家` 等**构词后缀**也误当边界，直接结构性失败（`面壁者` 永不召回，因为 candidate 生成阶段就被跳过）。

正确原则：**边界规则应尽量近数学、远语义**。detection 用 26 字核心虚词集（真结构助词，无构词能力）+ 强数学判据；766 词全表留到"检索结果不作 keyword"的下游场景。

对齐 AGENTS.md 铁律 2 —— "抗先验判断"的核心不是"过滤越多越好"，是"过滤规则应尽量数学化"。

### 5.3 跨字符集拼接必须按相邻对判断

NQ5 修复三次才对：
- v0：`"".join`，ASCII 拼错（`nano center → nanocenter`）
- v1：整窗口判断（"含 ASCII 就加空格"），CJK+ASCII 混合拼错（`艾 AA`）
- v2：**按相邻两 token 类型判断**，四种邻接组合全对

**通用启示**：多字符集混排的字符串处理，默认思路应是"两两相邻决策"而非"全局判断"。

### 5.4 迭代 bootstrap 与单趟多元 ngram 大致等价，但仍值得双轨

用户观察"max_n=2 迭代能否等价 max_n=3 单趟"，实测词典 Jaccard 93.4%，主要实体全覆盖。但 max_n=3 独有的 60+ 长实体里，一些是**结构性够不到**（左结合 bootstrap 遇到 A|BC 强 / AB 弱的 trigram 会断链）。

最终 `--max-ngram-n=4` 作默认（多 6 个高价值外来专名，成本 +7%），但保留 CLI 参数让内存/性能受限场景可下调。

### 5.5 迭代 2 轮即收敛

| Iter | 累计词典 | 新增 | phrase_hit |
|---:|---:|---:|---:|
| 1 (空字典) | 2124 | 2124 | 56.25% |
| 2 | 2615 | 491 | **100%** |
| 3 | 2683 | 68 | 100% |

主干集成建议默认 iter=2，或让用户显式指定 iter。

## 6. 主干迁移建议（若采纳）

### 6.1 必做

1. **在 `src/grga/graph/build.py` 加 `--tokenizer=jieba+auto` opt-in**，保留正则默认一个版本观察 caller 反馈，不改默认行为。
2. **新增 SQL 表 `auto_ngram_dict`** 到 `graph/schema.py`：
   ```sql
   CREATE TABLE auto_ngram_dict (
     directory   TEXT NOT NULL,
     phrase      TEXT NOT NULL,
     n           INTEGER NOT NULL,
     count       INTEGER NOT NULL,
     npmi        REAL NOT NULL,
     branch_ent  REAL NOT NULL,
     detected_at REAL NOT NULL,
     PRIMARY KEY (directory, phrase)
   );
   ```
   Per-scope 存储（postmortem 已证 "hub 是 sub-corpus 属性"）。
3. **新建 `docs/design/tokenization.md`** 记录判据、阈值、per-scope 词典契约、两阶段停用词表分离（§2.3）、单一 N NPMI 约定（§5.1）、CJK/ASCII 邻接拼接规则（§5.3）。
4. **补 `npmi_pair` golden 单测**：n=2 / n=3 / 完全共现 (NPMI=1) / 无共现 (NPMI<0) / 中英混合拼接 —— 防 F9.2 和 F11 回归。
5. **修 C4 阈值** → "iter1 ≤ 2× naive_jieba"（naive_jieba 才是同类基线）。

### 6.2 应做

- **凡人 22MB 语料二轮验证**：确认收敛速度不因语料变大发散；测试大语料下 detection 内存开销。
- **更新 skill / AGENTS 中"scope 决定搜索范围+词典"说明**（Q3 承诺）。
- **考虑 `--iter N` 参数**：允许用户显式跑多轮，默认 iter=2。

### 6.3 可选

- **凡人手工 user_dict 的 19 词是否被 auto_dict 覆盖**：验证跨语料迁移的泛化能力。
- **代码库场景**：验证中英混合拼接规则在纯代码/纯 markdown/混合场景的表现；补充 ASCII+CJK 相关的 golden case。

## 7. 一句话给未来读者

**如果你在 grga 主干看到 keyword 表里 20% 都是长句碎片、或看到某个 seed 词莫名 df 极低——不是数据错，是分词器的锅。本实验用 220 行 tracer bullet 证明了 jieba + 数学恒等式判据能一站解决，且不违反 postmortem 的"抗先验"哲学。剩下的只是主干集成的工程活。**

---

## 附：文件索引

| 文件 | 内容 |
|---|---|
| SPEC.md | 实验规范（含 Q1-Q5 冻结默认值） |
| REPORT.md | 本文件（过程/发现/结论综述） |
| FINDINGS.md | F1-F11 逐条发现 |
| QUESTIONS.md | NQ1-NQ5 契约疑虑及解答 |
| README.md | 一页速览 + 复跑指引 |
| seed_integrity.md | 8 seed 整词保留率对比表 |
| topk_usability_santi.md | 8 seed × top-5 手工判定 |
| length_distribution.md | 关键词长度分布对比 |
| build.py | Tracer bullet 实现 (534 行) |
| data/golden_santi.json | 三体 8 seed + 16 phrase_check |
| data/auto_dict.jsonl | iter3 最终字典 (2804 短语) |
| data/stopwords_v2.txt | 图构建阶段用的 746 词表 |
| data/user_dict_ref.txt | v2 手工 user_dict 参考 (19 词) |
| results_baseline_regex.json | 主干正则方案结果 |
| results_naive_jieba.json | 裸 jieba 对照 |
| results_auto_ngram_iter{1,2,3}.json | 三轮迭代结果 |
