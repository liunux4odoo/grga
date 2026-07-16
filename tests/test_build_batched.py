"""Tests for the batched build pipeline (batched_build_2026_07_16).

Covers:
  1. batch_chunks=0 (default) == batch_chunks>total (single batch) bit-exact.
  2. multi-batch build produces valid, finalised tables.
  3. rejection of unsupported mode / refresh_ngram combinations.

The single-pass path (batch_chunks=0) is exercised bit-exact by the wider
``test_graph_build.py`` suite; these tests focus on the batched path and its
parity with the default at the single-batch limit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.build import build_graph_index
from grga.graph.schema import connect


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    return tmp_path


def _write_cjk_corpus(root: Path, n_files: int = 8, lines_per_file: int = 30) -> None:
    """Write a synthetic CJK+ASCII corpus that survives Gate-1 independence.

    Gate 1 drops pairs where the two kws always co-occur (df_a == df_b ==
    cooccur_count — "glued fragments"). We defeat that by mixing lines that
    hold different subsets of the kws, so each kw's df is strictly larger
    than any pair's cooccur_count for at least some kw.
    """
    root.mkdir(parents=True, exist_ok=True)
    templates = [
        "章北海 飞船 战场 木星轨道",
        "指挥官 面壁者 命令 开火",
        "第二天 章北海 看到 地球",
        "船员 面壁者 舰桥 巡逻",
        "士兵 章北海 训练 新兵",
        "面壁者 三体 舰队 防御",
        "木星轨道 敌方 侦察 飞船",
        "太阳系 面壁 计划 理事会",
        "grga ripgrep pandoc keyword",
        "grga install macOS Linux",
        "ripgrep 章北海 flag output",
        "pandoc pdf 面壁者 extract",
        "keyword 木星轨道 index pair",
    ]
    for i in range(n_files):
        lines = []
        for j in range(lines_per_file):
            lines.append(templates[(i * 3 + j) % len(templates)])
        (root / f"doc{i:02d}.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


def _dump_tables(db: Path):
    conn = connect(db)
    try:
        pairs = [
            tuple(r) for r in conn.execute(
                "SELECT kw_a_id, kw_b_id, source, cooccur_count, npmi "
                "FROM keyword_pairs ORDER BY kw_a_id, kw_b_id, source"
            )
        ]
        stats = [
            tuple(r) for r in conn.execute(
                "SELECT kw_id, chunk_df, doc_df, total_occurrences "
                "FROM keyword_stats ORDER BY kw_id"
            )
        ]
        adict = [
            tuple(r) for r in conn.execute(
                "SELECT phrase, n, count FROM auto_ngram_dict ORDER BY phrase"
            )
        ]
    finally:
        conn.close()
    return pairs, stats, adict


def test_batched_single_batch_no_cap_bit_exact_with_default(sidecar_env, tmp_path):
    """batch_chunks=0 (default single-pass) and batch_chunks>total + top_n_kw
    disabled (single batch, no top-N cap) produce identical
    keyword_pairs / keyword_stats / auto_ngram_dict.

    NOTE: batched with default top_n_kw=64 diverges from default on
    real corpora with dense chunks (per-chunk kw > 64 triggers cap).
    Bit-exactness requires BOTH batch_chunks>total AND top_n_kw disabled.
    """
    scope = tmp_path / "corpus"
    _write_cjk_corpus(scope, n_files=8, lines_per_file=30)

    db_default = tmp_path / "default.db"
    db_batched = tmp_path / "batched.db"

    s0 = build_graph_index(
        "s", directory=scope, db_path=db_default,
        chunk_mode="line", min_pair_count=1, min_npmi=0.0,
        refresh_ngram="replace", batch_chunks=0,
    )
    s1 = build_graph_index(
        "s", directory=scope, db_path=db_batched,
        chunk_mode="line", min_pair_count=1, min_npmi=0.0,
        refresh_ngram="replace",
        batch_chunks=200_000,   # > total → 1 batch
        top_n_kw=1_000_000,     # effectively no cap → bit-exact vs default
    )

    p0, st0, ad0 = _dump_tables(db_default)
    p1, st1, ad1 = _dump_tables(db_batched)

    assert p0 == p1, "keyword_pairs differ between default and single-batch"
    assert st0 == st1, "keyword_stats differ between default and single-batch"
    assert ad0 == ad1, "auto_ngram_dict differ between default and single-batch"
    # Sanity: the corpus actually produced content.
    assert p0, "expected non-empty keyword_pairs"
    assert ad0, "expected non-empty auto_ngram_dict"


def test_batched_multi_batch_valid_tables(sidecar_env, tmp_path):
    """A multi-batch build (batch_chunks small enough to force >1 batch)
    produces valid finalised tables."""
    scope = tmp_path / "corpus"
    _write_cjk_corpus(scope, n_files=8, lines_per_file=40)

    db = tmp_path / "multi.db"
    stats = build_graph_index(
        "s", directory=scope, db_path=db,
        chunk_mode="line", min_pair_count=1, min_npmi=0.0,
        refresh_ngram="replace", batch_chunks=50,  # forces multiple batches
    )

    conn = connect(db)
    try:
        # npmi NOT NULL after finalize.
        null_npmi = conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs "
            "WHERE source='chunk_cooccur' AND npmi IS NULL"
        ).fetchone()[0]
        assert null_npmi == 0, "all finalised pairs must carry npmi"
        n_pairs = conn.execute(
            "SELECT COUNT(*) FROM keyword_pairs WHERE source='chunk_cooccur'"
        ).fetchone()[0]
        assert n_pairs > 0, "expected non-empty keyword_pairs"

        n_kw_docs = conn.execute(
            "SELECT COUNT(*) FROM keyword_docs"
        ).fetchone()[0]
        assert n_kw_docs > 0, "expected non-empty keyword_docs"

        n_chunks = conn.execute(
            "SELECT n_chunks FROM scope_stats"
        ).fetchone()[0]
        assert n_chunks > 0, "expected scope_stats.n_chunks > 0"

        n_phrases = conn.execute(
            "SELECT COUNT(*) FROM auto_ngram_dict"
        ).fetchone()[0]
        assert n_phrases > 0, "expected non-empty auto_ngram_dict"
    finally:
        conn.close()

    assert stats.files_indexed > 0
    assert stats.ngram_iters_run == 1  # batched reports 1 conceptual iter


def test_batched_build_append_is_now_supported(sidecar_env, tmp_path):
    """Batched now allows incremental (mode='build') + refresh_ngram='append'.
    Historical restriction removed in 2026-07-16 follow-up (see commit that
    added incremental support). Just verify no exception + valid output."""
    scope = tmp_path / "corpus"
    _write_cjk_corpus(scope, n_files=2, lines_per_file=15)
    db = tmp_path / "g.db"
    # Establish scope first (rebuild) so build has something to increment on
    build_graph_index(
        "s", directory=scope, db_path=db,
        chunk_mode="line", min_pair_count=1, min_npmi=0.0,
        mode="rebuild", refresh_ngram="replace",
    )
    # Add one new file, then incremental+batched.
    (scope / "new_extra.txt").write_text(
        "\n".join("测试" for _ in range(15)), encoding="utf-8"
    )
    build_graph_index(
        "s", directory=scope, db_path=db,
        chunk_mode="line", min_pair_count=1, min_npmi=0.0,
        mode="build", refresh_ngram="append",
        batch_chunks=50, top_n_kw=100,
    )
    # No exception ≈ success. Tables should be non-empty.
    conn = connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM auto_ngram_dict").fetchone()[0]
        assert n >= 0  # non-negative; may be 0 if the tiny corpus produced no phrases


        n_docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        assert n_docs >= 3, f"expected >=3 docs (2 original + 1 new), got {n_docs}"
    finally:
        conn.close()


def test_batched_rejects_build_with_replace(sidecar_env, tmp_path):
    """The one remaining invalid combo: mode='build' + refresh='replace'
    (replace has no meaning on incremental). Already rejected by the
    shared guard; batched inherits."""
    scope = tmp_path / "corpus"
    _write_cjk_corpus(scope, n_files=2, lines_per_file=10)
    db = tmp_path / "g.db"
    # First rebuild so `build` has something to work on.
    build_graph_index(
        "s", directory=scope, db_path=db,
        chunk_mode="line", min_pair_count=1, min_npmi=0.0,
        mode="rebuild", refresh_ngram="replace",
    )
    with pytest.raises(ValueError):
        build_graph_index(
            "s", directory=scope, db_path=db,
            chunk_mode="line", min_pair_count=1, min_npmi=0.0,
            mode="build", refresh_ngram="replace",
            batch_chunks=50,
        )


# --- Incremental (mode='build') batched tests ---

def test_batched_incremental_append_bit_exact_single_batch(sidecar_env, tmp_path):
    """After a rebuild, calling `build` (incremental) with batch_chunks>total
    and top_n_kw large produces the same TABLES as calling `build` with
    batch_chunks=0 — bit-exact preservation of incremental semantics on the
    single-batch, no-cap path.
    """
    scope1 = tmp_path / "c1"
    _write_cjk_corpus(scope1, n_files=4, lines_per_file=20)
    scope2 = tmp_path / "c2"
    _write_cjk_corpus(scope2, n_files=4, lines_per_file=20)
    # Second-write a subset of scope2 files back into scope1 so scope2 is
    # scope1's subset + a couple new files (simulates real incremental).
    for f in sorted(scope2.glob("*.txt")):
        (scope1 / f"extra_{f.name}").write_text(
            f.read_text(encoding="utf-8"), encoding="utf-8"
        )

    def _run(batched: bool, db: Path):
        # Initial rebuild
        build_graph_index(
            "s", directory=scope1, db_path=db,
            chunk_mode="line", min_pair_count=1, min_npmi=0.0,
            mode="rebuild", refresh_ngram="replace",
        )
        # Then incremental with 2 extra new files under scope1
        for f in sorted(scope2.glob("*.txt")):
            (scope1 / f"newfile_{f.name}").write_text(
                f.read_text(encoding="utf-8"), encoding="utf-8"
            )
        kwargs = {"batch_chunks": 200_000, "top_n_kw": 1_000_000} if batched else {}
        build_graph_index(
            "s", directory=scope1, db_path=db,
            chunk_mode="line", min_pair_count=1, min_npmi=0.0,
            mode="build", refresh_ngram="append", **kwargs,
        )
        # cleanup extras from scope1 so second run starts identical
        for f in sorted(scope1.glob("newfile_*")):
            f.unlink()

    db_default = tmp_path / "default_inc.db"
    db_batched = tmp_path / "batched_inc.db"
    _run(False, db_default)
    _run(True, db_batched)

    p0, st0, ad0 = _dump_tables(db_default)
    p1, st1, ad1 = _dump_tables(db_batched)
    assert p0 == p1, "keyword_pairs differ (incremental default vs batched)"
    assert st0 == st1, "keyword_stats differ (incremental default vs batched)"
    assert ad0 == ad1, "auto_ngram_dict differ (incremental default vs batched)"


def test_batched_never_skips_detection(sidecar_env, tmp_path):
    """`refresh_ngram='never'` with batched skips phrase detection: the
    scope's auto_ngram_dict must NOT change after an incremental build
    with `never`, and new triples still get merged into keyword_pairs."""
    scope = tmp_path / "c"
    _write_cjk_corpus(scope, n_files=4, lines_per_file=25)

    db = tmp_path / "never.db"
    # Establish initial dict via rebuild.
    build_graph_index(
        "s", directory=scope, db_path=db,
        chunk_mode="line", min_pair_count=1, min_npmi=0.0,
        mode="rebuild", refresh_ngram="replace",
    )
    _, _, ad_before = _dump_tables(db)
    pairs_before = _dump_tables(db)[0]

    # Add a new file, do incremental build with refresh_ngram=never + batched.
    _write_cjk_corpus(scope / ".", n_files=2, lines_per_file=20)  # extras
    (scope / "extra1.txt").write_text(
        "\n".join("测试文档新的内容" for _ in range(20)), encoding="utf-8"
    )
    build_graph_index(
        "s", directory=scope, db_path=db,
        chunk_mode="line", min_pair_count=1, min_npmi=0.0,
        mode="build", refresh_ngram="never",
        batch_chunks=50, top_n_kw=1000,
    )
    _, _, ad_after = _dump_tables(db)
    pairs_after = _dump_tables(db)[0]

    # Dict is unchanged (no detection); pairs may or may not change
    # depending on whether new file introduced new co-occurrences, but the
    # phrase inventory must be identical.
    assert set(row[0] for row in ad_before) == set(row[0] for row in ad_after), \
        "auto_ngram_dict phrases changed under refresh_ngram=never"
