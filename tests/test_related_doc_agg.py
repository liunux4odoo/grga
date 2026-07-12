"""P2.3 doc-aggregation equivalence — id-native vs legacy text-IN.

Contract: :func:`grga.related._aggregate_docs_id_native` must produce
byte-identical output to :func:`grga.related._aggregate_docs_text_legacy`.
The rollback env var ``GRGA_DOC_AGG_LEGACY=1`` switches the dispatcher back
to the legacy path; end-to-end ``related --mode graph`` must produce the
same top-K documents in both modes.

See ``docs/design/plan-2026-07-12-related-perf.md`` §2.2 and
``experiments/related_perf_2026_07_12/FINDINGS.md`` §F1.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.build import build_graph_index
from grga.graph.schema import connect, ensure_schema
from grga.related import (
    _aggregate_docs_id_native,
    _aggregate_docs_text_legacy,
    related_graph_with_preview,
)


@pytest.fixture
def indexed_two_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    a = tmp_path / "scope_a"
    b = tmp_path / "scope_b"
    a.mkdir()
    b.mkdir()
    (a / "install.md").write_text(
        "# Install\n\ngrga alpha alpha bridgeword bridgeword deploy.\n"
        "grga alpha beta ripgrep\n",
        encoding="utf-8",
    )
    (a / "usage.md").write_text(
        "# Usage\n\nusage grga bridgeword bridgeword alpha.\n"
        "grga command command examples.\n",
        encoding="utf-8",
    )
    (b / "notes.md").write_text(
        "# Notes\n\nbridgeword bridgeword beta ripgrep pandoc alpha.\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    build_graph_index(a, db_path=db, min_pair_count=1)
    build_graph_index(b, db_path=db, min_pair_count=1)
    return {"a": a, "b": b, "db": db}


def _related_key(r):
    """Stable identity for a :class:`Related` record (drops score float)."""
    return (r.directory, r.doc_path, tuple(r.keywords), r.mode, r.reason)


def test_aggregate_docs_id_native_matches_legacy(indexed_two_scope):
    """Direct function-level: identical sorted output on synthetic kw_score."""
    conn = connect(indexed_two_scope["db"])
    try:
        ensure_schema(conn)
        # Manufacture a kw_score by pulling a handful of real keywords with
        # varied weights from the indexed corpus.
        rows = conn.execute(
            "SELECT DISTINCT k.kw FROM keyword_chunks kc "
            "JOIN keywords k ON k.id = kc.kw_id "
            "LIMIT 20"
        ).fetchall()
        assert rows, "test corpus produced no keywords"
        kw_score = {row["kw"]: (idx + 1) * 0.5 for idx, row in enumerate(rows)}

        legacy = _aggregate_docs_text_legacy(
            conn, kw_score, doc_scope_key=None
        )
        native = _aggregate_docs_id_native(
            conn, kw_score, doc_scope_key=None
        )
    finally:
        conn.close()

    # Same (directory, doc_path, keywords) set — order-independent because
    # neither function sorts internally.
    assert {_related_key(r) for r in legacy} == {_related_key(r) for r in native}, (
        "id-native and legacy diverge on the (directory, doc_path, keywords) set"
    )
    # Scores must match within float tolerance.
    legacy_scores = {(r.directory, r.doc_path): r.score for r in legacy}
    native_scores = {(r.directory, r.doc_path): r.score for r in native}
    assert legacy_scores.keys() == native_scores.keys()
    for k in legacy_scores:
        assert abs(legacy_scores[k] - native_scores[k]) < 1e-9, (
            f"score mismatch at {k}: legacy={legacy_scores[k]} native={native_scores[k]}"
        )


def test_scoped_aggregation_matches_legacy(indexed_two_scope):
    """Same equivalence with ``doc_scope_key`` filter engaged."""
    conn = connect(indexed_two_scope["db"])
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT k.kw FROM keyword_chunks kc "
            "JOIN keywords k ON k.id = kc.kw_id "
            "GROUP BY k.kw ORDER BY COUNT(*) DESC LIMIT 8"
        ).fetchall()
        kw_score = {row["kw"]: 1.0 for row in rows}
        scope_a = str(indexed_two_scope["a"])
        legacy = _aggregate_docs_text_legacy(
            conn, kw_score, doc_scope_key=scope_a
        )
        native = _aggregate_docs_id_native(
            conn, kw_score, doc_scope_key=scope_a
        )
    finally:
        conn.close()

    # No cross-scope leakage
    for r in native:
        assert r.directory == scope_a
    assert {_related_key(r) for r in legacy} == {_related_key(r) for r in native}


def test_empty_kw_score_returns_empty():
    """Both paths short-circuit on empty ``kw_score``."""
    # No conn needed — both should bail before any SQL.
    assert _aggregate_docs_id_native(None, {}, None) == []
    assert _aggregate_docs_text_legacy(None, {}, None) == []


def test_end_to_end_env_toggle_produces_same_top_docs(
    indexed_two_scope, monkeypatch
):
    """End-to-end related_graph_with_preview: GRGA_DOC_AGG_LEGACY toggle
    must not change top-K document identity."""
    seeds = ["grga"]
    monkeypatch.delenv("GRGA_DOC_AGG_LEGACY", raising=False)
    default_run = related_graph_with_preview(
        seeds,
        db_path=indexed_two_scope["db"],
        directory=indexed_two_scope["a"],
        limit=10,
    )
    monkeypatch.setenv("GRGA_DOC_AGG_LEGACY", "1")
    from grga.graph.adjacency_cache import clear_adjacency_cache

    clear_adjacency_cache()  # unrelated but keep cache honest across runs
    legacy_run = related_graph_with_preview(
        seeds,
        db_path=indexed_two_scope["db"],
        directory=indexed_two_scope["a"],
        limit=10,
    )
    default_docs = [(r.directory, r.doc_path) for r in default_run["results"]]
    legacy_docs = [(r.directory, r.doc_path) for r in legacy_run["results"]]
    assert default_docs == legacy_docs, (
        "P2.3 default and GRGA_DOC_AGG_LEGACY diverge on top-K doc order"
    )
