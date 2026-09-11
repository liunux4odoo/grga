"""Tests for the ``grga probe`` neighbours feature (plan-2026-09-11).

Covers:
- ``IdInterner.resolve_scope_for_directory`` — directory → scope_id prefix match
- ``grga.probe._try_resolve_scope_for_directory`` — silent failure wrapper
- ``grga.probe._lookup_neighbors`` — top-N ``{kw, npmi}`` extraction
- ``grga.probe._attach_neighbors`` — end-to-end best-effort attachment

Acceptance matrix (§5 / §6 of the design doc):
  A1  has index + seed in graph  →  neighbors present, {kw,npmi}, npmi DESC, ≤5
  A2  has index + seed unknown   →  neighbors: [] (key present, empty)
  A3  no index / no scope hit    →  no neighbors key at all
  A4  --path-only                →  (caller responsibility; not tested here)
  A5  DB unavailable             →  no neighbors key, no exception
  A6  multi-keyword              →  only first seed reflected
  A7  human output               →  (CLI-layer; tested via unit-level structure)
  A8  existing tests pass        →  (run full suite)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grga.graph.build import build_graph_index
from grga.graph.ids import IdInterner
from grga.graph.schema import connect, ensure_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    # Use min_npmi=0.0 in tests so tiny-corpus pairs survive the read
    # gate (they all have low npmi on a tiny corpus).
    monkeypatch.setenv("GRGA_PPR_MIN_NPMI", "0.0")
    return tmp_path


def _build_corpus(scope_dir: Path) -> None:
    """Build a tiny corpus with a known co-occurrence structure.

    alpha ↔ beta (strong, 8 co-occurrences)
    alpha ↔ gamma (weak, 1 co-occurrence)
    beta  ↔ delta (medium, 4 co-occurrences)
    """
    scope_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    lines += [f"alpha beta shared line {i}" for i in range(8)]
    lines.append("alpha gamma rare pairing")
    lines += [f"beta delta medium pair {i}" for i in range(4)]
    lines += ["alpha only chunk"] * 3
    lines += ["beta only chunk"] * 2
    (scope_dir / "doc.txt").write_text("\n".join(lines), encoding="utf-8")


def _build_db(scope_dir: Path, db_path: Path, scope_name: str = "test-scope") -> None:
    """Build a graph index for *scope_dir* at *db_path*."""
    build_graph_index(
        scope_name,
        directory=scope_dir,
        db_path=db_path,
        min_pair_count=1,
        min_npmi=0.0,
    )


# ======================================================================
# 1. IdInterner.resolve_scope_for_directory
# ======================================================================

class TestResolveScopeForDirectory:
    """Directory → scope_id forward lookup (prefix match)."""

    def test_exact_match(self, sidecar_env, tmp_path):
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_corpus(scope_dir)
        _build_db(scope_dir, db, scope_name="s1")

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.resolve_scope_for_directory(str(scope_dir))
            assert sid is not None
            assert interner.scope_name(sid) == "s1"
        finally:
            conn.close()

    def test_subdirectory_match(self, sidecar_env, tmp_path):
        """A subdirectory of the bound directory must resolve to the same scope."""
        scope_dir = tmp_path / "corpus"
        sub = scope_dir / "subdir" / "deeper"
        sub.mkdir(parents=True)
        db = tmp_path / "g.db"
        _build_corpus(scope_dir)
        _build_db(scope_dir, db, scope_name="s1")

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.resolve_scope_for_directory(str(sub))
            assert sid is not None
            assert interner.scope_name(sid) == "s1"
        finally:
            conn.close()

    def test_no_match(self, sidecar_env, tmp_path):
        scope_dir = tmp_path / "corpus"
        other = tmp_path / "elsewhere"
        other.mkdir()
        db = tmp_path / "g.db"
        _build_corpus(scope_dir)
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            assert interner.resolve_scope_for_directory(str(other)) is None
        finally:
            conn.close()

    def test_most_specific_wins(self, sidecar_env, tmp_path):
        """When parent and child both bind, the deeper (longer) one wins."""
        parent = tmp_path / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        db = tmp_path / "g.db"

        # Build two scopes, one for parent and one for child directory.
        (parent / "doc.txt").write_text("alpha beta\n", encoding="utf-8")
        _build_db(parent, db, scope_name="parent-scope")
        (child / "doc.txt").write_text("gamma delta\n", encoding="utf-8")
        _build_db(child, db, scope_name="child-scope")

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            sid = interner.resolve_scope_for_directory(str(child))
            assert sid is not None
            assert interner.scope_name(sid) == "child-scope"
        finally:
            conn.close()

    def test_no_bindings_at_all(self, tmp_path):
        """Empty scope_directories table → None."""
        db = tmp_path / "empty.db"
        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            assert interner.resolve_scope_for_directory("/anything") is None
        finally:
            conn.close()


# ======================================================================
# 2. _try_resolve_scope_for_directory — silent failure
# ======================================================================

class TestTryResolveScope:
    """A5: any DB-level error → None, no exception."""

    def test_nonexistent_db_file(self, tmp_path):
        from grga.probe import _try_resolve_scope_for_directory
        result = _try_resolve_scope_for_directory(
            "/some/dir", tmp_path / "does_not_exist.db"
        )
        assert result is None

    def test_non_db_file(self, tmp_path):
        from grga.probe import _try_resolve_scope_for_directory
        bogus = tmp_path / "not_a_db.txt"
        bogus.write_text("definitely not sqlite")
        result = _try_resolve_scope_for_directory("/some/dir", bogus)
        assert result is None


# ======================================================================
# 3. _lookup_neighbors — shape and ordering
# ======================================================================

class TestLookupNeighbors:
    """A1 / A2: top-5 {kw, npmi} shape, npmi DESC, empty list for unknown seed."""

    def test_returns_kw_and_npmi_only(self, sidecar_env, tmp_path):
        from grga.probe import _lookup_neighbors
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_corpus(scope_dir)
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            scope_id = interner.scope_id_ro("test-scope")
        finally:
            conn.close()
        assert scope_id is not None

        result = _lookup_neighbors("alpha", scope_id, db)
        assert result is not None
        assert isinstance(result, list)
        assert len(result) > 0
        # A1: each item has exactly kw + npmi
        for item in result:
            assert set(item.keys()) == {"kw", "npmi"}
            assert isinstance(item["kw"], str)
            assert isinstance(item["npmi"], float)

    def test_sorted_by_npmi_desc(self, sidecar_env, tmp_path):
        from grga.probe import _lookup_neighbors
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_corpus(scope_dir)
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            scope_id = interner.scope_id_ro("test-scope")
        finally:
            conn.close()

        result = _lookup_neighbors("alpha", scope_id, db)
        assert result is not None and len(result) >= 2
        nmpis = [r["npmi"] for r in result]
        assert nmpis == sorted(nmpis, reverse=True), (
            f"neighbors must be npmi DESC, got {nmpis}"
        )

    def test_limit_is_5(self, sidecar_env, tmp_path):
        from grga.probe import _lookup_neighbors, _PROBE_NEIGHBORS_LIMIT
        # Build a corpus with many distinct neighbours for "hub".
        scope_dir = tmp_path / "corpus"
        scope_dir.mkdir()
        lines = []
        for i in range(20):
            lines.append(f"hub word{i} cooccurrence {i}")
        lines += ["hub only"] * 5
        (scope_dir / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
        db = tmp_path / "g.db"
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            scope_id = interner.scope_id_ro("test-scope")
        finally:
            conn.close()

        result = _lookup_neighbors("hub", scope_id, db)
        assert result is not None
        assert len(result) <= _PROBE_NEIGHBORS_LIMIT
        assert _PROBE_NEIGHBORS_LIMIT == 5

    def test_unknown_seed_returns_empty_list(self, sidecar_env, tmp_path):
        """A2: seed not in vocabulary → [] (key present, empty list)."""
        from grga.probe import _lookup_neighbors
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_corpus(scope_dir)
        _build_db(scope_dir, db)

        conn = connect(db)
        try:
            ensure_schema(conn)
            interner = IdInterner(conn)
            scope_id = interner.scope_id_ro("test-scope")
        finally:
            conn.close()

        result = _lookup_neighbors("nonexistent_xyzzy", scope_id, db)
        assert result == []

    def test_silent_failure_bad_db(self, tmp_path):
        """A5: bad DB → None (not []), no exception."""
        from grga.probe import _lookup_neighbors
        bogus = tmp_path / "bad.db"
        bogus.write_text("nope")
        result = _lookup_neighbors("anything", 1, bogus)
        assert result is None


# ======================================================================
# 4. _attach_neighbors — end-to-end
# ======================================================================

class TestAttachNeighbors:
    """Integration test for the full neighbours attach pipeline."""

    def test_attaches_neighbors_when_scope_matches(self, sidecar_env, tmp_path):
        """A1: full pipeline — corpus with index → neighbors attached."""
        from grga.probe import _attach_neighbors
        from grga.deep_search import DEFAULT_DB_PATH
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_corpus(scope_dir)
        _build_db(scope_dir, db)

        # Monkeypatch DEFAULT_DB_PATH so we use the test DB.
        import grga.probe as probe_mod
        import grga.deep_search as ds_mod
        original = ds_mod.DEFAULT_DB_PATH
        ds_mod.DEFAULT_DB_PATH = db
        try:
            base = {"ok": True, "total_lines": 0, "file_count": 0}
            query = {"patterns": ["alpha"], "terms": [{"kind": "term", "value": "alpha", "pattern": "alpha"}]}
            result = _attach_neighbors(base, scope_dir, query, debug=False)
            assert "neighbors" in result
            neighbors = result["neighbors"]
            assert isinstance(neighbors, list)
            assert len(neighbors) > 0
            for n in neighbors:
                assert set(n.keys()) == {"kw", "npmi"}
            # Sorted desc by npmi
            nmpis = [n["npmi"] for n in neighbors]
            assert nmpis == sorted(nmpis, reverse=True)
        finally:
            ds_mod.DEFAULT_DB_PATH = original

    def test_no_neighbors_key_when_no_scope(self, sidecar_env, tmp_path):
        """A3: directory not bound to any scope → no neighbors key."""
        from grga.probe import _attach_neighbors
        from grga.deep_search import DEFAULT_DB_PATH
        scope_dir = tmp_path / "corpus"
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        db = tmp_path / "g.db"
        _build_corpus(scope_dir)
        _build_db(scope_dir, db)

        import grga.deep_search as ds_mod
        original = ds_mod.DEFAULT_DB_PATH
        ds_mod.DEFAULT_DB_PATH = db
        try:
            base = {"ok": True, "total_lines": 0, "file_count": 0}
            query = {"patterns": ["alpha"], "terms": [{"kind": "term", "value": "alpha", "pattern": "alpha"}]}
            result = _attach_neighbors(base, other_dir, query, debug=False)
            assert "neighbors" not in result
        finally:
            ds_mod.DEFAULT_DB_PATH = original

    def test_multi_keyword_uses_first_seed(self, sidecar_env, tmp_path):
        """A6: only the first seed contributes neighbors."""
        from grga.probe import _attach_neighbors
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_corpus(scope_dir)
        _build_db(scope_dir, db)

        import grga.deep_search as ds_mod
        original = ds_mod.DEFAULT_DB_PATH
        ds_mod.DEFAULT_DB_PATH = db
        try:
            base = {"ok": True, "total_lines": 0, "file_count": 0}
            # First seed = alpha (has neighbours), second = nonexistent_xyzzy
            query = {
                "patterns": ["alpha", "nonexistent_xyzzy"],
                "terms": [
                    {"kind": "term", "value": "alpha", "pattern": "alpha"},
                    {"kind": "term", "value": "nonexistent_xyzzy", "pattern": "nonexistent_xyzzy"},
                ],
            }
            result = _attach_neighbors(base, scope_dir, query, debug=False)
            assert "neighbors" in result
            # If it were using the second seed, neighbors would be []
            assert len(result["neighbors"]) > 0
            # And all neighbours should belong to "alpha" (not xyzzy)
            kws = {n["kw"] for n in result["neighbors"]}
            assert "beta" in kws or "gamma" in kws
        finally:
            ds_mod.DEFAULT_DB_PATH = original

    def test_known_seed_no_neighbors_gives_empty_list(self, sidecar_env, tmp_path):
        """A2: seed in vocab but no qualifying neighbours → [] (key present, empty).

        We build with a high ``min_npmi`` so that weak pairs get filtered at
        write time, leaving the seed in the vocabulary (it appears in
        keyword_stats) but with zero surviving pairs.
        """
        from grga.probe import _attach_neighbors
        scope_dir = tmp_path / "corpus"
        scope_dir.mkdir()
        # Just one co-occurrence between alpha and beta → very weak npmi.
        # Build with a high min_npmi threshold so the pair doesn't survive.
        (scope_dir / "doc.txt").write_text(
            "alpha beta only once\n" + "\n".join(f"alpha alone {i}" for i in range(20))
            + "\n" + "\n".join(f"beta alone {i}" for i in range(20)),
            encoding="utf-8",
        )
        db = tmp_path / "g.db"
        # High write-time npmi floor → the single alpha/beta pair is dropped.
        build_graph_index(
            "test-scope",
            directory=scope_dir,
            db_path=db,
            min_pair_count=1,
            min_npmi=0.8,
        )

        import grga.deep_search as ds_mod
        original = ds_mod.DEFAULT_DB_PATH
        ds_mod.DEFAULT_DB_PATH = db
        try:
            base = {"ok": True, "total_lines": 0, "file_count": 0}
            query = {"patterns": ["alpha"], "terms": [{"kind": "term", "value": "alpha", "pattern": "alpha"}]}
            result = _attach_neighbors(base, scope_dir, query, debug=False)
            assert "neighbors" in result
            assert result["neighbors"] == []
        finally:
            ds_mod.DEFAULT_DB_PATH = original

    def test_silent_failure_missing_db(self, tmp_path):
        """A5: DB doesn't exist → no neighbors key, no exception."""
        from grga.probe import _attach_neighbors
        import grga.deep_search as ds_mod
        original = ds_mod.DEFAULT_DB_PATH
        ds_mod.DEFAULT_DB_PATH = tmp_path / "nope.db"
        try:
            base = {"ok": True, "total_lines": 0, "file_count": 0}
            query = {"patterns": ["anything"], "terms": [{"kind": "term", "value": "anything", "pattern": "anything"}]}
            result = _attach_neighbors(base, tmp_path, query, debug=False)
            assert "neighbors" not in result
            assert result["ok"] is True
        finally:
            ds_mod.DEFAULT_DB_PATH = original



    def test_first_seed_from_real_compiled_query(self, sidecar_env, tmp_path):
        """Integration: _first_seed_from_query works with real compiled queries."""
        from grga.probe import _first_seed_from_query
        from grga.search import _compile_search_query

        # Single term
        q1 = _compile_search_query("大史", pattern_mode="auto")
        assert _first_seed_from_query(q1) == "大史"

        # Multi-term (AND)
        q2 = _compile_search_query("大史 汪淼", pattern_mode="auto", implicit_operator="and")
        assert _first_seed_from_query(q2) == "大史"

        # Multi-term (OR)
        q3 = _compile_search_query("大史 || 汪淼", pattern_mode="auto")
        assert _first_seed_from_query(q3) == "大史"

    def test_homology_with_related_keywords(self, sidecar_env, tmp_path):
        """A1 (homology): probe neighbors == related top-5 (kw, npmi).

        Both draw from the same keyword_pairs table with the same sort key
        (npmi DESC).  The top-5 from both must match item-by-item.
        """
        from grga.probe import _attach_neighbors
        from grga.related import related_keywords
        scope_dir = tmp_path / "corpus"
        db = tmp_path / "g.db"
        _build_corpus(scope_dir)
        _build_db(scope_dir, db)

        # Get related top-5
        rel = related_keywords(
            ["alpha"], limit_per_seed=5, min_npmi=0.0, db_path=db,
            scopes=["test-scope"],
        ).results
        rel_pairs = [(r.kw, round(r.npmi, 6)) for r in rel]

        # Get probe neighbors
        import grga.deep_search as ds_mod
        original = ds_mod.DEFAULT_DB_PATH
        ds_mod.DEFAULT_DB_PATH = db
        try:
            base = {"ok": True, "total_lines": 0, "file_count": 0}
            query = {"patterns": ["alpha"], "terms": [{"kind": "term", "value": "alpha", "pattern": "alpha"}]}
            result = _attach_neighbors(base, scope_dir, query, debug=False)
            probe_pairs = [(n["kw"], n["npmi"]) for n in result["neighbors"]]
        finally:
            ds_mod.DEFAULT_DB_PATH = original

        # Same length, same ordering
        assert len(probe_pairs) == len(rel_pairs)
        assert probe_pairs == rel_pairs, (
            f"probe neighbors must match related top-5 exactly\n"
            f"  probe:   {probe_pairs}\n"
            f"  related: {rel_pairs}"
        )
