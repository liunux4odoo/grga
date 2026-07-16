"""Instrument each pair-generation sub-stage of _finalize_pairs_from_temp.

Synthesises a realistic ``_build_chunk_kws`` (doc_id, chunk_id, kw_id)
table at a configurable scale, then times each SQL stage separately
with EXPLAIN QUERY PLAN, to locate where "生成共现对" spends its time.

Scale knobs (env):
  DOCS   (default 200)    number of documents
  CHUNKS (default 500)    chunks per document
  KW     (default 25)     distinct keywords per chunk (post-3b-filter)
  VOCAB  (default 80000)  distinct keyword id space
  CACHE_MB (default 256)  PRAGMA cache_size

Emits per-stage wall time. Mirrors the exact SQL in build.py.
"""
from __future__ import annotations
import os, random, sqlite3, tempfile, time
from pathlib import Path

DOCS = int(os.environ.get("DOCS", "200"))
CHUNKS = int(os.environ.get("CHUNKS", "500"))
KW = int(os.environ.get("KW", "25"))
VOCAB = int(os.environ.get("VOCAB", "80000"))
CACHE_MB = int(os.environ.get("CACHE_MB", "256"))
MIN_PAIR = int(os.environ.get("MIN_PAIR", "3"))

random.seed(1234)


def _timed(label, fn):
    t0 = time.perf_counter()
    r = fn()
    dt = time.perf_counter() - t0
    print(f"  {label:<42} {dt:8.2f}s", flush=True)
    return r, dt


def main():
    dbdir = Path(tempfile.mkdtemp(prefix="pairbench_"))
    db = dbdir / "b.db"
    tmpdir = dbdir / "tmp"
    tmpdir.mkdir()
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute(f"PRAGMA temp_store_directory = '{tmpdir}'")
    conn.execute(f"PRAGMA cache_size = {-CACHE_MB*1024}")

    conn.execute("CREATE TABLE keyword_pairs ("
                 "scope_id INT, kw_a_id INT, kw_b_id INT, source TEXT,"
                 "cooccur_count INT, npmi REAL, updated_at TEXT,"
                 "PRIMARY KEY(scope_id, kw_a_id, kw_b_id, source)) WITHOUT ROWID")
    conn.execute("CREATE TEMP TABLE _build_chunk_kws ("
                 "doc_id INT, chunk_id INT, kw_id INT)")

    total_rows = DOCS * CHUNKS * KW
    print(f"scale: DOCS={DOCS} CHUNKS={CHUNKS} KW={KW} VOCAB={VOCAB} "
          f"→ ~{total_rows/1e6:.1f}M stage rows, CACHE={CACHE_MB}MB")

    # Populate _build_chunk_kws. Keywords per chunk: a Zipf-ish sample so
    # some kws are common (realistic co-occurrence density).
    def _populate():
        buf = []
        gid = 0
        for d in range(DOCS):
            for c in range(CHUNKS):
                # bias toward low ids (common words) + some doc-local ids
                ks = set()
                while len(ks) < KW:
                    if random.random() < 0.5:
                        ks.add(random.randint(0, VOCAB // 20))   # common
                    else:
                        ks.add(random.randint(0, VOCAB - 1))     # tail
                for k in ks:
                    buf.append((d, c, k))
                    if len(buf) >= 500000:
                        conn.executemany("INSERT INTO _build_chunk_kws VALUES(?,?,?)", buf)
                        buf.clear()
        if buf:
            conn.executemany("INSERT INTO _build_chunk_kws VALUES(?,?,?)", buf)
        conn.commit()
    _timed("populate _build_chunk_kws", _populate)

    now = "2026-07-15T00:00:00Z"
    scope_id = 1
    SRC = "chunk_cooccur"

    _timed("CREATE INDEX (doc_id, chunk_id)", lambda: conn.execute(
        "CREATE INDEX _idx_bcki_chunk ON _build_chunk_kws (doc_id, chunk_id)"))

    _timed("SELECT DISTINCT doc_id", lambda: conn.execute(
        "SELECT DISTINCT doc_id FROM _build_chunk_kws ORDER BY doc_id").fetchall())

    _timed("COUNT(*) stage rows", lambda: conn.execute(
        "SELECT COUNT(*) FROM _build_chunk_kws").fetchone())

    # --- PER-DOC path timing ---
    conn.execute("CREATE TEMP TABLE _pair_raw (kw_a_id INT, kw_b_id INT, c INT)")
    doc_ids = [r[0] for r in conn.execute("SELECT DISTINCT doc_id FROM _build_chunk_kws ORDER BY doc_id")]

    def _perdoc_loop():
        for doc_id in doc_ids:
            conn.execute("""
                INSERT INTO _pair_raw (kw_a_id, kw_b_id, c)
                SELECT a.kw_id, b.kw_id, COUNT(*)
                FROM _build_chunk_kws a JOIN _build_chunk_kws b
                  ON a.doc_id=b.doc_id AND a.chunk_id=b.chunk_id AND a.kw_id<b.kw_id
                WHERE a.doc_id = ?
                GROUP BY a.kw_id, b.kw_id
            """, (doc_id,))
    _timed(f"PER-DOC loop ({len(doc_ids)} docs → _pair_raw)", _perdoc_loop)

    raw_rows = conn.execute("SELECT COUNT(*) FROM _pair_raw").fetchone()[0]
    print(f"    _pair_raw rows = {raw_rows/1e6:.1f}M")

    def _merge():
        conn.execute("""
            INSERT INTO keyword_pairs
                (scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at)
            SELECT ?, kw_a_id, kw_b_id, ?, SUM(c), NULL, ?
            FROM _pair_raw GROUP BY kw_a_id, kw_b_id HAVING SUM(c) >= ?
            ON CONFLICT(scope_id, kw_a_id, kw_b_id, source) DO UPDATE SET
                cooccur_count = cooccur_count + excluded.cooccur_count
        """, (scope_id, SRC, now, MIN_PAIR))
    _timed("FINAL MERGE (_pair_raw → keyword_pairs)", _merge)

    surviving = conn.execute("SELECT COUNT(*) FROM keyword_pairs").fetchone()[0]
    print(f"    keyword_pairs surviving = {surviving/1e6:.2f}M")

    # EXPLAIN QUERY PLAN for one per-doc statement
    print("\n  EQP per-doc self-join:")
    for r in conn.execute("""
        EXPLAIN QUERY PLAN
        SELECT a.kw_id, b.kw_id, COUNT(*)
        FROM _build_chunk_kws a JOIN _build_chunk_kws b
          ON a.doc_id=b.doc_id AND a.chunk_id=b.chunk_id AND a.kw_id<b.kw_id
        WHERE a.doc_id = ?
        GROUP BY a.kw_id, b.kw_id
    """, (doc_ids[0],)):
        print("   ", tuple(r))

    conn.close()
    import shutil
    shutil.rmtree(dbdir, ignore_errors=True)


if __name__ == "__main__":
    main()
