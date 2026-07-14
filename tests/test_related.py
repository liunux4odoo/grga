"""Tests for :mod:`grga.related` (Phase 4.4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from grga.graph.build import build_graph_index
from grga.related import related


@pytest.fixture
def indexed_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    scope = tmp_path / "corpus"
    (scope / "install").mkdir(parents=True)
    (scope / "usage").mkdir(parents=True)
    (scope / "install" / "macos.md").write_text(
        "# Install\n\n## macOS\n\ngrga install with brew.\n"
        "brew install ripgrep pandoc.\n",
        encoding="utf-8",
    )
    (scope / "install" / "linux.md").write_text(
        "# Install\n\n## Linux\n\napt install grga.\n"
        "apt install ripgrep pandoc.\n",
        encoding="utf-8",
    )
    (scope / "usage" / "cli.md").write_text(
        "# Usage\n\n## macOS\n\nRun grga on macOS.\n"
        "grga ripgrep command.\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    build_graph_index("test-scope", directory=scope, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"scope": scope, "db": db}


def test_graph_mode_raises_migration_hint(indexed_scope):
    """v8 keyword-primary (Phase 3b-2): mode='graph' on related() is gone.

    ValueError message must point callers at ``related_keywords`` so the
    migration story is discoverable from the exception itself.
    """
    with pytest.raises(ValueError, match="related_keywords"):
        related(
            ["grga"],
            mode="graph",
            db_path=indexed_scope["db"],
            scope="test-scope",
        )


def test_dir_mode_raises_migration_hint(indexed_scope):
    """v8 keyword-primary (Phase 3b-2): mode='dir' removed.

    ValueError message should suggest the Unix-pipeline replacement
    (grga search --json | aggregate).
    """
    with pytest.raises(ValueError, match="grga search"):
        related(
            ["brew"],
            mode="dir",
            db_path=indexed_scope["db"],
            scope="test-scope",
        )


def test_heading_mode_crosses_documents(indexed_scope):
    results = related(
        ["brew"],
        mode="heading",
        db_path=indexed_scope["db"],
        scope="test-scope",
        limit=10,
    )
    docs = {r.doc_path for r in results}
    # install/macos.md contains the macOS heading; usage/cli.md shares it.
    assert any(d.endswith("cli.md") for d in docs)


def test_invalid_mode_raises(indexed_scope):
    with pytest.raises(ValueError):
        related(["grga"], mode="bogus", db_path=indexed_scope["db"])


def test_empty_seed_returns_empty(indexed_scope):
    assert (
        related([""], mode="heading", db_path=indexed_scope["db"], scope="test-scope")
        == []
    )


def test_unknown_keyword_gracefully_degrades(indexed_scope):
    # heading mode: unknown seeds return an empty list rather than raising.
    result = related(
        ["totally-unknown-token"],
        mode="heading",
        db_path=indexed_scope["db"],
        scope="test-scope",
    )
    assert result == []


@pytest.fixture
def cross_heading_indexed(monkeypatch, tmp_path, cross_heading_fixture):
    """Build a graph index over the shipped cross-heading fixture corpus.

    ``tests/fixtures/cross_heading/`` contains three documents whose heading
    paths share both **leaves** ("macOS", "常见问题") and full paths, giving
    the heading-mode branches (full-path match + leaf match) real signal to
    exercise. The v1 ``docs`` fixture has no such overlap, so we ship a
    dedicated corpus rather than mutate it.
    """
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    db = tmp_path / "graph.db"
    build_graph_index("cross-heading", directory=cross_heading_fixture, db_path=db, min_pair_count=1, min_npmi=0.0)
    return {"scope": cross_heading_fixture, "db": db}


def test_heading_mode_leaf_match_across_parents(cross_heading_indexed):
    """`homebrew-only` sits under `安装指南 > macOS`; `卸载指南 > macOS` shares the leaf."""
    results = related(
        ["homebrew-only"],
        mode="heading",
        db_path=cross_heading_indexed["db"],
        scope="cross-heading",
        limit=10,
    )
    docs = {r.doc_path for r in results}
    assert "uninstall.md" in docs, f"expected leaf-match across parents, got {docs}"
    # ``install.md`` is the seed document itself and must be excluded.
    assert "install.md" not in docs


def test_heading_mode_shared_leaf_reaches_third_document(cross_heading_indexed):
    """`常见问题` appears under three distinct parents; a seed inside one file
    should surface the other two via the leaf-match branch."""
    results = related(
        ["homebrew-only"],
        mode="heading",
        db_path=cross_heading_indexed["db"],
        scope="cross-heading",
        limit=10,
    )
    docs = {r.doc_path for r in results}
    # homebrew-only lives only in install.md, which has both `macOS` and
    # `常见问题` sub-headings; upgrade.md shares only `常见问题`.
    assert "upgrade.md" in docs
    # uninstall.md shares both `macOS` and `常见问题`, so it must outscore
    # upgrade.md which only shares `常见问题`.
    scores = {r.doc_path: r.score for r in results}
    assert scores["uninstall.md"] > scores["upgrade.md"]


def test_heading_mode_seed_docs_are_excluded(cross_heading_indexed):
    """Seed documents themselves are filtered out of heading-mode results."""
    results = related(
        ["安装指南"],  # heading token that only occurs in install.md
        mode="heading",
        db_path=cross_heading_indexed["db"],
        scope="cross-heading",
        limit=10,
    )
    assert all(r.doc_path != "install.md" for r in results)
