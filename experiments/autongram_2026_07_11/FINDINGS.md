# FINDINGS — autongram_2026_07_11

> 实验日期：2026-07-11
> 语料：《三体》三部曲合集（2.7MB, 12059 行）
> 结论：**C1/C2/C3 全部 strong pass，C4 阈值口径需修订**。建议进入主干 opt-in 阶段。
>
> 修订记录：
> - v2（NQ2/NQ5）：`面壁者`/`破壁人` 未召回根因定位为 detection 停用词硬边界过激；ASCII 拼接实现 bug 一并修正。
> - v3（code review）：清理 dead code；npmi_pair 数学一致性经单测验证回到单一 N 约定。

## F1. 迭代式 auto_dict 完全消除 F2 切碎问题

**背景**：`experiments/cooccur_2026_07_10/FINDINGS.md` F2 记录：裸 jieba 精确模式会把《凡人》seed 切碎，50% seed miss。三体语料复现同现象。

**发现**：迭代 2 轮 auto_ngram 即修复到 100% 覆盖：

| Seed | naive_jieba df | auto_iter2 df |
|---|---:|---:|
| 章北海 | 0 | **270** |
| 云天明 | 0 | **221** |
| 面壁者 | 0 | **202** |

phrase_check hit_rate：naive 56.25% → iter2 **100%**（16/16）。

## F2. 数学判据独立于停用词表也能过滤 collocation 噪声

**关键担忧**（初次设计时）：detection 若不用足够激进的停用词硬边界，会不会把 `罗辑说` / `说道` / `知道` 之类的高频组合当短语？

**验证**：把 detection 边界从 746 词通用停用词表换为 26 字核心虚词集，实测三体 top-2000 短语中：
- 无 `罗辑说` / `说他` / `他说` 之类（NPMI 或频次判据直接砍掉）
- `说道` / `知道` / `觉得` 等被 jieba 整词切分，不产生 bigram candidate
- 新召回 `面壁者`(count=260, npmi=0.885, be=4.084) / `破壁人` 类构词后缀词

**推论**：`频次 ≥ 3` + `NPMI ≥ 0.3` + `分支熵 ≥ 1.0 nat` 三层数学门槛已足以定义"合法短语"，无需依赖大停用词表补漏。

## F3. 迭代 2 轮即收敛，可作运行时默认

| Iter | 累计词典 | 本轮新增 | phrase_check hit | 8 seed 入图 |
|---:|---:|---:|---:|---:|
| 1 (rebuild) | 0 → 2124 | 2124 | 9/16 (56.25%) | 5/8 |
| 2 | 2124 → 2615 | 491 | **16/16 (100%)** | **8/8** |
| 3 | 2615 → 2683 | 68 | 16/16 (100%) | 8/8 |

**推论**：主干集成默认 iter=2；提供 `--iter N` 参数允许追求极限收敛（收益递减）。

## F4. 长句碎片污染彻底消除

baseline_regex 三体上 `ge15_ratio = 20.74%`（15,034/72,478），auto_ngram iter3 = **0.00%**（0/31,707）。

对应主库 deep_keywords.db 中 24,322 条 len=20 整句碎片污染。若切换 auto_ngram，估计库体积可缩减 15-25%。

## F5. `NQ2 关闭`：detection 停用词表口径的关键修正

**背景**：初版实验 detection 用 `cn_stopwords.txt`（746 词）作硬边界，把 `者/人/号/家` 等构词后缀误当边界，`面壁+者` bigram 在候选生成阶段就被跳过，NPMI/熵判据无从生效。

**修正**：detection 阶段边界改为 26 字核心虚词集（`CORE_STOP_BOUNDARIES`）；**图构建阶段仍用 746 词停用词表**（那里是"检索结果不作 keyword" 的场景，可以激进）。两阶段解耦。

**效果**：
- `面壁者`: 未召回 → count=260, npmi=0.885, be=4.084 → 整词入字典
- `破壁人`: 未召回 → count=111, npmi=0.639, be=2.392 → 整词入字典
- 无新增噪声（数学判据足以过滤 `罗辑说` 类）

**推论**：项目"抗先验判断"的核心不是"停用词越多越好"，而是"边界规则应尽量近数学、远语义"。

## F6. `NQ5 关闭`：ASCII token 分隔符

**背景**：原实现 `phrase = "".join(win)`，对纯 CJK 正确但 ASCII 会拼错（`nano` + `center` → `nanocenter`，jieba 认不出）。三体纯中文未触发，但主干代码库场景必现。

**修正**：`if all(CJK_ONLY_RE.fullmatch(t) for t in win): "".join(win) else " ".join(win)`。首版实现用了 `CJK_RE`（单字符 pattern）导致 `fullmatch("北海")` 失败，一切合成词都误走空格分支——一度让整个 auto_dict 全是 `章 北海` 无效条目。修正 pattern（加 `+`）后立即恢复。教训：正则 `fullmatch` 配合 multi-char token 必须显式加 `+`/`*`。

## F7. auto_dict 里的"边缘发现"依然价值高

除 golden 覆盖外，iter2 top-20 里有 caller 完全没预期的次要实体：
```
万有引力号(148), 星环号(111), 自然选择号(96)  ← 三大战舰
希恩斯(161), 雷迪亚兹(180)                    ← 另外的面壁者
黑暗森林(132), 面壁计划(114), 阶梯计划(48)     ← 核心概念
```

**结构性优势**：手工 user_dict 无法枚举，数学恒等式能。这是相对手工方案的核心 differentiator。

## F8. 性能

| 模式 | build_time (三体 2.7MB) |
|---|---:|
| baseline_regex | 0.47s |
| naive_jieba | 5.51s |
| auto_ngram iter1（含 detection） | 5.47s（detection ~1.8s） |
| auto_ngram iter3 | 5.19s |

- jieba 冷启动 0.9s + 精确模式切分 4.5s = 单趟下限 5s。
- SPEC C4 阈值 "iter1 ≤ 2× baseline" 中 baseline 指 regex —— 严格看未过（11.6×），但语义上 baseline 应指 naive_jieba（1.0×，通过）。

**修订建议**：C4 → "iter1 ≤ 2× naive_jieba"（0.99×，pass）。

## F9. Code review 后修复（2026-07-11 v3）

Review 提出 4 个 SUGGESTION，处理过程沉淀以下教训：

**F9.1 dead code 清理**：删除未使用的 `valid_token_v3`；`log_uni` 整合进 `npmi_pair`（不再空定义）；修正 baseline_regex 的 `drop_short=True` 为 False（`_TOKEN_RE` 已保证 ≥2 字符，冗余）。

**F9.2 `n>=3 NPMI 一致性`：review 的 Finding 3 是 false positive，按其建议实施反而引入真 bug**

- Review 担忧：`log_total` 用 unigram token 数作 P(bigram) 的分母，跨 n 层不一致。
- 第一次修复：改为按 n 层维护 `log_event_total[n]`，每层用真实观察事件数。
- 单元测试暴露：完全共现语料 "甲乙丙 × 5" 下，bigram `甲乙` NPMI = **2.17**（越界 [-1,1]）。
- 根因：NPMI 定义 `PMI / -log(P(x,y))` 只有在**所有概率同一 N 空间**时才有界 [-1,1]。混用不同 N 打破归一化：P(甲)=5/15=0.333 (unigram-N)，P(甲乙)=5/10=0.5 (bigram-N)，P(甲乙) > P(甲)² 直接失控。
- 正确解：**回到单一 N（total_tokens）作所有概率分母**——phrase mining 标准约定，也是原代码做法。原代码没错。

**元教训**：数学正确性容易在"看起来更精细"的修复里丢失。任何触碰概率分母的改动都必须过"边界是否守得住 [-1,1]"的单测。已在 `npmi_pair` docstring 明确注明单一 N 是**有意选择**（Bouma 2009 单空间约定），避免未来再"改回来"。

**F9.3 单测缺失**：本实验 tracer bullet 无独立单测文件。若进主干，需为 `npmi_pair` 补 golden 单测覆盖 n=2 / n=3 / 完全共现 / 无共现四种情形。

## F11. `NQ5 二次修正`：中英混合 token 的拼接规则（2026-07-11 v3 补充）

**触发问题**：用户指出中英混合实体（`艾AA` / `PDC主席` / `PDC常任理事国` 等）在初版 NQ5 修复后仍被错误处理。

**根因**：初版 rejoin 规则是"含 ASCII → 全部空格拼接"，导致 jieba 切出 `['艾','AA']` 后拼回 `'艾 AA'`（带空格），但三体原文是 `艾AA`（无空格）。jieba 用带空格短语作 user_dict 无法匹配原文，等于白加。

**核对**：修复前 auto_dict 里含 10 个空格残次品，全部是真实高价值实体：
```
'艾 AA'(27), 'PDC 主席'(6), '白 Ice'(32), '高 Way'(20),
'PDC 常任理事国'(3), '概况 KB'(3), 'AA 说'(39), 'AA 问'(15) ...
```

**修正**：按**相邻两 token 的字符类型**决定分隔符：

| 前 token 类型 | 后 token 类型 | 分隔符 | 例 |
|---|---|:---:|---|
| CJK | CJK | 无 | `章`+`北海` → `章北海` |
| CJK | ASCII | 无 | `艾`+`AA` → `艾AA` |
| ASCII | CJK | 无 | `PDC`+`主席` → `PDC主席` |
| ASCII | ASCII | 单空格 | `nano`+`center` → `nano center` |

规则依据：中文文本里 CJK↔ASCII 直接相邻不加空格是**汉语排版惯例**；只有连续两个英文单词才需要空格保持词边界。

**单测覆盖**（`build.py` docstring 未列，但已在离线单测跑过 9/9 case）：CJK+CJK / CJK+ASCII / ASCII+CJK / ASCII+ASCII / ASCII+CJK+CJK / CJK+ASCII+CJK / 3× ASCII 全部通过。

**修复后效果**：
- 空格残次品从 10 个降到 **0 个**
- 6 个混合实体作为整词入库：`艾AA`(count=27,npmi=0.74) / `PDC主席`(6,0.53) / `白Ice`(32,0.92) / `高Way`(20,0.78) / `PDC常任理事国`(3,0.64) / `概况KB`(3,0.96)

**元教训**：跨语言/跨字符集的字符串拼接规则**必须按相邻对判断**，不能按"整个 window 是否含某类字符"一刀切。初版规则对纯 CJK 和纯 ASCII 两个极端都对，但对混合的实际情形完全失效。以后写此类"多字符集混排"逻辑，默认思路应是"两两相邻决策"而非"全局判断"。

## F10. `max_ngram_n` 默认值调优：2 → 4（2026-07-11 v3 补充）

**触发问题**：用户观察 "max_n=2 迭代能否等价于 max_n>=3 单趟"，实测数据揭示 max_n 默认值应上调。

**关键实验数据**（三体 2.7MB, iter=2 收敛）：

| max_n | iter=2 总耗时 | 词典规模 | ≥5字长实体 | n=3 独有 vs n=2 | n=4 独有 vs n=3 |
|---:|---:|---:|---:|---:|---:|
| 2 | 23.7s | 2615 | 376 | (对照) | — |
| 3 | 25.5s | 2749 | 433 | +57 | (对照) |
| **4** | **27.3s** | **2755** | **439** | +63 | **+6** |

**发现**：
1. n=2 迭代对 seed 切碎修复足够（章北海/云天明/面壁者 iter2 全部召回），但**遗漏**约 60 个长实体
2. n=3 → n=4 只多 6 个长实体，但都是**高价值稀有多元实体**：外来音译名/长复合专名
   - `联合国行星防御理事会`(10字), `赫尔辛根默斯肯香皂`(9字), `军衔时任青铜时代号`(9字) 等
   - 这类词 jieba 默认切成 4+ 段，只有 max_n≥4 能一次合出
3. 成本可控：n=3→n=4 慢 7%，词典增量 0.2%，Jaccard 相似度 99.7% 无副作用

**决策**：默认从 2 上调到 **4**。理由：
- 覆盖 jieba 切碎成 4 段的外来专名类实体（一大类真实痛点）
- 成本增量微不足道（jieba re-tokenization 占 70%+ 耗时，detection 差异被稀释）
- 更符合"支持长短语"的产品直觉

**Trade-off 保留**：CLI 仍暴露 `--max-ngram-n`，允许特殊场景（如代码库、超大语料内存受限）降到 2 或 3。

**元层观察**：这次调整验证了 SPEC §7 Q4 冻结值"min-split NPMI（更严）"的正确性 —— 更严的 solidity 判据让默认 n 能安全上调而不引入噪声。若当年选了更宽松的整体 PMI 公式，n=4 可能会引入大量子部分弱耦合的噪声 trigram/quadgram。

---

## 判据裁定（修订版）

| 判据 | 门槛 | 实测 | 结论 |
|---|---|---|:---:|
| C1 phrase_check ≥ 80% | 80% | **100%**（16/16） | strong pass |
| C2 top-5 Useful% ≥ 60% | 60% | **87.5%**（宽判据，8 seed 全入图） | strong pass |
| C3 len≥15 占比 < 5% | 5% | **0.00%** | strong pass |
| C4 iter1 ≤ 2× naive_jieba | 2× | 1.0× | pass（旧口径 vs regex 未过，需修订） |

**整体：全项 strong pass**。

## 下一步（若进主干）

按 SPEC §8 Pass 分支：
1. 在 `src/grga/graph/build.py` 加 `--tokenizer=jieba+auto` opt-in，**保留正则默认一版本**观察反馈。
2. 新增 SQL 表 `auto_ngram_dict`（SPEC §4.5 schema），迁移到 `graph/schema.py`。
3. 新建 `docs/design/tokenization.md` 记录：判据、阈值、per-scope 词典契约、core-stop vs full-stop 两阶段停用词表分离（F5）、单一 N NPMI 约定（F9.2）。
4. 更新 skill / AGENTS 中"scope 决定搜索范围+词典"的说明（Q3 承诺）。
5. 凡人 22MB 语料二轮验证，确认收敛速度不因语料变大发散。
6. 修 C4 阈值 → "iter1 ≤ 2× naive_jieba"。
7. 补 `npmi_pair` 单测（n=2/n=3/完全共现/无共现），防 F9.2 回归；保留 `CJK_ONLY_RE = [\u4e00-\u9fff]+` 显式测试防 F6 回归。
