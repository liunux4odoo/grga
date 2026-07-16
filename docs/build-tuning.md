# build 调优指南：性能、存储与参数配置

**面向读者**：需要在真实语料上跑 `grga index build` / `grga index rebuild`
的用户；已经读完 [`install-and-usage.md`](install-and-usage.md) 基础用法，
现在想知道**参数怎么调、什么时候不能默认、跑不动怎么办**。

**不覆盖内容**：知识资产分层的语义（见
[`design/plan-2026-07-11-build-vs-rebuild.md`](design/plan-2026-07-11-build-vs-rebuild.md)）、
内存优化的实现细节（见
[`design/plan-2026-07-15-build-memory-oom.md`](design/plan-2026-07-15-build-memory-oom.md)）、
读侧 `related` 的用法（见 [`CLI.md`](CLI.md)）。

---

## 0. TL;DR：三张决策表

### 0.1 我是谁 / 我该跑什么

| 场景 | 命令模板 | 关键参数 |
|---|---|---|
| **首次索引一个 scope**（新加语料库） | `grga index build --scope X --directory /path` | `--min-npmi-build 0.2`（默认，够用） |
| **日常增量**（本机语料有增删改） | `grga index build --scope X --directory /path` | 无需额外参数，走 `(size, mtime, content_hash)` 三级增量 |
| **改动了参数、要重算**（如抬 npmi 地板） | `grga index rebuild --scope X --directory /path --min-npmi-build 0.4` | `--refresh-ngram=append` 保留词典 / `replace` 清词典 |
| **导入他人知识包后本机建证据** | `grga index build --scope X --directory /path --seed-ngram-from X` | 借入词典冷启动，见 §5.4 |
| **语料太大，机器 8 GB 装不下** | `grga index rebuild --scope X --directory /path --batch-chunks 50000` | 用 recall 换 wall clock + 内存，见 §3.2 |

### 0.2 常见"我该不该动这个参数"

| 参数 | 默认 | 什么时候改 | 什么时候不改 |
|---|---|---|---|
| `--min-npmi-build` | `0.2` | 想省 30-50% 磁盘 → 抬到 `0.4`；想留最完整的 pair → 降到 `0.1`  | 不清楚就别动，`related --min-npmi` 已经能读侧收紧 |
| `--min-pair-count` | 自适应（2-5） | 单文件超大语料想加严 | 一般不需要，自适应梯度覆盖了 200 到 20000+ chunks 的档次 |
| `--chunk-mode` | `line` | Markdown 文档且想按章节共现 → 用 `chars` | 代码 / 短行文本，保 `line` |
| `--chunk-max-chars` / `--chunk-min-chars` | `600` / `300` | 语料段落非常长（论文/小说）想收更多 pair → 抬到 `1200`/`600` | 一般不动 |
| `--ngram-iters` | `2` | 中文占比高、想让新词多迭代一轮收敛 → `3` | ASCII 代码/日志为主的语料，`1` 足够 |
| `--max-ngram-n` | `4` | 语料里有超长专有名词（如"联合国行星防御理事会"6 字）→ `5-6` | 一般 `4` 够用，`4` 已能覆盖大部分中文四字词 |
| `--refresh-ngram` | `append` | `rebuild` 时想彻底重来 → `replace` | 增量 `build` 默认 `append` 即可 |
| `--batch-chunks` | `0`（不分批） | 内存不够（见 §3.2 判断表） | 内存够，永远不设 |
| `--top-n-kw` | `64` | 仅在 `--batch-chunks > 0` 时才生效；想保更多 pair → `256` | 不用分批就无需考虑 |

### 0.3 常见"跑挂了怎么办"

| 症状 | 大概率原因 | 处理 |
|---|---|---|
| `disk I/O error` / SQLite `no space` | `db-temp/` 磁盘满 | 把 `deep_keywords.db` 移到大盘或 symlink `db-temp/` 到大盘 |
| 进程被 OOM killer 干掉 | RAM 不够 | 加 `--batch-chunks 50000`（见 §3.2） |
| jieba 初始化极慢（分钟级） | 词典累积到 100 万+ 条目 | 用 `grga index forget --phrase` 清掉噪声短语；不要频繁 `rebuild --refresh-ngram=replace` |
| 生成共现对阶段"卡住"（进度不动） | `_finalize_pairs_from_temp` SQL 静默执行 | 正常现象，看 `db-temp/` 磁盘增长确认在跑；不要 Ctrl+C |
| build 完成但 `related` 查不到东西 | `--min-npmi-build` 太严 或 rebuild 忘了 `--directory` | 检查 `grga scope list` 看 pairs / keywords 计数 |

---

## 1. build 阶段的整体流水线（了解到心里就好）

```
Pass 0. 扫描：iter 文件 → 过滤后缀 / 大小 → 分级判断变更 (size, mtime, hash)
Pass 1. Phase A：多轮 auto n-gram 检测（rebuild 时 ngram_iters=2；build 时 1）
Pass 1. Phase B：用冻结词典最后一次 tokenize，产出 chunk 关键字
Pass 1.5. 停用词候选：单字 CJK 的 chunk-df 门槛
Pass 2. 物化：keyword_docs / heading_docs / docs 元数据 + 3b 过滤后落 _build_chunk_kws
Pass 3. 共现聚合：_build_chunk_kws self-join → keyword_pairs UPSERT-add
Pass 4. NPMI 重算：SQL 一次 UPDATE + 一次 DELETE 完成
```

**你能调的旋钮只影响其中某几步**，下面各节按"要解决的问题"来组织，不按步骤。

---

## 2. 存储：`--min-npmi-build` 是决定 DB 大小的最大杠杆

> ⚠️ **`--min-npmi-build` 是不可逆的写入门槛，不是探索性参数。**
>
> - 写入侧删掉的 pair **永久丢失**；读时 `related --min-npmi` 只能收紧、
>   不能放宽。
> - 想找回被删掉的 pair 只能**重跑整次 build**（几十分钟到几小时级 wall
>   clock），**不存在"增量补齐"的路径**——`_build_chunk_kws → keyword_pairs`
>   的翻译是全量 SQL self-join，SQLite 无法"跳过已存的 pair 组合"：判断某
>   个 pair 是否已存、以及它的 npmi 落在哪一档，本身就要先跑完 join + npmi
>   重算。判断的成本 = 得到答案的成本，所以"先严后宽两次 build 复用第一
>   轮结果"在逻辑上不成立（详见 §2.6）。
> - **首次建库若不确定阈值**：宁可用更松的值（`0.1` 甚至 `0.0`），把选择权
>   留在读侧。磁盘不够再抬——但要清楚这是一次砍掉尾巴、终身不长回来的手术。

### 2.1 谁在吃磁盘

真实数据（`工作` scope，1435 文件 / 288 万 chunks / 76M pairs / DB 9.58 GiB）：

| 对象 | 大小 | 占 DB |
|---|---:|---:|
| `keyword_pairs` 表 | 4339 MiB | 44% |
| `idx_kp_a_npmi` 索引 | 2653 MiB | 27% |
| `idx_kp_b_npmi` 索引 | 2655 MiB | 27% |
| **pair 三件套合计** | **9647 MiB** | **97%** |
| 其余（keywords / stats / docs / auto_ngram_dict / ...） | ≈ 158 MiB | 2% |

**结论**：几乎全部磁盘都在 `keyword_pairs` 及其两个 `(scope_id, kw_a_id/kw_b_id, npmi DESC)` 索引上。想省磁盘 = 想删 pair 行。

### 2.2 `--min-npmi-build` 的存储节省

pair 行数近似线性 → 表 + 两个索引同比缩减：

| 地板 npmi | 保留 pairs | 保留占比 | pair 三件套 | 全 DB |
|---:|---:|---:|---:|---:|
| 0.2（默认） | 76,284,967 | 100.00% | 9.42 GiB | 9.58 GiB |
| 0.3 | 73,225,311 | 95.99% | 9.04 GiB | 9.20 GiB |
| 0.4 | 65,065,461 | 85.29% | 8.04 GiB | 8.20 GiB |
| 0.5 | 51,786,788 | 67.89% | 6.40 GiB | 6.56 GiB |
| **0.6** | 37,146,360 | 48.69% | **4.59 GiB** | **4.75 GiB** |
| 0.7 | 21,850,400 | 28.64% | 2.70 GiB | 2.86 GiB |

**0.2 → 0.6 省 4.83 GiB（-50.4%）**，但你会永久丢弃 51% 的 pair——其中
不都是噪声，也含中低强度但真实的关联。**读侧 `--min-npmi` 只能收紧、
不能恢复**，删了回不来。

### 2.3 抬地板后必须 `vacuum`

SQLite 不会自动收缩磁盘文件。重建后跑：

```bash
grga index vacuum --db-path ~/.config/grga/deep_keywords.db
```

否则文件大小不变，只是内部有大量空 page。

### 2.4 决策建议

- **不改**（保 `0.2`）：常规场景。你依然可以在查询时用 `grga related --min-npmi 0.5/0.6` 做"由粗到细"。
- **抬到 `0.35` 或 `0.4`**：省 1-1.4 GiB，清掉明显噪声区（0.2-0.3 里的 df 数千级泛词），保留 85-96% pair。**推荐的中间道路**。
- **抬到 `0.6`+**：只有当 pair 已经严重影响读性能或磁盘紧张时才考虑；这是一次不可逆的信息删除。

### 2.5 `--min-pair-count` 与 `--min-npmi-build` 的差别

两个是独立门槛，pair 必须**同时**通过：

| 门槛 | 语义 | 什么时候手动改 |
|---|---|---|
| `--min-pair-count` | 一对词在多少个 chunk 里共现过（整数计数门槛） | 自适应梯度已覆盖大部分场景；单文件超大语料（如把整个 wiki dump 塞一个文件）可以调到 `10-20` 加严 |
| `--min-npmi-build` | 共现的**归一化强度**（0-1 浮点数） | 见 §2.4 |

**独立性门槛**（`df_a == df_b == cooccur_count` 就删）永远开启，用户不能调——它专门治"两个词永远绑在一起、其中一个是 tokenization 碎片"这种 npmi=1.0 的伪高分。

### 2.6 为什么"分阶段 build 复用上轮结果"省不了存储

一个常见直觉是：先用 `--min-npmi-build 0.7` build 一次拿到高价值核心，
之后想扩展时再用 `0.3` build 一次，"只补 0.3-0.7 之间"的 pair。**这在
grga 现有架构下不成立**，且不是"实现难度"问题，是逻辑循环：

1. 要判断某个 pair `(A, B)` 是否已存 / npmi 落在哪一档，前提是知道它的
   npmi 值。
2. 要知道它的 npmi 值，前提是先跑完 Pass 3（self-join count）+ Pass 4
   （npmi 重算）。
3. 这两步一旦跑完，"过滤"已经在 Pass 4 的 DELETE 里免费完成——没有多
   出"再存已有的、只补新的"这一步能省。

即使用反连接 `WHERE NOT EXISTS (SELECT 1 FROM keyword_pairs ...)` 强行
跳过已存 pair，成本也**不减反增**——每个候选 pair 多一次索引查找，代价
比重新写入还高。

**存储层面的净结果**：不管你是"一次 0.3 build"还是"先 0.7 后 0.3 两次
build"，最终 `keyword_pairs` 里都是同一批 pair、同一数量、同一磁盘占用。
分阶段唯一的差别是**中间态**——第一次 build 完 DB 更小（只存 0.7+），
但第二次 build 完之后 DB 会**回到 0.3 的完整规模**，前面省的空间全部吐
回去。

**若真痛点是"以后语料再翻倍磁盘扛不住"**，正确的治理手段见 §2.7。

### 2.7 长期存储治理：语料规模持续增长时怎么办

`keyword_pairs` 的行数 ≈ `n_chunks × 每 chunk 平均 kw 数² / 2 × 独立组合率`，
跟语料规模**近似线性到超线性**（词汇饱和后线性）。你上次实测 288 万
chunks → 76M pairs → 9.6 GiB；假设未来涨到 1000 万 chunks 且词汇饱和，
估算 250-300M pairs / 30-35 GiB。这个曲线不能靠"分阶段 build 复用"改变
——只能靠**改变什么进库**。

按"能省磁盘的杠杆强度"排序：

| 手段 | 能省多少 | 代价 | 何时用 |
|---|---|---|---|
| **按主题拆多个 scope** | 分摊（每 scope 独立控制） | scope 边界要人工维护 | 语料本身有天然主题分区（如"工作" vs "小说" vs "论文"） |
| **抬 `--min-npmi-build` 到 0.4-0.5** | 15-30% | 永久丢中低强度 pair | 磁盘 30-50% 满且不 mission-critical |
| **抬 `--min-pair-count` 到 5-10** | 5-15% | 永久丢低频 pair | 单文件超大语料 |
| **抬 `--min-npmi-build` 到 0.6+** | 40-55% | 永久丢一半 pair，含中强度关联 | 磁盘警戒线；不再期待完整召回 |
| `grga index forget --phrase X` 手动清 | 视 X 分布 | 手动 curation 成本 | 已识别的具体噪声词 |
| 迁移到大盘 | 100%（治标） | 无 | 短期止血 |

**关键设计红利**：grga 的 scope 是**逻辑单位可跨机迁移**（v10 架构），
所以你可以：

```bash
# 把"工作"里过老的部分导出到冷存储
grga index export --scope 工作-2024 --out /cold/工作-2024.grgapkg
grga scope rm 工作-2024 --purge-knowledge

# 只保留活跃 scope 在热盘
grga scope list
```

被 export 的知识包是压缩后的 SQLite 文件（只含知识层，剥离证据层），
体积远小于对应主 DB 分片。需要时随时 `grga index import` 回来。这是唯
一"降低当下磁盘 + 保留信息"的路径。

**不要指望的方案**：
- **SQLite 内置压缩**：`keyword_pairs` 是纯数值列（4 个 int + 1 float），
  已经很紧凑，SQLite 无原生行压缩，加压缩层 = 加复杂度且几乎无收益。
- **改 `page_size` / VACUUM 换页大小**：见
  `design/plan-2026-07-15-build-memory-oom.md` §6.4，实验证明收益 <15%
  且迁移成本高，已归档"不动"。
- **删索引**：`idx_kp_a_npmi` / `idx_kp_b_npmi` 是 `related` 查询亚毫秒
  index-only 早停的地基，删了 = 查询从毫秒变秒级。省 55% 磁盘换来读性
  能崩溃，划不来。

---

## 3. 内存与速度：默认路径 vs 分批路径的真实取舍

### 3.1 三种路径的核心差别

| 特性 | 默认单遍 (`--batch-chunks 0`) | 分批单批 (`--batch-chunks >= 总chunks`) | 分批多批 (`--batch-chunks 50000` 等) |
|---|---|---|---|
| pair recall | **1.0** | **1.0** | 0.73（每批各自检测短语，跨批漂移不可逆） |
| pair 平均 npmi | baseline | baseline | 更高（top-N 优先保强连接） |
| wall clock | 1.0× | ~1.4× 快 | 1.4× - 3.2× 快 |
| Python 堆峰值 | 中（`docs` list 常驻 3-4 GB / M 级语料） | 中 | 低（top-N 裁剪 self-join 中间态） |
| SQLite temp file 峰值 | 高（几十 GB 级 self-join 中间态） | 高 | 中（分批 self-join） |

关键：**分批的加速主要来自 top-N per-chunk 裁剪把 `chunk × kw²` 的 self-join 中间态砍下去**，代价是永久丢 pair。单批模式（batch ≥ 总 chunks）的 1.4× 是分批管线本身的实现红利，不是"分批"这个动作本身的贡献。

### 3.2 该不该开 `--batch-chunks` 的判断表

| 语料规模（估算） | 机器 RAM | 磁盘剩余（db-temp 所在盘） | 推荐 |
|---|---|---|---|
| ≤ 100 MB / ~10 万 chunks | ≥ 8 GB | ≥ 10 GB | **默认路径** |
| 100 MB - 500 MB / ~50 万 chunks | ≥ 16 GB | ≥ 50 GB | **默认路径** |
| 500 MB - 2 GB / ~300 万 chunks | ≥ 16 GB | ≥ 100 GB | **默认路径** |
| 500 MB - 2 GB / ~300 万 chunks | 8-16 GB | ≥ 100 GB | 默认路径，边缘可行；风险大就 `--batch-chunks 50000` |
| ≥ 2 GB / 千万 chunks 级 | 任意 | ≥ 200 GB | `--batch-chunks 50000`；接受 recall 0.73 |
| ≥ 2 GB | 任意 | < 语料 × 30 | **加大盘先**，不是加 batch |

**核心口诀**：内存不够才分批；磁盘不够先扩盘；只有"内存分批也救不了"的极大规模才需要重新考虑架构。

### 3.3 分批时如何调 `--top-n-kw`

仅在 `--batch-chunks > 0` 时生效。控制每个 chunk 最多保留多少个关键字进入 pair 枚举。

| `top_n_kw` | pair recall | pair 平均 npmi | 速度 vs baseline | 定位 |
|---:|---:|---:|---:|---|
| 64 (默认) | 0.13 | 0.382 | 3.2× 快 | 集中学习：少而精 |
| 128 | 0.51 | 0.317 | 2.0× 快 | 平衡 |
| 256 | 0.73 | 0.305 | 1.4× 快 | 全面覆盖（受短语漂移限，无法再升） |
| 512+ | 0.73 | 0.305 | 1.4× 快 | 与 256 无差（cap 已不再触发） |

**默认 64 是"最快学核心"档**——你会得到 pair 数量少但平均质量最高（npmi 0.382 vs baseline 0.290）的资产。若这符合你"由快到慢、由粗到细"的直觉，就用默认；若 recall 太低影响后续查询完整性，抬到 256。

### 3.4 `GRGA_BUILD_CACHE_MB`：SQLite 页缓存

默认自适应：可用 RAM 的 1/4，下限 512 MB，上限 4096 MB。

- **RAM 充足**：可显式设 `GRGA_BUILD_CACHE_MB=2048` 或 `4096`，加速 self-join。
- **RAM 紧**（8 GB 机器）：显式设 `GRGA_BUILD_CACHE_MB=512` 或 `256`，把节省的内存留给 Python 堆和 OS。
- 上限 clamp 到 8192 防误配爆内存。

设太小的风险：self-join 反复 re-read temp 页（thrash），wall clock 陡升；设太大的风险：cache 吃满，OS 侧的 anonymous page 被换到 swap。

---

## 4. `--ngram-iters` 与词典生长

### 4.1 词典策略

`auto_ngram_dict` 是唯一的**资产层**（跨 build 只增不减），它决定所有下游 tokenization：

| 命令 + `--refresh-ngram` | 检测行为 | 词典变化 |
|---|---|---|
| `build` + `append` （默认） | 对变更文件做 1 轮检测 | 新短语 union 进去 |
| `build` + `never` | 不检测 | 用现有词典 |
| `rebuild` + `append` | 对全部文件做 `ngram_iters` 轮 | 新短语 union；旧短语 count/npmi 刷新 |
| `rebuild` + `replace` | 对全部文件做 `ngram_iters` 轮 | **清空后重建**（一次性遗忘全部 auto 短语） |

### 4.2 什么时候值得 `--ngram-iters 3`

- 中文占比高（> 50%）、有大量复合专名（人名 + 姓 + 号 + 头衔等）。
- 首次建库或大幅换语料，想让新词多迭代收敛。
- 之后**日常 build 不需要**，一轮足够。

代价：每多一轮约等于全语料再 tokenize 一次。100 MB 语料每轮 ~20 分钟，3 轮就是 1 小时。

### 4.3 什么时候值得 `--max-ngram-n 5` 或 `6`

看你语料里有没有超长专有名词：
- 4 字词已能覆盖："面壁计划"、"章北海"（3）、"联合舰队"、"破壁人"（3）等
- 5-6 字词才需要抬 max_n："联合国行星防御理事会"（8 字，但会被拆成 2-3 段组合）、"赫尔辛根默斯肯"（7 字）等

**代价**：n 每 +1，Phase A 窗口枚举量约 +N/n 倍（其中 N 是总 token 数），self-join 中间态也会涨。

### 4.4 `--seed-ngram-from`：知识包冷启动

刚 `grga index import <pack>.grgapkg --as X`，或者在新机器上建同名 scope，此时该 scope 的 `auto_ngram_dict` 是空的。可以借用另一个 scope 的词典：

```bash
grga index build --scope X --directory /path --seed-ngram-from Y
```

`Y` 的短语被 layer 在 `X` 之下（`X` 自己后来发现的短语会覆盖 seed）。适合"新机器 + 相同领域"场景，避免第一轮 build 冷启动学不到词。

---

## 5. chunk 参数：搜索粒度的底层

### 5.1 `chunk_mode` 的选择

- **`line`（默认）**：1 行 = 1 chunk。与 ripgrep 行级语义完全对齐；代码、日志、短行文本首选。
- **`chars`**：多行字符窗口，按"标题 > 空行 > 硬字符切"优先级切分。段落型 Markdown / 长文章首选——共现窗口大，能捕获段落内的语义关联。

### 5.2 `chunk_max_chars` / `chunk_min_chars`（仅 `chars` 模式）

默认 `600 / 300` 覆盖大多数中文段落。什么时候要抬：
- 大段落学术论文 / 小说：抬到 `1200 / 600`，段落级共现更完整。
- 参数含义："目标上限"和"目标下限"，实际大小按边界优先级决定；单行超 max_chars 不会被拆到中间。

代价：pair 数量随窗口大小近似平方增长（每 chunk 关键字数 × chunk 数）。

---

## 6. 环境变量清单（跟 build 相关的）

完整清单见 [`env-vars.md`](env-vars.md)。这里只挑 build 场景相关：

| 变量 | 默认 | 作用 |
|---|---|---|
| `GRGA_BUILD_CACHE_MB` | 自适应（1/4 RAM，clamp 到 [512, 4096]） | SQLite 页缓存 MB 数；见 §3.4 |
| `GRGA_BUILD_NPMI_BATCH` | `50000` | pair NPMI 重算的 fallback 分批大小；仅当 SQLite 缺 `ln()` 时走此路径。有 `ln()`（多数 Python 发行版）时不生效 |
| `GRGA_BATCH_TOP_N` | `64` | `--batch-chunks > 0` 时每 chunk 关键字上限；等价于 `--top-n-kw`，命令行优先 |
| `GRGA_PPR_MIN_NPMI` | `0.2` | 读侧 `related --mode graph` 的 npmi 下限；跟 build 无关但常一起讨论 |

**db-temp 位置**：当前无独立环境变量，走 `<主 db 所在目录>/db-temp/build-<uniq>/`。想搬到大盘：

```bash
# 方案一：把主 db 挪到大盘（数据库文件也一起挪）
mv ~/.config/grga/deep_keywords.db /media/big-disk/grga/
grga index build --db-path /media/big-disk/grga/deep_keywords.db --scope X --directory ...

# 方案二：symlink 只挪 temp（数据库本体留原地）
rmdir ~/.config/grga/db-temp 2>/dev/null
ln -s /media/big-disk/grga-db-temp ~/.config/grga/db-temp
```

---

## 7. 常见场景的完整方案

### 7.1 首次建立索引（500 MB 中文小说 / 300 万 chunks / 32 GB 机器）

```bash
grga index build \
  --scope 小说 \
  --directory /media/lqw/DATA/sync/novels \
  --chunk-mode chars --chunk-max-chars 1200 --chunk-min-chars 600 \
  --max-ngram-n 5 \
  --ngram-iters 3
```

- `chars` 模式 + 大窗口：捕获段落级共现。
- `max_ngram_n=5`：兼容 4-5 字的人名 / 组织名。
- `ngram_iters=3`：首次建库多迭代一轮收敛。
- 默认 `--min-npmi-build 0.2`：保留最多 pair，后续用 `related --min-npmi` 做粗细档。

### 7.2 日常增量 build

```bash
grga index build --scope 小说 --directory /media/lqw/DATA/sync/novels
```

只处理内容变更文件；词典 append，pair 全表重算 NPMI 但只 UPSERT 增量。

### 7.3 磁盘紧张，想省 40%

```bash
# 一次性抬地板重建
grga index rebuild \
  --scope 小说 \
  --directory /media/lqw/DATA/sync/novels \
  --min-npmi-build 0.4 \
  --refresh-ngram append   # 保词典，只重算 pair 和 stats

grga index vacuum --db-path ~/.config/grga/deep_keywords.db
```

pair 保留 ~85%，DB 缩到原来的 ~85%（若 pair 本占 97%，几乎线性节省）。**读侧完整性**由 `related --min-npmi 0.5/0.6` 灵活控制。

### 7.4 8 GB 机器 + 2 GB 大语料

```bash
export GRGA_BUILD_CACHE_MB=512
export GRGA_BATCH_TOP_N=128  # 或直接 --top-n-kw 128

grga index rebuild \
  --scope 大库 \
  --directory /media/big-disk/corpus \
  --batch-chunks 50000
```

- 分批处理，每批 5 万 chunks，Python 堆峰值 < 1 GB。
- `top_n_kw=128` 换来 recall 0.51；接受质量损失或后续多查几轮补齐。
- 缓存降到 512 MB 留内存给 OS。

### 7.5 迁移到新机器（用知识包）

在源机器：
```bash
grga index export --scope 小说 --out /tmp/novels.grgapkg
```

在目标机器（假设本机语料在 `~/data/novels`）：
```bash
grga index import /tmp/novels.grgapkg --as 小说
grga scope bind 小说 ~/data/novels

# 借入源机器的词典冷启动
grga index build --scope 小说 --directory ~/data/novels --seed-ngram-from 小说
```

seed 是"用同名 scope 借自己"，因为 import 只带 `auto_ngram_dict` 但没 chunk-df 证据。build 一次即可。

---

## 8. 你不应该做的事（反模式）

1. **频繁 `rebuild --refresh-ngram=replace`**。这会清空 `auto_ngram_dict`，
   下次 build 从零重新学词——每次都是完整的 100% 计算成本，且高质量的
   稀有词（只在少数文档出现）可能永远学不回来。**只在真的想遗忘全部
   分词知识时才这么做**（例如换了语料主题）。

2. **依赖 build 时的 `--min-npmi-build` 做临时调参**。这是**写入期永久
   门槛**，不是探索性参数。真正的"由粗到细"应该走 `related --min-npmi`
   读侧过滤——它免费、可逆、无需 rebuild。

3. **同时开 `--batch-chunks` 和纠结 recall**。分批的 recall 0.73 天花板
   来自短语漂移，是这条路径的设计代价，不是可调参数。想要 recall 1.0
   就别开分批；想要低内存就接受 0.73。**不要"两个都要"**。

4. **手动改 SQLite `PRAGMA`**。grga 已在 build 期强制了 `temp_store=FILE`
   和自适应 `cache_size`。手动干预要么被覆盖、要么打破 spill 保证。
   有环境变量的用环境变量。

5. **假设"进度不动 = 进程挂了"**。`_finalize_pairs_from_temp` 里的 SQL
   self-join + `UPDATE npmi` 是无进度回调的重活，可以几十分钟才动一次
   进度条。看 `db-temp/` 文件夹在长大 + `iostat` 显示磁盘在跑 = 进程
   活着，不要 Ctrl+C。

6. **想通过"分阶段 build"节省 pair 存储**。基于"先严后宽 + 复用第一
   轮结果"的直觉在 grga 架构下走不通（§2.6）——不管几阶段，最终存下
   来的 pair 集合和一次性 build 完全一样，前面省的空间会在补建时全部
   吐回去。真正的长期存储治理是按主题拆 scope + 冷 scope 用
   `grga index export` 归档到 grgapkg（§2.7）。

---

## 9. 参考

- 存储对比（0.2 vs 0.6 地板）的完整数据推导：本项目 chat log 2026-07-17
- 内存优化的设计文档：[`design/plan-2026-07-15-build-memory-oom.md`](design/plan-2026-07-15-build-memory-oom.md)
- 分批构建的实验数据：[`../experiments/batched_build_2026_07_16/FINDINGS.md`](../experiments/batched_build_2026_07_16/FINDINGS.md)
- 用户生产环境实测：[`../experiments/batched_build_2026_07_16/FINDINGS_user_production_run.md`](../experiments/batched_build_2026_07_16/FINDINGS_user_production_run.md)
- 增量语义与三层分工：[`design/plan-2026-07-11-build-vs-rebuild.md`](design/plan-2026-07-11-build-vs-rebuild.md)
- 分词与 auto n-gram 契约：[`design/tokenization.md`](design/tokenization.md)
- CLI 参数总览：[`CLI.md`](CLI.md)
- 环境变量清单：[`env-vars.md`](env-vars.md)
