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
    assert set(d) == {
        "kw", "npmi", "cooccur_count", "via_seed", "chunk_df", "source",
        "scopes",
    }
    # v3.1 (plan-2026-07-14 §5.3): scopes carries the query-range scope
    # names for this candidate. Single-scope build → singleton list.
    assert d["scopes"] == ["test-scope"]


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


def test_related_keywords_scopes_field_multi_scope(sidecar_env, tmp_path):
    """v3.1: ``RelatedKeyword.scopes`` tells the caller which scopes carry
    each candidate (plan-2026-07-14 §5.3, decision D14).

    Setup: alpha+beta lives in both scopes; alpha+zeta only in scope-a;
    alpha+omega only in scope-b. A cross-scope query must attribute each
    candidate to its correct scope list.
    """
    scope_a = tmp_path / "a"
    scope_b = tmp_path / "b"
    scope_a.mkdir()
    scope_b.mkdir()
    (scope_a / "doc.txt").write_text(
        "\n".join(
            [f"alpha beta shared line {i}" for i in range(6)]
            + [f"alpha zeta line {i}" for i in range(3)]
            + ["alpha solo"]
        ),
        encoding="utf-8",
    )
    (scope_b / "doc.txt").write_text(
        "\n".join(
            [f"alpha beta shared line {i}" for i in range(6)]
            + [f"alpha omega line {i}" for i in range(3)]
            + ["alpha solo"]
        ),
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    build_graph_index(
        "scope-a", directory=scope_a, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    build_graph_index(
        "scope-b", directory=scope_b, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )

    # Cross-scope query: no scope filter → each candidate carries its
    # actual scope list.
    result = related_keywords(
        ["alpha"], min_npmi=0.0, db_path=db,
    ).results
    by_kw = {r.kw: r for r in result}
    # beta appears in both scopes → both names.
    assert "beta" in by_kw
    assert set(by_kw["beta"].scopes) == {"scope-a", "scope-b"}
    # zeta only in scope-a.
    if "zeta" in by_kw:
        assert by_kw["zeta"].scopes == ["scope-a"]
    # omega only in scope-b.
    if "omega" in by_kw:
        assert by_kw["omega"].scopes == ["scope-b"]

    # Single-scope filter: scopes list is a singleton of the filter.
    only_a = related_keywords(
        ["alpha"], min_npmi=0.0, db_path=db, scopes=["scope-a"],
    ).results
    for r in only_a:
        assert r.scopes == ["scope-a"]

    # Multi-scope filter: same as cross-scope but scoped down.
    both = related_keywords(
        ["alpha"], min_npmi=0.0, db_path=db, scopes=["scope-a", "scope-b"],
    ).results
    beta_both = next((r for r in both if r.kw == "beta"), None)
    assert beta_both is not None
    assert set(beta_both.scopes) == {"scope-a", "scope-b"}


def test_related_keywords_scopes_sorted_deterministic(sidecar_env, tmp_path):
    """``scopes`` list is sorted ascending — stable ``--json`` output."""
    scope_z = tmp_path / "z"
    scope_a = tmp_path / "a"
    scope_z.mkdir()
    scope_a.mkdir()
    # Co-occurrence must vary so alpha+beta survives Gate 1 (the glued
    # df_a==df_b==co degenerate case is dropped). gamma dilutes beta.
    text = "\n".join(
        [f"alpha beta line {i}" for i in range(6)]
        + [f"beta gamma line {i}" for i in range(2)]
        + ["alpha solo"]
    )
    (scope_z / "doc.txt").write_text(text, encoding="utf-8")
    (scope_a / "doc.txt").write_text(text, encoding="utf-8")
    db = tmp_path / "graph.db"
    build_graph_index(
        "zzz", directory=scope_z, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    build_graph_index(
        "aaa", directory=scope_a, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    result = related_keywords(["alpha"], min_npmi=0.0, db_path=db).results
    # Pick any candidate that surfaced in BOTH scopes (identical corpora,
    # so at least one such candidate exists). Its scopes list must be
    # sorted ascending: ["aaa", "zzz"] (not insertion / build order).
    both = [r for r in result if len(r.scopes) == 2]
    assert both, f"expected a cross-scope candidate, got {[(r.kw, r.scopes) for r in result]}"
    for r in both:
        assert r.scopes == sorted(r.scopes) == ["aaa", "zzz"]


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


def test_related_keywords_scopes_survives_limit_truncation(sidecar_env, tmp_path):
    """Regression for D14: per-SELECT LIMIT must not drop scopes from the
    ``scopes`` field.

    Setup: build one scope with many high-npmi neighbours (they saturate
    the LIMIT window) plus a low-npmi pair (target,seed=alpha,beta);
    build a second scope containing ONLY the same target pair at low
    npmi. In a cross-scope query with a small ``limit_per_seed``, the
    low-npmi row from scope-b would be truncated by the per-SELECT
    LIMIT before the merge dict sees it, so pre-fix
    ``RelatedKeyword.scopes`` for target reported only ["scope-a"] even
    though scope-b memorises the pair. Post-fix, the follow-up query in
    ``_complete_scope_sets`` must recover the full membership.
    """
    from grga.graph.schema import connect, ensure_schema
    from grga.graph.ids import IdInterner

    db = tmp_path / "graph.db"
    conn = connect(db)
    try:
        ensure_schema(conn)
        interner = IdInterner(conn)
        sa = interner.scope_id("scope-a")
        sb = interner.scope_id("scope-b")
        seed = interner.kw_id("alpha")
        target = interner.kw_id("beta")
        # 20 filler high-npmi pairs in scope-a so LIMIT=5 truncates target
        # from scope-b (which has a lower npmi and would rank below).
        for i in range(20):
            filler = interner.kw_id(f"filler{i}")
            a, b = sorted([seed, filler])
            conn.execute(
                "INSERT INTO keyword_pairs(scope_id, kw_a_id, kw_b_id, "
                "source, cooccur_count, npmi, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, '')",
                (sa, a, b, "chunk_cooccur", 10, 0.80),
            )
        # target: high npmi in scope-a, low npmi in scope-b
        a, b = sorted([seed, target])
        conn.execute(
            "INSERT INTO keyword_pairs(scope_id, kw_a_id, kw_b_id, source, "
            "cooccur_count, npmi, updated_at) VALUES(?, ?, ?, ?, ?, ?, '')",
            (sa, a, b, "chunk_cooccur", 5, 0.90),
        )
        conn.execute(
            "INSERT INTO keyword_pairs(scope_id, kw_a_id, kw_b_id, source, "
            "cooccur_count, npmi, updated_at) VALUES(?, ?, ?, ?, ?, ?, '')",
            (sb, a, b, "chunk_cooccur", 5, 0.25),
        )
        conn.commit()
    finally:
        conn.close()

    # Cross-scope query with a small LIMIT.
    result = related_keywords(
        ["alpha"], limit_per_seed=5, min_npmi=0.2, db_path=db,
    ).results
    target_row = next((r for r in result if r.kw == "beta"), None)
    assert target_row is not None, (
        "target should survive as top-ranked candidate from scope-a"
    )
    assert set(target_row.scopes) == {"scope-a", "scope-b"}, (
        f"D14 regression: expected both scopes, got {target_row.scopes}"
    )


def test_related_keywords_whitespace_arg_splits_when_no_phrase_evidence(sidecar_env, tmp_path):
    """A whitespace-containing arg with no phrase-level evidence is split
    into multiple seeds (the CJK multi-seed case: ``"楚光 小柒"`` → two).

    The ngram detector never produces space-joined CJK phrases, so a
    string like ``"alpha beta"`` passed as a single quoted arg should be
    split into its component tokens — provided the exact phrase is NOT
    attested in ``keyword_docs``. (If it IS attested, we keep it as a
    phrase — see the next test.)
    """
    scope = tmp_path / "corpus"
    scope.mkdir()
    lines = ["alpha beta gamma line"] * 5 + ["alpha solo", "beta solo", "gamma solo"]
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    # Single arg with whitespace → should be split into ["alpha", "beta"].
    # Both are known individually; both expand to neighbours.
    result = related_keywords(
        ["alpha beta"], limit_per_seed=10, min_npmi=0.0, db_path=db,
    )
    # resolved_seeds should show two seeds, both in index.
    assert {s.seed for s in result.resolved_seeds} == {"alpha", "beta"}
    assert all(s.in_index for s in result.resolved_seeds)
    # Results should include candidates from BOTH seeds.
    via_seeds = {r.via_seed for r in result.results}
    assert "alpha" in via_seeds
    assert "beta" in via_seeds


def test_related_keywords_whitespace_arg_kept_as_phrase_when_attested(sidecar_env, tmp_path):
    """When the exact whitespace-joined string HAS doc-level evidence
    (e.g. an ASCII phrase re-joined by the ngram detector), keep it as a
    single phrase seed instead of splitting.
    """
    scope = tmp_path / "corpus"
    scope.mkdir()
    # Use a known multi-token phrase. The build-time ngram detector
    # produces space-joined phrases; we simulate that by inserting a
    # phrase-level keyword_docs row manually.
    lines = ["first second shared"] * 5 + ["first only", "second only"]
    (scope / "doc.txt").write_text("\n".join(lines), encoding="utf-8")
    db = tmp_path / "graph.db"
    _build(scope, db)

    from grga.graph.schema import connect, ensure_schema

    conn = connect(db)
    try:
        ensure_schema(conn)
        # If the ngram detector already produced "first second" as a
        # phrase, then ``keyword_docs`` already has it and this test is
        # already covered. Otherwise, manually inject phrase-level
        # evidence to simulate the ASCII-phrase rejoin case.
        row = conn.execute(
            "SELECT 1 FROM keyword_docs kd "
            "JOIN keywords k ON k.id = kd.kw_id "
            "WHERE k.kw = ? LIMIT 1",
            ("first second",),
        ).fetchone()
        if row is None:
            # Inject: add the phrase to keywords + keyword_docs + a pair
            # with a known neighbour so we can observe it expanding.
            conn.execute("INSERT OR IGNORE INTO keywords(kw) VALUES(?)", ("first second",))
            kw_id = conn.execute("SELECT id FROM keywords WHERE kw = ?", ("first second",)).fetchone()[0]
            # Grab scope_id and doc_id.
            scope_id = conn.execute("SELECT id FROM scopes WHERE name = ?", ("test-scope",)).fetchone()[0]
            doc_id = conn.execute("SELECT id FROM docs WHERE scope_id = ? AND doc_path = ?", (scope_id, "doc.txt")).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO keyword_docs(scope_id, kw_id, doc_id, occurrence_count, chunk_count) "
                "VALUES(?, ?, ?, 5, 3)",
                (scope_id, kw_id, doc_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO keyword_stats(scope_id, kw_id, chunk_df, doc_df, total_occurrences) "
                "VALUES(?, ?, 3, 1, 5)",
                (scope_id, kw_id),
            )
            gamma_id = conn.execute("SELECT id FROM keywords WHERE kw = ?", ("gamma",)).fetchone()
            if gamma_id is None:
                conn.execute("INSERT OR IGNORE INTO keywords(kw) VALUES(?)", ("gamma",))
                gamma_id = conn.execute("SELECT id FROM keywords WHERE kw = ?", ("gamma",)).fetchone()[0]
            else:
                gamma_id = gamma_id[0]
            a, b = sorted([kw_id, gamma_id])
            conn.execute(
                "INSERT OR IGNORE INTO keyword_pairs(scope_id, kw_a_id, kw_b_id, source, cooccur_count, npmi) "
                "VALUES(?, ?, ?, 'chunk_cooccur', 3, 0.5)",
                (scope_id, a, b),
            )
            conn.commit()
    finally:
        conn.close()

    result = related_keywords(
        ["first second"], limit_per_seed=10, min_npmi=0.0, db_path=db,
    )
    # The phrase should be treated as a single seed, not split.
    assert len(result.resolved_seeds) == 1
    assert result.resolved_seeds[0].seed == "first second"
    assert result.resolved_seeds[0].in_index is True
    # All candidates should be via the phrase seed.
    assert all(r.via_seed == "first second" for r in result.results)
