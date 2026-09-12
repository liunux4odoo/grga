# grga Roadmap

## 检索功能增强（2026-09-11 拍板；优先级自上而下）

> 前提：不引入 embedding / 向量库 / 大模型常驻；只在"关键字+图"框架内加东西。
> 信号层（让 caller 更容易判断下一步）优先于算法层（让命中更准），避免撞"能力诚实"红线。

- **[P0] · #1 probe 词汇邻域提示**
  probe 5 秒摸边界时，顺带给出该词在语料里的近义/邻域词（复用已有 `keyword_pairs` 图，
  纯暴露事实、不评价），减少 caller T1 阶段无效换措辞。
  *贴合 caller-first，成本极低，无新算法。*

- **[P1] · #2 search 结果按文档分散采样**
  `--max-total` 默认收窄到 100 后，大语料（整本小说 / GB 级日志）首 100 条可能全来自
  同一文档/章节，信息熵低。需先实证是否真实发生，再加"按文档/章节分散采样"选项。

- **[P2] · #3 suggest 多轮状态复用**
  把 `suggest --reformulate/--expand/--rerank` 从单发改成"前一轮输出作为下一轮输入"
  的多轮反思循环，让 caller 不用每次手动搬运上下文。落地"智能反思"核心信念。

- **[P3] · 多词 AND/OR 窄化**
  `search` 是否支持 `A AND B`（同段共现）窄化检索——中文里"同一段落同时提到两个实体"
  是强信号。需先确认现状（多参数是 OR？）。

- **[P3] · search 多轮信号字段补全核查**
  检查 `heading_path` / `occurrence_index` / `line_ratio` 等迭代信号在每个 hit 是否带全，
  缺一个就少一个 caller 判断依据。

- **[P3] · L2 图导览 graph tour**
  多跳 BFS 子图（`graph tour KEY --depth N`）+ 零依赖 HTML 可视化。
  两个前提待拍板：local vs global、agent(JSON) vs 人(HTML)。见 09-10 讨论。

- **[P3] · related npmi 边界提示**
  输出 seed 的邻居 npmi 分布（min/median/max），caller 自己判断 0.3 算强还是弱；
  只暴露数据、不加评价。

- **[P0] · #4 互补对信号（negative npmi）** ✅ 已落地
  新增 `complementary_pairs` 表；`related --mode graph` 与 `probe` 输出并列
  `complementary` 字段：收录"共现显著低于随机期望"（显著负 npmi，超几何下尾）
  的词对，作为共现对线索的补充。probe 顺带移除 `elapsed_ms`（无消费方）。
  *不是别名检测器——互补对是泛信号（别名/范畴两极/时间线称呼），语义归因留给 caller。*
  *正侧（keyword_pairs / related results / probe neighbors）零改动，查询零额外计算负担（索引查找）。*
  *方案：`docs/design/plan-2026-09-11-complementary-pairs.md`*

- **[Backlog] · deep-search 定位重审**
  v1 遗产（2187 行 Advanced Planner），与 v2 五动词 + skill T0-T4 重叠。
  减法方向：对齐到原语动词之上，或退役。

## 其他既有条目（保留）

- cli 子命令合并优化，使不同层级间的子命令、参数组合起来尽量语义化
