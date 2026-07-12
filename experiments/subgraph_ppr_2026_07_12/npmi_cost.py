"""Test on-the-fly npmi cost (decision 1 = plan B).

Simulates the NEW query path:
  1. pair table stores raw cooccur_count + df_a + df_b (NOT npmi)
  2. n_chunks is a scalar (from meta)
  3. per query: fetch seed's neighbours, compute npmi in Python, sort, top-M

Compare against: pair table stores precomputed npmi (plan A), just read+sort.
"""
import os, sqlite3, time, math, statistics
DB = os.path.expanduser("~/.config/grga/deep_keywords.db")
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

# n_chunks scalar + per-kw df map (would be keyword_stats table)
n_chunks = c.execute("SELECT COUNT(*) FROM (SELECT 1 FROM keyword_chunks GROUP BY doc_id, chunk_id)").fetchone()[0]
print(f"n_chunks = {n_chunks}\n")

# Build df map once (this would BE the keyword_stats table, read once at query start)
t = time.perf_counter()
df_map = {int(r[0]): int(r[1]) for r in c.execute(
    "SELECT kw_id, COUNT(*) FROM keyword_chunks GROUP BY kw_id")}
df_build = (time.perf_counter() - t) * 1000
print(f"df_map (= keyword_stats table, {len(df_map)} kws): built in {df_build:.0f} ms "
      f"(one-time; in prod this is a table SELECT, ~{len(df_map)*8/1024:.0f}KB)\n")

# Pick seeds across df spectrum
df_rows = sorted(df_map.items(), key=lambda x: x[1])
n = len(df_rows)
seeds = []
for label, pct in [("p50",0.5),("p75",0.75),("p90",0.9),("p95",0.95),("p99",0.99),("p99.9",0.999),("max",1.0)]:
    idx = min(int(n*pct), n-1)
    seeds.append((label, df_rows[idx][0], df_rows[idx][1]))

def npmi(co, da, db, N):
    if co<=0 or da<=0 or db<=0: return None
    p_ab = co/N
    if p_ab>=1: return None
    denom = -math.log(p_ab)
    if denom<=0: return None
    return math.log(p_ab*N*N/(da*db))/denom

MIN_NPMI = 0.2
TOP_M = 30

print("Plan B: fetch raw pairs, compute npmi in Python, filter+sort")
print(f"{'seed':>8} {'df':>7} {'raw_rows':>9} {'fetch_ms':>9} {'npmi_ms':>8} {'sort_ms':>8} {'total_ms':>9} {'kept':>5}")
b_totals = []
for label, kwid, df in seeds:
    reps = 5
    best = None
    for _ in range(reps):
        t0 = time.perf_counter()
        rows = c.execute(
            "SELECT kw_a_id, kw_b_id, cooccur_count FROM keyword_pairs "
            "WHERE source='chunk_cooccur' AND (kw_a_id=? OR kw_b_id=?)",
            (kwid, kwid)).fetchall()
        t1 = time.perf_counter()
        cands = []
        for r in rows:
            other = r[1] if r[0]==kwid else r[0]
            v = npmi(r[2], df, df_map.get(other,0), n_chunks)
            if v is not None and v >= MIN_NPMI:
                cands.append((other, v))
        t2 = time.perf_counter()
        cands.sort(key=lambda x: -x[1])
        top = cands[:TOP_M]
        t3 = time.perf_counter()
        tot = (t3-t0)*1000
        if best is None or tot < best[0]:
            best = (tot, (t1-t0)*1000, (t2-t1)*1000, (t3-t2)*1000, len(rows), len(top))
    tot, fetch, npmi_ms, sort_ms, nrows, kept = best
    b_totals.append(tot)
    print(f"{label:>8} {df:>7} {nrows:>9} {fetch:>9.2f} {npmi_ms:>8.2f} {sort_ms:>8.2f} {tot:>9.2f} {kept:>5}")

print(f"\nPlan A: precomputed npmi in table, read+sort only (baseline)")
print(f"{'seed':>8} {'df':>7} {'total_ms':>9} {'kept':>5}")
a_totals = []
for label, kwid, df in seeds:
    best = None
    for _ in range(5):
        t0 = time.perf_counter()
        rows = c.execute(
            "SELECT kw_a_id, kw_b_id, npmi FROM keyword_pairs "
            "WHERE source='chunk_cooccur' AND (kw_a_id=? OR kw_b_id=?) AND npmi>=? "
            "ORDER BY npmi DESC LIMIT ?",
            (kwid, kwid, MIN_NPMI, TOP_M)).fetchall()
        tot = (time.perf_counter()-t0)*1000
        if best is None or tot < best: best = tot
    a_totals.append(best)
    print(f"{label:>8} {df:>7} {best:>9.2f} {len(rows):>5}")

print(f"\n=== 汇总 ===")
print(f"Plan B (现算) 平均 total: {statistics.mean(b_totals):.2f} ms, 中位: {statistics.median(b_totals):.2f} ms, 最坏(max-df hub): {max(b_totals):.2f} ms")
print(f"Plan A (预存) 平均 total: {statistics.mean(a_totals):.2f} ms, 最坏: {max(a_totals):.2f} ms")
print(f"\n注意: Plan A/B 都受制于当前 OR-form 全扫 (idx 未命中)。真实新方案 pair 表只有 ~16万行,")
print(f"      且我们会给 (kw_a_id) / (kw_b_id) 建覆盖索引。下面测有索引的情况:")

# Simulate with proper index: create temp filtered table + index, measure
c.execute("DROP TABLE IF EXISTS temp.kp_small")
c.execute("CREATE TABLE temp.kp_small AS SELECT kw_a_id, kw_b_id, cooccur_count, npmi FROM keyword_pairs WHERE source='chunk_cooccur' AND cooccur_count>=5 AND npmi>=0.15")
nsmall = c.execute("SELECT COUNT(*) FROM temp.kp_small").fetchone()[0]
c.execute("CREATE INDEX temp.ix_a ON kp_small(kw_a_id)")
c.execute("CREATE INDEX temp.ix_b ON kp_small(kw_b_id)")
print(f"\n模拟严过滤 pair 表 (co>=5,npmi>=0.15): {nsmall} 行 + 双索引")
print(f"{'seed':>8} {'df':>7} {'B现算_ms':>10} {'kept':>5}")
for label, kwid, df in seeds:
    best = None
    for _ in range(5):
        t0 = time.perf_counter()
        rows = c.execute(
            "SELECT kw_a_id, kw_b_id, cooccur_count FROM temp.kp_small "
            "WHERE kw_a_id=? UNION ALL SELECT kw_a_id, kw_b_id, cooccur_count "
            "FROM temp.kp_small WHERE kw_b_id=?", (kwid, kwid)).fetchall()
        cands = []
        for r in rows:
            other = r[1] if r[0]==kwid else r[0]
            v = npmi(r[2], df, df_map.get(other,0), n_chunks)
            if v is not None and v>=MIN_NPMI: cands.append((other,v))
        cands.sort(key=lambda x:-x[1])
        tot = (time.perf_counter()-t0)*1000
        if best is None or tot<best: best=(tot,len(cands[:TOP_M]))
    print(f"{label:>8} {df:>7} {best[0]:>10.3f} {best[1]:>5}")
