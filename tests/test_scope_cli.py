"""Tests for :mod:`grga.graph.scope` + ``grga scope`` CLI subcommands.

Covers the v10 knowledge / evidence split (plan-2026-07-14 §5.3):

* ``list_scopes`` reports bindings + sizing signals.
* ``bind_directory`` is idempotent (refreshes ``indexed_at``).
* ``unbind_directory`` returns 0 on missing scope (no-op).
* ``rename_scope`` preserves knowledge and rejects collisions.
* ``remove_scope`` default keeps knowledge (design D6 grow-and-refine);
  ``--knowledge`` drops it and the ``scopes`` row.
* CLI: ``grga scope list/bind/unbind/rename/rm`` end-to-end via
  ``CliRunner``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga.cli import app
from grga.graph.build import build_graph_index
from grga.graph.scope import (
    bind_directory,
    list_scopes,
    remove_scope,
    rename_scope,
    unbind_directory,
)


runner = CliRunner()


@pytest.fixture
def sidecar_env(monkeypatch, tmp_path):
    """Keep StructureDoc sidecars hermetic — matches other build-touching tests."""
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    return tmp_path


def _write_corpus(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # Co-occurrence must VARY so pairs survive the build-time independence
    # gate (Gate 1 drops perfectly-glued pairs where df_a == df_b == co,
    # i.e. the npmi=1.0 degenerate case). alpha+beta dominate; beta+gamma
    # appear on a subset of lines — this breaks the three-way equality so
    # real pairs are memorised.
    lines = [f"alpha beta shared line {i}" for i in range(8)]
    lines += [f"beta gamma other line {i}" for i in range(3)]
    lines.append("alpha gamma rare pairing")
    (root / "doc.txt").write_text("\n".join(lines), encoding="utf-8")


def _built_scope(tmp_path: Path, scope: str = "physics-cn") -> tuple[Path, Path]:
    """Build one scope and return (corpus_dir, db_path)."""
    corpus = tmp_path / f"{scope}-corpus"
    _write_corpus(corpus)
    db = tmp_path / "graph.db"
    build_graph_index(
        scope, directory=corpus, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    return corpus, db


# ---------------------------------------------------------------------------
# Python API — list / bind / unbind
# ---------------------------------------------------------------------------


def test_list_scopes_empty_db(tmp_path: Path):
    db = tmp_path / "empty.db"
    scopes = list_scopes(db_path=db)
    assert scopes == []


def test_list_scopes_after_build(sidecar_env, tmp_path: Path):
    corpus, db = _built_scope(tmp_path)
    scopes = list_scopes(db_path=db)
    assert len(scopes) == 1
    s = scopes[0]
    assert s.name == "physics-cn"
    assert s.n_pairs >= 1
    assert s.n_keywords >= 1
    assert s.n_docs >= 1
    # build already recorded one binding.
    dirs = {d.directory for d in s.directories}
    assert str(corpus.resolve()) in dirs


def test_bind_creates_scope_and_binding(tmp_path: Path):
    """bind_directory creates the scope on demand + records the binding."""
    db = tmp_path / "graph.db"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    bind_directory("newdomain", corpus, db_path=db)
    scopes = list_scopes(db_path=db)
    assert [s.name for s in scopes] == ["newdomain"]
    dirs = {d.directory for d in scopes[0].directories}
    assert str(corpus.resolve()) in dirs


def test_bind_is_idempotent_and_refreshes_indexed_at(tmp_path: Path):
    db = tmp_path / "graph.db"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    bind_directory("s1", corpus, db_path=db)
    first = list_scopes(db_path=db)[0].directories[0].indexed_at
    bind_directory("s1", corpus, role="primary", db_path=db)
    scopes = list_scopes(db_path=db)
    assert len(scopes[0].directories) == 1  # still one row (upserted)
    b = scopes[0].directories[0]
    assert b.role == "primary"
    # indexed_at monotonically advances (or stays equal at 1-second grain).
    assert b.indexed_at >= first


def test_unbind_all_returns_row_count(tmp_path: Path):
    db = tmp_path / "graph.db"
    corpus_a = tmp_path / "a"
    corpus_b = tmp_path / "b"
    corpus_a.mkdir()
    corpus_b.mkdir()
    bind_directory("s1", corpus_a, db_path=db)
    bind_directory("s1", corpus_b, db_path=db)
    # Unbind all.
    n = unbind_directory("s1", db_path=db)
    assert n == 2
    assert list_scopes(db_path=db)[0].directories == []


def test_unbind_specific_directory(tmp_path: Path):
    db = tmp_path / "graph.db"
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    bind_directory("s1", a, db_path=db)
    bind_directory("s1", b, db_path=db)
    n = unbind_directory("s1", a, db_path=db)
    assert n == 1
    remaining = {d.directory for d in list_scopes(db_path=db)[0].directories}
    assert remaining == {str(b.resolve())}


def test_unbind_missing_scope_is_noop(tmp_path: Path):
    db = tmp_path / "graph.db"
    n = unbind_directory("never-existed", db_path=db)
    assert n == 0


# ---------------------------------------------------------------------------
# Python API — rename
# ---------------------------------------------------------------------------


def test_rename_scope_preserves_knowledge(sidecar_env, tmp_path: Path):
    """Rename is a single-row UPDATE — knowledge assets survive."""
    _corpus, db = _built_scope(tmp_path, "physics-cn")
    before = list_scopes(db_path=db)[0]
    rename_scope("physics-cn", "physics", db_path=db)
    after = list_scopes(db_path=db)
    assert [s.name for s in after] == ["physics"]
    assert after[0].n_pairs == before.n_pairs
    assert after[0].n_keywords == before.n_keywords


def test_rename_scope_lookup_error(tmp_path: Path):
    db = tmp_path / "graph.db"
    with pytest.raises(LookupError):
        rename_scope("nope", "target", db_path=db)


def test_rename_scope_target_exists(sidecar_env, tmp_path: Path):
    _corpus, db = _built_scope(tmp_path, "s1")
    bind_directory("s2", tmp_path, db_path=db)
    with pytest.raises(FileExistsError):
        rename_scope("s1", "s2", db_path=db)


def test_rename_scope_rejects_empty_or_equal(tmp_path: Path):
    db = tmp_path / "graph.db"
    bind_directory("s1", tmp_path, db_path=db)
    with pytest.raises(ValueError):
        rename_scope("s1", "  ", db_path=db)
    with pytest.raises(ValueError):
        rename_scope("s1", "s1", db_path=db)


# ---------------------------------------------------------------------------
# Python API — remove
# ---------------------------------------------------------------------------


def test_remove_default_keeps_knowledge(sidecar_env, tmp_path: Path):
    """Default rm wipes evidence but memorised knowledge stays (design D6)."""
    _corpus, db = _built_scope(tmp_path)
    before = list_scopes(db_path=db)[0]
    assert before.n_pairs > 0
    stats = remove_scope("physics-cn", db_path=db)
    assert stats.docs_removed >= 1
    assert stats.pairs_removed == 0  # knowledge preserved
    after = list_scopes(db_path=db)
    assert len(after) == 1  # scope row still present
    assert after[0].name == "physics-cn"
    assert after[0].n_pairs == before.n_pairs
    assert after[0].n_docs == 0  # docs wiped
    assert after[0].directories == []


def test_remove_with_knowledge_drops_scope_row(sidecar_env, tmp_path: Path):
    _corpus, db = _built_scope(tmp_path)
    stats = remove_scope("physics-cn", purge_knowledge=True, db_path=db)
    assert stats.scope_row_removed is True
    assert stats.pairs_removed > 0
    assert list_scopes(db_path=db) == []


def test_remove_missing_scope_is_noop(tmp_path: Path):
    db = tmp_path / "graph.db"
    stats = remove_scope("nope", db_path=db)
    assert stats.scope_row_removed is False
    assert stats.docs_removed == 0


# ---------------------------------------------------------------------------
# CLI — end-to-end via CliRunner
# ---------------------------------------------------------------------------


def test_cli_scope_list_empty(tmp_path: Path):
    db = tmp_path / "empty.db"
    result = runner.invoke(app, ["scope", "list", "--db", str(db)])
    assert result.exit_code == 0
    assert "no scopes" in result.stderr or "no scopes" in result.output


def test_cli_scope_list_json(sidecar_env, tmp_path: Path):
    _corpus, db = _built_scope(tmp_path)
    result = runner.invoke(app, ["scope", "list", "--db", str(db), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["name"] == "physics-cn"
    assert payload[0]["n_pairs"] > 0


def test_cli_scope_bind_and_list(tmp_path: Path):
    db = tmp_path / "graph.db"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    r = runner.invoke(app, [
        "scope", "bind", "domain-a", str(corpus),
        "--role", "primary", "--db", str(db),
    ])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["scope", "list", "--db", str(db), "--json"])
    payload = json.loads(r.output)
    assert payload[0]["name"] == "domain-a"
    assert payload[0]["directories"][0]["role"] == "primary"


def test_cli_scope_unbind(tmp_path: Path):
    db = tmp_path / "graph.db"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    runner.invoke(app, ["scope", "bind", "s1", str(corpus), "--db", str(db)])
    r = runner.invoke(app, ["scope", "unbind", "s1", str(corpus), "--db", str(db)])
    assert r.exit_code == 0, r.output


def test_cli_scope_rename(sidecar_env, tmp_path: Path):
    _corpus, db = _built_scope(tmp_path, "physics-cn")
    r = runner.invoke(app, [
        "scope", "rename", "physics-cn", "physics", "--db", str(db),
    ])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["scope", "list", "--db", str(db), "--json"])
    assert json.loads(r.output)[0]["name"] == "physics"


def test_cli_scope_rm_requires_yes(sidecar_env, tmp_path: Path):
    _corpus, db = _built_scope(tmp_path)
    r = runner.invoke(app, ["scope", "rm", "physics-cn", "--db", str(db)])
    # Non-zero without --yes; confirmation gate.
    assert r.exit_code != 0


def test_cli_scope_rm_evidence_only(sidecar_env, tmp_path: Path):
    _corpus, db = _built_scope(tmp_path)
    r = runner.invoke(app, [
        "scope", "rm", "physics-cn", "--yes", "--db", str(db),
    ])
    assert r.exit_code == 0, r.output
    scopes = list_scopes(db_path=db)
    assert scopes[0].n_docs == 0
    assert scopes[0].n_pairs > 0  # knowledge kept


def test_cli_scope_rm_knowledge_drops_scope(sidecar_env, tmp_path: Path):
    _corpus, db = _built_scope(tmp_path)
    r = runner.invoke(app, [
        "scope", "rm", "physics-cn", "--knowledge", "--yes", "--db", str(db),
    ])
    assert r.exit_code == 0, r.output
    assert list_scopes(db_path=db) == []
