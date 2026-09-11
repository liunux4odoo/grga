# 方案：probe 词汇邻域提示（probe neighbors）

> **状态**：设计定稿，待 code agent 认领
> **日期**：2026-09-11
> **决策人**：用户（边界红线确认 + 补充"输出字段最少，避免成为 related 替代品"）
> **关联**：
> - `docs/design/roadmap.md`（P0 · #1）
> - `docs/design/related-signals-v3.md`（related v3 契约；本方案复用其图查询层）
> - `docs/design/decisions.md`（决策留痕）

---

## 0. TL;DR

`grga probe` 在有图索引的目录上运行时，JSON 输出顺带附一个 **`neighbors`** 字段：
该词在 `keyword_pairs` 图里的少而精近邻（默认 top-5，只含 `{kw, npmi}`）。

**目标**：把 T2 的一小块能力下沉到 T1——caller 在 probe（摸边界）时就能看到替代叫法
（例：`probe 大史` 的 neighbors 里有 `史强说`），当场换词，不必等到 T2 `related` 才发现。
**定位**：是"换词候选"提示，**不是** related 的替代品——related 仍是全量图校准
（top-N 更多、字段更全、看形态）。

**最高价值场景**：probe 命中但 caller 可能收敛过早（"假通"），neighbors 给出别名候选
作为第一道防线；以及 0-hit（词汇错位）时给出换词方向。

---

## 1. 现状与缺口

- `probe_directory()`（`src/grga/probe.py`）是**纯 L1 动词**：只跑 `rga --count-matches`
  （或 `--path-only` 时 `rg --files`），JSON 含 `ok/directory/patterns/terms/pcre2/
  implicit_operator/implicit_notice/total_lines/file_count/truncated/warning/
  skipped_files`（+ 条件性的 `per_file/sample/elapsed_ms`）。**完全不碰图索引**。
- `related_keywords()`（`src/grga/related.py`）是现成图查询入口：单 seed → `keyword_pairs`
  按 `npmi DESC` 取 top-N，O(limit_per_seed) 索引查询（07-17 优化后毫秒级）。
- **目录→scope 解析**：`scope_directories` 表存 scope_id→directory 绑定；`IdInterner`
  只有 `list_directories(scope_id)`（反查），**没有现成 directory→scope_id 正向函数**。
  本方案需要这个解析（见 §4 复用锚点）。

---

## 2. 契约

### 2.1 JSON 输出（`--json`）

现有字段**一个都不动**。新增**一个可选顶层字段**：

```jsonc
{
  // ...全部现有字段不变...
  "neighbors": [          // 可选；仅在"目录命中已绑定 scope"时出现
    { "kw": "史强说", "npmi": 0.51 },
    { "kw": "科学边界", "npmi": 0.56 }
  ]
}
```

- `neighbors` 每项**恰含 2 个字段**：`kw`（str）+ `npmi`（float，round 6）。
  **禁止**加 `cooccur_count` / `chunk_df` / `via_seed` / `source` / `scopes` 等
  related 字段——那是 related 的字段面，加了就成替代品。
- 按 `npmi DESC` 排序（复用 related 同款排序键，数学恒等式）。
- **默认 top-5**，无 `--neighbors N` 之类的数量 flag（保持契约最小；要更多看形态走 related）。
- **键缺席语义**（区别于空数组）：
  - `neighbors` **缺席** = 该目录没有可用的图索引（无命中 scope / DB 不可用 / 图查询失败/超时）
  - `neighbors: []` = 有索引，但该 seed 在 `keywords` 词汇表里无候选（未知词 / 孤立节点 / 全部低于 npmi 下限）
  - 键缺席 ≠ 空数组：caller 据此区分"没有图可用"vs"图里有这个词但无邻居"

### 2.2 非 JSON 人类输出

有 `neighbors` 时追加一行（仅当非空）：

```
neighbors: 史强说(0.51), 科学边界(0.56)
```

空数组 / 缺席 → 不输出这行。人类输出保持极简。

### 2.3 边界

- **只在内容探测（非 `--path-only`）时附加**：`--path-only` 是文件路径发现，与词级
  邻域正交，不做。
- **多关键字 probe**：只对**第一个** seed 输出 neighbors（探针聚焦，保持最少）。
  其余 seed 的邻域走 `related`（T2）。此点写进文档。
- **不新增 `--db-path`**：probe 保持无 DB 参数（与现状一致）；邻域查询使用
  `related_keywords` 默认的共享 DB 路径（`DEFAULT_DB_PATH`）。若要查非默认库，
  这是 later 讨论项，不在本方案范围。

---

## 3. 边界红线（用户确认，写死）

1. **不评价**：neighbors 只含 `kw + npmi` 两个**可数字化事实**字段；禁止
   `is_alias` / `quality` / `confidence` / `semantic_tag` 等任何 L2+ 判断字段。
   caller 自己判断"史强说"是不是"史强"的别名。
2. **输出最少**：每项 2 字段；列表 ≤ 5；不加数量 flag；不做多 seed 展开。
   **避免成为 related 替代品**（related = 全量图校准，probe = 少而精换词候选）。
3. **不拖慢热路径**：邻域查询任何失败（DB 打不开 / 表不存在 / 超时 / 异常）→
   **静默省略 neighbors 键**，probe 照常返回，绝不把图错误当 probe error。
4. **复用不重写**：邻域数据必须来自现有 `keyword_pairs` 图查询层
   （`related_keywords` 同源），**不得新写第二套邻域扫描逻辑**（防双份代码漂移）。
5. **additive 契约**：probe 现有 JSON 字段 / 人类输出 / 退出码 **0 变化**。
   仅新增可选键。

---

## 4. 复用锚点（实现必须走这些，不新造）

| 锚点 | 用途 |
|---|---|
| `grga.related.related_keywords(seeds, limit_per_seed=N, db_path=None, ...)` | 图邻域查询入口。传 `[第一个 seed]` + `limit_per_seed=5`，取 `result.results` 的 `{kw, npmi}` |
| `scope_directories` 表（`graph/ids.py` IdInterner 管理） | **directory→scope 解析**：按 probe 的 `directory` 反查绑定。需新增一个正向解析（见下） |
| `DEFAULT_DB_PATH`（共享库路径解析） | 邻域查询的默认 DB |

**目录→scope 正向解析缺口**：现有只有 `list_directories(scope_id)`（反查）。本方案需要
"给定目录 → 命中哪些 scope_id"。实现该解析时：

- 语义：probe 的 `directory`（已 resolve 的绝对路径）与 `scope_directories.directory`
  做匹配。**前缀匹配**（probe 目录是绑定目录自身或其子目录）视为命中；
  不做模糊 / 通配 / 大小写折叠猜测。
- 命中多个 scope 时：按什么取？**不合并、不平均**——按绑定 `indexed_at` 最新者取一个
  （与 `list_directories` 的"primary directory = [0]"语义一致）。本方案不引入
  多 scope 邻域合并（那是 related 的 `--scope` 多选能力，probe 保持单 scope 极简）。
- 若现有代码里已有等价解析（可能藏在 deep_search / MCP），**优先复用**；只有确无现成
  时才新增，且必须走 `scope_directories` 表（禁止猜路径 / 扫目录）。

---

## 5. 验收行为（code agent 必须全部可复验）

> 用最小脚本 / CLI 直接调 `probe_directory` 验证，测试进 `tests/unit/` + `tests/integration/`。

- **A1 · 有索引 + seed 在图里**：建一个小 scope（含"大史/史强"别名语料），
  `grga probe 大史 --json` → 输出含 `neighbors`，长度 ≤ 5，每项恰含 `kw`+`npmi`，
  按 `npmi` 降序，且前几个与 `grga related 大史 --mode graph --limit 5 --json`
  的 `results` 前 5 个 `(kw, npmi)` **逐项一致**（同源证明）。
- **A2 · 有索引 + seed 无候选**：probe 一个图里没有的词 → `neighbors: []`（键存在、空数组）。
- **A3 · 无索引目录**：未建索引目录上 probe → **无 `neighbors` 键**，其余字段与
  改动前逐字节一致（additive 证明）。
- **A4 · `--path-only`**：无论有无索引 → 无 `neighbors` 键。
- **A5 · 图查询异常降级**：制造 DB 不可用 / 查询异常 → probe 正常返回，`neighbors` 缺席，
  退出码 0，不抛错（热路径保护证明）。
- **A6 · 多关键字**：`probe A B` → `neighbors` 只反映第一个 seed A。
- **A7 · 人类输出**：非 `--json` 且 `neighbors` 非空 → 打印 `neighbors: ...` 一行；
  空 / 缺席 → 不打印。
- **A8 · 现有测试全绿**：probe 相关既有测试不因本次改动失效。

---

## 6. 边界维度表（8 维，防漏）

| 维度 | 行为 |
|---|---|
| 空 seed | 不变（现有 `keywords cannot be empty` 路径） |
| 目录不存在 / 非目录 | 不变（现有 error 路径） |
| 无索引 / 无命中 scope | `neighbors` 缺席 |
| 有 scope + seed 在图 | top-5 `{kw, npmi}`，npmi 降序 |
| 有 scope + seed 不在图 | `neighbors: []` |
| 图查询超时 / DB 异常 | `neighbors` 缺席（静默降级） |
| `--path-only` | `neighbors` 缺席 |
| 多关键字 | 只对第一个 seed |

---

## 7. 文档更新

- `docs/CLI.md`：probe 一节补 `neighbors` 字段说明（含"键缺席 vs 空数组"语义、仅首 seed）。
- `skills/grga/SKILL.md`：T1 一节加一句——"有索引时 probe 附带 `neighbors`
  （少而精换词候选，默认 5 个）；要完整图邻域走 `related`（T2）"。
  明确 **probe 不是 related 的替代品**，维持 T2 全量校准的定位。
- `docs/design/decisions.md`：决策留痕（neighbors 最小字段面 / 仅首 seed / 键缺席语义 /
  不加 `--db-path` 的理由）。

---

## 8. 决策留痕（为什么这么定）

- **输出最少**（用户补充拍板）：neighbors 每项 2 字段、默认 5 个、不加 flag。
  避免 probe 变成 related 的廉价克隆——related 的 `cooccur_count/chunk_df/source/scopes`
  是多维 K 线看形态用的，probe 只解决"换词候选"这一个动作。
- **复用 related 查询层**（HC4）：同一份 `keyword_pairs` 数据、同一个排序键，
  保证 A1 的"逐项一致"可复验；不新写扫描逻辑。
- **键缺席 vs 空数组**：缺席 = 没有图可用（caller 不必再试图）；空数组 = 图里有词但无邻居
  （caller 升级到 T3 换措辞）。两个动作不同，值得区分；且不需要加评价字段。
- **不加 `--db-path`**：probe 是无参轻量动词（摸边界），保持"0 配置就能跑"的定位；
  非默认库场景极少，交给 related（它已支持 `--db-path`）。
- **仅首 seed**：probe 是聚焦探针，多 seed 邻域展开 = related 的活。保持输出扁平、最少。

---

## 9. 待用户可挑战的假设（本方案写死，可推翻）

1. **"仅首 seed"**：若你希望多关键字 probe 也带多 seed 邻域，会引入 `{seed: [...]}`
   嵌套结构，违背"最少"——我倾向维持首 seed。
2. **"默认 5 个"**：5 是"换词候选"量级；若想更像 related 可调大，但那会滑向替代品。
3. **"不加数量 flag"**：想要可配置数量时，later 用 `--probe-neighbors N` 增量加，
   本方案不预留。
