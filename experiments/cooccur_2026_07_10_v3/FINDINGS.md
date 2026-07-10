# FINDINGS.md — cooccur_2026_07_10_v3

> 数学退化观察、性能瓶颈、超出规范的意外行为。**只归档不修**（spec §10）。

## 1. 主结论（一句话）

**npmi ranking + 去 DF-adaptive**：非 hub seed 上 caller-first 假设**成立**
（top-5 Useful% 8-seed 均值 = 87.5% ≥ 60%），hub seed(韩立)上**不成立**
（top-30 by npmi 里 entity 命中 = 0）。两条判据未同时满足 → 按 spec §1，
方案作为"单一 ranking key"整体**不成立**，进入 §11 fallback 讨论。

## 2. 数学退化观察（npmi 结构性性质）

### 2.1 hub 上的"独占叙述词吊打关联实体"

韩立 top-30 by npmi 全是 co/df≈1 的独占窄词（韩某 0.88、眉梢 0.86、心念、
微微一笑…），判定集合 6 个实体的真实 npmi 排名：

| Entity | co(韩立,X) | df(X) | npmi | npmi_rank / 17759 |
|---|---:|---:|---:|---|
| 曲魂 | 341 | 549 | 0.1039 | 208 |
| 李化元 | 113 | 171 | 0.0965 | 566 |
| 墨大夫 | 337 | 576 | 0.0938 | 615 |
| 黄枫谷 | 137 | 301 | 0.0453 | 4279 |
| 七玄门 | 48 | 157 | -0.0106 | 12104 |
| 极阴祖师 | 91 | 306 | -0.0153 | 12612 |

这是 **npmi 归一化的固有性质**，不是 bug：npmi 奖励"最纯粹的共现"
（`co/df_target → 1`），而 hub 的最纯粹共现恰好是叙述配色词，不是剧情实体。
详见 `hub_diff_v3.md` 的"数学根因"。

### 2.2 稀疏对 npmi 偏高（预期内，不修）

- `w == 3`（= min_pair_count 下限）的 pair 共 305 个进入各 seed top-100，
  npmi 范围 [0.1794, 0.5861]，**最高的稀疏对（筑基丹-洗髓易 w=3 df=4
  npmi=0.586）npmi 比很多 w=50 的对还高**。
- 根因：`co == df_target` 时（该词只与 seed 共现），pmi 达到该 co 下的上限，
  npmi 随之偏高。共 **62 个 `co == df_target` 的"完全独占"对**，npmi 都
  在 0.4-0.56 区间。
- **未触发 npmi 接近 ±1 的爆值**：因为 npmi 上限受 `co/N` 制约，co 只有
  个位数时 `-log(co/N)` 很大（分母大），npmi 反而被压在 0.6 以下。最大
  npmi 全局 = 0.6374（极阴祖师-乌某, w=12 df=14），远离 +1。
- 处理：**不动 min_pair_count，不加平滑**（spec §2 单变量 + §10）。这类
  稀疏高-npmi 对多数其实是**正确的强关联**（洗髓易/妻室/七龙桩 都是 seed
  的专属语域词），对 caller 有用，不算噪声。若下轮想抑制，属 §11 的
  smoothed pmi / min_pair_count↑ 讨论。

### 2.3 无负-npmi 进 hub top-30，但全局大量负值

- 韩立 top-30 by npmi 全部为正（min 0.1076），负值天然排到 200+ 之后。
- 全局 17759 个韩立邻居里有相当比例负 npmi（反相关信号，如"修士"
  npmi=-0.032、"老者"-0.073），符合 spec §5"允许负值、按降序排后段"。

## 3. tokenizer 切分错误（归档不修）

在 topk 分类中发现 3 个明显的 jieba 切分错误进入了 top-30：

- 银月 top-10「我和珑」：应为"我 / 和 / 珑"（珑=配角），"和珑"被误合
- 银月 top-22「三大真」："三大真人/三大真火"被截断
- 部分单字残留（如"之器"/"以器"）来自"XX之器/以XX器"的边界

这些属 **tokenizer/词典层面**问题，不在本实验单变量（ranking key + 去
DF-adaptive）范围内。沿用 v2 的 19 词词典**未扩**（spec §2）。归档供
下轮词典迭代参考，本实验**不修**。

## 4. 性能

| 阶段 | 耗时 |
|---|---|
| tokenize（缓存命中） | ~0s（复用 v2 line_tokens.pkl） |
| tokenize（缓存 miss，含 jieba init） | ~34s（首次或 dict mtime 变化） |
| build 图 + npmi + top-100×9 | **26.2s**（`corpus_stats.build_time_sec`） |
| gen_reports | <1s |
| **全流程 wallclock（缓存命中）** | **27.9s** ✓ < 60s |

- `functools.lru_cache` 缓存 `log_df(term)` / `log_co(co)`（spec §8 建议）
  已采用。npmi 计算不是瓶颈；瓶颈是 `combinations` 生成 pair（O(Σ Lᵢ²)）。
- **首次运行需注意**：若 line_tokens.pkl 缓存失效（本目录从 v2 拷贝，
  user_dict.txt 的 mtime 变了会触发一次重 tokenize，~34s），仍在 60s 内。
  但 `build_time_sec` 只计 build 阶段（26s），S9 稳过。

## 5. 与前作对照（一句话）

- V0（整行 raw-count）：hub top-30 entity=0（高频虚词）
- V2_retok（整行 raw-count + DF-adaptive）：hub 被剪出图，entity N/A
- V4（章节 raw-count）：hub top-30 entity=0（章节窗口退化更严重）
- **V5_npmi（整行 npmi，本次）：hub top-30 entity=0（npmi 结构性把实体压到
  200+），但非 hub top-5 Useful=87.5%**

四轮一致证明：**hub seed 无法靠"窗口粒度 + 单一 ranking key"救回**。真正
的产品出路是 **caller-side 按 seed_df 分流**（hub 走问题分解，非 hub 走
npmi 邻居补齐），而非继续在共现图内部调参。
