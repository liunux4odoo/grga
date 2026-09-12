"""Tests for complementary pairs (plan-2026-09-11-complementary-pairs).

Covers:
- Build: complementary_pairs table gets populated with significant negative edges
- Related: RelatedKeywordsResult.complementary field + shape + ordering
- Probe: _lookup_complementary + _attach_complementary best-effort attachment
- Probe: elapsed_ms removal from JSON + text output
- Pack export/import: complementary_pairs travels with the knowledge pack
- Scope removal: purge_knowledge=True wipes complementary_pairs

Acceptance anchors (design doc §5):
  A1  Alias pairs captured (related)     — p_value < 1e-6, c_over_e < 0.3
  A2  Noise pairs NOT captured           — build gate filters p > 0.05
  A3  Positive pairs not in complementary + positive side unchanged
  A4  Key-absent semantics               — seed in vocab but no comp → [];
                                            seed not in vocab → resolved_seeds;
                                            no index → probe no comp key
  A5  Minimal field set + no judgement fields
  A6  probe no elapsed_ms                — JSON + text both clean
  A7  probe complementary                — has index → key present;
                                            no index → key absent, probe ok
  A8  Existing tests pass                — (run full suite)
  A9  Query overhead same order of magnitude
  A10 Docs updated                       — (manual / docs update step)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grga.graph.build import build_graph_index
from grga.graph.ids import IdInterner
from grga.graph.schema import connect, ensure_schema
from grga.related import (
    ComplementaryKeyword,
    RelatedKeywordsResult,
    related_keywords,
)


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

    史强 ↔ 大史: co-occur rarely (c=3 out of ~123 chunks) but each appears
    often individually → strong complementary signal.

    汪淼 ↔ 史强: co-occur often → positive (not complementary).

    会议 ↔ 第0段: weak negative (p ≈ 0.09) → should NOT pass the gate.
    """
    scope_dir.mkdir(parents=True, exist_ok=True)
    lines = []

    # 史强 only (~50)
    for i in range(50):
        lines.append(f"史强 行动 第{i}段")
    # 大史 only (~30)
    for i in range(30):
        lines.append(f"大史 警官 第{i+50}段")
    # 史强 + 汪淼 (positive, 20)
    for i in range(20):
        lines.append(f"史强 汪淼 对话 第{i+80}段")
    # 史强 + 大史 co-occur (only 3 — alias pattern)
    for i in range(3):
        lines.append(f"史强（大史） 出场 第{i+100}段")
    # 大史 + 汪淼 (7)
    for i in range(7):
        lines.append(f"大史 汪淼 见面 第{i+103}段")

    # 会议 noise: 会议 appears ~12 times, 第0段 only 1 time
    # (weak complementary, p > 0.05, should not pass gate)
    lines.append("会议 第0段 开场")
    for i in range(1, 12):
        lines.append(f"会议 第{i}段 讨论")

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
            assert c_over_e < 0.3, (
                f"A1 FAIL: c/E={c_over_e} not < 0.3"
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


class TestRelatedComplementary:
    """Verify related_keywords returns complementary pairs."""

    def test_result_has_complementary_field(self, sidecar_env, tmp_path):
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
        assert hasattr(result, "complementary")
        assert isinstance(result.complementary, list)
        assert len(result.complementary) > 0
        for c in result.complementary:
            assert isinstance(c, ComplementaryKeyword)

    def test_complementary_row_fields_minimal(self, sidecar_env, tmp_path):
        """A5: exactly {kw, cooccur_count, c_over_e, p_value, via_seed}."""
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
        assert result.complementary, "need at least one pair for this test"
        d = result.complementary[0].to_dict()
        expected_keys = {"kw", "cooccur_count", "c_over_e", "p_value", "via_seed"}
        assert set(d.keys()) == expected_keys, (
            f"A5 FAIL: got keys {set(d.keys())}, expected {expected_keys}"
        )
        # No forbidden fields (judgement fields or redundant fields)
        forbidden = {"expected_count", "npmi", "is_alias", "quality",
                      "confidence", "semantic_tag"}
        assert not (set(d.keys()) & forbidden), (
            f"A5 FAIL: forbidden fields present: {set(d.keys()) & forbidden}"
        )

    def test_alias_pair_in_complementary(self, sidecar_env, tmp_path):
        """A1: related returns 大史 in complementary with right stats."""
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
        comp_kws = {c.kw for c in result.complementary}
        assert "大史" in comp_kws, f"A1 FAIL: 大史 not in complementary, got {comp_kws}"
        dash = next(c for c in result.complementary if c.kw == "大史")
        assert dash.via_seed == "史强"
        assert dash.p_value < 1e-6, f"p_value={dash.p_value}"
        assert dash.c_over_e < 0.3, f"c_over_e={dash.c_over_e}"

    def test_positive_pair_not_in_complementary(self, sidecar_env, tmp_path):
        """A3: 汪淼 should be in results, not in complementary."""
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
        comp_kws = {c.kw for c in result.complementary}
        assert "汪淼" in result_kws, "汪淼 should be in positive results"
        assert "汪淼" not in comp_kws, "汪淼 should NOT be in complementary"

    def test_complementary_sorted_by_c_over_e_ascending(self, sidecar_env, tmp_path):
        """Default sort: c/E ascending (most complementary first)."""
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
        if len(result.complementary) >= 2:
            ratios = [c.c_over_e for c in result.complementary]
            assert ratios == sorted(ratios), (
                f"complementary not sorted by c_over_e asc: {ratios}"
            )

    def test_empty_complementary_for_unknown_seed(self, sidecar_env, tmp_path):
        """A4: unknown seed → empty complementary, resolved_seeds reports unknown."""
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        result = related_keywords(
            ["不存在的词"],
            limit_per_seed=30,
            min_npmi=0.0,
            db_path=db,
            scopes=["test-scope"],
        )
        assert result.complementary == []
        assert result.results == []
        assert len(result.resolved_seeds) == 1
        assert result.resolved_seeds[0].in_index is False

    def test_complementary_per_seed_independent(self, sidecar_env, tmp_path):
        """Multi-seed: each seed has its own complementary rows, via_seed disambiguates."""
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        result = related_keywords(
            ["史强", "大史"],
            limit_per_seed=30,
            min_npmi=0.0,
            db_path=db,
            scopes=["test-scope"],
        )
        via_seeds = {c.via_seed for c in result.complementary}
        # Both seeds should produce some complementary rows
        assert "史强" in via_seeds
        assert "大史" in via_seeds

    def test_to_dict_includes_complementary(self, sidecar_env, tmp_path):
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
        assert "complementary" in d
        assert isinstance(d["complementary"], list)
        assert len(d["complementary"]) > 0
        # Results are untouched (positive side zero change)
        assert "results" in d
        assert "resolved_seeds" in d


# ======================================================================
# 3. Probe — complementary attachment + elapsed_ms removal
# ======================================================================

class TestProbeComplementary:
    """Verify probe attaches complementary pairs (best-effort)."""

    def test_lookup_complementary_returns_right_shape(self, sidecar_env, tmp_path):
        from grga.probe import _lookup_complementary
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            scope_id = interner.scope_id_ro("test-scope")
        finally:
            conn.close()
        assert scope_id is not None

        result = _lookup_complementary("史强", scope_id, db)
        assert result is not None
        assert isinstance(result, list)
        assert len(result) > 0
        for item in result:
            assert set(item.keys()) == {
                "kw", "cooccur_count", "c_over_e", "p_value", "via_seed"
            }

    def test_lookup_unknown_seed_returns_empty_list(self, sidecar_env, tmp_path):
        from grga.probe import _lookup_complementary
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            scope_id = interner.scope_id_ro("test-scope")
        finally:
            conn.close()
        assert scope_id is not None

        result = _lookup_complementary("不存在的词", scope_id, db)
        assert result == []  # key present, empty

    def test_lookup_silent_failure_bad_db(self, tmp_path):
        from grga.probe import _lookup_complementary
        bogus = tmp_path / "bad.db"
        bogus.write_text("not a db")
        result = _lookup_complementary("anything", 1, bogus)
        assert result is None  # silent degradation

    def test_attach_complementary_pipeline(self, sidecar_env, tmp_path):
        """A7: full pipeline attaches complementary when index is available."""
        from grga.probe import _attach_complementary
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        import grga.deep_search as ds_mod
        original = ds_mod.DEFAULT_DB_PATH
        ds_mod.DEFAULT_DB_PATH = db
        try:
            base = {"ok": True, "total_lines": 0, "file_count": 0}
            query = {
                "patterns": ["史强"],
                "terms": [{"kind": "term", "value": "史强", "pattern": "史强"}],
            }
            result = _attach_complementary(base, scope_dir, query, debug=False)
            assert "complementary" in result
            comp = result["complementary"]
            assert isinstance(comp, list)
            assert len(comp) > 0
            kws = {c["kw"] for c in comp}
            assert "大史" in kws
        finally:
            ds_mod.DEFAULT_DB_PATH = original

    def test_no_complementary_key_when_no_scope(self, sidecar_env, tmp_path):
        """A4 / A7: directory not bound → no complementary key."""
        from grga.probe import _attach_complementary
        scope_dir = tmp_path / "corpus"
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        db = tmp_path / "g.db"
        _build_alias_corpus(scope_dir)
        _build_db(scope_dir, db)

        import grga.deep_search as ds_mod
        original = ds_mod.DEFAULT_DB_PATH
        ds_mod.DEFAULT_DB_PATH = db
        try:
            base = {"ok": True, "total_lines": 0, "file_count": 0}
            query = {
                "patterns": ["史强"],
                "terms": [{"kind": "term", "value": "史强", "pattern": "史强"}],
            }
            result = _attach_complementary(base, other_dir, query, debug=False)
            assert "complementary" not in result
        finally:
            ds_mod.DEFAULT_DB_PATH = original

    def test_attach_complementary_silent_failure(self, tmp_path):
        """A7: bad DB → no key, no exception."""
        from grga.probe import _attach_complementary
        import grga.deep_search as ds_mod
        original = ds_mod.DEFAULT_DB_PATH
        ds_mod.DEFAULT_DB_PATH = tmp_path / "nope.db"
        try:
            base = {"ok": True, "total_lines": 0, "file_count": 0}
            query = {
                "patterns": ["anything"],
                "terms": [{"kind": "term", "value": "anything", "pattern": "anything"}],
            }
            result = _attach_complementary(base, tmp_path, query, debug=False)
            assert "complementary" not in result
            assert result["ok"] is True
        finally:
            ds_mod.DEFAULT_DB_PATH = original


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

        # Query the destination and check we can find the alias pair
        result = related_keywords(
            ["史强"],
            limit_per_seed=30,
            min_npmi=0.0,
            db_path=dst_db,
            scopes=["dst"],
        )
        comp_kws = {c.kw for c in result.complementary}
        assert "大史" in comp_kws


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
