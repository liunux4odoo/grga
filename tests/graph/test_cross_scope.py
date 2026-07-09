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
    build_graph_index(scope_a, db_path=db, min_term_count=1)
    build_graph_index(scope_b, db_path=db, min_term_count=1)
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
    build_graph_index(a, db_path=db, min_term_count=1)
    build_graph_index(b, db_path=db, min_term_count=1)
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
