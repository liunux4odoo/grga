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


def test_batched_rejects_build_mode(sidecar_env, tmp_path):
    scope = tmp_path / "corpus"
    _write_cjk_corpus(scope, n_files=2)
    db = tmp_path / "g.db"
    with pytest.raises(ValueError, match="requires mode='rebuild'"):
        build_graph_index(
            "s", directory=scope, db_path=db,
            mode="build", refresh_ngram="append", batch_chunks=100,
        )


def test_batched_rejects_non_replace_refresh(sidecar_env, tmp_path):
    scope = tmp_path / "corpus"
    _write_cjk_corpus(scope, n_files=2)
    db = tmp_path / "g.db"
    with pytest.raises(ValueError, match="requires refresh_ngram='replace'"):
        build_graph_index(
            "s", directory=scope, db_path=db,
            mode="rebuild", refresh_ngram="append", batch_chunks=100,
        )
