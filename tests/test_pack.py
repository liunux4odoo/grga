"""Tests for :mod:`grga.graph.pack` + ``grga index export/import`` CLI.

Covers the plan-2026-07-14 §5 knowledge pack contract:

* Export includes the seven knowledge tables and the ``grga_pack.*``
  meta namespace; evidence tables stay empty in the pack.
* Round-trip: export scope-A → import as scope-A' into a fresh DB →
  keyword_pairs / stats / votes match row-for-row after id remap.
* Multi-scope (``--all``) export copies every scope in one pass.
* Import rejects: missing pack, format mismatch, pack with ≠ 1 scope,
  target-scope collision, ``--merge-into`` (not implemented).
* CLI: end-to-end via :class:`CliRunner`.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga.cli import app
from grga.graph.build import build_graph_index
from grga.graph.pack import (
    PACK_FORMAT,
    PACK_FORMAT_VERSION,
    export_knowledge_pack,
    import_knowledge_pack,
)
from grga.graph.schema import SCHEMA_VERSION, connect, ensure_schema
from grga.graph.scope import list_scopes
from grga.graph.votes import record_vote


runner = CliRunner()


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    return tmp_path


def _write_corpus(root: Path) -> None:
    """Corpus with real co-occurrence variation (survives Gate 1)."""
    root.mkdir(parents=True, exist_ok=True)
    lines = [f"alpha beta shared line {i}" for i in range(8)]
    lines += [f"beta gamma other line {i}" for i in range(3)]
    lines.append("alpha gamma rare pairing")
    (root / "doc.txt").write_text("\n".join(lines), encoding="utf-8")


def _built_scope(tmp_path: Path, scope: str = "physics-cn") -> Path:
    """Build one scope + return the source DB path."""
    corpus = tmp_path / f"{scope}-corpus"
    _write_corpus(corpus)
    db = tmp_path / "source.db"
    build_graph_index(
        scope, directory=corpus, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    return db


def _pair_snapshot(conn: sqlite3.Connection, scope_name: str) -> set:
    """Return ``{(kw_a, kw_b, source, cooccur, npmi)}`` for one scope."""
    rows = conn.execute(
        """
        SELECT ka.kw AS a, kb.kw AS b, p.source AS src,
               p.cooccur_count AS co, p.npmi AS npmi
        FROM keyword_pairs p
        JOIN scopes s ON s.id = p.scope_id
        JOIN keywords ka ON ka.id = p.kw_a_id
        JOIN keywords kb ON kb.id = p.kw_b_id
        WHERE s.name = ?
        """,
        (scope_name,),
    ).fetchall()
    return {
        (str(r["a"]), str(r["b"]), str(r["src"]),
         float(r["co"]),
         (None if r["npmi"] is None else round(float(r["npmi"]), 6)))
        for r in rows
    }


# ---------------------------------------------------------------------------
# Python API — export
# ---------------------------------------------------------------------------


def test_export_single_scope_writes_pack(sidecar_env, tmp_path: Path):
    db = _built_scope(tmp_path)
    out = tmp_path / "physics.grgapkg"
    stats = export_knowledge_pack(
        out_path=out, scope="physics-cn", db_path=db,
    )
    assert stats.scopes_included == ["physics-cn"]
    assert stats.n_pairs >= 1
    assert stats.n_keywords >= 2
    assert out.exists()


def test_export_writes_pack_meta_namespace(sidecar_env, tmp_path: Path):
    db = _built_scope(tmp_path)
    out = tmp_path / "physics.grgapkg"
    export_knowledge_pack(out_path=out, scope="physics-cn", db_path=db)
    pack = connect(out)
    try:
        meta = dict(pack.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'grga_pack.%'"
        ).fetchall())
        assert meta["grga_pack.format"] == PACK_FORMAT
        assert meta["grga_pack.format_version"] == PACK_FORMAT_VERSION
        assert meta["grga_pack.schema_version"] == str(SCHEMA_VERSION)
        assert "physics-cn" in meta["grga_pack.scopes"]
        assert meta["grga_pack.exported_at"]
    finally:
        pack.close()


def test_export_evidence_tables_are_empty(sidecar_env, tmp_path: Path):
    """Knowledge / evidence boundary: evidence tables stay empty in pack."""
    db = _built_scope(tmp_path)
    out = tmp_path / "pack.grgapkg"
    export_knowledge_pack(out_path=out, scope="physics-cn", db_path=db)
    pack = connect(out)
    try:
        for tbl in ("scope_directories", "docs", "keyword_docs",
                    "heading_docs", "keyword_sources", "keyword_counts"):
            n = pack.execute(
                f"SELECT COUNT(*) FROM {tbl}"
            ).fetchone()[0]
            assert n == 0, f"evidence table {tbl} leaked {n} row(s)"
    finally:
        pack.close()


def test_export_all_captures_every_scope(sidecar_env, tmp_path: Path):
    corpus_a = tmp_path / "a"; _write_corpus(corpus_a)
    corpus_b = tmp_path / "b"; _write_corpus(corpus_b)
    db = tmp_path / "source.db"
    build_graph_index(
        "scope-a", directory=corpus_a, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    build_graph_index(
        "scope-b", directory=corpus_b, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    out = tmp_path / "all.grgapkg"
    stats = export_knowledge_pack(out_path=out, scope=None, db_path=db)
    assert set(stats.scopes_included) == {"scope-a", "scope-b"}


def test_export_unknown_scope_raises(sidecar_env, tmp_path: Path):
    db = _built_scope(tmp_path)
    with pytest.raises(LookupError):
        export_knowledge_pack(
            out_path=tmp_path / "x.grgapkg", scope="nope", db_path=db,
        )


def test_export_empty_db_raises(tmp_path: Path):
    db = tmp_path / "empty.db"
    with pytest.raises(ValueError):
        export_knowledge_pack(out_path=tmp_path / "x.grgapkg", db_path=db)


def test_export_carries_votes_and_promoted_at(sidecar_env, tmp_path: Path):
    """Design D12: votes + llm_promoted travel together (promoted_at intact)."""
    db = _built_scope(tmp_path)
    record_vote("beta", "gamma", scope="physics-cn", db_path=db)
    out = tmp_path / "with-votes.grgapkg"
    stats = export_knowledge_pack(
        out_path=out, scope="physics-cn", db_path=db,
    )
    assert stats.n_votes >= 1
    pack = connect(out)
    try:
        # promoted_at column exists (nullable — untouched by a plain vote).
        row = pack.execute(
            "SELECT vote_count, promoted_at FROM llm_extension_votes"
        ).fetchone()
        assert row is not None
    finally:
        pack.close()


# ---------------------------------------------------------------------------
# Python API — round-trip (export → import)
# ---------------------------------------------------------------------------


def test_round_trip_preserves_pair_rows(sidecar_env, tmp_path: Path):
    """Export then import into a fresh DB — pair rows match after remap."""
    src_db = _built_scope(tmp_path, "physics-cn")
    out = tmp_path / "pack.grgapkg"
    export_knowledge_pack(out_path=out, scope="physics-cn", db_path=src_db)

    dst_db = tmp_path / "dst.db"
    stats = import_knowledge_pack(
        out, as_scope="physics-en", db_path=dst_db,
    )
    assert stats.into_scope == "physics-en"
    assert stats.n_pairs >= 1
    assert stats.scope_stats_written is True

    src_conn = connect(src_db)
    dst_conn = connect(dst_db)
    try:
        src_pairs = _pair_snapshot(src_conn, "physics-cn")
        dst_pairs = _pair_snapshot(dst_conn, "physics-en")
        # Every source pair must appear in destination (identical
        # cooccur/npmi/source; scope name is the caller-chosen new label).
        assert src_pairs == dst_pairs
    finally:
        src_conn.close()
        dst_conn.close()


def test_round_trip_scope_stats_transferred(sidecar_env, tmp_path: Path):
    src_db = _built_scope(tmp_path)
    out = tmp_path / "pack.grgapkg"
    export_knowledge_pack(out_path=out, scope="physics-cn", db_path=src_db)
    dst_db = tmp_path / "dst.db"
    import_knowledge_pack(out, as_scope="physics-en", db_path=dst_db)

    src_conn = connect(src_db)
    dst_conn = connect(dst_db)
    try:
        src_n = src_conn.execute(
            "SELECT n_chunks FROM scope_stats s JOIN scopes sc "
            "ON sc.id = s.scope_id WHERE sc.name = 'physics-cn'"
        ).fetchone()[0]
        dst_n = dst_conn.execute(
            "SELECT n_chunks FROM scope_stats s JOIN scopes sc "
            "ON sc.id = s.scope_id WHERE sc.name = 'physics-en'"
        ).fetchone()[0]
        assert src_n == dst_n
    finally:
        src_conn.close()
        dst_conn.close()


def test_round_trip_evidence_stays_empty_on_target(sidecar_env, tmp_path: Path):
    """The target scope is knowledge-only until a local build runs."""
    src_db = _built_scope(tmp_path)
    out = tmp_path / "pack.grgapkg"
    export_knowledge_pack(out_path=out, scope="physics-cn", db_path=src_db)
    dst_db = tmp_path / "dst.db"
    import_knowledge_pack(out, as_scope="physics-en", db_path=dst_db)
    scopes = list_scopes(db_path=dst_db)
    physics = next(s for s in scopes if s.name == "physics-en")
    assert physics.n_pairs > 0
    assert physics.n_docs == 0
    assert physics.directories == []


def test_round_trip_shared_keyword_vocabulary(sidecar_env, tmp_path: Path):
    """Two imports of the same pack (under different names) share keywords."""
    src_db = _built_scope(tmp_path)
    out = tmp_path / "pack.grgapkg"
    export_knowledge_pack(out_path=out, scope="physics-cn", db_path=src_db)
    dst_db = tmp_path / "dst.db"
    first = import_knowledge_pack(out, as_scope="a", db_path=dst_db)
    second = import_knowledge_pack(out, as_scope="b", db_path=dst_db)
    # First import creates the vocab; second reuses it.
    assert first.n_keywords_new >= 1
    assert second.n_keywords_new == 0


# ---------------------------------------------------------------------------
# Python API — import error paths
# ---------------------------------------------------------------------------


def test_import_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        import_knowledge_pack(
            tmp_path / "nope.grgapkg",
            as_scope="x",
            db_path=tmp_path / "dst.db",
        )


def test_import_rejects_non_pack_file(tmp_path: Path):
    """A plain SQLite file without ``grga_pack.format`` is rejected."""
    bogus = tmp_path / "bogus.db"
    conn = connect(bogus)
    try:
        ensure_schema(conn)  # v10 shape but no pack namespace
    finally:
        conn.close()
    with pytest.raises(ValueError, match="not a grga knowledge pack"):
        import_knowledge_pack(bogus, as_scope="x", db_path=tmp_path / "dst.db")


def test_import_target_collision(sidecar_env, tmp_path: Path):
    """`--as <name>` colliding with an existing scope raises."""
    src_db = _built_scope(tmp_path)
    out = tmp_path / "pack.grgapkg"
    export_knowledge_pack(out_path=out, scope="physics-cn", db_path=src_db)
    # First import succeeds.
    import_knowledge_pack(out, as_scope="taken", db_path=src_db)
    # Second import into the same target must reject.
    with pytest.raises(ValueError, match="already has scope"):
        import_knowledge_pack(out, as_scope="taken", db_path=src_db)


def test_import_merge_into_not_implemented(sidecar_env, tmp_path: Path):
    src_db = _built_scope(tmp_path)
    out = tmp_path / "pack.grgapkg"
    export_knowledge_pack(out_path=out, scope="physics-cn", db_path=src_db)
    with pytest.raises(NotImplementedError, match="npmi merge policy"):
        import_knowledge_pack(
            out, as_scope=None,
            merge_into="physics-cn",
            db_path=tmp_path / "dst.db",
        )


def test_import_requires_as_scope(sidecar_env, tmp_path: Path):
    src_db = _built_scope(tmp_path)
    out = tmp_path / "pack.grgapkg"
    export_knowledge_pack(out_path=out, scope="physics-cn", db_path=src_db)
    with pytest.raises(ValueError, match="required"):
        import_knowledge_pack(out, as_scope=None, db_path=tmp_path / "dst.db")


def test_import_rejects_multi_scope_pack(sidecar_env, tmp_path: Path):
    """`--all` produces a multi-scope pack; single-scope import rejects it."""
    corpus_a = tmp_path / "a"; _write_corpus(corpus_a)
    corpus_b = tmp_path / "b"; _write_corpus(corpus_b)
    db = tmp_path / "source.db"
    build_graph_index(
        "scope-a", directory=corpus_a, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    build_graph_index(
        "scope-b", directory=corpus_b, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    out = tmp_path / "multi.grgapkg"
    export_knowledge_pack(out_path=out, scope=None, db_path=db)
    with pytest.raises(ValueError, match="exactly 1 scope"):
        import_knowledge_pack(
            out, as_scope="new", db_path=tmp_path / "dst.db",
        )


# ---------------------------------------------------------------------------
# CLI — end-to-end
# ---------------------------------------------------------------------------


def test_cli_export_json(sidecar_env, tmp_path: Path):
    db = _built_scope(tmp_path)
    out = tmp_path / "physics.grgapkg"
    result = runner.invoke(app, [
        "index", "export",
        "--scope", "physics-cn",
        "--out", str(out),
        "--db", str(db),
        "--json",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scopes_included"] == ["physics-cn"]
    assert payload["n_pairs"] > 0
    assert out.exists()


def test_cli_export_requires_scope_or_all(sidecar_env, tmp_path: Path):
    db = _built_scope(tmp_path)
    result = runner.invoke(app, [
        "index", "export",
        "--out", str(tmp_path / "x.grgapkg"),
        "--db", str(db),
    ])
    assert result.exit_code == 2


def test_cli_import(sidecar_env, tmp_path: Path):
    src_db = _built_scope(tmp_path)
    out = tmp_path / "pack.grgapkg"
    export_knowledge_pack(out_path=out, scope="physics-cn", db_path=src_db)
    dst_db = tmp_path / "dst.db"
    result = runner.invoke(app, [
        "index", "import", str(out),
        "--as", "physics-en",
        "--db", str(dst_db),
        "--json",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["into_scope"] == "physics-en"
    assert payload["n_pairs"] > 0


# ---------------------------------------------------------------------------
# --seed-ngram-from (plan-2026-07-14 §6)
# ---------------------------------------------------------------------------


def test_seed_ngram_from_cold_starts_vocabulary(sidecar_env, tmp_path: Path):
    """After import, `--seed-ngram-from` borrows a sibling scope's phrases.

    Setup: seed a source scope's auto_ngram_dict directly (detection on
    tiny synthetic corpora is unreliable), then build a NEW local scope
    on a fresh corpus with --seed-ngram-from pointing at it. The build
    must load the seed phrases and emit a ``seed_ngram_loaded`` event.
    """
    from grga.graph.build import build_graph_index
    from grga.graph.schema import connect
    from grga.graph.ids import IdInterner

    # Build a real source scope, then inject an auto_ngram_dict phrase so
    # the test is deterministic regardless of the detector's behaviour on
    # small corpora.
    src_corpus = tmp_path / "src"
    _write_corpus(src_corpus)
    src_db = tmp_path / "src.db"
    build_graph_index(
        "ml-src", directory=src_corpus, db_path=src_db,
        min_pair_count=1, min_npmi=0.0, refresh_ngram="never",
    )
    conn = connect(src_db)
    try:
        scope_id = IdInterner(conn).scope_id_ro("ml-src")
        conn.execute(
            "INSERT OR REPLACE INTO auto_ngram_dict "
            "(scope_id, phrase, n, count, npmi, branch_ent, updated_at) "
            "VALUES (?, '深度学习', 2, 10, 0.8, 1.2, datetime('now'))",
            (scope_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # Build a *second* scope on a fresh corpus, seeding from ml-src.
    tgt_corpus = tmp_path / "tgt"
    _write_corpus(tgt_corpus)
    events = []
    build_graph_index(
        "ml-tgt", directory=tgt_corpus, db_path=src_db,
        min_pair_count=1, min_npmi=0.0, refresh_ngram="never",
        seed_ngram_from="ml-src",
        on_event=lambda e: events.append(e),
    )
    seed_events = [e for e in events if e.get("phase") == "seed_ngram_loaded"]
    assert seed_events, "expected a seed_ngram_loaded event"
    assert seed_events[0]["from_scope"] == "ml-src"
    assert seed_events[0]["phrases_seeded"] >= 1

    # The seeded phrase is now persisted under the target scope too.
    conn = connect(src_db)
    try:
        tgt_scope_id = IdInterner(conn).scope_id_ro("ml-tgt")
        n = conn.execute(
            "SELECT COUNT(*) FROM auto_ngram_dict "
            "WHERE scope_id = ? AND phrase = '深度学习'",
            (tgt_scope_id,),
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_seed_ngram_from_missing_scope_is_noop(sidecar_env, tmp_path: Path):
    """A missing seed scope silently falls back to normal detection."""
    from grga.graph.build import build_graph_index

    corpus = tmp_path / "c"
    _write_corpus(corpus)
    db = tmp_path / "graph.db"
    events = []
    # Should not raise; just no seed_ngram_loaded event.
    build_graph_index(
        "scope-x", directory=corpus, db_path=db,
        min_pair_count=1, min_npmi=0.0,
        seed_ngram_from="does-not-exist",
        on_event=lambda e: events.append(e),
    )
    assert not [e for e in events if e.get("phase") == "seed_ngram_loaded"]
