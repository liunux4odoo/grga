"""Compare two SQLite build outputs at 1e-12 float tolerance.

Excludes ``updated_at`` (wall-clock, always differs between runs).
Not literally bit-exact — floats are rounded to 12 decimal digits before
compare, which is ~4 ULP for doubles. Adequate for jieba-determinism
checks (PR-4 goal); for stricter comparisons switch to ``float.hex()``.

Run: python equivalence_check.py baseline.db pr4.db
"""
from __future__ import annotations
import argparse, sqlite3, sys

TABLES = [
    ("keyword_pairs",
     "SELECT scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi "
     "FROM keyword_pairs ORDER BY scope_id, kw_a_id, kw_b_id, source"),
    ("keyword_stats",
     "SELECT scope_id, kw_id, chunk_df, doc_df, total_occurrences "
     "FROM keyword_stats ORDER BY scope_id, kw_id"),
    ("keyword_docs",
     "SELECT scope_id, kw_id, doc_id, occurrence_count, chunk_count "
     "FROM keyword_docs ORDER BY scope_id, kw_id, doc_id"),
    ("auto_ngram_dict",
     "SELECT scope_id, phrase, n, count, npmi, branch_ent "
     "FROM auto_ngram_dict ORDER BY scope_id, phrase"),
    ("scope_stats",
     "SELECT scope_id, n_chunks, n_docs, vocab_size "
     "FROM scope_stats ORDER BY scope_id"),
    ("docs",
     "SELECT scope_id, doc_path, size, mtime, content_hash, n_chunks_with_kw "
     "FROM docs ORDER BY scope_id, doc_path"),
]


def _fetch(db, sql):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _norm(row):
    return tuple(round(x, 12) if isinstance(x, float) else x for x in row)


def diff(a, b):
    ok = True
    for table, sql in TABLES:
        try:
            ra = [_norm(r) for r in _fetch(a, sql)]
            rb = [_norm(r) for r in _fetch(b, sql)]
        except sqlite3.OperationalError as e:
            print(f"  [SKIP] {table}: {e}"); ok = False; continue
        if ra == rb:
            print(f"  [ OK ] {table}: {len(ra)} rows identical")
        else:
            ok = False
            sa, sb = set(map(str, ra)), set(map(str, rb))
            only_a, only_b = sa - sb, sb - sa
            print(f"  [FAIL] {table}: A={len(ra)} B={len(rb)} "
                  f"only_A={len(only_a)} only_B={len(only_b)}")
            for r in list(only_a)[:3]:
                print(f"    only_A: {r[:120]}")
            for r in list(only_b)[:3]:
                print(f"    only_B: {r[:120]}")
    return ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("a"); p.add_argument("b")
    args = p.parse_args()
    print(f"A={args.a}\nB={args.b}\n")
    ok = diff(args.a, args.b)
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
