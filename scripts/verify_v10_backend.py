"""Minimal end-to-end backend smoke test for schema v10.

Verifies the plan-2026-07-14 scope / directory split:

1. ``ensure_schema`` on a fresh DB creates the v10 shape without error.
2. ``build_graph_index(scope='<name>', directory='<path>')`` records
   the scope name in ``scopes`` and the directory binding in
   ``scope_directories``.
3. ``related_keywords(scopes=['<name>'])`` returns co-occurrence pairs
   filtered by scope name (no path involved).
4. ``related_keywords(scopes=None)`` returns the cross-scope union.
5. ``inspect_keyword`` reports ``docs_by_scope`` keyed by logical name.

Run:

    /media/lqw/DATA/apps/miniforge3/envs/py312/bin/python3 \
        scripts/verify_v10_backend.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grga.graph.build import build_graph_index
from grga.graph.inspect import inspect_keyword
from grga.graph.schema import (
    SCHEMA_VERSION,
    connect,
    ensure_schema,
    read_schema_version,
    resolve_scope_name,
)
from grga.graph.ids import IdInterner
from grga.related import related_keywords


def make_corpus(root: Path) -> None:
    """Create a tiny two-file corpus with clear co-occurrence signal."""
    root.mkdir(parents=True, exist_ok=True)
    # doc1: alpha + beta always co-occur; gamma appears alongside beta
    (root / "doc1.txt").write_text(
        "alpha beta gamma\nalpha beta\nalpha beta\nalpha beta\n"
        "alpha beta\nalpha beta\nbeta gamma\nbeta gamma\n"
        "alpha beta gamma\nalpha beta\n"
    )
    (root / "doc2.txt").write_text(
        "alpha beta\nalpha beta\nbeta gamma\nbeta gamma\n"
        "alpha beta\nalpha beta\nalpha beta\nbeta gamma\n"
    )


def check(cond: bool, msg: str) -> None:
    marker = "PASS" if cond else "FAIL"
    print(f"[{marker}] {msg}")
    if not cond:
        raise SystemExit(1)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="grga_v10_check_"))
    try:
        db_path = tmp / "graph.db"
        corpus = tmp / "physics"
        make_corpus(corpus)

        # 1) Fresh DB reaches v10 without error.
        conn = connect(db_path)
        try:
            ensure_schema(conn)
            check(read_schema_version(conn) == SCHEMA_VERSION,
                  f"fresh DB stamped user_version={SCHEMA_VERSION}")
            # ``scopes`` and ``scope_directories`` exist.
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            check("scopes" in tables, "scopes table exists")
            check("scope_directories" in tables,
                  "scope_directories table exists")
            check("directories" not in tables,
                  "legacy directories table absent")
        finally:
            conn.close()

        # resolve_scope_name is pure-string, no filesystem touch.
        check(resolve_scope_name(None) is None,
              "resolve_scope_name(None) -> None")
        check(resolve_scope_name("  physics-cn  ") == "physics-cn",
              "resolve_scope_name strips whitespace")
        raised = False
        try:
            resolve_scope_name("")
        except ValueError:
            raised = True
        check(raised, "resolve_scope_name('') raises ValueError")

        # 2) build under a logical scope name.
        stats = build_graph_index(
            "physics-cn",
            directory=corpus,
            db_path=str(db_path),
            mode="rebuild",
            refresh_ngram="never",
            chunk_mode="line",
            min_pair_count=2,
            min_npmi=0.0,
        )
        check(stats.scope == "physics-cn",
              f"BuildStats.scope == 'physics-cn' (got {stats.scope!r})")
        check(stats.files_indexed >= 1,
              f"indexed {stats.files_indexed} files (>= 1)")

        conn = connect(db_path)
        try:
            interner = IdInterner(conn)
            scope_id = interner.scope_id_ro("physics-cn")
            check(scope_id is not None, "physics-cn scope was interned")
            bindings = interner.list_directories(scope_id)
            check(len(bindings) == 1, f"one directory binding recorded (got {len(bindings)})")
            check(bindings[0]["directory"] == str(corpus.resolve()),
                  f"directory binding matches ({bindings[0]['directory']})")
            # scope_stats populated
            n_chunks = conn.execute(
                "SELECT n_chunks FROM scope_stats WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
            check(n_chunks is not None and int(n_chunks[0]) > 0,
                  f"scope_stats.n_chunks = {n_chunks[0] if n_chunks else None}")
            # at least one chunk_cooccur pair
            n_pairs = conn.execute(
                "SELECT COUNT(*) FROM keyword_pairs "
                "WHERE scope_id = ? AND source = 'chunk_cooccur'",
                (scope_id,),
            ).fetchone()[0]
            check(int(n_pairs) >= 1,
                  f"chunk_cooccur pairs recorded (got {n_pairs})")
        finally:
            conn.close()

        # 3) related_keywords with scopes=['physics-cn']
        result = related_keywords(
            ["alpha"],
            scopes=["physics-cn"],
            db_path=str(db_path),
            min_npmi=0.0,
        )
        check(any(r.via_seed == "alpha" for r in result.results)
              or any(s.in_index for s in result.resolved_seeds),
              "seed 'alpha' resolves in scope 'physics-cn'")
        alpha_hits = [r.kw for r in result.results]
        check("beta" in alpha_hits or "gamma" in alpha_hits,
              f"alpha's neighbours include beta/gamma ({alpha_hits})")

        # 4) related_keywords with scopes=None (cross-scope union)
        result_all = related_keywords(
            ["alpha"],
            scopes=None,
            db_path=str(db_path),
            min_npmi=0.0,
        )
        check(len(result_all.results) >= len(result.results),
              "cross-scope union returns >= single-scope result count")

        # 5) inspect reports logical scope names
        report = inspect_keyword("alpha", db_path=str(db_path))
        check(report.get("total_docs", 0) >= 1,
              f"inspect(alpha).total_docs = {report.get('total_docs')}")
        check("physics-cn" in (report.get("docs_by_scope") or {}),
              f"docs_by_scope keyed by scope name: "
              f"{list((report.get('docs_by_scope') or {}).keys())}")

        # 6) unknown scope returns empty results (not an error)
        empty = related_keywords(
            ["alpha"],
            scopes=["unknown-scope-x"],
            db_path=str(db_path),
            min_npmi=0.0,
        )
        check(empty.results == [],
              "unknown scope returns empty results")
        check(any(s.in_index for s in empty.resolved_seeds),
              "seed vocabulary probe still populated")

        # 7) legacy DB (user_version=9 + a fake directories table) is rejected.
        legacy_db = tmp / "legacy.db"
        c = sqlite3.connect(str(legacy_db))
        c.execute("CREATE TABLE directories (id INTEGER PRIMARY KEY, path TEXT)")
        c.execute("PRAGMA user_version = 9")
        c.commit()
        c.close()
        legacy_conn = connect(legacy_db)
        rejected = False
        try:
            ensure_schema(legacy_conn)
        except RuntimeError as exc:
            rejected = True
            check("scope / directory split" in str(exc),
                  "legacy rejection mentions scope / directory split")
            check("SELECT path FROM directories" in str(exc),
                  "v5-v9 rejection message includes directories-inspect hint")
        finally:
            legacy_conn.close()
        check(rejected, "v5-v9 legacy DB triggers ensure_schema rejection")

        # 7b) pre-v5 KeywordLibrary shape (no `directories`, user_version=0,
        #     TEXT-directory keyword_counts) is ALSO rejected — regression
        #     coverage for the review finding about missed pre-v5 shapes.
        prev5_db = tmp / "prev5.db"
        c = sqlite3.connect(str(prev5_db))
        c.execute(
            "CREATE TABLE keyword_counts (scope TEXT, directory TEXT, "
            "term TEXT, count REAL)"
        )
        c.execute(
            "CREATE TABLE keyword_sources (directory TEXT, keyword TEXT, "
            "path TEXT, line_number INTEGER, count REAL)"
        )
        # user_version left at 0 — matches the historical unstamped shape.
        c.commit()
        c.close()
        prev5_conn = connect(prev5_db)
        rejected2 = False
        try:
            ensure_schema(prev5_conn)
        except RuntimeError as exc:
            rejected2 = True
            check(".tables" in str(exc) or "PRAGMA user_version" in str(exc),
                  "pre-v5 rejection uses the .tables / pragma hint "
                  "(no bogus 'SELECT path FROM directories')")
        finally:
            prev5_conn.close()
        check(rejected2, "pre-v5 KeywordLibrary DB triggers rejection")

        # 8) Single-scope query plan preserves index-ordered LIMIT
        #    (no TEMP B-TREE FOR ORDER BY). Regression test for the perf
        #    finding on multi-scope IN vs single-scope =.
        conn = connect(db_path)
        try:
            plan_rows = conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT kw_b_id FROM keyword_pairs "
                "WHERE scope_id = ? AND kw_a_id = ? AND source = ? "
                "AND npmi >= ? ORDER BY npmi DESC LIMIT ?",
                (1, 1, "chunk_cooccur", 0.0, 10),
            ).fetchall()
            plan_text = " | ".join(str(r["detail"]) for r in plan_rows)
            check("TEMP B-TREE FOR ORDER BY" not in plan_text.upper(),
                  f"single-scope EQ plan avoids temp-sort: {plan_text!r}")
        finally:
            conn.close()

        # 9) `record_votes(scope=None)` silently skips; explicit path-shaped
        #    scope in the KeywordLibrary layer is rejected loudly.
        from grga.graph.votes import record_votes
        from grga.deep_search import KeywordLibrary

        empty_vote = record_votes(
            [("a", "b")], scope=None, db_path=str(db_path)
        )
        check(empty_vote.pairs_recorded == 0,
              "record_votes(scope=None) silently skips")

        lib = KeywordLibrary(str(db_path))
        try:
            path_rejected = False
            try:
                lib._scope_id("/tmp/looks-like-a-path")
            except ValueError:
                path_rejected = True
            check(path_rejected,
                  "KeywordLibrary._scope_id rejects path-shaped input")
        finally:
            lib.close()

        # 10) `_normalise_scopes([""])` returns None (tolerance fix)
        from grga.related import _normalise_scopes

        check(_normalise_scopes([""]) is None,
              "_normalise_scopes tolerates empty-string items")
        check(_normalise_scopes(["  ", "\t"]) is None,
              "_normalise_scopes tolerates whitespace-only items")
        check(_normalise_scopes(["physics-cn", ""]) == ["physics-cn"],
              "_normalise_scopes drops blank items but keeps real names")

        # 11) v3.1 (plan-2026-07-14 §5.3): RelatedKeyword.scopes carries
        #     the query-range scope names for each candidate.
        one_scope_result = related_keywords(
            ["alpha"],
            scopes=["physics-cn"],
            db_path=str(db_path),
            min_npmi=0.0,
        )
        check(all(r.scopes == ["physics-cn"] for r in one_scope_result.results),
              "single-scope query: RelatedKeyword.scopes is singleton")

        # Build a second scope on the same corpus so we can exercise the
        # multi-scope path (independent scope, same pairs).
        build_graph_index(
            "physics-en",
            directory=corpus,
            db_path=str(db_path),
            mode="rebuild",
            refresh_ngram="never",
            chunk_mode="line",
            min_pair_count=2,
            min_npmi=0.0,
        )
        multi = related_keywords(
            ["alpha"],
            scopes=["physics-cn", "physics-en"],
            db_path=str(db_path),
            min_npmi=0.0,
        )
        both_scopes = [r for r in multi.results if len(r.scopes) == 2]
        check(both_scopes,
              "multi-scope query: at least one candidate has scopes from both")
        for r in both_scopes:
            check(r.scopes == sorted(r.scopes),
                  f"scopes list sorted ascending: {r.scopes}")
            check(r.scopes == ["physics-cn", "physics-en"],
                  f"scopes union both queried scopes: {r.scopes}")

        cross = related_keywords(
            ["alpha"],
            scopes=None,
            db_path=str(db_path),
            min_npmi=0.0,
        )
        check(all(r.scopes for r in cross.results),
              "cross-scope query: every result has non-empty scopes")

        print("\nALL CHECKS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
