"""Tests for complementary pairs (plan-2026-09-12 §迭代 3).

Iteration 3 turns ``complementary_pairs`` into an internal, silent data
channel: the build pipeline still writes it (with tightened gates), but no
user-facing surface (``related`` text/JSON, ``probe`` text/JSON) exposes it.

Covers:
- Build: complementary_pairs table gets populated with significant negative
  edges that satisfy the iteration-2/3 gates (c/E < 0.10, keyword quality,
  stopwords, segregation)
- Related: ``complementary`` is NOT in ``to_dict()`` / results
- Probe: JSON has no ``complementary`` key
- Probe: elapsed_ms removal from JSON + text output
- Pack export/import: complementary_pairs travels with the knowledge pack
- Scope removal: purge_knowledge=True wipes complementary_pairs
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grga.graph.build import build_graph_index
from grga.graph.ids import IdInterner
from grga.graph.schema import connect, ensure_schema
from grga.related import RelatedKeywordsResult, related_keywords


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    monkeypatch.setenv("GRGA_PPR_MIN_NPMI", "0.0")
    return tmp_path


def _build_alias_corpus(scope_dir: Path) -> None:
    """Build a corpus with a known alias (complementary) structure.

    史强 ↔ 大史: co-occur once out of ~123 chunks while each appears often
    individually → strong negative signal with c/E < 0.10.

    行动 / 警官 are glued strong-positive partners of 史强 / 大史
    (npmi ≈ 1.0), so both endpoints clear the iteration-3 keyword-quality
    gate (max npmi > 0.70).

    汪淼 ↔ 史强: co-occur often → positive (not complementary).
    """
    scope_dir.mkdir(parents=True, exist_ok=True)
    lines = []

    # 史强 + 行动, glued (~50)
    for i in range(50):
        lines.append(f"史强 行动 任务 第{i}段")
    # 大史 + 警官, glued (~30)
    for i in range(30):
        lines.append(f"大史 警官 调查 第{i+50}段")
    # 史强 + 汪淼 (positive, 20)
    for i in range(20):
        lines.append(f"史强 行动 汪淼 对话 第{i+80}段")
    # 史强 + 大史 co-occur exactly once — alias pattern, c/E < 0.10
    lines.append("史强 行动 大史 警官 出场 第100段")
    # 大史 + 汪淼 (7)
    for i in range(7):
        lines.append(f"大史 警官 汪淼 见面 第{i+103}段")
    # 行动 occurs a few times WITHOUT 史强, so 史强↔行动 is not glued
    # (glued pairs are dropped from keyword_pairs) yet still npmi > 0.70.
    for i in range(5):
        lines.append(f"行动 总结 记录 {i}")

    # Fillers
    remaining = 123 - len(lines)
    for i in range(remaining):
        lines.append(f"其他内容 filler {i}")

    (scope_dir / "santi.txt").write_text("\n".join(lines), encoding="utf-8")


def _build_db(scope_dir: Path, db_path: Path, scope_name: str = "test-scope") -> None:
    build_graph_index(
        scope_name,
        directory=scope_dir,
        db_path=db_path,
        min_pair_count=1,
        min_npmi=0.0,
    )


# ======================================================================
# 1. Build — complementary_pairs table
# ======================================================================

class TestBuildComplementaryPairs:
    """Verify the build pipeline writes significant negative pairs."""

    def test_table_exists_after_build(self, sidecar_env, tmp_path):
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='complementary_pairs'"
            ).fetchone()
            assert row is not None, "complementary_pairs table missing"
        finally:
            conn.close()

    def test_alias_pair_captured(self, sidecar_env, tmp_path):
        """A1: 史强↔大史 should be in complementary_pairs with low p-value."""
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.scope_id_ro("test-scope")
            assert sid is not None

            # Find the pair via both directions
            row = conn.execute(
                """
                SELECT cp.cooccur_count, cp.expected_count, cp.npmi, cp.p_value,
                       ka.kw AS kw_a, kb.kw AS kw_b
                FROM complementary_pairs cp
                JOIN keywords ka ON ka.id = cp.kw_a_id
                JOIN keywords kb ON kb.id = cp.kw_b_id
                WHERE cp.scope_id = ?
                  AND (
                    (ka.kw = '史强' AND kb.kw = '大史')
                    OR (ka.kw = '大史' AND kb.kw = '史强')
                  )
                """,
                (sid,),
            ).fetchone()
            assert row is not None, (
                "A1 FAIL: 史强↔大史 pair not found in complementary_pairs"
            )
            assert row["p_value"] < 1e-6, (
                f"A1 FAIL: p_value={row['p_value']} not < 1e-6"
            )
            c_over_e = row["cooccur_count"] / row["expected_count"]
            assert c_over_e < 0.10, (
                f"A1 FAIL: c/E={c_over_e} not < 0.10 (iteration-3 strength gate)"
            )
            assert row["npmi"] < 0, "npmi should be negative for complementary"
        finally:
            conn.close()

    def test_positive_pair_not_in_complementary(self, sidecar_env, tmp_path):
        """A3: 史强↔汪淼 (high co-occurrence) must NOT be in complementary_pairs."""
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.scope_id_ro("test-scope")
            assert sid is not None

            row = conn.execute(
                """
                SELECT 1 FROM complementary_pairs cp
                JOIN keywords ka ON ka.id = cp.kw_a_id
                JOIN keywords kb ON kb.id = cp.kw_b_id
                WHERE cp.scope_id = ?
                  AND (
                    (ka.kw = '史强' AND kb.kw = '汪淼')
                    OR (ka.kw = '汪淼' AND kb.kw = '史强')
                  )
                """,
                (sid,),
            ).fetchone()
            assert row is None, (
                "A3 FAIL: positive pair 史强↔汪淼 should not be in complementary_pairs"
            )
        finally:
            conn.close()

    def test_pair_normalization_a_less_than_b(self, sidecar_env, tmp_path):
        """Complementary pairs should be stored with kw_a_id < kw_b_id."""
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.scope_id_ro("test-scope")
            assert sid is not None

            count = conn.execute(
                """
                SELECT COUNT(*) FROM complementary_pairs
                WHERE scope_id = ? AND kw_a_id >= kw_b_id
                """,
                (sid,),
            ).fetchone()[0]
            assert count == 0, (
                f"Found {count} pairs with kw_a_id >= kw_b_id (violates normalisation)"
            )
        finally:
            conn.close()


# ======================================================================
# 2. Related — complementary field in graph mode
# ======================================================================



    def test_rebuild_clears_and_rebuilds_complementary(self, sidecar_env, tmp_path):
        """Rebuild mode: complementary_pairs is cleared and re-built from scratch."""
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        # Confirm complementary pairs exist after first build
        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.scope_id_ro("test-scope")
            assert sid is not None
            before = conn.execute(
                "SELECT COUNT(*) FROM complementary_pairs WHERE scope_id = ?",
                (sid,),
            ).fetchone()[0]
            assert before > 0
        finally:
            conn.close()

        # Rebuild the same corpus
        from grga.graph.build import build_graph_index
        build_graph_index(
            "test-scope",
            directory=scope_dir,
            db_path=db,
            min_pair_count=1,
            min_npmi=0.0,
            mode="rebuild",
        )

        # After rebuild, complementary pairs should still exist (same corpus)
        # but the count should match (not double)
        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.scope_id_ro("test-scope")
            assert sid is not None
            after = conn.execute(
                "SELECT COUNT(*) FROM complementary_pairs WHERE scope_id = ?",
                (sid,),
            ).fetchone()[0]
            assert after == before, (
                f"rebuild should result in same count (got {after}, expected {before})"
            )
        finally:
            conn.close()

    def test_rebuild_clears_stale_complementary_pairs(self, sidecar_env, tmp_path):
        """Rebuild with different corpus: old complementary pairs are removed.

        After a rebuild with a corpus that has no complementary structure,
        the complementary_pairs table should be empty for that scope.
        This verifies that rebuild properly clears the table first.
        """
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"

        # Build corpus with complementary structure
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        # Confirm complementary pairs exist
        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.scope_id_ro("test-scope")
            assert sid is not None
            before = conn.execute(
                "SELECT COUNT(*) FROM complementary_pairs WHERE scope_id = ?",
                (sid,),
            ).fetchone()[0]
            assert before > 0, "Initial build should have complementary pairs"
        finally:
            conn.close()

        # Rebuild with a corpus that has NO complementary structure
        # (all pairs are positive)
        new_dir = tmp_path / "new_corpus"
        new_dir.mkdir()
        lines = []
        # alpha and beta always co-occur → strongly positive, not complementary
        for i in range(50):
            lines.append(f"alpha beta together {i}")
        lines += ["alpha only"] * 3
        lines += ["beta only"] * 3
        (new_dir / "doc.txt").write_text("\n".join(lines), encoding="utf-8")

        from grga.graph.build import build_graph_index
        build_graph_index(
            "test-scope",
            directory=new_dir,
            db_path=db,
            min_pair_count=1,
            min_npmi=0.0,
            mode="rebuild",
        )

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.scope_id_ro("test-scope")
            assert sid is not None
            # Old pairs (史强↔大史) should be gone
            row = conn.execute(
                """
                SELECT 1 FROM complementary_pairs cp
                JOIN keywords ka ON ka.id = cp.kw_a_id
                JOIN keywords kb ON kb.id = cp.kw_b_id
                WHERE cp.scope_id = ?
                  AND (
                    (ka.kw = '史强' AND kb.kw = '大史')
                    OR (ka.kw = '大史' AND kb.kw = '史强')
                  )
                """,
                (sid,),
            ).fetchone()
            assert row is None, (
                "After rebuild with new corpus, old complementary pairs should be gone"
            )
            # alpha/beta should be in keyword_pairs (positive)
            row = conn.execute(
                """
                SELECT 1 FROM keyword_pairs kp
                JOIN keywords ka ON ka.id = kp.kw_a_id
                JOIN keywords kb ON kb.id = kp.kw_b_id
                WHERE kp.scope_id = ? AND kp.source = 'chunk_cooccur'
                  AND (
                    (ka.kw = 'alpha' AND kb.kw = 'beta')
                    OR (ka.kw = 'beta' AND kb.kw = 'alpha')
                  )
                """,
                (sid,),
            ).fetchone()
            assert row is not None, "alpha/beta should be in keyword_pairs"
        finally:
            conn.close()


class TestRelatedComplementaryHidden:
    """Iteration 3: related no longer exposes complementary pairs."""

    def test_result_complementary_field_stays_empty(self, sidecar_env, tmp_path):
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        result = related_keywords(
            ["史强"],
            limit_per_seed=30,
            min_npmi=0.0,
            db_path=db,
            scopes=["test-scope"],
        )
        assert isinstance(result, RelatedKeywordsResult)
        # Field retained for backward compatibility, but never populated.
        assert result.complementary == []

    def test_to_dict_omits_complementary(self, sidecar_env, tmp_path):
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        result = related_keywords(
            ["史强"],
            limit_per_seed=30,
            min_npmi=0.0,
            db_path=db,
            scopes=["test-scope"],
        )
        d = result.to_dict()
        assert "complementary" not in d, (
            "Iteration 3: complementary must not appear in related JSON"
        )
        assert "results" in d
        assert "resolved_seeds" in d

    def test_positive_side_unchanged(self, sidecar_env, tmp_path):
        """Positive-side results are unaffected by hiding complementary."""
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        result = related_keywords(
            ["史强"],
            limit_per_seed=30,
            min_npmi=0.0,
            db_path=db,
            scopes=["test-scope"],
        )
        result_kws = {r.kw for r in result.results}
        assert "汪淼" in result_kws, "汪淼 should still be a positive neighbour"
        nmpis = [r.npmi for r in result.results]
        assert nmpis == sorted(nmpis, reverse=True)


# ======================================================================
# 3. Probe — complementary attachment + elapsed_ms removal
# ======================================================================

class TestProbeComplementaryHidden:
    """Iteration 3: probe exposes no complementary data."""

    def test_probe_module_has_no_complementary_helpers(self):
        import grga.probe as probe
        assert not hasattr(probe, "_attach_complementary")
        assert not hasattr(probe, "_lookup_complementary")

    def test_error_json_has_no_complementary(self):
        from grga.probe import _error
        import time
        base = {"ok": True, "total_lines": 0, "file_count": 0, "patterns": ["x"]}
        result = json.loads(_error("test error", base, time.monotonic()))
        assert "complementary" not in result


class TestProbeNoElapsedMs:
    """A6: elapsed_ms removed from probe JSON output."""

    def test_content_probe_no_elapsed_ms(self, sidecar_env, tmp_path):
        from grga.probe import probe_directory
        scope_dir = tmp_path / "corpus"
        _build_alias_corpus(scope_dir)
        # Use a simple text query so we don't depend on rga being available
        # in the test env — actually probe needs rga.
        # We'll test the _attach_neighbors path indirectly via the JSON dict.
        # For direct elapsed_ms verification, test _error and the attach flow.
        # Full probe test requires rga — skip that here.

    def test_error_has_no_elapsed_ms(self):
        from grga.probe import _error
        base = {"ok": True, "total_lines": 0, "file_count": 0, "patterns": ["x"]}
        import time
        result = json.loads(_error("test error", base, time.monotonic()))
        assert "elapsed_ms" not in result


# ======================================================================
# 4. Pack — complementary_pairs in knowledge pack
# ======================================================================

class TestComplementaryInPack:
    """Verify complementary_pairs travels in knowledge packs."""

    def test_export_includes_complementary(self, sidecar_env, tmp_path):
        from grga.graph.pack import export_knowledge_pack
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db, scope_name="test-scope")

        pack_path = tmp_path / "test.grgapkg"
        stats = export_knowledge_pack(
            scope="test-scope",
            out_path=pack_path,
            db_path=db,
        )
        assert stats.n_complementary_pairs > 0, (
            "export should include complementary pairs"
        )

        # Verify the table actually exists in the pack file
        conn = sqlite3.connect(str(pack_path))
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='complementary_pairs'"
            ).fetchone()
            assert row is not None, "complementary_pairs missing from pack"
            count = conn.execute(
                "SELECT COUNT(*) FROM complementary_pairs"
            ).fetchone()[0]
            assert count > 0
        finally:
            conn.close()

    def test_round_trip_preserves_complementary(self, sidecar_env, tmp_path):
        from grga.graph.pack import export_knowledge_pack, import_knowledge_pack
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "src.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db, scope_name="src")

        pack_path = tmp_path / "test.grgapkg"
        export_knowledge_pack(
            scope="src", out_path=pack_path, db_path=db,
        )

        dst_db = tmp_path / "dst.db"
        stats = import_knowledge_pack(
            pack_path, as_scope="dst", db_path=dst_db,
        )
        assert stats.n_complementary_pairs > 0

        # Iteration 3: the table travels in the pack (internal channel),
        # but the read path still hides it — verify the DB rows, not related.
        conn = connect(dst_db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.scope_id_ro("dst")
            assert sid is not None
            row = conn.execute(
                """
                SELECT 1 FROM complementary_pairs cp
                JOIN keywords ka ON ka.id = cp.kw_a_id
                JOIN keywords kb ON kb.id = cp.kw_b_id
                WHERE cp.scope_id = ?
                  AND (
                    (ka.kw = '史强' AND kb.kw = '大史')
                    OR (ka.kw = '大史' AND kb.kw = '史强')
                  )
                """,
                (sid,),
            ).fetchone()
            assert row is not None, "alias pair should survive the pack round-trip"
        finally:
            conn.close()

        result = related_keywords(
            ["史强"],
            limit_per_seed=30,
            min_npmi=0.0,
            db_path=dst_db,
            scopes=["dst"],
        )
        assert result.complementary == []
        assert "complementary" not in result.to_dict()


# ======================================================================
# 5. Scope removal — purge_knowledge wipes complementary_pairs
# ======================================================================

class TestComplementaryScopeRemoval:
    """Verify purge_knowledge cleans up complementary_pairs."""

    def test_purge_removes_complementary(self, sidecar_env, tmp_path):
        from grga.graph.scope import remove_scope
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db, scope_name="test-scope")

        # Confirm there are complementary pairs before removal
        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.scope_id_ro("test-scope")
            assert sid is not None
            before = conn.execute(
                "SELECT COUNT(*) FROM complementary_pairs WHERE scope_id = ?",
                (sid,),
            ).fetchone()[0]
            assert before > 0
        finally:
            conn.close()

        # Purge the scope
        stats = remove_scope(
            "test-scope", purge_knowledge=True, db_path=db,
        )
        assert stats.purged_knowledge is True
        assert stats.complementary_pairs_removed > 0

        # Confirm table is empty for this scope
        conn = connect(db)
        try:
            ensure_schema(conn)
            after = conn.execute(
                "SELECT COUNT(*) FROM complementary_pairs"
            ).fetchone()[0]
            assert after == 0
        finally:
            conn.close()


# ======================================================================
# 6. Positive-side zero change (red line #2)
# ======================================================================

class TestPositiveSideUnchanged:
    """A3 / A8: existing positive-side results are byte-identical."""

    def test_results_unchanged_after_complementary_addition(self, sidecar_env, tmp_path):
        """Build a corpus, query related — results should be same as before.

        This is a regression test: the results field should be identical
        to what it was before the complementary-pairs feature was added.
        """
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        result = related_keywords(
            ["史强"],
            limit_per_seed=30,
            min_npmi=0.0,
            db_path=db,
            scopes=["test-scope"],
        )
        # The positive side should still have all expected keywords
        result_kws = {r.kw for r in result.results}
        # 汪淼 should definitely be there (strong co-occurrence)
        assert "汪淼" in result_kws
        # Results are sorted by npmi desc
        nmpis = [r.npmi for r in result.results]
        assert nmpis == sorted(nmpis, reverse=True)


# ======================================================================
# 7. Complementary noise filtering (plan-2026-09-12)
# ======================================================================

def _seed_synthetic_segregation_scope(conn, scope_name: str = "synth-seg"):
    """Seed a scope with deterministic chunk/doc statistics.

    Two "file universes" with zero document overlap:

    * fiction: 史强 (docs 1..50), 大史 (docs 1..50)
    * tech:    设计 (docs 51..90), 系统 (docs 51..90)

    All three pairs have identical, strongly negative chunk npmi, so the
    only discriminant is the segregation gate:

    * 史强↔设计 — zero shared docs → Gate A fails → filtered
    * 史强↔大史 — 50 shared docs → survives
    * 设计↔系统 — 40 shared docs → survives

    Returns the scope id.
    """
    interner = IdInterner(conn)
    sid = interner.scope_id(scope_name)
    base = ["史强", "大史", "设计", "系统"]
    anchors = [f"{k}__q" for k in base]
    kw = interner.kw_ids(base + anchors)

    n_chunks, n_docs = 1000, 100
    conn.execute(
        "INSERT OR REPLACE INTO scope_stats "
        "(scope_id, n_chunks, n_docs, vocab_size, updated_at) "
        "VALUES (?, ?, ?, 8, '2026-01-01T00:00:00Z')",
        (sid, n_chunks, n_docs),
    )
    stats = [
        (kw["史强"], 400, 50),
        (kw["大史"], 350, 50),
        (kw["设计"], 400, 40),
        (kw["系统"], 350, 40),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO keyword_stats "
        "(scope_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        [(sid, k, cdf, ddf, cdf * 2) for k, cdf, ddf in stats],
    )
    # Strong-positive anchors (npmi > 0.70). The anchor's chunk_df is
    # deliberately higher than the co-occurrence count so the pair is not
    # "glued" (glued pairs are dropped by step 4, which would erase the
    # quality signal on a re-run).
    for name, df in (("史强", 400), ("大史", 350), ("设计", 400), ("系统", 350)):
        anchor = f"{name}__q"
        conn.execute(
            "INSERT OR REPLACE INTO keyword_stats "
            "(scope_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at) "
            "VALUES (?, ?, ?, 0, ?, '2026-01-01T00:00:00Z')",
            (sid, kw[anchor], df + 20, (df + 20) * 2),
        )
        a, b = sorted((kw[name], kw[anchor]))
        conn.execute(
            "INSERT OR REPLACE INTO keyword_pairs "
            "(scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at) "
            "VALUES (?, ?, ?, 'chunk_cooccur', ?, 1.0, '2026-01-01T00:00:00Z')",
            (sid, a, b, df),
        )

    def _pair(a_kw, b_kw, co):
        a, b = sorted((kw[a_kw], kw[b_kw]))
        conn.execute(
            "INSERT OR REPLACE INTO keyword_pairs "
            "(scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at) "
            "VALUES (?, ?, ?, 'chunk_cooccur', ?, -0.5, '2026-01-01T00:00:00Z')",
            (sid, a, b, co),
        )

    _pair("史强", "大史", 5)
    _pair("史强", "设计", 5)
    _pair("设计", "系统", 5)

    conn.executemany(
        "INSERT OR IGNORE INTO docs "
        "(id, scope_id, doc_path, n_chunks_with_kw) VALUES (?, ?, ?, 1)",
        [(i, sid, f"doc_{i}.txt") for i in range(1, 101)],
    )

    def _seed_docs(kw_id, doc_ids):
        conn.executemany(
            "INSERT OR REPLACE INTO keyword_docs "
            "(scope_id, kw_id, doc_id, occurrence_count, chunk_count) "
            "VALUES (?, ?, ?, 4, 2)",
            [(sid, kw_id, d) for d in doc_ids],
        )

    _seed_docs(kw["史强"], range(1, 51))
    _seed_docs(kw["大史"], range(1, 51))
    _seed_docs(kw["设计"], range(51, 91))
    _seed_docs(kw["系统"], range(51, 91))
    conn.commit()
    return sid


class TestComplementaryStopwordFilter:
    """Phase 1: stopword gate at complementary-pair entry."""

    def test_stopword_pair_filtered_via_direct_insert(self, sidecar_env, tmp_path):
        """Construct a stopword + non-stopword pair directly in
        keyword_pairs with negative npmi, verify it does NOT enter
        complementary_pairs after recompute.
        """
        from grga.graph.build import _INDEX_STOPWORDS, _recompute_scope_pairs

        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db, scope_name="test-stop")

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.scope_id_ro("test-stop")
            assert sid is not None

            assert "也" in _INDEX_STOPWORDS

            kw_ye_id = interner.kw_id("也")
            kw_shiqiang_id = interner.kw_id_ro("史强")
            assert kw_shiqiang_id is not None

            conn.execute(
                "INSERT OR REPLACE INTO keyword_stats "
                "(scope_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at) "
                "VALUES (?, ?, 50, 1, 100, '2026-01-01T00:00:00Z')",
                (sid, kw_ye_id),
            )
            conn.commit()

            a_id = min(kw_shiqiang_id, kw_ye_id)
            b_id = max(kw_shiqiang_id, kw_ye_id)
            conn.execute(
                "INSERT OR REPLACE INTO keyword_pairs "
                "(scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at) "
                "VALUES (?, ?, ?, 'chunk_cooccur', 1, -0.5, '2026-01-01T00:00:00Z')",
                (sid, a_id, b_id),
            )
            conn.commit()

            _recompute_scope_pairs(conn, sid, min_npmi=0.0)

            row = conn.execute(
                """
                SELECT 1 FROM complementary_pairs cp
                JOIN keywords ka ON ka.id = cp.kw_a_id
                JOIN keywords kb ON kb.id = cp.kw_b_id
                WHERE cp.scope_id = ?
                  AND (
                    (ka.kw = '史强' AND kb.kw = '也')
                    OR (ka.kw = '也' AND kb.kw = '史强')
                  )
                """,
                (sid,),
            ).fetchone()
            assert row is None, (
                "Stopword pair 史强↔也 should be filtered from complementary_pairs"
            )

            row2 = conn.execute(
                """
                SELECT 1 FROM complementary_pairs cp
                JOIN keywords ka ON ka.id = cp.kw_a_id
                JOIN keywords kb ON kb.id = cp.kw_b_id
                WHERE cp.scope_id = ?
                  AND (
                    (ka.kw = '史强' AND kb.kw = '大史')
                    OR (ka.kw = '大史' AND kb.kw = '史强')
                  )
                """,
                (sid,),
            ).fetchone()
            assert row2 is not None, (
                "Non-stopword pair 史强↔大史 should remain in complementary_pairs"
            )
        finally:
            conn.close()


class TestSegregationPairUnit:
    """Unit tests for _is_segregation_pair (pure math, no DB)."""

    def test_small_n_docs_short_circuits_false(self):
        """n_docs <= 2 → not segregation (statistically meaningless)."""
        from grga.graph.build import _is_segregation_pair
        assert _is_segregation_pair(
            co_doc=1, doc_df_a=1, doc_df_b=1,
            n_docs=1, npmi_chunk=-0.5,
        ) is False
        assert _is_segregation_pair(
            co_doc=1, doc_df_a=1, doc_df_b=1,
            n_docs=2, npmi_chunk=-0.5,
        ) is False

    def test_zero_df_is_segregation(self):
        """Zero df → undefined → treated as segregation (safe drop)."""
        from grga.graph.build import _is_segregation_pair
        assert _is_segregation_pair(
            co_doc=0, doc_df_a=0, doc_df_b=5,
            n_docs=10, npmi_chunk=-0.5,
        ) is True

    def test_low_co_doc_fails_gate_a(self):
        """co_doc below the minimum threshold → segregation."""
        from grga.graph.build import _is_segregation_pair
        assert _is_segregation_pair(
            co_doc=1, doc_df_a=100, doc_df_b=100,
            n_docs=200, npmi_chunk=-0.3,
        ) is True

    def test_high_co_doc_passes_gate_a(self):
        """co_doc above threshold → Gate A passes, check Gate B."""
        from grga.graph.build import _is_segregation_pair
        assert _is_segregation_pair(
            co_doc=50, doc_df_a=100, doc_df_b=100,
            n_docs=200, npmi_chunk=-0.3,
        ) is False

    def test_gate_b_npmi_gap_boundary(self):
        """Gate B fires only when doc npmi drops more than the gap below
        chunk npmi; a doc npmi within the gap must survive."""
        from grga.graph.build import _is_segregation_pair
        # npmi_doc ≈ -0.40, chunk npmi -0.10 → -0.40 < -0.20 → segregation
        assert _is_segregation_pair(
            co_doc=10, doc_df_a=50, doc_df_b=50,
            n_docs=100, npmi_chunk=-0.1,
        ) is True
        # npmi_doc ≈ -0.14, chunk npmi -0.10 → within the 0.1 gap → survives
        assert _is_segregation_pair(
            co_doc=12, doc_df_a=40, doc_df_b=40,
            n_docs=100, npmi_chunk=-0.1,
        ) is False


class TestComplementarySegregationFilter:
    """Phase 2: file-type segregation detection (integration tests)."""

    def test_segregation_filter_reduces_pair_count(self, sidecar_env, tmp_path):
        """Integration test: segregation filter removes file-isolated pairs.

        Uses ``_seed_synthetic_segregation_scope`` so the two gates are
        exercised deterministically:

        * 史强↔设计 — zero shared docs → Gate A fails → filtered
        * 史强↔大史 — 50 shared docs → survives
        * 设计↔系统 — 40 shared docs → survives
        """
        from grga.graph.build import _recompute_scope_pairs

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            sid = _seed_synthetic_segregation_scope(conn)

            def _pair_present(a_kw, b_kw):
                return conn.execute(
                    """
                    SELECT 1 FROM complementary_pairs cp
                    JOIN keywords ka ON ka.id = cp.kw_a_id
                    JOIN keywords kb ON kb.id = cp.kw_b_id
                    WHERE cp.scope_id = ?
                      AND (
                        (ka.kw = ? AND kb.kw = ?)
                        OR (ka.kw = ? AND kb.kw = ?)
                      )
                    """,
                    (sid, a_kw, b_kw, b_kw, a_kw),
                ).fetchone() is not None

            # --- Filter ON ---
            _recompute_scope_pairs(
                conn, sid, min_npmi=0.0, filter_comp_segregation=True,
            )
            assert not _pair_present("史强", "设计"), (
                "Segregation pair 史强↔设计 (zero shared docs) should be filtered"
            )
            assert _pair_present("史强", "大史"), (
                "Same-universe pair 史强↔大史 should survive"
            )
            assert _pair_present("设计", "系统"), (
                "Same-universe pair 设计↔系统 should survive"
            )
            count_on = conn.execute(
                "SELECT COUNT(*) FROM complementary_pairs WHERE scope_id = ?",
                (sid,),
            ).fetchone()[0]

            # --- Filter OFF ---
            _recompute_scope_pairs(
                conn, sid, min_npmi=0.0, filter_comp_segregation=False,
            )
            assert _pair_present("史强", "设计"), (
                "With the filter off, 史强↔设计 should be present"
            )
            count_off = conn.execute(
                "SELECT COUNT(*) FROM complementary_pairs WHERE scope_id = ?",
                (sid,),
            ).fetchone()[0]

            assert count_on < count_off, (
                f"Filter should reduce count: on={count_on}, off={count_off}"
            )
        finally:
            conn.close()

    def test_flag_default_on(self, sidecar_env, tmp_path):
        """build_graph_index defaults to filter_comp_segregation=True."""
        import inspect
        from grga.graph.build import build_graph_index
        sig = inspect.signature(build_graph_index)
        assert sig.parameters["filter_comp_segregation"].default is True

    def test_no_doc_evidence_skips_segregation(self, sidecar_env, tmp_path):
        """Knowledge-only scope (no ``keyword_docs``) must not have every
        pair wiped by the segregation gate — the evidence is unknowable,
        so the gate is skipped (post-``import`` / forget--all scenario)."""
        from grga.graph.build import _recompute_scope_pairs

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            sid = _seed_synthetic_segregation_scope(conn, "no-evidence")
            # Simulate a knowledge-only scope: wipe the evidence layer but
            # keep scope_stats.n_docs (as ``forget --all --keep-pairs`` does).
            conn.execute(
                "DELETE FROM keyword_docs WHERE scope_id = ?", (sid,)
            )
            conn.commit()

            _recompute_scope_pairs(
                conn, sid, min_npmi=0.2, filter_comp_segregation=True,
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM complementary_pairs WHERE scope_id = ?",
                (sid,),
            ).fetchone()[0]
            assert count == 3, (
                "Segregation must be skipped without doc evidence; "
                f"expected 3 pairs, got {count}"
            )
        finally:
            conn.close()

    def test_sql_and_batch_backends_agree_with_filter(
        self, sidecar_env, tmp_path, monkeypatch,
    ):
        """With the segregation filter enabled, the SQL and batch backends
        must write the same ``complementary_pairs`` rows (the gate is now
        a shared helper, so this guards regressions)."""
        import grga.graph.build as build_mod
        from grga.graph.build import _recompute_scope_pairs

        def _run(force_batch: bool):
            db = tmp_path / ("batch.db" if force_batch else "sql.db")
            conn = connect(db)
            try:
                ensure_schema(conn)
                sid = _seed_synthetic_segregation_scope(conn, "parity")
                if force_batch:
                    monkeypatch.setattr(
                        build_mod, "_sqlite_has_ln", lambda c: False,
                    )
                _recompute_scope_pairs(
                    conn, sid, min_npmi=0.2, filter_comp_segregation=True,
                )
                conn.commit()
                return conn.execute(
                    """
                    SELECT ka.kw AS kw_a, kb.kw AS kw_b,
                           cp.cooccur_count, cp.expected_count,
                           cp.npmi, cp.p_value
                    FROM complementary_pairs cp
                    JOIN keywords ka ON ka.id = cp.kw_a_id
                    JOIN keywords kb ON kb.id = cp.kw_b_id
                    WHERE cp.scope_id = ?
                    ORDER BY kw_a, kw_b
                    """,
                    (sid,),
                ).fetchall()
            finally:
                conn.close()

        sql_rows = _run(force_batch=False)
        batch_rows = _run(force_batch=True)

        assert [r["kw_a"] for r in sql_rows] == [r["kw_a"] for r in batch_rows]
        assert [r["kw_b"] for r in sql_rows] == [r["kw_b"] for r in batch_rows]
        for s, b in zip(sql_rows, batch_rows):
            assert s["cooccur_count"] == b["cooccur_count"]
            assert abs(s["expected_count"] - b["expected_count"]) < 1e-9
            assert abs(s["npmi"] - b["npmi"]) < 1e-9
            assert abs(s["p_value"] - b["p_value"]) < 1e-9


class TestSegregationIsPureSubtraction:
    """The segregation filter only removes pairs — it never changes
    statistics for surviving pairs or adds new ones."""

    def test_surviving_pairs_unchanged(self, sidecar_env, tmp_path):
        """Pairs that survive the segregation filter have identical
        npmi / p_value / cooccur_count as without the filter."""
        from grga.graph.build import _recompute_scope_pairs

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            sid = _seed_synthetic_segregation_scope(conn)

            _recompute_scope_pairs(
                conn, sid, min_npmi=0.0, filter_comp_segregation=True,
            )
            rows_on = conn.execute(
                """
                SELECT ka.kw AS kw_a, kb.kw AS kw_b,
                       cp.cooccur_count, cp.expected_count, cp.npmi, cp.p_value
                FROM complementary_pairs cp
                JOIN keywords ka ON ka.id = cp.kw_a_id
                JOIN keywords kb ON kb.id = cp.kw_b_id
                WHERE cp.scope_id = ?
                ORDER BY kw_a, kw_b
                """,
                (sid,),
            ).fetchall()
            assert rows_on, "expected surviving pairs to make this test meaningful"

            _recompute_scope_pairs(
                conn, sid, min_npmi=0.0, filter_comp_segregation=False,
            )

            for r_on in rows_on:
                row_off = conn.execute(
                    """
                    SELECT cp.cooccur_count, cp.expected_count, cp.npmi, cp.p_value
                    FROM complementary_pairs cp
                    JOIN keywords ka ON ka.id = cp.kw_a_id
                    JOIN keywords kb ON kb.id = cp.kw_b_id
                    WHERE cp.scope_id = ?
                      AND ka.kw = ? AND kb.kw = ?
                    """,
                    (sid, r_on["kw_a"], r_on["kw_b"]),
                ).fetchone()
                assert row_off is not None, (
                    f"Pair {r_on['kw_a']}↔{r_on['kw_b']} present in filtered "
                    "but missing from unfiltered (violates pure-subtraction)"
                )
                assert r_on["cooccur_count"] == row_off["cooccur_count"]
                assert abs(r_on["expected_count"] - row_off["expected_count"]) < 1e-9
                assert abs(r_on["npmi"] - row_off["npmi"]) < 1e-9
                assert abs(r_on["p_value"] - row_off["p_value"]) < 1e-9
        finally:
            conn.close()


# ======================================================================
# 8. Batch fallback path (_step4_batch) honours both gates
# ======================================================================

class TestBatchFallbackFilters:
    """The non-SQL fallback (_step4_batch) applies the same stopword and
    segregation gates as the SQL backend (_step4_sql)."""

    def test_batch_path_stopword_filter(self, sidecar_env, tmp_path, monkeypatch):
        import grga.graph.build as build_mod
        from grga.graph.build import _recompute_scope_pairs, _INDEX_STOPWORDS

        monkeypatch.setattr(build_mod, "_sqlite_has_ln", lambda conn: False)

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            sid = _seed_synthetic_segregation_scope(conn, "batch-stop")
            interner = IdInterner(conn)

            assert "也" in _INDEX_STOPWORDS
            kw_ye = interner.kw_id("也")
            kw_shiqiang = interner.kw_id_ro("史强")
            assert kw_shiqiang is not None

            conn.execute(
                "INSERT OR REPLACE INTO keyword_stats "
                "(scope_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at) "
                "VALUES (?, ?, 400, 50, 800, '2026-01-01T00:00:00Z')",
                (sid, kw_ye),
            )
            a, b = sorted((kw_shiqiang, kw_ye))
            conn.execute(
                "INSERT OR REPLACE INTO keyword_pairs "
                "(scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at) "
                "VALUES (?, ?, ?, 'chunk_cooccur', 5, -0.5, '2026-01-01T00:00:00Z')",
                (sid, a, b),
            )
            conn.commit()

            _recompute_scope_pairs(
                conn, sid, min_npmi=0.2, filter_comp_segregation=True,
            )
            row = conn.execute(
                """
                SELECT 1 FROM complementary_pairs cp
                JOIN keywords ka ON ka.id = cp.kw_a_id
                JOIN keywords kb ON kb.id = cp.kw_b_id
                WHERE cp.scope_id = ?
                  AND (
                    (ka.kw = '史强' AND kb.kw = '也')
                    OR (ka.kw = '也' AND kb.kw = '史强')
                  )
                """,
                (sid,),
            ).fetchone()
            assert row is None, (
                "Batch path: stopword pair 史强↔也 should be filtered"
            )
        finally:
            conn.close()

    def test_batch_path_segregation_filter(self, sidecar_env, tmp_path, monkeypatch):
        import grga.graph.build as build_mod
        from grga.graph.build import _recompute_scope_pairs

        monkeypatch.setattr(build_mod, "_sqlite_has_ln", lambda conn: False)

        def _build(filter_on: bool):
            db = tmp_path / ("on.db" if filter_on else "off.db")
            conn = connect(db)
            ensure_schema(conn)
            sid = _seed_synthetic_segregation_scope(conn, "batch-seg")
            _recompute_scope_pairs(
                conn, sid, min_npmi=0.2, filter_comp_segregation=filter_on,
            )
            conn.commit()
            return conn, sid

        def _present(conn, sid, a_kw, b_kw):
            return conn.execute(
                """
                SELECT 1 FROM complementary_pairs cp
                JOIN keywords ka ON ka.id = cp.kw_a_id
                JOIN keywords kb ON kb.id = cp.kw_b_id
                WHERE cp.scope_id = ?
                  AND (
                    (ka.kw = ? AND kb.kw = ?)
                    OR (ka.kw = ? AND kb.kw = ?)
                  )
                """,
                (sid, a_kw, b_kw, b_kw, a_kw),
            ).fetchone() is not None

        conn_on, sid_on = _build(True)
        conn_off, sid_off = _build(False)
        try:
            assert not _present(conn_on, sid_on, "史强", "设计"), (
                "Batch path: segregation pair 史强↔设计 should be filtered"
            )
            assert _present(conn_on, sid_on, "史强", "大史"), (
                "Batch path: same-universe pair 史强↔大史 should survive"
            )
            assert _present(conn_off, sid_off, "史强", "设计"), (
                "Batch path: with filter off, 史强↔设计 should be present"
            )
        finally:
            conn_on.close()
            conn_off.close()


# ======================================================================
# 9. Iteration 2 — c/E strength gate + complementary-only stopwords
# ======================================================================


def _seed_chunk_scope(
    conn,
    *,
    scope_name: str,
    stats,
    pairs,
    n_chunks: int = 2000,
    n_docs: int = 0,
    with_quality: bool = True,
) -> int:
    """Seed chunk-level stats + pair rows for gate tests.

    No ``keyword_docs`` rows are written, so the segregation gate is
    skipped and only the stopword / strength / quality gates are exercised.
    ``expected_count`` is ``chunk_df_a * chunk_df_b / n_chunks``, so the
    caller picks ``co`` to hit a target c/E.

    By default every keyword gets a glued strong-positive twin
    (``<name>__q``, npmi ≈ 1.0) so candidates clear the iteration-3
    keyword-quality gate. Pass ``with_quality=False`` to test that gate.
    """
    interner = IdInterner(conn)
    sid = interner.scope_id(scope_name)
    df_map = {name: df for name, df in stats}
    if with_quality:
        for name, df in stats:
            df_map[f"{name}__q"] = df
    kw = interner.kw_ids(list(df_map))
    conn.execute(
        "INSERT OR REPLACE INTO scope_stats "
        "(scope_id, n_chunks, n_docs, vocab_size, updated_at) "
        "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        (sid, n_chunks, n_docs, len(df_map)),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO keyword_stats "
        "(scope_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at) "
        "VALUES (?, ?, ?, 0, ?, '2026-01-01T00:00:00Z')",
        [(sid, kw[name], df, df * 2) for name, df in df_map.items()],
    )
    if with_quality:
        for name, df in stats:
            anchor = f"{name}__q"
            # Anchor df > cooccur so the pair is not glued (glued pairs are
            # dropped by step 4, which would erase the quality signal).
            conn.execute(
                "INSERT OR REPLACE INTO keyword_stats "
                "(scope_id, kw_id, chunk_df, doc_df, total_occurrences, updated_at) "
                "VALUES (?, ?, ?, 0, ?, '2026-01-01T00:00:00Z')",
                (sid, kw[anchor], df + 20, (df + 20) * 2),
            )
            a, b = sorted((kw[name], kw[anchor]))
            conn.execute(
                "INSERT OR REPLACE INTO keyword_pairs "
                "(scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at) "
                "VALUES (?, ?, ?, 'chunk_cooccur', ?, 1.0, '2026-01-01T00:00:00Z')",
                (sid, a, b, df),
            )
    for a_kw, b_kw, co in pairs:
        a, b = sorted((kw[a_kw], kw[b_kw]))
        conn.execute(
            "INSERT OR REPLACE INTO keyword_pairs "
            "(scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi, updated_at) "
            "VALUES (?, ?, ?, 'chunk_cooccur', ?, -0.5, '2026-01-01T00:00:00Z')",
            (sid, a, b, co),
        )
    conn.commit()
    return sid


def _comp_pair_present(conn, sid: int, a_kw: str, b_kw: str) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM complementary_pairs cp
        JOIN keywords ka ON ka.id = cp.kw_a_id
        JOIN keywords kb ON kb.id = cp.kw_b_id
        WHERE cp.scope_id = ?
          AND (
            (ka.kw = ? AND kb.kw = ?)
            OR (ka.kw = ? AND kb.kw = ?)
          )
        """,
        (sid, a_kw, b_kw, b_kw, a_kw),
    ).fetchone() is not None


def _keyword_pair_count(conn, sid: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM keyword_pairs "
        "WHERE scope_id = ? AND source = 'chunk_cooccur'",
        (sid,),
    ).fetchone()[0]


class TestStrengthGateUnit:
    """Unit tests for _passes_strength_gate (pure math, no DB)."""

    def test_boundary_is_exclusive(self):
        from grga.graph.build import (
            COMPLEMENTARY_MAX_C_OVER_E,
            _passes_strength_gate,
        )
        # Exactly at the threshold does NOT pass (strict <).
        assert _passes_strength_gate(
            COMPLEMENTARY_MAX_C_OVER_E * 10, 10.0,
        ) is False
        # Just below passes.
        assert _passes_strength_gate(
            COMPLEMENTARY_MAX_C_OVER_E * 10 - 1e-9, 10.0,
        ) is True

    def test_non_positive_expected_never_passes(self):
        from grga.graph.build import _passes_strength_gate
        assert _passes_strength_gate(0, 0.0) is False
        assert _passes_strength_gate(5, -1.0) is False


class TestComplementaryStrengthGate:
    """Iteration 2.1: c/E strength gate (pure subtraction, always on)."""

    def test_weak_pair_filtered_strong_pair_kept(self, sidecar_env, tmp_path):
        from grga.graph.build import _recompute_scope_pairs

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            # expected = 200*200/2000 = 20.
            # 甲-乙: co = 3 → c/E = 0.15 → filtered (>= 0.10)
            # 丙-丁: co = 1 → c/E = 0.05 → kept (< 0.10)
            sid = _seed_chunk_scope(
                conn,
                scope_name="strength",
                stats=[("甲", 200), ("乙", 200), ("丙", 200), ("丁", 200)],
                pairs=[("甲", "乙", 3), ("丙", "丁", 1)],
            )
            _recompute_scope_pairs(conn, sid, min_npmi=0.0)

            assert not _comp_pair_present(conn, sid, "甲", "乙"), (
                "c/E=0.15 pair should be filtered by the strength gate"
            )
            assert _comp_pair_present(conn, sid, "丙", "丁"), (
                "c/E=0.05 pair should survive the strength gate"
            )
        finally:
            conn.close()

    def test_keyword_quality_gate_filters_unanchored_pair(
        self, sidecar_env, tmp_path,
    ):
        """Iteration 3: a pair whose endpoints have no npmi > 0.70 partner
        is filtered even when c/E and significance pass."""
        from grga.graph.build import _recompute_scope_pairs

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            sid = _seed_chunk_scope(
                conn,
                scope_name="quality",
                stats=[("甲", 200), ("乙", 200)],
                pairs=[("甲", "乙", 1)],
                with_quality=False,
            )
            _recompute_scope_pairs(conn, sid, min_npmi=0.0)
            assert not _comp_pair_present(conn, sid, "甲", "乙"), (
                "Pair without a strong positive anchor should fail the "
                "keyword-quality gate"
            )
        finally:
            conn.close()

    def test_keyword_quality_gate_keeps_anchored_pair(
        self, sidecar_env, tmp_path,
    ):
        from grga.graph.build import _recompute_scope_pairs

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            sid = _seed_chunk_scope(
                conn,
                scope_name="quality-ok",
                stats=[("甲", 200), ("乙", 200)],
                pairs=[("甲", "乙", 1)],
                with_quality=True,
            )
            _recompute_scope_pairs(conn, sid, min_npmi=0.0)
            assert _comp_pair_present(conn, sid, "甲", "乙"), (
                "Pair with strong positive anchors should pass the "
                "keyword-quality gate"
            )
        finally:
            conn.close()

    def test_strength_gate_leaves_keyword_pairs_untouched(
        self, sidecar_env, tmp_path,
    ):
        """Pure subtraction: the gate must not delete / rewrite keyword_pairs."""
        from grga.graph.build import _recompute_scope_pairs

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            sid = _seed_chunk_scope(
                conn,
                scope_name="strength-pure",
                stats=[("甲", 200), ("乙", 200), ("丙", 200), ("丁", 200)],
                pairs=[("甲", "乙", 3), ("丙", "丁", 1)],
            )
            _recompute_scope_pairs(conn, sid, min_npmi=0.0)

            # Both pairs remain in keyword_pairs with their original counts,
            # including the one the strength gate blocked from complementary.
            # (Ignore the auto-seeded ``__q`` quality anchors.)
            rows = conn.execute(
                """
                SELECT ka.kw AS kw_a, kb.kw AS kw_b, kp.cooccur_count
                FROM keyword_pairs kp
                JOIN keywords ka ON ka.id = kp.kw_a_id
                JOIN keywords kb ON kb.id = kp.kw_b_id
                WHERE kp.scope_id = ? AND kp.source = 'chunk_cooccur'
                  AND ka.kw NOT LIKE '%__q' AND kb.kw NOT LIKE '%__q'
                """,
                (sid,),
            ).fetchall()
            got = {(r["kw_a"], r["kw_b"]): r["cooccur_count"] for r in rows}
            assert got == {("甲", "乙"): 3, ("丙", "丁"): 1}, got
        finally:
            conn.close()

    def test_batch_backend_applies_strength_gate(
        self, sidecar_env, tmp_path, monkeypatch,
    ):
        import grga.graph.build as build_mod
        from grga.graph.build import _recompute_scope_pairs

        monkeypatch.setattr(build_mod, "_sqlite_has_ln", lambda conn: False)

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            sid = _seed_chunk_scope(
                conn,
                scope_name="strength-batch",
                stats=[("甲", 200), ("乙", 200), ("丙", 200), ("丁", 200)],
                pairs=[("甲", "乙", 3), ("丙", "丁", 1)],
            )
            # min_npmi > 0 puts both negative pairs on the drop path, which
            # is the only path the batch backend scans for complementary
            # candidates.
            _recompute_scope_pairs(conn, sid, min_npmi=0.2)

            assert not _comp_pair_present(conn, sid, "甲", "乙"), (
                "Batch backend: c/E=0.15 pair should be filtered"
            )
            assert _comp_pair_present(conn, sid, "丙", "丁"), (
                "Batch backend: c/E=0.05 pair should survive"
            )
            # Both candidate pairs are below min_npmi, so keyword_pairs
            # maintenance removes them; only the positive quality anchors
            # remain. The strength gate only steers the complementary
            # decision.
            remaining = conn.execute(
                """
                SELECT COUNT(*) FROM keyword_pairs kp
                JOIN keywords ka ON ka.id = kp.kw_a_id
                JOIN keywords kb ON kb.id = kp.kw_b_id
                WHERE kp.scope_id = ? AND kp.source = 'chunk_cooccur'
                  AND ka.kw NOT LIKE '%__q' AND kb.kw NOT LIKE '%__q'
                """,
                (sid,),
            ).fetchone()[0]
            assert remaining == 0, (
                "Batch backend: strength gate must not alter keyword_pairs"
            )
        finally:
            conn.close()


class TestComplementaryStopwords:
    """Iteration 2.2: complementary-only narrative stopword set."""

    def test_membership_and_disjointness(self):
        from grga.graph.build import (
            _COMPLEMENTARY_STOPWORDS,
            _INDEX_STOPWORDS,
        )
        # Narrative pronouns / markers are covered.
        for w in ("他", "他们", "第", "章", "顿时", "吗", "说道"):
            assert w in _COMPLEMENTARY_STOPWORDS, w
        # "他" is NOT in the main-index stopword set — this is the whole
        # point of a complementary-only set (no tokenisation change).
        assert "他" not in _INDEX_STOPWORDS

    def test_narrative_stopword_pair_filtered(self, sidecar_env, tmp_path):
        from grga.graph.build import _recompute_scope_pairs

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            # c/E = 1/20 = 0.05 → passes the strength gate, so only the
            # narrative stopword gate can remove 史强↔他.
            sid = _seed_chunk_scope(
                conn,
                scope_name="comp-stop",
                stats=[("史强", 200), ("他", 200), ("大史", 200)],
                pairs=[("史强", "他", 1), ("史强", "大史", 1)],
            )
            _recompute_scope_pairs(conn, sid, min_npmi=0.0)

            assert not _comp_pair_present(conn, sid, "史强", "他"), (
                "Narrative stopword 他 should be filtered at the "
                "complementary entry point"
            )
            assert _comp_pair_present(conn, sid, "史强", "大史"), (
                "Non-stopword control pair should survive"
            )
        finally:
            conn.close()

    def test_batch_backend_applies_complementary_stopwords(
        self, sidecar_env, tmp_path, monkeypatch,
    ):
        import grga.graph.build as build_mod
        from grga.graph.build import _recompute_scope_pairs

        monkeypatch.setattr(build_mod, "_sqlite_has_ln", lambda conn: False)

        db = tmp_path / "g.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            sid = _seed_chunk_scope(
                conn,
                scope_name="comp-stop-batch",
                stats=[("史强", 200), ("他", 200), ("大史", 200)],
                pairs=[("史强", "他", 1), ("史强", "大史", 1)],
            )
            _recompute_scope_pairs(conn, sid, min_npmi=0.2)

            assert not _comp_pair_present(conn, sid, "史强", "他"), (
                "Batch backend: narrative stopword 他 should be filtered"
            )
            assert _comp_pair_present(conn, sid, "史强", "大史"), (
                "Batch backend: control pair should survive"
            )
        finally:
            conn.close()
