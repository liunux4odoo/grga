# QUESTIONS — oom_build_2026_07_14

跑实验之前先把 open questions 冻结。跑完前不改；跑完之后动了哪一条要在
FINDINGS 里明标"Q# 变更"。

| Q | 问题 | 冻结值 | 理由 |
|---|---|---|---|
| Q1 | subprocess vs 同进程 | subprocess（`multiprocessing.Process` fork） | ru_maxrss 单调；OOM 隔离；跨 run 无状态污染 |
| Q2 | tracemalloc frame 深度 | 1 | depth=25 在 51s build 上拖慢 >5×（smoke test 超时验证）；本轮只要 peak 数字不做 topN traceback，depth=1 足够 |
| Q3 | sampler 采样周期 | 100 ms | 平衡 peak 捕捉率（快尖峰 ~几百 ms）与 log 大小 |
| Q4 | 每格 run 次数 | 3 | 得 median + 方差；再多 XL 档时间预算撑不住 |
| Q5 | 是否加临时 hook 到 build.py | ✅ 允许**只读**探针 | 加在 detect_ngram_phrases 内部读 len(ngram_count)。**Hook 不落主干**，用 monkey-patch |
| Q6 | XL 档合成方式 | 内容拷贝 + 改文件名（3-5 份） | 保留独立 doc 结构；不做真实语料下载 |
| Q7 | 测 iters=3 吗 | ❌ 不测 | 默认 2、Phase A 有 `del line_token_lists`，第 3 轮和第 2 轮 profile 相同（假设 B）；这假设本身由 iters=1 vs 2 检验 |
| Q8 | jieba tokenizer 复用（Phase A→B optimisation）算作变量吗 | ❌ 不算 | 现在的代码已经有 `last_iter_added == 0` 走复用；实验用默认行为，不额外开关 |
| Q9 | OOM 时怎么办 | subprocess 挂了就把该 config 标 `oom_before_finalize`，继续下一格 | 让 profile 数据覆盖到实际 crash 现场；run_all.py 不 abort |
| Q10 | 记录到哪个 DB 路径 | `tempfile.mkstemp(suffix=".db", prefix="oom_bench_")` 每次新建 | 不污染生产库；run 结束删；崩溃留档以便事后 forensic |
| Q11 | 采样线程用 Thread 还是 Process | Thread（daemon） | 同进程读 psutil 更准；不参与 fork isolation（那是 run 之间的事） |
| Q12 | 语料是否包含 rga 触发文件（PDF/office） | ❌ 只测 .txt | 隔离 rga adapter 的 I/O 变量，避免"rga_reads 慢"的干扰因素；rga 相关的 OOM 单开实验 |
