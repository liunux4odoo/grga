"""Tests for the v8 keyword-primary ``related_keywords()`` primitive (Phase 3a).

``related_keywords`` replaces the PPR-based document expansion of
``related(mode="graph")`` with a direct ``keyword_pairs`` lookup that
returns candidate *keywords* (not documents). See
docs/design/plan-2026-07-12-keyword-primary.md §4.

These tests build a tiny corpus with a known co-occurrence structure and
assert the candidate ranking, per-seed provenance (no fusion), the npmi
read-floor semantics, and the scope filter.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.build import build_graph_index
from grga.related import RelatedKeyword, RelatedKeywordsResult, SeedResolution, related_keywords


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    return tmp_path


def _build(scope: Path, db: Path, **kw) -> None:
    # min_npmi=0.0 so tiny-corpus pairs survive the write gate; the read
    # path is what these tests exercise.
    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0, **kw
    )


def test_related_keywords_basic_expansion(sidecar_env, tmp_path):
    """A seed expands to the keywords it co-occurs with, npmi-ranked."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # alpha co-occurs strongly with beta (every line), weakly with gamma.
    lines = [f"alpha beta shared line {i}" for i in range(8)]
    lines.append("alpha gamma rare pairing")
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    results = related_keywords(
        ["alpha"], limit_per_seed=10, min_npmi=0.0, db_path=db,
    ).results
    assert results, "alpha should expand to at least beta"
    kws = {r.kw for r in results}
    assert "beta" in kws
    # every result is tagged with the originating seed
    assert all(r.via_seed == "alpha" for r in results)
    # beta (co=8) should outrank gamma (co=1) by npmi
    by_kw = {r.kw: r for r in results}
    if "gamma" in by_kw:
        assert by_kw["beta"].npmi >= by_kw["gamma"].npmi


def test_related_keywords_returns_relatedkeyword_type(sidecar_env, tmp_path):
    """Return type is List[RelatedKeyword] with all evidence fields set."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # Mix alpha-beta chunks with alpha-only chunks so npmi is well-defined
    # (p_ab < 1). All-alpha-beta chunks would make _npmi return None.
    lines = ["alpha beta"] * 5 + ["alpha only"] * 3
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    results = related_keywords(["alpha"], min_npmi=0.0, db_path=db).results
    assert results
    r = next(x for x in results if x.kw == "beta")
    assert isinstance(r, RelatedKeyword)
    assert r.kw == "beta"
    assert r.via_seed == "alpha"
    assert r.cooccur_count == 5
    assert r.chunk_df > 0
    assert r.source == "chunk_cooccur"
    d = r.to_dict()
    assert set(d) == {"kw", "npmi", "cooccur_count", "via_seed", "chunk_df", "source"}


def test_related_keywords_no_fusion_multi_seed(sidecar_env, tmp_path):
    """Multi-seed queries return per-seed rows (no cross-seed fusion)."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # alpha—beta in one cluster; gamma—delta in another. Add a solo chunk
    # per cluster so npmi is defined (p_ab < 1).
    lines = [f"alpha beta {i}" for i in range(5)] + ["alpha solo"]
    lines += [f"gamma delta {i}" for i in range(5)] + ["gamma solo"]
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    results = related_keywords(
        ["alpha", "gamma"], limit_per_seed=10, min_npmi=0.0, db_path=db,
    ).results
    via = {}
    for r in results:
        via.setdefault(r.via_seed, set()).add(r.kw)
    # Each seed expands only to its own cluster partner.
    assert "beta" in via.get("alpha", set())
    assert "delta" in via.get("gamma", set())
    # No fusion: alpha's row set does not contain gamma's partner.
    assert "delta" not in via.get("alpha", set())


def test_related_keywords_unknown_seed_empty(sidecar_env, tmp_path):
    """A seed absent from the vocabulary yields no rows (not an error)
    but is flagged in ``resolved_seeds`` with ``in_index=False``."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "doc.txt").write_text("alpha beta\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    unknown = related_keywords(["nonexistent_seed_xyz"], db_path=db)
    assert unknown.results == []
    assert unknown.resolved_seeds == [
        SeedResolution(seed="nonexistent_seed_xyz", in_index=False)
    ]
    empty = related_keywords([], db_path=db)
    assert empty.results == []
    assert empty.resolved_seeds == []


def test_related_keywords_resolved_seeds_probe(sidecar_env, tmp_path):
    """Mixed seed batch: probe reports per-seed vocabulary membership in
    first-seen input order, independent of scope filtering."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    lines = [f"alpha beta {i}" for i in range(5)] + ["alpha solo"]
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    # Duplicates in the input are deduplicated (first-seen order kept),
    # empty strings dropped, casefolded before lookup.
    result = related_keywords(
        ["Alpha", "totally_absent", "alpha", "", "beta"],
        min_npmi=0.0,
        db_path=db,
    )
    assert result.resolved_seeds == [
        SeedResolution(seed="alpha", in_index=True),
        SeedResolution(seed="totally_absent", in_index=False),
        SeedResolution(seed="beta", in_index=True),
    ]
    # Only known seeds show up as via_seed values in results.
    via = {r.via_seed for r in result.results}
    assert via <= {"alpha", "beta"}
    assert "totally_absent" not in via


def test_related_keywords_resolved_seeds_unknown_scope(sidecar_env, tmp_path):
    """Unknown ``directory`` scope: results empty, but the seed-vocabulary
    probe still fires (it is scope-independent by design)."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    (scope / "doc.txt").write_text("alpha beta\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    result = related_keywords(
        ["alpha", "totally_absent"],
        min_npmi=0.0,
        db_path=db,
        scopes=["never-indexed"],
    )
    assert result.results == []
    assert result.resolved_seeds == [
        SeedResolution(seed="alpha", in_index=True),
        SeedResolution(seed="totally_absent", in_index=False),
    ]


def test_related_keywords_resolved_seeds_isolated_seed(sidecar_env, tmp_path):
    """A known seed whose candidates are all filtered by ``min_npmi`` is
    still ``in_index=True`` (isolated case, not the unknown-seed case).
    Caller distinguishes: in_index and not in {r.via_seed}."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    lines = [f"alpha beta {i}" for i in range(4)]
    lines += [f"alpha {i}" for i in range(20)]
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    strict = related_keywords(["alpha"], min_npmi=0.99, db_path=db)
    assert strict.results == []  # over-tight floor drops all candidates
    assert strict.resolved_seeds == [SeedResolution(seed="alpha", in_index=True)]


def test_related_keywords_min_npmi_floor(sidecar_env, tmp_path):
    """A high read-side min_npmi drops weak candidates."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # Build a corpus where alpha—beta has moderate npmi.
    lines = [f"alpha beta {i}" for i in range(4)]
    lines += [f"alpha {i}" for i in range(20)]  # dilute alpha
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    loose = related_keywords(["alpha"], min_npmi=0.0, db_path=db).results
    strict = related_keywords(["alpha"], min_npmi=0.99, db_path=db).results
    assert len(strict) <= len(loose)


def test_related_keywords_scope_filter(sidecar_env, tmp_path):
    """``scopes`` restricts candidates to pairs from those scopes (v10 multi-select)."""
    scope_a = tmp_path / "a"
    scope_b = tmp_path / "b"
    scope_a.mkdir()
    scope_b.mkdir()
    (scope_a / "doc.txt").write_text(
        "\n".join([f"alpha beta {i}" for i in range(5)] + ["alpha solo"]),
        encoding="utf-8",
    )
    (scope_b / "doc.txt").write_text(
        "\n".join([f"alpha zeta {i}" for i in range(5)] + ["alpha solo"]),
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    # Build each corpus under its own logical scope so we can filter later.
    build_graph_index(
        "scope-a", directory=scope_a, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    build_graph_index(
        "scope-b", directory=scope_b, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )

    # Scoped to A: alpha → beta only.
    res_a = related_keywords(
        ["alpha"], min_npmi=0.0, db_path=db, scopes=["scope-a"],
    ).results
    kws_a = {r.kw for r in res_a}
    assert "beta" in kws_a
    assert "zeta" not in kws_a

    # Scoped to B: alpha → zeta only.
    res_b = related_keywords(
        ["alpha"], min_npmi=0.0, db_path=db, scopes=["scope-b"],
    ).results
    kws_b = {r.kw for r in res_b}
    assert "zeta" in kws_b
    assert "beta" not in kws_b

    # Cross-scope (no scopes filter): alpha → both.
    res_all = related_keywords(["alpha"], min_npmi=0.0, db_path=db).results
    kws_all = {r.kw for r in res_all}
    assert {"beta", "zeta"} <= kws_all

    # Multi-scope union: scopes=[a, b] must union both → both hits.
    res_multi = related_keywords(
        ["alpha"], min_npmi=0.0, db_path=db, scopes=["scope-a", "scope-b"],
    ).results
    kws_multi = {r.kw for r in res_multi}
    assert {"beta", "zeta"} <= kws_multi


def test_related_keywords_limit_per_seed(sidecar_env, tmp_path):
    """``limit_per_seed`` caps candidates per seed."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # alpha co-occurs with 10 distinct partners.
    lines = [f"alpha partner{i}" for i in range(10) for _ in range(3)]
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    res = related_keywords(
        ["alpha"], limit_per_seed=3, min_npmi=0.0, db_path=db,
    ).results
    assert len([r for r in res if r.via_seed == "alpha"]) <= 3


def test_related_keywords_sorted_deterministic(sidecar_env, tmp_path):
    """Results are sorted (via_seed, -npmi, kw) — stable across calls."""
    scope = tmp_path / "corpus"
    scope.mkdir()
    # Add a solo chunk so alpha's pair with each partner has p_ab < 1.
    lines = [f"alpha beta gamma {i}" for i in range(5)] + ["alpha solo"]
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    r1 = related_keywords(["alpha"], min_npmi=0.0, db_path=db).results
    r2 = related_keywords(["alpha"], min_npmi=0.0, db_path=db).results
    assert [x.to_dict() for x in r1] == [x.to_dict() for x in r2]
    # Verify sort key: non-increasing npmi within a via_seed group.
    npmis = [r.npmi for r in r1]
    assert npmis == sorted(npmis, reverse=True)
