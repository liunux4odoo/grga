# 方案：互补对信号（complementary pairs）—— 负侧显著性检测

> **状态**：设计定稿（含用户四问拍板 + 两处修订），待 code agent 认领
> **日期**：2026-09-11（修订：2026-09-12）
> **决策人**：用户（"维持正侧现状不改，增加负侧检测"；"不要先入为主当作别名检测"；
> "直接把互补对作为 related 中的一个输出字段"；"probe 里也要返回互补对，
> 尽量避免其它无关信息的返回，比如处理耗时"；"复用现有处理流程，不要额外计算负担"）
> **关联**：
> - `docs/design/plan-2026-09-11-probe-neighbors.md`（P0 · #1，已落地）
> - `docs/design/related-signals-v3.md`（related v3 契约；本方案扩其输出面）
> - `docs/design/roadmap.md`（P0 · #4）

---

## 0. TL;DR

在现有**共现对**（keyword_pairs，正 npmi 边）之外，新增一张**互补对**表
（`complementary_pairs`）：收录"共现显著低于随机期望"（显著负 npmi）的词对。
**查询面不新增动词、不新增 mode**：`related --mode graph` 和 `probe` 的输出里
各并列一个新字段 `complementary`，与共现 key / 邻居词同场给出——互补对是共现
对线索的**补充**。

**定位**：**不是"别名检测器"**。互补对是一类更泛的信号——"这两个词在正文里
异常地不同段出现"。别名的确是其中一个用途（同一实体的不同称呼天然互补），但
互补对的语义由 **caller 自己判断**：可能是别名、可能是一个范畴的两极（木属性/
火属性）、可能是不同时间线的称呼……grga 只暴露统计事实，不做语义归因。

**与正侧的关系**：正侧（keyword_pairs / related `results` / npmi 排序 /
probe `neighbors`）**完全不动**。本方案是**加法**：新表 + 输出里的新字段 +
probe 一处输出清理（去掉耗时），不改任何现有字段/排序/退出码的语义。

---

## 1. 现状与缺口

- 现有图只有**正关联**：`keyword_pairs` 存 `npmi ≥ min_npmi`（build 默认
  `BUILD_MIN_NPMI=0.2`）的共现对，`related --mode graph` 按 npmi 降序给出
  候选关键词。**负 npmi 的边在 build 时被滤掉，图里不存在**。
- 但**互补分布本身是强信号**（实证，合成语料 123 段 / 214 对）：
  - 别名对 `史强↔大史`：共现 c=3，随机期望 E[c]=19.3，c/E=0.16，
    超几何下尾 **P=3.5e-20**（显著互补）
  - 噪声对 `会议↔第0段`：c=1，E[c]=2.4，c/E=0.41，**P=0.089**（不显著）
  - **决定性指标不是 npmi 数值，而是"c 相对 E[c] 偏离多少 + 显著性"**
    （正负两侧通用；负侧这是主判别器，正侧是入场券——见决策留痕）
- 用户拍板：**维持正侧现状不改**，**增加负侧检测**作为对共现对线索的补充，
  互补对直接作为 `related` 与 `probe` 输出里的一个字段（与共现 key / 邻居词
  并列）。

---

## 2. 契约

### 2.1 数据层：新表 `complementary_pairs`

与 `keyword_pairs` 同构（per-scope、无向、`kw_a_id < kw_b_id` 规范化），
但只存**通过显著性门**的互补对：

| 字段 | 说明 |
|---|---|
| `scope_id` | 所属 scope（与 keyword_pairs 同语义） |
| `kw_a_id` / `kw_b_id` | 规范化对（a < b） |
| `cooccur_count` | 实际共现次数 c |
| `expected_count` | 随机期望 E[c] = df_a·df_b / N（N = 建图时该 scope chunk 总数） |
| `npmi` | Bouma npmi（负值） |
| `p_value` | 超几何下尾 P(共现 ≤ c \| 独立假设) |
| `updated_at` | 与 keyword_pairs 同语义 |

**build 时**：在现有 pair 生成路径上，对**被 min_npmi 门滤掉的负 npmi 边**
（npmi < 0 的那部分）追加一个互补显著性检查；通过者写入新表，不写入
keyword_pairs（正侧数据零变化）。**只对 npmi < 0 的对计算显著性**——
这是对全量负边的子集，build 期不增加全图扫描。

### 2.2 查询面一：`related --mode graph` 输出并入 `complementary` 字段

**不新增 mode / 不新增动词**。`RelatedKeywordsResult` 信封（`to_dict()`）
从两个字段扩成三个：

```jsonc
{
  "results": [ /* 共现 key，现有契约，逐字节不变 */ ],
  "complementary": [ /* 新：互补对，见 2.4 */ ],
  "resolved_seeds": [ /* 现有契约，不变 */ ]
}
```

- `complementary` 只存在于 graph mode 输出（heading mode 是文档返回路径，
  不适用）。
- 每个 seed 独立出一组互补行（`via_seed` 区分），**无跨 seed 融合**
  （与 `results` 的 D4 决策一致）。
- 排序：**默认 c/E 升序**（偏离随机期望最极端者在前）。
- 数量：受同一 `limit_per_seed` 约束（与 results 相同默认 30）。
- 空语义：seed 在词表但无显著互补对 → `complementary: []`；seed 不在词表 →
  `resolved_seeds` 照常标记，不报错。
- scope / tags 过滤：与 `results` **完全相同**的 scope 解析与 tag 过滤路径
  （同一函数入口，互补查询只在数据源上分叉）。
- 附带：MCP `related` 工具透传 `to_dict()`，**自动继承** `complementary`，
  无需额外改动。

### 2.3 查询面二：`probe` 输出并入 `complementary` 字段

`probe` 是 T1 换词候选的入口（已挂 `neighbors`），互补对与其并列：

```jsonc
{
  /* ...现有 probe 字段（除 elapsed_ms，见下）... */
  "neighbors": [ /* 现有 top-5 正邻域，逐字节不变 */ ],
  "complementary": [ /* 新：首关键词的互补对，见 2.4 */ ]
}
```

- **只取第一个 seed**（与 `neighbors` 现有逻辑一致——probe 是聚焦探测，
  多 seed 的互补归 `related`）。
- 排序 / 数量：与 related 相同（c/E 升序，`_PROBE_NEIGHBORS_LIMIT` 即 5）。
- 空语义 / 降级：与 `neighbors` 完全一致——无可用图 → 键缺席；图里有词但无
  互补对 → `[]`。**probe 热路径永不因图不可用而失败**（现有 try/except
  防御兜底同样覆盖互补查询）。
- **输出清理**：`probe` 移除 `elapsed_ms`（处理耗时）——JSON 和文本输出都去。
  现状核实：`elapsed_ms` 无任何决策逻辑消费（SKILL 参考文档列了它但没读它做
  判断），纯装饰性。保留的话等于每次 probe 都带一条 caller 用不上的信息，
  与"只返回有用字段"冲突。**这是本方案唯一动到现有输出的地方**，属于减法
  不是正侧行为改变。

### 2.4 输出字段：互补行（最小集）

**只返回 caller 做决策要用的字段**，字段面刻意比正侧 `RelatedKeyword`
（kw/npmi/cooccur_count/via_seed/chunk_df/source/scopes 七个）收敛：

```jsonc
{
  "kw": "大史",              // casefold 候选关键词
  "cooccur_count": 3,       // 实际共现 c（证据量：c=1 的互补 vs c=50 的互补可信度不同）
  "c_over_e": 0.16,         // 偏离度 c/E（决定性指标 + 排序键，见决策留痕）
  "p_value": 3.5e-20,       // 显著性（caller 自设门槛）
  "via_seed": "史强"        // 来源 seed（多 seed 区分；probe 单 seed 时恒等于探测词）
}
```

**为什么砍掉这些**（对比初版字段面）：

| 初版字段 | 处置 | 理由 |
|---|---|---|
| `kw` | ✅ 保留 | 候选词本体 |
| `cooccur_count` | ✅ 保留 | 证据量：p 值相同但 c=1 与 c=50 可信度不同 |
| `expected_count` | ❌ 砍掉 | c 和 c_over_e 都在 → E = c/(c_over_e) 可推导；E 的绝对值不改变 caller 任何决策 |
| `npmi` | ❌ 砍掉 | 已确立 npmi 数值**跨词对不可比**（R38 隐性偏好），互补侧决定性指标是 c/E + 显著性；留着反而诱导 caller 拿 npmi 值互相比较（错误用法） |
| `p_value` | ✅ 保留 | 显著性门槛，caller 的信任过滤器 |
| `via_seed` | ✅ 保留 | 多 seed 来源区分 |

**禁止**加 `is_alias` / `quality` / `confidence` / `semantic_tag` 等任何
L2+ 判断字段——**这是红线**。

文本输出：related / probe 的文本模式各追加一个 complementary 段（字段面与
JSON 一致，格式细节留给 code agent；probe 文本输出同时去掉 `elapsed:` 行）。

---

## 3. 边界红线（写死）

1. **不评价**：互补对表 + 查询输出只含统计事实（c、c/E、p_value）。
   禁止任何"这是别名/这是范畴两极"的归因字段。
2. **正侧零变化**：`keyword_pairs` / `related results` / `probe neighbors` /
   npmi 阈值 / probe neighbors（#1 已落地）**一个字节都不改**。`results`、
   `neighbors` 的内容、排序、数量与改动前逐字节一致。本方案对现有输出的
   唯一改动是 probe 去掉 `elapsed_ms`（减法，见 2.3）。
3. **显著性是门槛，不是排序**：build 门用显著性（p_value）筛掉噪声，
   查询排序用 **c/E 升序**（用户拍板）；**不是**用 npmi 数值（npmi 数值跨
   词对不可比，正负侧同理——见决策留痕）。
4. **复用不重写**：复用 `related_keywords` 的 scope / seed 解析、多 seed、
   limit、JSON 信封框架；互补查询走**同一对反向索引查找模式**
   （`(scope_id, kw_a_id)` / `(scope_id, kw_b_id)`），只在数据源上分叉。
   probe 侧复用 `_attach_neighbors` 同款 attach 路径（改名或加一个对称的
   `_attach_complementary`）。不新写第二套 CLI / 查询框架。
5. **零额外计算负担**（用户要求）：
   - 查询侧：每个 seed 一次 `complementary_pairs` 索引查找（O(limit)，
     与 keyword_pairs 同复杂度），不加载全表、不扫全图。
   - build 侧：只对 **npmi < 0**（将被 min_npmi 门滤掉）的对计算显著性，
     不做全图扫描、不做 O(V²) 全对对比。
6. **存储可控**：互补对表只存通过显著性门的子集（受 07-17 OOM 教训约束，
   不存全量负边）。默认门阈值见 §5 验收锚点。

---

## 4. 复用锚点（实现必须走这些）

| 锚点 | 用途 |
|---|---|
| `src/grga/graph/build.py` pair 生成路径 | 在 min_npmi 滤掉负边的**同一处**截留，对 npmi < 0 的对追加显著性检查 |
| `src/grga/related.py` `related_keywords` / `RelatedKeywordsResult` | 复用 scope 解析、seed 解析、多 seed、limit、JSON 信封；新增 `complementary` 字段 + 数据源分叉 |
| `src/grga/related.py` keyword_pairs 反向索引查找模式 | 互补查询照同一模式查 `complementary_pairs` |
| `src/grga/probe.py` `_attach_neighbors` / `_lookup_neighbors` | 互补查询照同一 attach 路径挂到 probe 输出；同时删掉 `elapsed_ms`（三处赋值 + cli.py 文本输出） |
| `src/grga/graph/schema.py` | 新表 `complementary_pairs`（与 keyword_pairs 同构） |
| 超几何下尾计算 | 纯数学恒等式（与 npmi、log1p 同类，符合"数学恒等式 grga 做完就好"） |
| `BUILD_MIN_NPMI`（=0.2） | 负边截留的判定基准：npmi < 0 且被此门滤掉的对 = 互补候选池 |
| MCP `related` 工具（`grga.mcp`） | **自动继承**：MCP 透传 `to_dict()`，信封加字段即工具输出加字段，无需额外改动 |

---

## 5. 验收行为（code agent 必须全部可复验）

> 测试进 `tests/unit/` + `tests/integration/`。以下用合成语料（史强/大史别名
> 结构，见 TL;DR 实证）表述。

- **A1 · 别名互补对被捕获（related）**：建含"史强/大史"互补结构的 scope 后，
  `grga related 史强 --mode graph --json` → `complementary` 数组含 `大史`，
  其 `p_value < 1e-6`，`c_over_e < 0.3`。
- **A2 · 噪声对不被捕获**：同 scope 里 `会议↔第0段` 这类共现 c=1、E[c]≈2.4、
  P≈0.089 的对 **不出现在** `complementary` 里（build 门已滤）。
- **A3 · 正关联对不进互补结果 + 正侧零变化**：`史强↔汪淼`（高共现、正 npmi）
  不在 `complementary` 里；同一查询的 `results` 数组**与改动前逐字节一致**。
- **A4 · 键缺席语义**：seed 在词表但无互补对 → `complementary: []`；seed 不在
  词表 → 与改动前相同 `resolved_seeds` 处理；无索引目录 → probe 不挂
  `complementary` 键（与 `neighbors` 一致）、related 与改动前相同降级。
- **A5 · 字段最小集 + 无判断字段**：互补行 JSON 只有
  `kw/cooccur_count/c_over_e/p_value/via_seed` 五个键（probe 的 via_seed 恒等
  于探测词也保留，保持单一契约）；grep 不到 `expected_count` / `npmi` /
  `is_alias` / `quality` / `confidence` / `semantic_tag`（红线 1 + 字段收敛
  的机械验证）。
- **A6 · probe 去耗时**：probe 的 JSON 与文本输出**都不含** `elapsed_ms` /
  `elapsed:`；现有 probe 其余字段与改动前逐字节一致（`neighbors` 在内）。
- **A7 · probe 补互补**：`grga probe 史强 -d <dir> --json`（有索引）→ JSON 含
  `complementary`，其中 `大史` 的 `c_over_e < 0.3`；无索引目录 → 无该键，
  且 probe 正常返回（热路径不破）。
- **A8 · 现有测试全绿**：keyword_pairs / related graph / probe neighbors /
  probe 既有测试不因本次改动失效（红线 2 + 输出清理的证明）。
- **A9 · 查询开销同数量级**：对无互补对的 seed，`related --mode graph` 调用
  耗时与改动前**同数量级**（互补查询是索引查找，非全图扫描——红线 5 的验证）。
- **A10 · 文档更新**：CLI.md + SKILL.md 补 complementary 字段说明（见 §7）。

---

## 6. 边界维度表

| 维度 | 行为 |
|---|---|
| 空 seed | 不变（现有 error 路径） |
| seed 不在词表 | `resolved_seeds` 照常标记（与 graph 模式一致）；probe 同 `neighbors` 空语义 |
| seed 在词表、无显著互补对 | `complementary: []` |
| 无索引 / 无命中 scope | related 与 graph 模式一致降级；probe 不挂 `complementary` 键（热路径不破） |
| 负边但 p 不显著 | build 门滤掉，不进表 |
| 正关联对 | 不进表（npmi ≥ 0 不是互补候选） |
| 零共现对（c=0） | **不在本方案范围**（用户拍板；全量零共现扫描是 O(V²)，留给 caller 自行 df 对比） |
| 多 seed（related） | per-seed 行 + via_seed（同 graph 模式）；probe 只取第一个 seed |
| tag / scope 过滤 | 与 graph 模式完全一致（同一解析路径） |
| heading mode | 无 `complementary`（文档返回路径，不适用） |
| probe 输出 | 增加 `complementary`，去掉 `elapsed_ms`，其余不变 |

---

## 7. 文档更新

- `docs/CLI.md`：`related --mode graph` 输出说明补 `complementary` 字段；
  `probe` 输出说明补 `complementary` 字段、删 `elapsed_ms`。
- `src/grga/skills/grga/SKILL.md` + `references/reflection-loop.md` +
  `references/verbs.md`：T0 probe 一节补"`complementary`（可选）— 首关键词的
  互补对（`{kw, cooccur_count, c_over_e, p_value}`），作为**换词候选/词汇盲区
  补充线索**；不是别名检测器，语义归 caller 判断"；verbs.md 信封字段表删
  `elapsed_ms`；T2 related 一节补 complementary 字段说明。
- `docs/design/decisions.md`：决策留痕（见 §8）。
- `docs/design/roadmap.md`：P0 #4 条目更新（并入 related + probe 输出字段形态）。

---

## 8. 决策留痕（为什么这么定）

- **命名避开"别名检测"**（用户拍板）：互补对是泛信号，别名只是用途之一。
  若命名为别名检测，会把语义归因强加给统计信号——违背"不评价"红线，也
  缩小了适用场景（范畴两极、时间线称呼等同样适用）。
- **决定性指标 = c 相对 E[c] 偏离 + 显著性，不是 npmi 数值**：
  - 负侧：这是主判别器（别名 P=3.5e-20 vs 噪声 P=0.089，干净分开）
  - 正侧：这只是入场券（min_npmi 门已滤；换排序键也救不了 hub——眉梢/墨大夫
    c/E 相同——那是语义问题，grga 不做）
  - 正侧维持现状（用户拍板），负侧用显著性做门槛
- **查询面：并入 related + probe 输出字段，而非新 mode / 新动词**（用户拍板）：
  - 少一个契约面——复用 `RelatedKeywordsResult` 信封、`_attach_neighbors`
    路径、scope 解析、limit、多 seed 语义，code agent 只加字段 + 数据源分叉
  - probe 与 related 同场给出互补对，让 T1 换词阶段就能撞见互补线索（别名
    盲区的最早拦截点），不必等 T2 bookend
  - 代价：每次 related / probe 调用都顺带查互补表——但查询是索引查找
    （O(limit)，同 keyword_pairs 复杂度），零额外计算负担成立（红线 5）
- **互补行只返回决策字段，不返回价值低的信息**（用户拍板修订）：
  - 砍 `npmi`：已确立跨词对不可比（R38 隐性偏好），互补侧决定性指标是
    c/E + 显著性；留着诱导错误比较
  - 砍 `expected_count`：c 与 c_over_e 都在 → E 可推导；E 绝对值不改变
    caller 决策
  - 保留 `cooccur_count`：p 值相同但证据量不同（c=1 vs c=50），caller 的
    信任过滤器
- **probe 去掉 `elapsed_ms`**（用户拍板修订"避免无关信息返回"）：现状核实
  无任何决策逻辑消费（SKILL 参考文档只列不读）；纯装饰性输出，与最小字段
  原则冲突
- **build 门默认 p_value < 0.05**（用户拍板"可以"）：合成语料里噪声 0.089 /
  别名 3.5e-20 之间取 0.05 已能干净分离；同时暴露 p_value，caller 可自行
  再筛。真实语料大词对下若需更严，caller 用 p_value 过滤即可，无需改门。
- **查询排序 c/E 升序**（用户拍板）：显著性已是门槛，排序用"偏离强度"
  （c/E）比 p_value 更直观（p 值在显著区内都极小，排序意义弱）。
- **c=0 零共现对不做**（用户拍板）：现无 pair 记录（keyword_pairs 只有
  c ≥ 1 的对）；全量零共现检测是 O(V²)，成本不可接受。若未来需要，单独开题。
- **复用现有处理流程、零额外计算负担**（用户要求）：build 侧只对负 npmi
  对算显著性；查询侧同复杂度索引查找。不做全图扫描、不做 O(V²) 对比。
- **每次构建清空 complementary_pairs**（用户曾挑战"互补对应是可保存资产，
  为什么清空"）：结论 — **维持清空**。详见 §10「已挑战问题」。
- **只存显著子集，不存全量负边**（红线 6）：07-17 OOM 教训——低阈值边数量
  巨大；互补表只收显著对，体积可控。
- **超几何检验是纯数学恒等式**：与 npmi、log1p 同类（07-10 R32："数学恒等式
  grga 做完就好"），不引入语义先验，符合加工边界。

---

## 9. 已拍板清单（原待挑战项全部收敛）

| 原问题 | 拍板 |
|---|---|
| 查询入口：新 mode vs 独立命令 vs 输出字段 | **并入 related 输出字段**（与共现 key 并列）；**probe 同样并入** |
| build 门阈值：0.05 vs 更严 | **0.05**，暴露 p_value 供 caller 再筛 |
| 排序键：c/E vs p_value vs npmi | **c/E 升序** |
| c=0 零共现对是否立项 | **不做** |
| 互补行字段面 | **只留 kw/cooccur_count/c_over_e/p_value/via_seed**（砍 npmi、expected_count） |
| probe 输出 | **加 complementary，去 elapsed_ms**（唯一动到现有输出的减法） |

## 10. 已挑战问题

### 10.1 "互补对应是可保存资产，为什么每次构建清空？"（用户拍板后挑战）

**用户论点**：互补对跟共现对（keyword_pairs）、字典（auto_ngram_dict）一样，应该是可保存的资产，build 时清空会丢弃已发现的互补知识。

**结论：维持清空**（理由如下）：

**事实基**（代码证据）：
- keyword_pairs 在增量构建 (`mode="build"`) 下是 **UPSERT 累加**：
  ```sql
  ON CONFLICT(...) DO UPDATE SET
      cooccur_count = cooccur_count + excluded.cooccur_count
  ```
  旧 chunk 的配对数据永久保留，不清空。
- 步骤 4 的 npmi UPDATE 覆盖全 scope：`UPDATE keyword_pairs SET npmi = ... WHERE scope_id = ? AND source = ?`
- complementary_pairs 的重提取**从 keyword_pairs 的当前负 npmi 集合读取**——而 keyword_pairs 已累计全量 chunk 数据。所以**清空 complementary_pairs ≠ 丢数据**：未变更 chunk 的互补关系在重提取时会重新出现（数据已累计在 keyword_pairs 里）。

**为什么不清除反而有害**：
complementary_pairs 是**纯派生信号**——没有 raw 成分。
对比三者的资产性质：

| 资产 | raw 成分 | 派生成分 | 可否"原样保存" |
|---|---|---|---|
| auto_ngram_dict | 短语本身（如"404避难所"） | — | 可，一旦学会永留 |
| keyword_pairs | cooccur_count（可累加） | npmi（每 build 重算） | raw 可累加，派生重算 |
| complementary_pairs | **无**（c/E/p 全可推导） | c/E + p_value（每 build 重算） | 不可，无 raw 可留 |

若不清除 complementary_pairs：
- pair 从互补→共现（如 c 从 3 涨到 50，p 从 3.5e-20 → 0.9）后，**表内会保留已 transition 的旧行**（p=0.9 仍标记为"显著互补"——错误）
- E[c]、p_value 已随 corpus 变化而失效（df_a/df_b/N 变了），保留等于陈腐信号

**所以"保存资产"的正确机制是：keyword_pairs 累加 raw + 每 build 重算派生 npmi；complementary_pairs 每 build 从已累加的 keyword_pairs 重提取派生信号。** 两者同构，不对称。代码注释已写此 rationale：*"Pairs can transition between complementary and positive across incremental builds, so we clear the table first and re-populate from the current negative-npmi set"*（`build.py:4038-4042`）。

**代价验证（符合"零额外计算负担"红线）**：重提取在步骤 4 内批量执行（`_extract_complementary_sql`，分批超几何检验），与 npmi 重算同代价，不额外增加构建负担。

### 10.2 已解决（§9 清单）

见 §9。

## 11. 剩余开放点（code agent 可自行定，不回退拍板）

 （同原 §9，内容不变）
