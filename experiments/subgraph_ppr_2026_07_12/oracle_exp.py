"""Oracle experiment: if we cheat and know the full-graph top-K nodes ahead
of time, and build the *induced* subgraph on exactly those nodes, does
subgraph PPR agree with full PPR?

If NO: local PPR is fundamentally not equivalent (mass leaks differently
in a truncated graph, no matter how you truncate).
If YES: my expansion rule is broken and the approach is salvageable.

HISTORICAL — requires ``grga.graph.ppr``, which was DELETED in Phase 3b-2
(commit ``7a232c2``, before the v8/v9 keyword-primary series). Kept for
the evidence chain that led to removing PPR entirely (results are quoted
verbatim in the design doc and README). NOT runnable on current HEAD.
To re-run, check out a pre-``7a232c2`` commit.
"""
import os, sys, sqlite3, time, statistics
sys.path.insert(0, "/media/lqw/DATA/myrepo/grga/src")
from grga.graph.ppr import (  # noqa: F401 — HISTORICAL, module deleted post-3b-2
    load_edges_indexed, personalized_pagerank_indexed,
    edge_weight, IndexedGraph, _fold,
)

DB = os.path.expanduser("~/.config/grga/deep_keywords.db")
MIN_NPMI = 0.2
SCOPE_DIR_ID = 1


def induced_subgraph_from_nodes(full: IndexedGraph, oracle_node_ids):
    """Build induced subgraph on ``oracle_node_ids`` from an IndexedGraph."""
    kept = set(full.node_index[n] for n in oracle_node_ids if n in full.node_index)
    old_to_new = {}
    node_ids = []
    for old_idx in kept:
        old_to_new[old_idx] = len(node_ids)
        node_ids.append(full.node_ids[old_idx])
    neighbors = [[] for _ in node_ids]
    out_degree = [0.0] * len(node_ids)
    for old_idx in kept:
        ni = old_to_new[old_idx]
        for (j, w) in full.neighbors[old_idx]:
            if j in kept:
                nj = old_to_new[j]
                neighbors[ni].append((nj, w))
                out_degree[ni] += w
    node_index = {n: i for i, n in enumerate(node_ids)}
    return IndexedGraph(node_ids=node_ids, node_index=node_index,
                        neighbors=neighbors, out_degree=out_degree)


def rec_at(full, sub, k):
    fa = [x for x,_ in full[:k]]
    sb = set(x for x,_ in sub[:k])
    return sum(1 for x in fa if x in sb)/max(1,len(fa))
def jac(a,b):
    sa,sb = set(a),set(b); return len(sa&sb)/max(1,len(sa|sb))

def main():
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    print(f"MIN_NPMI={MIN_NPMI}\n")
    full = load_edges_indexed(conn, directory=None, min_npmi=MIN_NPMI)
    id2kw = {int(r[0]): r[1] for r in conn.execute("SELECT id, kw FROM keywords")}
    df_rows = conn.execute("SELECT kw_id, COUNT(*) df FROM keyword_chunks GROUP BY kw_id ORDER BY df").fetchall()

    # Sample seeds
    seeds = []
    for pct in [0.75, 0.90, 0.95, 0.99, 0.999]:
        kwid, df = int(df_rows[int(len(df_rows)*pct)][0]), int(df_rows[int(len(df_rows)*pct)][1])
        kw = id2kw.get(kwid, "")
        if _fold(kw) in full.node_index:
            seeds.append((f"p{int(pct*100) if pct<0.99 else pct}", kwid, df, kw))
    # + some ngram
    for r in conn.execute("SELECT phrase FROM auto_ngram_dict WHERE npmi>=0.5 ORDER BY count DESC LIMIT 30"):
        phrase = r[0]
        row = conn.execute("SELECT id FROM keywords WHERE kw=?", (phrase.casefold(),)).fetchone()
        if row and _fold(phrase) in full.node_index:
            seeds.append((f"N-{phrase[:10]}", int(row[0]), 0, phrase))
        if len([s for s in seeds if s[0].startswith("N-")]) >= 6: break

    # For each seed: get full top-30, use as oracle, build induced graph,
    # rerun PPR on it, measure match.
    print(f"{'seed':>15} {'oracle_N':>9} {'ind_E':>7} {'match@10':>9} {'match@30':>9} {'jac':>6}")
    print("-" * 70)
    m10_l, m30_l, jac_l = [], [], []
    for (label, kwid, df, kw) in seeds:
        st = [_fold(kw)]
        # Full PPR — take top-60 as the "oracle" node set (bigger than eval K)
        frank_60 = personalized_pagerank_indexed(st, full, top_k=60)
        oracle_nodes = [n for n,_ in frank_60] + [_fold(kw)]
        sub = induced_subgraph_from_nodes(full, oracle_nodes)
        srank = personalized_pagerank_indexed(st, sub, top_k=30)
        frank_30 = personalized_pagerank_indexed(st, full, top_k=30)
        m10 = rec_at(frank_30, srank, 10)
        m30 = rec_at(frank_30, srank, 30)
        j = jac([x for x,_ in frank_30], [x for x,_ in srank])
        n_edges = sum(len(nb) for nb in sub.neighbors)//2
        print(f"{label:>15} {len(sub.node_ids):>9} {n_edges:>7} {m10:>9.3f} {m30:>9.3f} {j:>6.3f}")
        m10_l.append(m10); m30_l.append(m30); jac_l.append(j)
    print("-" * 70)
    print(f"  avg match@10={statistics.mean(m10_l):.3f}  match@30={statistics.mean(m30_l):.3f}  jac={statistics.mean(jac_l):.3f}")

    # For comparison: pure "seed-only vote" — what if we just rank by
    # sum(edge_weight) over direct edges from seed? That's the trivial no-PPR
    # baseline.
    print(f"\n=== trivial baseline: 1-hop weight sum (no PPR) ===")
    print(f"{'seed':>15} {'r@10':>6} {'r@30':>6} {'jac30':>6}")
    r10_l, r30_l, j_l = [], [], []
    for (label, kwid, df, kw) in seeds:
        st = [_fold(kw)]
        frank = personalized_pagerank_indexed(st, full, top_k=30)
        seed_idx = full.node_index.get(_fold(kw))
        if seed_idx is None: continue
        scores = {}
        for (j_, w) in full.neighbors[seed_idx]:
            scores[full.node_ids[j_]] = scores.get(full.node_ids[j_], 0.0) + w
        top = sorted(scores.items(), key=lambda x: -x[1])[:30]
        r10 = rec_at(frank, top, 10); r30 = rec_at(frank, top, 30)
        j = jac([x for x,_ in frank], [x for x,_ in top])
        print(f"{label:>15} {r10:>6.3f} {r30:>6.3f} {j:>6.3f}")
        r10_l.append(r10); r30_l.append(r30); j_l.append(j)
    print(f"  avg r@10={statistics.mean(r10_l):.3f}  r@30={statistics.mean(r30_l):.3f}  jac={statistics.mean(j_l):.3f}")

if __name__ == "__main__":
    main()
