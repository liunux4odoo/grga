"""
Compare full-graph PPR top-K vs K-hop subgraph PPR top-K on the real DB.

Uses grga's own ppr.py (edge_weight, personalized_pagerank_indexed,
IndexedGraph, _fold) so the PPR math is identical — only the graph's
domain differs (full vs subgraph).

HISTORICAL — requires ``grga.graph.ppr``, DELETED in Phase 3b-2 (commit
``7a232c2``). Kept as evidence for the "local PPR is not viable" decision.
NOT runnable on current HEAD; check out a pre-``7a232c2`` commit to re-run.
"""
import os
import sys
import sqlite3
import time
import statistics

sys.path.insert(0, "/media/lqw/DATA/myrepo/grga/src")
sys.path.insert(0, "/tmp/kilo/grga_subgraph_exp")

from grga.graph.ppr import (  # noqa: E402
    load_edges_indexed,
    personalized_pagerank_indexed,
    edge_weight,
    IndexedGraph,
    _fold,
    _HAS_NUMPY,
)
from subgraph import build_subgraph  # noqa: E402

DB = os.path.expanduser("~/.config/grga/deep_keywords.db")
MIN_NPMI = 0.2   # production default
TOP_K = 30
MAX_HOPS = 3
TOP_M = 100


def subgraph_to_indexed(conn, nodes, edges) -> IndexedGraph:
    """Build an IndexedGraph (grga's PPR input) from subgraph nodes/edges."""
    # Aggregate parallel edges via edge_weight, same as load_edges_indexed.
    local_index = {}
    neighbors = []
    out_degree = []
    used_kw_ids = []

    def _local(kwid):
        idx = local_index.get(kwid)
        if idx is None:
            idx = len(local_index)
            local_index[kwid] = idx
            neighbors.append([])
            out_degree.append(0.0)
            used_kw_ids.append(kwid)
        return idx

    for (a, b), (co, npmi, src) in edges.items():
        w = edge_weight(src, co, npmi)
        if w <= 0:
            continue
        ia, ib = _local(a), _local(b)
        neighbors[ia].append((ib, w))
        neighbors[ib].append((ia, w))
        out_degree[ia] += w
        out_degree[ib] += w

    # translate kw_id -> text
    node_ids = [""] * len(used_kw_ids)
    if used_kw_ids:
        chunk = 900
        id_to_text = {}
        for i in range(0, len(used_kw_ids), chunk):
            piece = used_kw_ids[i:i + chunk]
            ph = ",".join("?" * len(piece))
            for r in conn.execute(
                f"SELECT id, kw FROM keywords WHERE id IN ({ph})", piece
            ):
                id_to_text[int(r[0])] = _fold(r[1])
        for li, kwid in enumerate(used_kw_ids):
            node_ids[li] = id_to_text.get(kwid, "")
    node_index = {name: i for i, name in enumerate(node_ids) if name}
    return IndexedGraph(
        node_ids=node_ids, node_index=node_index,
        neighbors=neighbors, out_degree=out_degree,
    )


def jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def rank_overlap_at_k(full, sub, k):
    """Fraction of full top-k present in sub top-k (recall@k)."""
    fa = [x for x, _ in full[:k]]
    sb = set(x for x, _ in sub[:k])
    if not fa:
        return 1.0
    return sum(1 for x in fa if x in sb) / len(fa)


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print(f"numpy backend available: {_HAS_NUMPY}")
    print(f"MIN_NPMI={MIN_NPMI} TOP_K={TOP_K} MAX_HOPS={MAX_HOPS} TOP_M={TOP_M}\n")

    # scope: single dir_id=1
    scope_dir_id = 1

    # Full graph (baseline), warm once.
    t = time.perf_counter()
    full_graph = load_edges_indexed(conn, directory=None, min_npmi=MIN_NPMI)
    full_load_ms = (time.perf_counter() - t) * 1000
    n_full_edges = sum(len(nb) for nb in full_graph.neighbors) // 2
    print(f"full graph: {len(full_graph.node_ids)} nodes, "
          f"{n_full_edges} edges, load={full_load_ms:.0f} ms\n")

    # id<->text maps for seed selection
    id2kw = {}
    kw2id = {}
    for r in conn.execute("SELECT id, kw FROM keywords"):
        id2kw[int(r[0])] = r[1]
        kw2id[_fold(r[1])] = int(r[0])

    # Choose seeds across the df spectrum that ARE present in the full graph.
    dfrows = conn.execute(
        "SELECT kw_id, COUNT(*) df FROM keyword_chunks GROUP BY kw_id ORDER BY df"
    ).fetchall()
    dfrows = [(int(r[0]), int(r[1])) for r in dfrows]
    n = len(dfrows)
    seed_specs = []
    for label, pct in [("p50", 0.50), ("p75", 0.75), ("p90", 0.90),
                       ("p95", 0.95), ("p99", 0.99), ("p99.9", 0.999)]:
        kwid, df = dfrows[int(n * pct)]
        kw = id2kw.get(kwid, "")
        if _fold(kw) in full_graph.node_index:
            seed_specs.append((label, kwid, df, kw))

    print(f"{'seed':>8} {'df':>7} {'kw':<18} {'sub_nodes':>10} {'sub_edges':>10} "
          f"{'build_ms':>9} {'ppr_ms':>8} {'jac@30':>7} {'rec@10':>7} {'rec@30':>7}")
    print("-" * 110)

    jac_list, rec10_list, rec30_list = [], [], []
    sub_build_list, sub_total_list = [], []

    for label, kwid, df, kw in seed_specs:
        seeds_text = [_fold(kw)]

        # --- Full-graph PPR (baseline) ---
        full_rank = personalized_pagerank_indexed(
            seeds_text, full_graph, top_k=TOP_K
        )

        # --- Subgraph PPR ---
        t0 = time.perf_counter()
        nodes, edges, sstats = build_subgraph(
            conn, [kwid], scope_dir_id=scope_dir_id,
            max_hops=MAX_HOPS, top_m_per_node=TOP_M, min_npmi=MIN_NPMI,
        )
        sub_graph = subgraph_to_indexed(conn, nodes, edges)
        build_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        sub_rank = personalized_pagerank_indexed(
            seeds_text, sub_graph, top_k=TOP_K
        )
        ppr_ms = (time.perf_counter() - t1) * 1000

        jac = jaccard([x for x, _ in full_rank], [x for x, _ in sub_rank])
        rec10 = rank_overlap_at_k(full_rank, sub_rank, 10)
        rec30 = rank_overlap_at_k(full_rank, sub_rank, 30)

        jac_list.append(jac)
        rec10_list.append(rec10)
        rec30_list.append(rec30)
        sub_build_list.append(build_ms)
        sub_total_list.append(build_ms + ppr_ms)

        print(f"{label:>8} {df:>7} {kw[:18]:<18} {sstats.total_nodes:>10} "
              f"{sstats.total_edges:>10} {build_ms:>9.1f} {ppr_ms:>8.1f} "
              f"{jac:>7.3f} {rec10:>7.3f} {rec30:>7.3f}")

    print("-" * 110)
    print(f"\nAvg jaccard@30: {statistics.mean(jac_list):.3f}")
    print(f"Avg recall@10:  {statistics.mean(rec10_list):.3f}")
    print(f"Avg recall@30:  {statistics.mean(rec30_list):.3f}")
    print(f"Avg subgraph build: {statistics.mean(sub_build_list):.1f} ms")
    print(f"Avg subgraph total (build+ppr): {statistics.mean(sub_total_list):.1f} ms")
    print(f"vs full-graph one-time load: {full_load_ms:.0f} ms "
          f"(+ ppr per query on full graph)")

    # Show one concrete top-10 diff for the p95 seed
    print("\n=== concrete top-10 comparison (p95 seed) ===")
    for label, kwid, df, kw in seed_specs:
        if label != "p95":
            continue
        seeds_text = [_fold(kw)]
        full_rank = personalized_pagerank_indexed(seeds_text, full_graph, top_k=10)
        nodes, edges, _ = build_subgraph(
            conn, [kwid], scope_dir_id=scope_dir_id,
            max_hops=MAX_HOPS, top_m_per_node=TOP_M, min_npmi=MIN_NPMI)
        sub_graph = subgraph_to_indexed(conn, nodes, edges)
        sub_rank = personalized_pagerank_indexed(seeds_text, sub_graph, top_k=10)
        print(f"seed = {kw}")
        print(f"{'rank':>4}  {'FULL':<24} {'SUBGRAPH':<24}")
        for i in range(10):
            f = full_rank[i][0] if i < len(full_rank) else ""
            s = sub_rank[i][0] if i < len(sub_rank) else ""
            mark = "" if f == s else "  <-- differs"
            print(f"{i+1:>4}  {f:<24} {s:<24}{mark}")


if __name__ == "__main__":
    main()
