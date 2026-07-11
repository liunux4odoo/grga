# CHANGES · 2026-07-11

## Task A · `graph inspect` top_cooccurring 契约漂修复

`grga graph inspect <keyword>` 的 `top_cooccurring_keywords` 现在按 **chunk 级**
聚合，返回真正与 seed 出现在同一 chunk 内的邻居关键字，不再退化为"seed 所在
文档的整体关键字直方图"。修改集中在 `src/grga/graph/inspect.py::_cooccurrences`：
把原本按 `(directory, doc_path)` 过滤的 `keyword_chunks` 聚合改为在
`keyword_chunks` 上的自连接，join key 为 `(directory, doc_path, chunk_id)`。
`_detect_polysemy` 依赖的 `per_scope_cooccur` 由同一函数产生，随之从 doc-level
修正为 chunk-level。函数签名、返回 dict schema、`index_hint` 分支、schema v4
表结构均保持不变。凡人修仙传 22MB 单文件语料上，`大衍诀` / `元婴` / `长春功`
等语义无关 seed 现在给出明显不同、可辨认的 top-5。
