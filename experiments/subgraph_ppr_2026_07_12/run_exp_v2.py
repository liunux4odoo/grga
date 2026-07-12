import os, sys, sqlite3, time, statistics
sys.path.insert(0, "/media/lqw/DATA/myrepo/grga/src")
sys.path.insert(0, "/tmp/kilo/grga_subgraph_exp")

from grga.graph.ppr import (
    load_edges_indexed, personalized_pagerank_indexed,
    edge_weight, IndexedGraph, _fold, _HAS_NUMPY,
)
from subgraph_v2 import build_subgraph

DB = os.path.expanduser("~/.config/grga/deep_keywords.db")
MIN_NPMI = 0.2
TOP_K = 30
SCOPE_DIR_ID = 1


def sub_to_indexed(conn, nodes, edges):
    local_index = {}
    neighbors, out_degree, used_kw_ids = [], [], []
    def _local(kwid):
        idx = local_index.get(kwid)
        if idx is None:
            idx = len(local_index); local_index[kwid] = idx
            neighbors.append([]); out_degree.append(0.0); used_kw_ids.append(kwid)
        return idx
    for (a, b), (co, npmi, src) in edges.items():
        w = edge_weight(src, co, npmi)
        if w <= 0: continue
        ia, ib = _local(a), _local(b)
        neighbors[ia].append((ib, w)); neighbors[ib].append((ia, w))
        out_degree[ia] += w; out_degree[ib] += w
    node_ids = [""] * len(used_kw_ids)
    if used_kw_ids:
        chunk = 900; id_to_text = {}
        for i in range(0, len(used_kw_ids), chunk):
            piece = used_kw_ids[i:i+chunk]
            ph = ",".join("?" * len(piece))
            for r in conn.execute(f"SELECT id, kw FROM keywords WHERE id IN ({ph})", piece):
                id_to_text[int(r[0])] = _fold(r[1])
        for li, kwid in enumerate(used_kw_ids):
            node_ids[li] = id_to_text.get(kwid, "")
    node_index = {name: i for i, name in enumerate(node_ids) if name}
    return IndexedGraph(node_ids=node_ids, node_index=node_index,
                        neighbors=neighbors, out_degree=out_degree)


def rec_at(full, sub, k):
    fa = [x for x, _ in full[:k]]
    sb = set(x for x, _ in sub[:k])
    return sum(1 for x in fa if x in sb) / max(1, len(fa))

def jaccard(a, b):
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, len(sa | sb))


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print(f"numpy={_HAS_NUMPY}  MIN_NPMI={MIN_NPMI}  TOP_K={TOP_K}\n")

    t = time.perf_counter()
    full = load_edges_indexed(conn, directory=None, min_npmi=MIN_NPMI)
    full_load = (time.perf_counter() - t) * 1000
    print(f"full graph: {len(full.node_ids)} nodes, "
          f"{sum(len(nb) for nb in full.neighbors)//2} edges, "
          f"cold load {full_load:.0f} ms\n")

    id2kw = {int(r[0]): r[1] for r in conn.execute("SELECT id, kw FROM keywords")}
    df_rows = conn.execute("SELECT kw_id, COUNT(*) df FROM keyword_chunks GROUP BY kw_id ORDER BY df").fetchall()
    dfmap = {int(r[0]): int(r[1]) for r in df_rows}

    # Two seed cohorts:
    # A. df 分布分位（可能是虚词/一般词）
    dfs = list(dfmap.items()); dfs.sort(key=lambda x: x[1])
    n = len(dfs)
    quantile_seeds = []
    for label, pct in [("p75", 0.75), ("p90", 0.90), ("p95", 0.95),
                        ("p99", 0.99), ("p99.9", 0.999)]:
        kwid, df = dfs[int(n * pct)]
        kw = id2kw.get(kwid, "")
        if _fold(kw) in full.node_index:
            quantile_seeds.append(("Q-" + label, kwid, df, kw))

    # B. 从 auto_ngram_dict 里取"看起来像实体名"的高质量种子（专有名词/复合词）
    #    ngram phrases 经过 npmi+branch_entropy 双门槛，天然更"实体"
    ngram_kws = []
    for r in conn.execute(
        "SELECT phrase FROM auto_ngram_dict WHERE npmi >= 0.5 ORDER BY count DESC LIMIT 40"
    ):
        phrase = r[0]
        # match with keywords table (must exist as a kw for graph presence)
        row = conn.execute("SELECT id FROM keywords WHERE kw = ?", (phrase.casefold(),)).fetchone()
        if row is None: continue
        kwid = int(row[0])
        if kwid in dfmap and _fold(phrase) in full.node_index:
            ngram_kws.append(("N-" + phrase[:12], kwid, dfmap[kwid], phrase))
        if len(ngram_kws) >= 6: break

    seed_specs = quantile_seeds + ngram_kws

    # Param sweeps
    param_sets = [
        ("H2/M20", 2, 20),
        ("H2/M30", 2, 30),
        ("H2/M50", 2, 50),
        ("H3/M20", 3, 20),
    ]

    for (pname, max_hops, top_m) in param_sets:
        print(f"=== {pname}  max_hops={max_hops}, top_m_per_node={top_m} ===")
        print(f"{'seed':>16} {'df':>7} {'sN':>6} {'sE':>7} {'build':>7} "
              f"{'ppr':>6} {'jac30':>6} {'r@10':>6} {'r@30':>6}")
        jac_l, r10_l, r30_l, build_l, ppr_l, sN_l = [], [], [], [], [], []
        for (label, kwid, df, kw) in seed_specs:
            seeds_text = [_fold(kw)]
            frank = personalized_pagerank_indexed(seeds_text, full, top_k=TOP_K)
            t0 = time.perf_counter()
            nodes, edges, sstats = build_subgraph(
                conn, [kwid], scope_dir_id=SCOPE_DIR_ID,
                max_hops=max_hops, top_m_per_node=top_m, min_npmi=MIN_NPMI,
            )
            sg = sub_to_indexed(conn, nodes, edges)
            build_ms = (time.perf_counter() - t0) * 1000
            t1 = time.perf_counter()
            srank = personalized_pagerank_indexed(seeds_text, sg, top_k=TOP_K)
            ppr_ms = (time.perf_counter() - t1) * 1000
            jac = jaccard([x for x, _ in frank], [x for x, _ in srank])
            r10 = rec_at(frank, srank, 10)
            r30 = rec_at(frank, srank, 30)
            jac_l.append(jac); r10_l.append(r10); r30_l.append(r30)
            build_l.append(build_ms); ppr_l.append(ppr_ms); sN_l.append(sstats.total_nodes)
            print(f"{label:>16} {df:>7} {sstats.total_nodes:>6} {sstats.total_edges:>7} "
                  f"{build_ms:>7.1f} {ppr_ms:>6.1f} {jac:>6.3f} {r10:>6.3f} {r30:>6.3f}")
        print(f"  avg: nodes={statistics.mean(sN_l):>6.0f} build={statistics.mean(build_l):>6.1f}ms "
              f"ppr={statistics.mean(ppr_l):>5.1f}ms  jac={statistics.mean(jac_l):.3f} "
              f"r@10={statistics.mean(r10_l):.3f} r@30={statistics.mean(r30_l):.3f}\n")

if __name__ == "__main__":
    main()
