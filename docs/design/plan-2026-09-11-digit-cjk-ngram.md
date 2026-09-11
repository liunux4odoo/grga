# 方案：数字+汉字混合短语支持（变体甲：数字仅在 CJK 邻接时成 token）

> **状态**：待实施（设计已拍板）
> **日期**：2026-09-11
> **决策人**：用户（三个决策点全部确认）
> **关联代码**：`src/grga/graph/build.py`（`_COMBINED_RE` / `_DIGIT_RUN` / `_keywords_from_tokens`）
> **关联文档**：`docs/design/tokenization.md`（分词器契约）、`docs/design/decisions.md`（决策留痕）

---

## 0. TL;DR

让 `404避难所` / `404号避难所` / `三体3` 这类**数字+汉字混合短语**能被 n-gram 检测发现并进入 `auto_ngram_dict`。

**变体甲（保守，用户拍板）**：数字串**仅在紧邻 CJK 字符时**才产出为 token；裸数字（`404`）、纯数字串（`3.3`）、数字夹在符号里的情况保持现状（丢弃）。

配套**索引层闸**：数字 token 参与 n-gram **检测**（让混合短语能被发现），但**不单独落为 keyword**（防止 `404` 变成共现图 hub 污染 `keyword_pairs`）。

**不改**：`extract_chunk_keywords` / `_TOKEN_RE`（back-compat 纯 regex 路径冻结，tokenization.md §8 兼容承诺）。

---

## 1. 问题背景

### 1.1 现象

`404避难所`、`404号避难所` 不在 `auto_ngram_dict` 里，即使它们在语料中反复出现。

### 1.2 根因

`_COMBINED_RE = _ASCII_TOKEN | _CJK_RUN`，其中：

```python
_ASCII_TOKEN = r"[A-Za-z][A-Za-z0-9_+#.-]{0,38}[A-Za-z0-9_+#]"
```

**首字符必须是 `[A-Za-z]`**。以数字开头的串（`404`、`1.2.3`、`33a`）不是合法 ASCII token，也不是 CJK run → 在分词阶段被当作字符间噪声**直接丢弃**，根本走不到 n-gram 检测。

### 1.3 为什么这是缺口而不是设计

- **字母+汉字混合**（`PDC主席`、`艾AA`）**已经支持**：ASCII token 被保留，rejoin 规则（CJK|ASCII → 无分隔）正确处理，`detect_ngram_phrases` 的窗口也允许混 CJK/ASCII。
- **数字+汉字被排除**纯粹是 `_ASCII_TOKEN` 首字符必须为字母的历史遗留（v1-v5 为 code-search 保零回归）。
- 在中文语料里（科幻/游戏/设定集/网文），`404避难所`、`三体3`、`第404章` 这类实体常见且有意义。**形式不该预设，该让统计闸决定**（铁律 2：纯数学判断、零语言先验）。

### 1.4 已实证的行为（2026-09-11 实测）

```
'404避难所'   -> ['避难所']        ← "404" 直接消失（现状）
'404号避难所' -> ['号', '避难所']   ← "404" 直接消失（现状）
'PDC主席'     -> ['PDC', '主席']   ← 字母被保留（现状，已支持）
'艾AA'        -> ['艾', 'AA']      ← 字母被保留（现状，已支持）
'v1.2.3'      -> ['v1.2.3']       ← 字母开头整词保留（现状）
'1.2.3'       -> []               ← 数字开头整词清空（现状）
```

---

## 2. 决策记录（用户拍板）

| # | 决策点 | 结论 | 理由 |
|---|---|---|---|
| D1 | 短语形式是否必须符合 code 变量命名 | **否** | `33a`/`3.3` 非法变量名但可成有意义短语；形式不预设，统计闸决定 |
| D2 | 检测判据 | **按当前统计规则**（freq ≥3 + NPMI ≥0.3 + 分支熵 ≥1.0），不引入新语义先验 | "只看频率"是笼统说法，本意是按现有统计闸判定 |
| D3 | 数字 token 产出条件 | **变体甲（保守）**：仅在 CJK 邻接时产出 | 覆盖 `404避难所` 核心诉求，裸数字零风险 |
| D4 | 邻接方向 | **双向**（数字在汉字前/后都支持） | `三体3`、`避难所404` 后缀数字实体同样常见；与 rejoin 表 CJK|ASCII 对称 |
| D5 | `extract_chunk_keywords` / `_TOKEN_RE` | **不改**（保持冻结） | back-compat 纯 regex 路径专供外部 caller，v6 起已分离；tokenization.md §8 兼容承诺 |

---

## 3. 方案设计

### 3.1 检测层：`_DIGIT_RUN` 正则分支

在 `build.py` 的 `_COMBINED_RE` 增加一个数字分支，**仅当数字串紧邻 CJK 字符时匹配**：

```python
_DIGIT_RUN = r"\d+(?=[\u4e00-\u9fff])|(?<=[\u4e00-\u9fff])\d+"
# 或等价的替代写法，由实施者决定；语义要求：
#   - 数字串后紧跟 CJK（lookahead）→ 产出
#   - 数字串前紧邻 CJK（lookbehind）→ 产出
#   - 裸数字 / 数字前后均非 CJK → 不产出

_COMBINED_RE = re.compile(_ASCII_TOKEN + "|" + _DIGIT_RUN + "|" + _CJK_RUN)
```

**alternation 顺序注意**：`_ASCII_TOKEN` 必须仍在最前（保证 `A404B` 这类整体匹配为单个 ASCII token，不拆出 `404`）。`_DIGIT_RUN` 放中间，`_CJK_RUN` 最后。

### 3.2 检测层的预期行为

| 输入 | 变体甲后 token | 后续走向 |
|---|---|---|
| `404避难所` | `['404', '避难所']` | rejoin → `404避难所`；窗口 [`404`,`避难所`] → 统计闸判定 |
| `404号避难所` | `['404', '号', '避难所']` | 窗口 [`404`,`号`]、[`号`,`避难所`]…统计闸判定 |
| `避难所404` | `['避难所', '404']` | rejoin → `避难所404` |
| `第404章` | `['第', '404', '章']` | 若反复出现 → `第404章` 成短语 |
| `三体3` | `['三体', '3']` | rejoin → `三体3` |
| 裸 `404` / `3.3` / `2024` | `[]`（同现状） | **不进流** |
| `A404B` | `['A404B']` | code 场景零回归 |
| `v1.2.3` | `['v1.2.3']` | code 场景零回归 |

**数字 token 不经过 jieba**：`tokenize_hybrid` 里 `_CJK_ONLY_RE.fullmatch("404")` 为 None → 走 `else` 分支原样保留。rejoin 里 `_is_ascii_tok("404")` 为 False → 数字+汉字直接拼接无空格（`404避难所`）。**`_join_by_pair_rule` 无需改动**。

### 3.3 索引层闸：数字 token 不单独落 keyword

**为什么必须有这道闸**：`_keywords_from_tokens` 把所有非 stopword token 直接 casefold 落成 keyword。如果不设闸，`404` 会以 keyword 身份进 `keyword_chunks` / `keyword_pairs`——数字天然高频且上下文无关（跟谁都共现），会成为共现图 hub。参考 07-10 R28 教训：hub 邻居全是噪声；纯数字 hub 是加强版（无身份可消歧）。成本落在 `keyword_pairs`（DB 97%、76M rows、build 77% 时间）。

**做法**：`_keywords_from_tokens` 增加过滤——**纯数字 token（`^\d+$`）不落 keyword**。合并后的混合短语（`404避难所`）不受影响（它不是纯数字）。这是"数字进检测不进索引"的落地。

**复用两层分离设计**：检测层（`_CORE_STOP_BOUNDARIES`）≠ 索引层（`_STOPWORDS`/`_INDEX_STOPWORDS`）本来就是两个集合。本方案延续这一模式：检测层允许数字 token 进窗口，索引层把纯数字 token 挡在 keyword 之外。

**注意**：`_DIGIT_RUN` 只产出紧邻 CJK 的数字 token。理论上"紧邻 CJK"已经大幅降低裸数字污染，但**高频 CJK 邻接数字仍会成 keyword**（如 `2024年` → `2024` 若没合并成短语，`2024` 就有 df）。索引层闸是防御性兜底，两种变体都该有。

### 3.4 不改的东西

- `_TOKEN_RE`（back-compat 纯 regex）—— `extract_chunk_keywords` 外部 caller 行为不变
- `_CJK_RUN` / `_CJK_ONLY_RE` / `_join_by_pair_rule` / `_is_ascii_tok` —— 均无需改动
- schema（v11 不变）、CLI、契约（related-signals-v3 不变）

---

## 4. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| `第404章` 这类章节号模式高频成短语刷字典 | 中 | 统计闸（NPMI/熵）应挡住纯格式噪声；落地后实测验证，若刷字典严重再评估 |
| 检测层噪声增多（数字参与窗口） | 低-中 | 变体甲已最小化（仅 CJK 邻接）；索引层闸兜底 |
| alternation 顺序引入回归（`A404B` 被拆开） | 低 | `_ASCII_TOKEN` 保持最前；code 语料回归测试覆盖 |
| 大语料 build 时间/内存增量 | 低 | 新增 token 量有限（仅 CJK 邻接数字）；无需改 PR-4 流式结构 |
| 与 batched build 的交互 | 低 | 检测逻辑不变，仅 token 流多一类 token；batched 测试覆盖 |

---

## 5. 验收标准

### 5.1 单元测试（`tests/unit/` 或 `tests/` 现有 tokenizer 测试文件）

1. `tokenize_hybrid("404避难所", tok) == ["404", "避难所"]`
2. `tokenize_hybrid("404号避难所", tok) == ["404", "号", "避难所"]`
3. `tokenize_hybrid("避难所404", tok) == ["避难所", "404"]`（双向邻接）
4. `tokenize_hybrid("第404章", tok) == ["第", "404", "章"]`
5. `tokenize_hybrid("三体3", tok) == ["三体", "3"]`
6. **裸数字不进流**：`tokenize_hybrid("404", tok) == []`、`tokenize_hybrid("3.3", tok) == []`、`tokenize_hybrid("2024", tok) == []`
7. **code 零回归**：`tokenize_hybrid("A404B", tok) == ["A404B"]`、`tokenize_hybrid("v1.2.3", tok) == ["v1.2.3"]`
8. **索引层闸**：`_keywords_from_tokens(["404", "避难所"])` 不含 `"404"`，含 `"避难所"`
9. **rejoin**：`_join_by_pair_rule(("404", "避难所")) == "404避难所"`、`_join_by_pair_rule(("三体", "3")) == "三体3"`

### 5.2 端到端（`tests/integration/`）

10. 构造含反复出现的 `404避难所` 的小语料，`build_graph_index` 后 `auto_ngram_dict` 含 `404避难所`
11. `keyword_stats` / `keyword_pairs` 中**不出现** `404` 单独作为 keyword（纯数字被索引层闸挡掉）

### 5.3 回归

12. 全量 pytest（141 项基线）全绿
13. 既有 tokenizer 测试（`test_tokenize_hybrid_*`、`test_join_by_pair_rule_*`、`test_detect_ngram_phrases_*`、`test_build_populates_auto_ngram_dict`）不变

---

## 6. 实施范围

- **改**：`src/grga/graph/build.py`（`_DIGIT_RUN` 常量 + `_COMBINED_RE` 组合 + `_keywords_from_tokens` 数字过滤）
- **改**：`docs/design/tokenization.md`（§0 TL;DR 补数字分支说明、§4 rejoin 表补数字行）
- **改**：`docs/design/decisions.md`（决策留痕）
- **新增**：测试（上述 5.1/5.2）
- **不改**：schema、CLI、契约、`_TOKEN_RE`、其他模块

---

## 7. 待实施者确认的开放点

1. `_DIGIT_RUN` 的正则写法（lookahead/lookbehind vs 其他等价形式）——语义以 §3.1/§3.2 行为表为准
2. `_keywords_from_tokens` 数字过滤的精确位置——是加一个 `_is_digit_only(tok)` 判断，还是复用现有 stopword 机制，由实施者按代码结构决定
3. 数字 token 的 casefold 行为——纯数字 casefold 无变化，但确认与现有 keyword 归一化路径一致

---

## 8. 决策留痕（为什么是这个方案）

- **为什么不是"放开整个正则"（变体乙）**：`33a`/`3.3`/`2024` 会全部变 token 进流，数字 hub 洪泛，成本落在最贵的 `keyword_pairs` 表。变体甲保留"裸数字不进流"，零风险覆盖核心诉求。
- **为什么不是"只看频率"**：纯频率是 07-10 实验证否过的（V0 raw count P@5=0.10 vs V5 npmi 87.5%）。保留三闸（freq+NPMI+熵），只去掉形式闸。
- **为什么索引层要设闸**：`_keywords_from_tokens` 现状把所有非 stopword token 落 keyword；数字天然高频且上下文无关，不设闸会成 hub（R28 教训）。
- **为什么 `extract_chunk_keywords` 不改**：back-compat 纯 regex 路径专供外部 caller，v6 起已与内部 pipeline 分离；改动会破坏 tokenization.md §8 兼容承诺。
- **为什么双向邻接**：`三体3`、`避难所404` 这类后缀数字实体在设定集里常见；与 rejoin 表 CJK|ASCII 对称。
