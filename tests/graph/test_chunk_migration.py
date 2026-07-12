"""Chunk-level migration boundary tests (plan §6, 15 rows).

Every row of the "8 维边界必答清单" in
``docs/design/plan-2026-07-11-chunk-migration.md`` maps to a test here.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grga.graph.build import build_graph_index, split_chunks
from grga.graph.schema import (
    LLM_PROMOTED_SOURCE,
    SCHEMA_VERSION,
    connect,
    ensure_schema,
    read_schema_version,
)
from grga.related import related, related_graph_with_preview


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))


def _index(scope: Path, db: Path, *, mode: str = "line", min_pair_count: int = 1) -> None:
    # ``min_npmi=0.0`` disables the v8 write-time quality gate — these
    # chunk-migration tests use tiny corpora that legitimately cannot
    # clear a 0.2 npmi floor.
    build_graph_index(
        scope, db_path=db, chunk_mode=mode,
        min_pair_count=min_pair_count, min_npmi=0.0,
    )


# ---------------------------------------------------------------------------
# 空集 · scope with no chunks
# ---------------------------------------------------------------------------


def test_empty_scope_returns_empty_related(tmp_path):
    scope = tmp_path / "empty_scope"
    scope.mkdir()
    db = tmp_path / "graph.db"
    _index(scope, db)
    results = related(["anything"], mode="graph", db_path=db, directory=scope)
    assert results == []


# 空集 · seed absent from graph
def test_unknown_seed_returns_empty(tmp_path):
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "a.md").write_text("alpha alpha beta beta\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    _index(scope, db)
    results = related(["nonexistent-token"], mode="graph", db_path=db, directory=scope)
    assert results == []


# ---------------------------------------------------------------------------
# 单元素 · scope with exactly one chunk
# ---------------------------------------------------------------------------


def test_single_chunk_scope_graph_mode(tmp_path):
    scope = tmp_path / "single_chunk"
    scope.mkdir()
    (scope / "a.md").write_text("alpha beta gamma", encoding="utf-8")
    db = tmp_path / "graph.db"
    _index(scope, db)
    results = related(["alpha"], mode="graph", db_path=db, directory=scope)
    # Direct-seed fallback or PPR: alpha's file surfaces.
    assert any(r.doc_path == "a.md" for r in results)


# 单元素 · single keyword seed
def test_single_keyword_seed(tmp_path):
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "a.md").write_text("alpha alpha beta beta\nalpha gamma\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    _index(scope, db)
    results = related(["alpha"], mode="graph", db_path=db, directory=scope)
    assert results, "single seed must produce results"


# ---------------------------------------------------------------------------
# 唯一性 · cross-scope uniqueness
# ---------------------------------------------------------------------------


def test_shared_keyword_across_scopes_does_not_collapse(tmp_path):
    scope_a = tmp_path / "scope_a"
    scope_b = tmp_path / "scope_b"
    scope_a.mkdir(); scope_b.mkdir()
    (scope_a / "a.md").write_text("韩立 韩立 修士 修士\n", encoding="utf-8")
    (scope_b / "b.md").write_text("韩立 韩立 三体 三体\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    _index(scope_a, db)
    _index(scope_b, db)
    payload = related_graph_with_preview(
        ["韩立"], db_path=db, directory=scope_a, cross_scope=True, limit=20,
    )
    dirs = {r.directory for r in payload["results"]}
    assert str(scope_a.resolve()) in dirs
    assert str(scope_b.resolve()) in dirs


# ---------------------------------------------------------------------------
# 冲突 · same-name keyword with different df distribution
# ---------------------------------------------------------------------------


def test_seed_chunk_df_reported_for_current_scope(tmp_path):
    scope_a = tmp_path / "a"; scope_b = tmp_path / "b"
    scope_a.mkdir(); scope_b.mkdir()
    (scope_a / "one.md").write_text(
        "韩立 韩立\n韩立 韩立\n韩立 韩立\n韩立 韩立\n", encoding="utf-8"
    )  # many chunks with 韩立
    (scope_b / "one.md").write_text(
        "韩立 韩立\n", encoding="utf-8"
    )  # single chunk
    db = tmp_path / "graph.db"
    _index(scope_a, db)
    _index(scope_b, db)

    # v2.1: chunk_df now lives on expansion.seeds[].chunk_df, not per-doc.
    payload = related_graph_with_preview(["韩立"], db_path=db, directory=scope_a, limit=20)
    exp = payload["expansion"]
    seed_entries = {s["kw"]: s for s in exp["seeds"]}
    assert "韩立" in seed_entries
    # Scope-local df should reflect scope A\'s count, not the union.
    assert seed_entries["韩立"]["chunk_df"] >= 4


# ---------------------------------------------------------------------------
# 重叠 · seeds in high-freq vs rare set
# ---------------------------------------------------------------------------


def test_high_and_rare_seeds_both_converge(tmp_path):
    scope = tmp_path / "corpus"
    scope.mkdir()
    lines = []
    for _ in range(20):
        lines.append("hub hub common word here")
    lines.append("rareseed hub something")
    (scope / "a.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    _index(scope, db)

    # Hub seed
    r_hub = related(["hub"], mode="graph", db_path=db, directory=scope)
    assert r_hub  # never NaN/hang, returns something

    # Rare seed
    r_rare = related(["rareseed"], mode="graph", db_path=db, directory=scope)
    assert r_rare  # rare seed also produces results


# ---------------------------------------------------------------------------
# 自反 · neighbor_edges never include the seed itself
# ---------------------------------------------------------------------------


def test_neighbor_edges_exclude_seed_self(tmp_path):
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "a.md").write_text(
        "seed seed neighbor other\nseed foo bar\nseed baz qux\n", encoding="utf-8"
    )
    db = tmp_path / "graph.db"
    _index(scope, db)
    # v2.2: expanded_keywords excludes seeds by construction (see
    # _build_expansion). The self-loop guard is now: no expanded entry\'s
    # kw is itself a seed, and (for the multi-seed case) any via_seeds
    # entries must be actual seeds, never the neighbour\'s own kw.
    payload = related_graph_with_preview(["seed"], db_path=db, directory=scope, limit=20)
    exp = payload["expansion"]
    seed_kws = {s["kw"] for s in exp["seeds"]}
    for entry in exp["expanded_keywords"]:
        assert entry["kw"] not in seed_kws, entry
        for via in entry.get("via_seeds", []):
            assert via in seed_kws, via
            assert via != entry["kw"], via


# ---------------------------------------------------------------------------
# 可结合性 · --chunk-mode chars + --cross-scope compose
# ---------------------------------------------------------------------------


def test_chunk_mode_chars_plus_cross_scope(tmp_path):
    scope_a = tmp_path / "a"; scope_b = tmp_path / "b"
    scope_a.mkdir(); scope_b.mkdir()
    (scope_a / "a.md").write_text(
        "# Title\n\n" + "alpha bridge word content here.\n" * 10, encoding="utf-8"
    )
    (scope_b / "b.md").write_text(
        "# Title\n\n" + "bridge betaword info blob text.\n" * 10, encoding="utf-8"
    )
    db = tmp_path / "graph.db"
    _index(scope_a, db, mode="chars")
    _index(scope_b, db, mode="chars")
    results = related(
        ["alpha"], mode="graph", db_path=db, directory=scope_a, cross_scope=True, limit=20,
        # Synthetic mini-corpus: npmi is tiny across the board, so pin the
        # threshold off to isolate cross-scope mechanics from the P1.2
        # default (0.2) which would drop every bridge edge here.
        min_npmi=0.0,
    )
    dirs = {r.directory for r in results}
    assert str(scope_b.resolve()) in dirs


# ---------------------------------------------------------------------------
# 降级路径 · minified 100KB single line
# ---------------------------------------------------------------------------


def test_chars_mode_single_huge_line_becomes_one_chunk():
    text = "x" * 100_000
    chunks = split_chunks(text, mode="chars", min_chars=300, max_chars=600)
    assert len(chunks) == 1
    assert chunks[0].char_count == 100_000


# 降级路径 · Markdown heading + tiny paragraph
def test_chars_mode_short_heading_paragraph_not_merged():
    text = "# A\nx\n## B\ny\n"
    chunks = split_chunks(text, mode="chars", is_markdown=True, min_chars=100, max_chars=500)
    # heading forces cut even below min_chars
    heads = [c.text.splitlines()[0] for c in chunks]
    assert any(h.startswith("# A") for h in heads)
    assert any(h.startswith("## B") for h in heads)


# ---------------------------------------------------------------------------
# hub / domain / rare seed scenarios
# ---------------------------------------------------------------------------


def test_hub_seed_no_special_field(tmp_path):
    """Contract: no is_hub / confidence / warning / recommend_action fields."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "a.md").write_text("hub " * 200 + "\n" + "hub other\n" * 5, encoding="utf-8")
    db = tmp_path / "graph.db"
    _index(scope, db)
    payload = related_graph_with_preview(["hub"], db_path=db, directory=scope, limit=20)
    for r in payload["results"]:
        d = r.to_dict()
        for banned in ("is_hub", "confidence", "quality", "warning", "recommend_action"):
            assert banned not in d


def test_domain_seed_returns_normal_neighbors(tmp_path):
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "a.md").write_text(
        "domain word\n" * 3 + "domain level rank\n" * 3, encoding="utf-8"
    )
    db = tmp_path / "graph.db"
    _index(scope, db)
    results = related(["domain"], mode="graph", db_path=db, directory=scope)
    assert results


def test_rare_seed_no_warning_field(tmp_path):
    scope = tmp_path / "corpus"
    scope.mkdir()
    lines = ["common word here"] * 30 + ["rareword mention once"]
    (scope / "a.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    _index(scope, db)
    payload = related_graph_with_preview(["rareword"], db_path=db, directory=scope, limit=20)
    for r in payload["results"]:
        assert "warning" not in r.to_dict()


# ---------------------------------------------------------------------------
# schema migration · atomicity + idempotency
# ---------------------------------------------------------------------------


def test_v3_migration_atomic_transaction(tmp_path):
    """A v3 database migrates in one shot: single ensure_schema call brings
    it to the current schema with keyword_docs dropped and llm_promoted rows
    salvaged (v3 → v4 → v5 chain, one atomic transaction)."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE keyword_docs (
            directory TEXT, keyword TEXT, doc_path TEXT, occurrence_count REAL,
            first_line INTEGER, updated_at TEXT, scope TEXT DEFAULT '',
            PRIMARY KEY (directory, keyword, doc_path)
        );
        CREATE TABLE keyword_pairs (
            scope TEXT NOT NULL, directory TEXT NOT NULL,
            keyword_a TEXT NOT NULL, keyword_b TEXT NOT NULL,
            count REAL NOT NULL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (scope, directory, keyword_a, keyword_b)
        );
        """
    )
    conn.execute(
        "INSERT INTO keyword_docs (directory, keyword, doc_path, occurrence_count, first_line) "
        "VALUES ('/d', 'alpha', 'a.md', 3, 1)"
    )
    conn.execute(
        "INSERT INTO keyword_pairs (scope, directory, keyword_a, keyword_b, count) "
        "VALUES ('llm_promoted', '/d', 'x', 'y', 5)"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit(); conn.close()

    c = connect(db)
    try:
        ensure_schema(c)
        assert read_schema_version(c) == SCHEMA_VERSION
        # Legacy keyword_docs gone
        assert c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='keyword_docs'"
        ).fetchone()[0] == 0
        # LLM-promoted rows salvaged
        rows = c.execute(
            "SELECT ka.kw AS keyword_a, kb.kw AS keyword_b, p.source AS source "
            "FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["source"] == LLM_PROMOTED_SOURCE
    finally:
        c.close()


def test_current_schema_migration_idempotent(tmp_path):
    db = tmp_path / "graph.db"
    c = connect(db)
    try:
        ensure_schema(c)
        first = c.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
        ensure_schema(c)  # no-op
        second = c.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
        assert [tuple(r) for r in first] == [tuple(r) for r in second]
    finally:
        c.close()


def test_llm_promote_flow_marks_source_correctly(tmp_path):
    """Promotion path writes source='llm_promoted' in the new schema."""
    from grga.graph.promote import promote_votes
    from grga.graph.votes import record_vote

    db = tmp_path / "graph.db"
    for _ in range(3):
        record_vote("alpha", "beta", db_path=db)
    stats = promote_votes(threshold=3, db_path=db)
    assert stats.promoted == 1

    c = connect(db)
    try:
        row = c.execute(
            "SELECT p.source AS source FROM keyword_pairs p "
            "JOIN keywords ka ON ka.id = p.kw_a_id "
            "JOIN keywords kb ON kb.id = p.kw_b_id "
            "WHERE ka.kw = 'alpha' AND kb.kw = 'beta'"
        ).fetchone()
        assert row is not None
        assert row["source"] == LLM_PROMOTED_SOURCE
    finally:
        c.close()


# ---------------------------------------------------------------------------
# graph_signals contract
# ---------------------------------------------------------------------------


def test_expansion_block_shape(tmp_path):
    """v2.2: query-level ``expansion`` replaces per-doc ``graph_signals``.

    Each expanded keyword folds ``ppr_score`` / ``chunk_df`` /
    ``hops_from_seed`` into one entry; hop-1 neighbours additionally
    inline the strongest direct edge as ``npmi`` / ``cooccur_count`` on
    the entry itself (the pre-v2.2 ``edges_to_seeds`` list wrapper was
    dropped as pure nesting overhead).
    """
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "a.md").write_text(
        "alpha beta gamma\nalpha delta\nalpha epsilon\n", encoding="utf-8"
    )
    db = tmp_path / "graph.db"
    _index(scope, db)
    payload = related_graph_with_preview(["alpha"], db_path=db, directory=scope, limit=5)
    for r in payload["results"]:
        d = r.to_dict()
        assert "graph_signals" not in d, "graph_signals removed in v2.1"
        assert "keyword_contributions" in d
    exp = payload["expansion"]
    assert "seeds" in exp and "expanded_keywords" in exp and "total_chunks_in_scope" in exp
    for kw in exp["expanded_keywords"]:
        assert "kw" in kw and "ppr_score" in kw
        assert "edges_to_seeds" not in kw, "v2.2: dropped list wrapper"
        # Inlined edge fields present iff a direct edge exists in
        # keyword_pairs (independent of hops_from_seed — the pairs
        # query ignores min_npmi by design). npmi/cooccur_count always
        # appear together.
        if "npmi" in kw or "cooccur_count" in kw:
            assert "npmi" in kw and "cooccur_count" in kw, (
                f"npmi and cooccur_count must appear together: {kw}"
            )
            assert isinstance(kw["cooccur_count"], int)


def test_expanded_keywords_bounded_by_ppr_top_k(tmp_path):
    """v2.1: the pre-2026-07-12 ``--graph-signals-limit`` knob is removed.

    ``expanded_keywords`` is naturally bounded by the PPR ``top_k``
    (``max(limit * 3, limit)``), so the deterministic cap emerges from
    ``related --limit`` without an extra knob. This test asserts the
    natural upper bound rather than restoring the removed parameter.
    """
    scope = tmp_path / "corpus"
    scope.mkdir()
    body_lines = [
        f"alpha kw{i} kw{i+1} kw{i+2}" for i in range(0, 30)
    ]
    (scope / "a.md").write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    _index(scope, db)
    payload = related_graph_with_preview(
        ["alpha"], db_path=db, directory=scope, limit=5,
    )
    assert payload["results"]
    # PPR requested top_k = max(limit*3, limit) = 15 non-seed keywords.
    assert len(payload["expansion"]["expanded_keywords"]) <= 15
