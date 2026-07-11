# QUESTIONS — autongram_2026_07_11

> Q1-Q5 已由用户在 2026-07-11 讨论中回答并冻结（见 SPEC §7）。
> NQ1-NQ5 是实验执行中新暴露、经用户确认的契约层疑虑。

## Q1-Q5 冻结值（见 SPEC §7）

## NQ 状态跟踪

### NQ1：C4 性能阈值口径失当 —— **待主干阶段修订**

SPEC C4 "iter1 ≤ 2× baseline" 中 baseline 指 regex（0.47s），但任何 jieba 方案下限就是 5s+。实测 iter1=5.47s = 11.6× regex，字面未过；但相对 naive_jieba（5.51s）= 0.99×，通过。

**处理**：本实验按"iter1 vs naive_jieba"口径通过。主干阶段迁移时修订 SPEC 措辞。

### NQ2：`面壁者`/`破壁人` 未召回 —— **已修复 ✅**

**结论**：不是数学判据的合理 miss，是**detection 停用词硬边界过激**（用 746 词通用表把 `者/人/号/家` 等构词后缀误当边界）。

**修复方案**（2026-07-11 v2）：detection 边界改为 26 字核心虚词集（仅结构助词/连词），图构建阶段仍用 746 词表。两阶段解耦。

**效果**：`面壁者` count=260/npmi=0.885/be=4.084 完美召回，`破壁人` 同理。phrase_check 从 82.35% → 100%。见 FINDINGS §F5。

**副作用验证**：担心的 `罗辑说` / `说道` / `知道` 类无组合意义高频串，实测未进 auto_dict（NPMI/频次判据独立挡住）。数学判据够用，不需要大停用词表补漏。

### NQ3：top-K Useful% 判据宽严 —— **主干阶段用宽判据**

topk_usability_santi.md v1 用严判据得 60%（临界），v2 修复后用宽判据（与 v3 experiment 对齐）得 87.5%，两种判据下都 pass。

**处理**：主干迁移的验收统一用 v3 宽判据（语域词/情节概念/人物均算 useful），与 postmortem §4.1 "top-5 Useful% 均值 87.5%" 保持可比。

### NQ4：detection 统计口径 全语料 vs per-document —— **保持全 scope 聚合**

- ngram **候选**在单行内生成（不跨行）→ 天然避免"两个文件末首拼接"污染
- ngram **频次/熵**在全 scope 聚合 → 多文件语料下 `foo_bar` 在多文件出现会累加

多文件场景（代码库）下正确性 OK。**主干迁移时保持此口径**。

> **更新 2026-07-11（部分限定，非撤销）**：全量重扫路径下本结论依然正确。
> 但 `docs/design/plan-2026-07-11-build-vs-rebuild.md` 引入了**增量 build**
> 新语义：`grga index build`（默认增量）时 detection 只对*变化文件*的 token
> 跑（per-doc 局部聚合），以换取文件级增量。这不是推翻 NQ4——**全 scope
> 聚合退居 `grga index rebuild --refresh-ngram=append`（默认）/ `replace`**，
> 是校准路径的口径；增量 build 是新增的次级路径。
>
> 之所以能接受增量 build 的局部聚合：词汇表**只增不减**（C4 立场），局部
> 检测漏掉的短语最多"这次没学"，不会破坏已有好词，下次 rebuild 用全 scope
> 聚合兜底。决策链见该文档 §13.1（"曾拒绝 per-doc 的三条理由两条不成立"）
> 与 §13.7。
>
> 附带修正：当初"拒绝 per-doc 检测"给的理由里，"`min_count=3` 单文件过严"
> 与"三体拆章样本不足"两条不成立——前者是绝对计数门槛的固有问题（全 scope
> 下同样无通用值），后者前提错误（三体是单文件，per-doc≡per-scope）。真正
> 成立的只有"大量小文件目录下 NPMI 方差大"，且被词汇表只增的性质吸收。

### NQ5：ASCII 短语拼接分隔符 —— **已修复 ✅**

**问题**：原实现 `phrase = "".join(win)` 对 CJK 正确但 ASCII 会拼错（`nano` + `center` → `nanocenter`，jieba 认不出）。

**修复 v1**：CJK-only 窗口 `"".join`；含 ASCII 的窗口 `" ".join`。判定用 `CJK_ONLY_RE = re.compile(r"[\u4e00-\u9fff]+")`。

**修复 v2（用户 2026-07-11 补充）**：v1 对中英混合实体仍错（`艾AA` → `艾 AA`）。改按"相邻两 token 都 ASCII 才加空格，否则无分隔符"规则，正确处理 CJK+CJK / CJK+ASCII / ASCII+CJK / ASCII+ASCII 四种邻接组合。见 FINDINGS §F11。

**实现层教训**（F6 沉淀）：首次实现用了 `CJK_RE = [\u4e00-\u9fff]`（单字符 pattern），`fullmatch("北海")` 返回 False，导致 CJK 合成词也误走空格拼接分支 → auto_dict 变成 `章 北海` 无效条目。正则 `fullmatch` 语义配合 multi-char token 时必须显式加 `+`/`*`。

**待补**：主干迁移前补一个 golden test 覆盖 CJK 多字 token（`章北海`）+ ASCII bigram（`nano center`）两个 case，防回归。
