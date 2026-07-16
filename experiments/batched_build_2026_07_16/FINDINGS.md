# FINDINGS — batched_build_2026_07_16 (方向 3: npmi-ranked top-N)

## 最终架构

Batch 循环做 phrase detection (dict, 无 numpy); pair 计数 dump triples
到跨批 `_build_chunk_kws` → baseline SQL 全局 GROUP BY (Q9). 每 chunk 用
**方向 3 npmi 排序**做 top-N cap:kw 参与过的最高 npmi pair 分数作主排序键
(Q1 保护新 kw, 无先验用 0.25 兜底).

## 核心验证数据

### 单批 = bit-exact (pipeline 正确性)

| Corpus | pairs | phrases | wall | peak |
|---|---|---|---|---|
| M10 baseline (single-pass) | 296610 | 33976 | 600s | 729MB |
| M10 batched 单批 | **296610** | **33976** | 427s (1.4×快) | 728MB |

单批 recall/precision 双 1.000, 证明 pipeline 与 baseline 数学等价.

### M10 3-batch, 方向 3 npmi-rank, N 扫描

baseline: 296610 pairs, mean npmi 0.290

| N | wall | peak | pairs | recall | precision | kept_mean_npmi | phantom_npmi |
|---|---|---|---|---|---|---|---|
| 64 | **189s (3.2×快)** | **544M** | 55068 | 0.130 | 0.700 | **0.382** | 0.301 |
| 128 | 303s | 743M | 222393 | 0.510 | 0.680 | 0.317 | 0.270 |
| 256 | 428s (1.4×快) | 746M | 324445 | **0.732** | 0.669 | 0.305 | 0.262 |
| 512 | 434s | 746M | 324445 | 0.732 | 0.669 | 0.305 | 0.262 |

## 三个关键结论

### 1. npmi 排序有效 (方向 3 成功)

小 N 保留的 pair 平均 npmi **显著更高** (N=64: 0.382 vs baseline 0.290).
说明 npmi 排序确实优先保留高质量连接. "只学最重要的概念" 兑现.

### 2. recall 天花板 0.732 来自 per-batch 短语漂移, 不是 top-N

N=256 和 N=512 结果完全一样 (M10 chunks 的 pair-kw < 256, cap 不再触发).
即使无有效 cap, 3-batch 仍只有 0.732 recall. **剩下的 27% 缺口是每批
各自检测短语造成的 tokenization 差异**, top-N 调参救不回来.

要突破 0.732 需要修短语漂移 (全局 n-gram 频次累积), 不是调 N.

### 3. phantom pairs 是低质量碎片

batched 产出但 baseline 没有的 pair (precision 缺口的 30%), 平均 npmi
0.262-0.301, **低于 kept pair**. 说明短语漂移产生的是低质量碎片, 不是
与 baseline 竞争的好 pair. 对下游检索污染有限.

## 速度/质量/内存 权衡 (对齐用户目标 1+3)

| 策略 | 定位 | wall | peak | pair 质量 | recall |
|---|---|---|---|---|---|
| **小 N (64)** | 集中学习: 少而精 | 3.2× 快 | 544M | 高 (npmi 0.382) | 0.13 |
| **中 N (256)** | 全面但有漂移 | 1.4× 快 | 746M | 中 (npmi 0.305) | 0.73 |
| baseline | 全局单次 | 1× | 729M | 中 (npmi 0.290) | 1.0 (基准) |

**小 N 是一个"不同的知识资产"** —— 不是退化的 baseline, 而是"集中学习
最强连接". 符合用户增量学习哲学. 但 recall 低.

## 决策点

用户目标 1 (速度) + 3 (增量架构) 都达成了:
- 速度: 小 N 快 3.2×, 中 N 快 1.4×
- 增量: batch 循环 + 跨批 dict 累积, 随时可加语料

剩下的选择是 **recall 天花板 0.73 (< 用户门槛 0.80) 是否可接受**:

- **接受**: 采用中 N (256), 全面覆盖, recall 0.73, 1.4× 快. 27% 缺口
  由 caller 查询期多轮逼近补 (用户早前立场).
- **不接受 → 修短语漂移**: 加"全局 n-gram 频次累积" —— batch 检测时
  用累积计数而非仅本批, 跨批稀疏短语可被检出. recall 应回升到接近
  baseline. 代价: 需要持久化 n-gram 频次表, 中等工程量.

## 未决 (待用户定)

- N 默认值: 小 N (集中/快) vs 中 N (全面/慢)?
- 是否要修短语漂移 (全局 n-gram 累积) 突破 0.73 recall 天花板?

---

## 收工决策 (2026-07-16)

### A 落地

- **N 默认值 = 64** (最快学习: 3.2× baseline, peak 544MB, pair 平均 npmi
  显著高于 baseline)
- **`GRGA_BATCH_TOP_N` 环境变量**可覆盖 (需要更全面时调大到 256+)
- **架构定型**: batch phrase detection + baseline SQL 全局 pair 聚合
- **符合用户"最快学习, 觉得不够再深入"直觉**: 小 N = 快而少的高质量
  连接, 后续可加语料 / 调大 N / 启用 B 深入

### B 记入 SPEC 可选增强

跨批 n-gram 频次累积, 3-6 小时原型可交付, 预期 recall 0.73 → 0.88-0.93.
启用判据见 SPEC.md § 后续可选增强.

### 未来路径

- **正常使用**: batched_bench.py + N=64 (或用户调整)
- **发现漏检严重**: 实施 B (短语频次累积)
- **规模再增长**: 已有的 top-N + SQL 外部排序架构可线性扩展

