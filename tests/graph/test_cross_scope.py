"""Cross-scope ``related`` behaviour (user-scoped storage model).

Two indexed scopes A and B share a single DB. These tests pin down the
levers described in ``MIGRATION_user_scoped_db.md`` §3.1:

* ``related`` without ``-d`` spans every scope (A ∪ B).
* ``related -d A`` (graph, default) stays isolated to A.
* ``related -d A --cross-scope`` keeps seeds in A but lets PPR expand into B.
* ``related -d A --mode heading`` crosses to B by default (shared heading).
* ``related -d A --mode heading --same-scope`` reverts to A-only.
"""
from __future__ import annotations

import pytest

from grga.graph.build import build_graph_index
from grga.related import related


@pytest.fixture
def two_scopes(monkeypatch, tmp_path):
    """Index two scopes A and B into one shared DB.

    Graph bridge: ``a1.md`` co-locates ``alpha`` + ``bridgeword``; ``b1.md``
    co-locates ``bridgeword`` + ``betaword``. So PPR seeded on ``alpha`` can
    only reach ``betaword`` (and thus ``b1.md``) when scope filtering is off.

    Heading bridge: ``a_guide.md`` and ``b_guide.md`` both carry a
    ``... > macOS`` leaf heading, but the seed keyword ``seedheadingkw`` only
    lives in ``a_guide.md``.
    """
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    scope_a = tmp_path / "scope_a"
    scope_b = tmp_path / "scope_b"
    scope_a.mkdir()
    scope_b.mkdir()

    (scope_a / "a1.md").write_text(
        "# Alpha doc\n\nalpha alpha bridgeword bridgeword talk.\n",
        encoding="utf-8",
    )
    (scope_a / "a_guide.md").write_text(
        "# 安装指南\n\n## macOS\n\nseedheadingkw seedheadingkw content here.\n",
        encoding="utf-8",
    )
    (scope_b / "b1.md").write_text(
        "# Beta doc\n\nbridgeword bridgeword betaword betaword talk.\n",
        encoding="utf-8",
    )
    (scope_b / "b_guide.md").write_text(
        "# 卸载指南\n\n## macOS\n\nsome unrelated betaword content.\n",
        encoding="utf-8",
    )

    db = tmp_path / "shared.db"
    build_graph_index(scope_a, db_path=db, min_pair_count=1, min_npmi=0.0)
    build_graph_index(scope_b, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"a": scope_a, "b": scope_b, "db": db}


def _docs(results):
    return {r.doc_path for r in results}


def test_global_query_spans_all_scopes(two_scopes):
    results = related(
        ["alpha"],
        mode="graph",
        db_path=two_scopes["db"],
        directory=None,
        limit=20,
    )
    docs = _docs(results)
    assert "a1.md" in docs
    assert "b1.md" in docs, f"global query should reach scope B, got {docs}"


def test_scoped_graph_is_isolated_by_default(two_scopes):
    results = related(
        ["alpha"],
        mode="graph",
        db_path=two_scopes["db"],
        directory=two_scopes["a"],
        limit=20,
    )
    docs = _docs(results)
    assert "a1.md" in docs
    assert "b1.md" not in docs, f"default -d A must not leak scope B, got {docs}"


def test_cross_scope_expands_into_other_scope(two_scopes):
    results = related(
        ["alpha"],
        mode="graph",
        db_path=two_scopes["db"],
        directory=two_scopes["a"],
        cross_scope=True,
        limit=20,
    )
    docs = _docs(results)
    assert "a1.md" in docs
    assert "b1.md" in docs, f"--cross-scope should reach scope B, got {docs}"


def test_cross_scope_seed_must_live_in_scope(two_scopes):
    # ``betaword`` only exists in scope B; seeding it under -d A yields
    # nothing even with --cross-scope (seeds are validated against A).
    results = related(
        ["betaword"],
        mode="graph",
        db_path=two_scopes["db"],
        directory=two_scopes["a"],
        cross_scope=True,
        limit=20,
    )
    assert results == []


def test_heading_mode_crosses_scope_by_default(two_scopes):
    results = related(
        ["seedheadingkw"],
        mode="heading",
        db_path=two_scopes["db"],
        directory=two_scopes["a"],
        limit=20,
    )
    docs = _docs(results)
    assert "b_guide.md" in docs, (
        f"heading mode should cross to scope B on shared 'macOS' leaf, got {docs}"
    )


def test_heading_same_scope_restricts_to_directory(two_scopes):
    results = related(
        ["seedheadingkw"],
        mode="heading",
        db_path=two_scopes["db"],
        directory=two_scopes["a"],
        same_scope=True,
        limit=20,
    )
    docs = _docs(results)
    assert "b_guide.md" not in docs, (
        f"--same-scope must keep heading matches inside scope A, got {docs}"
    )


@pytest.fixture
def shared_seed_scopes(monkeypatch, tmp_path):
    """Two scopes whose seed keyword exists in BOTH, sharing a heading leaf.

    This is the real user scenario the fixture with a unique seed word
    ('seedheadingkw', only in scope A) accidentally sidesteps: a common
    keyword like 'install' lives in every corpus. Both scope A and scope B
    have a doc containing ``sharedkw`` under a ``## macOS`` heading.
    """
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    scope_a = tmp_path / "shared_a"
    scope_b = tmp_path / "shared_b"
    scope_a.mkdir()
    scope_b.mkdir()

    (scope_a / "a_guide.md").write_text(
        "# 安装指南\n\n## macOS\n\nsharedkw sharedkw content.\n",
        encoding="utf-8",
    )
    (scope_b / "b_guide.md").write_text(
        "# 安装教程\n\n## macOS\n\nsharedkw and other stuff.\n",
        encoding="utf-8",
    )

    db = tmp_path / "shared_seed.db"
    build_graph_index(scope_a, db_path=db, min_pair_count=1, min_npmi=0.0)
    build_graph_index(scope_b, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"a": scope_a, "b": scope_b, "db": db}


def test_heading_mode_crosses_when_seed_is_shared(shared_seed_scopes):
    """The regression bug: seed keyword exists in BOTH scopes.

    Caller passes -d A. Expectation:
      - seed doc set = { (A, a_guide.md) } (limited to A)
      - matching heading docs = docs in ANY scope sharing that heading
      - result contains b_guide.md
    """
    results = related(
        ["sharedkw"],
        mode="heading",
        db_path=shared_seed_scopes["db"],
        directory=shared_seed_scopes["a"],
        limit=20,
    )
    docs = _docs(results)
    assert "b_guide.md" in docs, (
        f"heading mode should reach scope B via shared 'macOS' leaf even "
        f"when seed keyword exists in both scopes, got {docs}"
    )
    # Sanity: scope A 的种子文档本身不出现在结果里
    assert "a_guide.md" not in docs


def test_heading_same_scope_with_shared_seed_stays_in_directory(shared_seed_scopes):
    """--same-scope tightens BOTH seed_scope and match_scope to --directory.

    Even though ``sharedkw`` and the ``macOS`` heading exist in scope B too,
    --same-scope must keep the match confined to scope A, yielding nothing
    (A's only doc is the seed itself).
    """
    results = related(
        ["sharedkw"],
        mode="heading",
        db_path=shared_seed_scopes["db"],
        directory=shared_seed_scopes["a"],
        same_scope=True,
        limit=20,
    )
    docs = _docs(results)
    assert "b_guide.md" not in docs, (
        f"--same-scope must keep heading matches inside scope A even with a "
        f"shared seed, got {docs}"
    )


def test_crossed_scopes_preview_reports_other_scope(two_scopes):
    from grga.related import related_graph_with_preview

    payload = related_graph_with_preview(
        ["alpha"],
        db_path=two_scopes["db"],
        directory=two_scopes["a"],
        limit=20,
        preview=True,
    )
    # Isolated results (default) do not include B ...
    assert "b1.md" not in {r.doc_path for r in payload["results"]}
    # ... but the preview tells the caller B is reachable.
    preview = payload["crossed_scopes_preview"]
    assert preview is not None
    assert preview["would_add"] >= 1
    scopes = {entry["scope"] for entry in preview["top_other_scopes"]}
    assert str(two_scopes["b"]) in scopes
    # Every bridge keyword carries the polysemy verdict inline.
    for entry in preview["top_bridge_keywords"]:
        assert "suspected_polysemy" in entry
        assert isinstance(entry["suspected_polysemy"], bool)


@pytest.fixture
def polysemy_bridge_scopes(monkeypatch, tmp_path):
    """Two scopes wired to bridge on a polysemous keyword.

    ``ambientkw`` lives in scope ``ml`` co-occurring with ML vocabulary and
    in scope ``psych`` co-occurring with an entirely disjoint psychology
    vocabulary. Seeding on ``mlseed`` under -d ml should surface
    ``ambientkw`` as a bridge into psych, and the preview should flag it
    as suspected polysemy.
    """
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    ml = tmp_path / "ml"
    psych = tmp_path / "psych"
    ml.mkdir()
    psych.mkdir()

    (ml / "a.md").write_text(
        "# ML\n\nmlseed mlseed ambientkw ambientkw transformer encoder decoder.\n"
        "transformer encoder decoder embedding softmax.\n",
        encoding="utf-8",
    )
    (psych / "b.md").write_text(
        "# Psychology\n\nambientkw ambientkw cognition perception memory arousal.\n"
        "cognition perception memory salience.\n",
        encoding="utf-8",
    )
    db = tmp_path / "shared.db"
    build_graph_index(ml, db_path=db, min_pair_count=1, min_npmi=0.0)
    build_graph_index(psych, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"ml": ml, "psych": psych, "db": db}


def test_preview_includes_polysemy_warning_on_ambient_keyword(polysemy_bridge_scopes):
    """Preview should self-annotate polysemy — no separate `graph inspect` pass.

    ``ambientkw`` co-occurs with disjoint vocabularies in the two scopes;
    the SKILL.md upgrade sequence used to require a manual
    ``grga graph inspect`` per bridge keyword to detect this.
    """
    from grga.related import related_graph_with_preview

    payload = related_graph_with_preview(
        ["mlseed"],
        db_path=polysemy_bridge_scopes["db"],
        directory=polysemy_bridge_scopes["ml"],
        limit=20,
        preview=True,
    )
    preview = payload["crossed_scopes_preview"]
    assert preview is not None
    bridges = {entry["keyword"]: entry for entry in preview["top_bridge_keywords"]}
    assert "ambientkw" in bridges, f"expected ambientkw as bridge, got {bridges}"
    assert bridges["ambientkw"]["suspected_polysemy"] is True


def test_preview_off_by_default(two_scopes):
    """Preview is opt-in: bare related_graph_with_preview does not pay for it."""
    from grga.related import related_graph_with_preview

    payload = related_graph_with_preview(
        ["alpha"],
        db_path=two_scopes["db"],
        directory=two_scopes["a"],
        limit=20,
    )
    assert payload["crossed_scopes_preview"] is None


def test_preview_none_for_global_query(two_scopes):
    from grga.related import related_graph_with_preview

    payload = related_graph_with_preview(
        ["alpha"],
        db_path=two_scopes["db"],
        directory=None,
        limit=20,
        preview=True,
    )
    assert payload["crossed_scopes_preview"] is None


def test_preview_reuses_ppr_reduces_cost(two_scopes):
    """P0-2 revised: --preview must not change the main result list.

    An earlier P0-2 draft merged the two PPRs into a single unrestricted
    pass to save ~380ms per preview call. That silently changed the docs
    returned by the main query — 6 out of 20 top results differed between
    preview=True and preview=False on the 600-doc bench (see the
    2026-07-09 review followup). Observability tools MUST NOT perturb the
    results they are describing, so preview now runs its own separate
    unrestricted PPR. The invariants this test pins:

    1. ``results`` list is byte-identical with preview toggled on / off
       (same doc paths, same scores, same order).
    2. The preview annotation still populates when a scope is active.
    3. Both PPRs are cheap because the adjacency cache serves both the
       scoped and unrestricted indexed graphs from memory after the
       first call.
    """
    from grga.graph.adjacency_cache import clear_adjacency_cache
    from grga.related import related_graph_with_preview

    clear_adjacency_cache()
    kwargs = dict(
        db_path=two_scopes["db"],
        directory=two_scopes["a"],
        limit=20,
    )
    off = related_graph_with_preview(["alpha"], preview=False, **kwargs)
    on = related_graph_with_preview(["alpha"], preview=True, **kwargs)

    off_docs = [(r.doc_path, r.directory, round(r.score, 6)) for r in off["results"]]
    on_docs = [(r.doc_path, r.directory, round(r.score, 6)) for r in on["results"]]
    assert off_docs == on_docs, (
        "preview flag must not perturb the main result list: "
        f"len(off)={len(off_docs)} len(on)={len(on_docs)}, "
        f"first diff at {next((i for i, (a, b) in enumerate(zip(off_docs, on_docs)) if a != b), None)}"
    )
    # Preview annotation is present when a scope is active.
    assert on["crossed_scopes_preview"] is not None
    assert "top_bridge_keywords" in on["crossed_scopes_preview"]
    assert off["crossed_scopes_preview"] is None


@pytest.fixture
def collision_scopes(monkeypatch, tmp_path):
    """Two scopes that share identical relative filenames.

    Both scope A and scope B contain ``README.md`` and ``docs/install.md``,
    with distinct content. If cross-scope aggregation ever keys on the bare
    ``doc_path`` string the results from A and B will silently merge (or
    one will be dropped by an ``in seed_doc_set`` check).
    """
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    a = tmp_path / "proj_a"
    b = tmp_path / "proj_b"
    (a / "docs").mkdir(parents=True)
    (b / "docs").mkdir(parents=True)

    (a / "README.md").write_text(
        "# Project A\n\nalphaseed alphaseed pinkelephant pinkelephant.\n",
        encoding="utf-8",
    )
    (a / "docs" / "install.md").write_text(
        "# Install A\n\n## macOS\n\nalphaseed install content for A.\n",
        encoding="utf-8",
    )
    (b / "README.md").write_text(
        "# Project B\n\npinkelephant pinkelephant blueelephant blueelephant.\n",
        encoding="utf-8",
    )
    (b / "docs" / "install.md").write_text(
        "# Install B\n\n## macOS\n\nblueelephant install content for B.\n",
        encoding="utf-8",
    )

    db = tmp_path / "shared.db"
    build_graph_index(a, db_path=db, min_pair_count=1, min_npmi=0.0)
    build_graph_index(b, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"a": a, "b": b, "db": db}


def test_graph_cross_scope_does_not_collide_on_same_relative_path(collision_scopes):
    """Cross-scope graph results must expose (directory, doc_path) distinctly.

    Seeding on ``alphaseed`` under scope A with --cross-scope should surface
    the B-side README/install docs via the shared ``pinkelephant`` bridge.
    Prior to the fix, both A/README.md and B/README.md keyed on the string
    ``README.md`` and silently merged.
    """
    results = related(
        ["alphaseed"],
        mode="graph",
        db_path=collision_scopes["db"],
        directory=collision_scopes["a"],
        cross_scope=True,
        limit=20,
    )
    pairs = {(r.directory, r.doc_path) for r in results}
    # Distinct scope tags survive the aggregation.
    assert (str(collision_scopes["a"]), "README.md") in pairs
    assert (str(collision_scopes["b"]), "README.md") in pairs
    # A qualified_path is filesystem-resolvable and unique per scope.
    qualified = {r.qualified_path for r in results}
    assert len(qualified) == len(results)


def test_heading_cross_scope_does_not_drop_same_named_result(collision_scopes):
    """Heading cross-scope must not treat B/install.md as a seed-doc twin.

    ``alphaseed`` lives in A/docs/install.md; both scopes' install.md carry
    a ``... > macOS`` heading. B/docs/install.md must still surface as a
    heading match — pre-fix, the ``if doc in seed_doc_set`` check keyed on
    the string ``docs/install.md`` and dropped it.
    """
    results = related(
        ["alphaseed"],
        mode="heading",
        db_path=collision_scopes["db"],
        directory=collision_scopes["a"],  # heading is cross-scope by default
        limit=20,
    )
    pairs = {(r.directory, r.doc_path) for r in results}
    assert (str(collision_scopes["b"]), "docs/install.md") in pairs, pairs


def test_related_to_dict_carries_directory(collision_scopes):
    """JSON envelope items include directory / qualified_path fields."""
    from grga.related import related_graph_with_preview

    payload = related_graph_with_preview(
        ["alphaseed"],
        db_path=collision_scopes["db"],
        directory=collision_scopes["a"],
        cross_scope=True,
    )
    for item in payload["results"]:
        d = item.to_dict()
        assert "directory" in d and d["directory"]
        assert "qualified_path" in d and d["qualified_path"].endswith(d["doc_path"])
