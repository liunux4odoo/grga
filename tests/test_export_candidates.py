"""Tests for ``grga index export-candidates`` (dream raw material, D5).

Plan: docs/design/plan-2026-10-14-dream-ontology.md. Contract under test:

- JSONL row schema: {seed, candidate, npmi, cooccur_count, source_kws,
  scope, context} — context truncated to --max-context-chars.
- --scope bounds the export; --cross-scope crosses (union).
- --seeds / --auto-seed are mutually exclusive (CLI exit 2).
- No DB writes: exporting leaves the graph DB byte-identical.
- Idempotent: same parameters → identical output.
- Atomic output: --out appears complete (or absent) even on interruption.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga.cli import app
from grga.graph.build import build_graph_index

runner = CliRunner()


def _write_corpus(root: Path, kind: str) -> None:
    """Small corpus with a known co-occurrence neighbourhood."""
    root.mkdir(parents=True, exist_ok=True)
    if kind == "work":
        lines = []
        for i in range(40):
            lines.append(f"史强 行动 任务 第{i}段")
        for i in range(25):
            lines.append(f"大史 警官 调查 第{i+40}段")
        for i in range(15):
            lines.append(f"史强 行动 汪淼 对话 第{i+65}段")
        lines.append("史强 行动 大史 警官 出场 第80段")
        for i in range(6):
            lines.append(f"大史 警官 汪淼 见面 第{i+81}段")
        for i in range(6):
            lines.append(f"行动 总结 记录 {i}")
        for i in range(26):
            lines.append(f"其他内容 filler {i}")
        (root / "santi.txt").write_text("\n".join(lines), encoding="utf-8")
    else:
        lines = []
        for i in range(40):
            lines.append(f"韩立 灵根 资质 第{i}段")
        for i in range(25):
            lines.append(f"李化元 师门 收徒 第{i+40}段")
        for i in range(10):
            lines.append(f"韩立 李化元 拜师 第{i+65}段")
        for i in range(25):
            lines.append(f"其他内容 filler {i}")
        (root / "xiuxian.txt").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def dream_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GRGA_SIDECAR_ROOT", str(tmp_path / "sidecars"))
    work_dir = tmp_path / "work_corpus"
    other_dir = tmp_path / "other_corpus"
    _write_corpus(work_dir, "work")
    _write_corpus(other_dir, "other")

    db = tmp_path / "graph.db"
    build_graph_index(
        "work", directory=work_dir, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    build_graph_index(
        "other", directory=other_dir, db_path=db,
        min_pair_count=1, min_npmi=0.0,
    )
    return {"db": db, "work_dir": work_dir, "other_dir": other_dir}


def _invoke(args: list, db: Path, env_sidecars: Path):
    env = {"GRGA_SIDECAR_ROOT": str(env_sidecars)}
    result = runner.invoke(
        app,
        ["index", "export-candidates"] + args + ["--db-path", str(db)],
        env=env,
    )
    return result


def _read_jsonl(path: Path) -> list:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


# --------------------------------------------------------------------------- #
# Row schema + content
# --------------------------------------------------------------------------- #

class TestExportSchema:
    def test_rows_have_full_schema(self, dream_env, tmp_path):
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        out = tmp_path / "cand.jsonl"
        result = _invoke(["--seeds", "史强", "--scope", "work", "--top", "10",
                          "--out", str(out)], db, sidecars)
        assert result.exit_code == 0, result.output

        rows = _read_jsonl(out)
        assert rows, "expected at least one candidate"
        expected_keys = {
            "seed", "candidate", "npmi", "cooccur_count",
            "source_kws", "scope", "context",
        }
        for row in rows:
            assert set(row.keys()) == expected_keys, sorted(row.keys())
            assert row["seed"] == "史强"
            assert row["source_kws"] == ["史强", row["candidate"]]
            assert row["scope"] == "work"
            assert row["context"], "context should be non-empty on a built corpus"
            assert isinstance(row["npmi"], (int, float))
            assert isinstance(row["cooccur_count"], (int, float))

    def test_context_truncated_to_max_chars(self, dream_env, tmp_path):
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        out = tmp_path / "cand.jsonl"
        result = _invoke(["--seeds", "史强", "--scope", "work", "--top", "10",
                          "--max-context-chars", "30", "--out", str(out)],
                         db, sidecars)
        assert result.exit_code == 0, result.output
        for row in _read_jsonl(out):
            assert len(row["context"]) <= 30

    def test_stdout_jsonl(self, dream_env, tmp_path):
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        result = _invoke(["--seeds", "史强", "--scope", "work", "--top", "5"],
                         db, sidecars)
        assert result.exit_code == 0, result.output
        out_lines = [l for l in result.stdout.splitlines() if l.startswith("{")]
        assert out_lines, "expected JSONL on stdout"
        for line in out_lines:
            row = json.loads(line)
            assert set(row.keys()) == {
                "seed", "candidate", "npmi", "cooccur_count",
                "source_kws", "scope", "context",
            }


# --------------------------------------------------------------------------- #
# Scope semantics
# --------------------------------------------------------------------------- #

class TestScopeSemantics:
    def test_scope_bounds_export(self, dream_env, tmp_path):
        """Seed 史强 exists only in 'work' — 'other' must return nothing."""
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        result = _invoke(["--seeds", "史强", "--scope", "other", "--top", "10"],
                         db, sidecars)
        assert result.exit_code == 0, result.output
        assert "0 candidates" in result.stderr
        assert not any(l.startswith("{") for l in result.stdout.splitlines())

    def test_cross_scope_union(self, dream_env, tmp_path):
        """--cross-scope (no --scope) finds 史强 in 'work'."""
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        out = tmp_path / "cand.jsonl"
        result = _invoke(["--seeds", "史强", "--cross-scope", "--top", "10",
                          "--out", str(out)], db, sidecars)
        assert result.exit_code == 0, result.output
        rows = _read_jsonl(out)
        assert rows, "cross-scope should surface the seed's neighbourhood"
        assert all(r["scope"].startswith("work") for r in rows)

    def test_no_scope_without_cross_scope_is_error(self, dream_env, tmp_path):
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        result = _invoke(["--seeds", "史强", "--top", "10"], db, sidecars)
        assert result.exit_code == 2
        assert "--scope" in result.output

    def test_seeds_auto_seed_mutually_exclusive(self, dream_env, tmp_path):
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        result = _invoke(["--seeds", "史强", "--auto-seed", "2",
                          "--scope", "work"], db, sidecars)
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_auto_seed_cold_start(self, dream_env, tmp_path):
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        out = tmp_path / "cand.jsonl"
        result = _invoke(["--auto-seed", "3", "--scope", "work", "--top", "5",
                          "--out", str(out)], db, sidecars)
        assert result.exit_code == 0, result.output
        rows = _read_jsonl(out)
        assert rows, "auto-seed should produce candidates"
        # Auto-selected seeds must not be stopwords / single-char tokens.
        for row in rows:
            assert len(row["seed"]) >= 2


# --------------------------------------------------------------------------- #
# Purity: no DB writes, idempotent, atomic
# --------------------------------------------------------------------------- #

class TestExportPurity:
    def test_db_byte_identical_after_export(self, dream_env, tmp_path):
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        before = db.read_bytes()
        result = _invoke(["--seeds", "史强", "--scope", "work", "--top", "10",
                          "--out", str(tmp_path / "c.jsonl")], db, sidecars)
        assert result.exit_code == 0, result.output
        assert db.read_bytes() == before, "export must not write to the DB"

    def test_idempotent(self, dream_env, tmp_path):
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        _invoke(["--seeds", "史强", "--scope", "work", "--top", "10",
                 "--out", str(a)], db, sidecars)
        _invoke(["--seeds", "史强", "--scope", "work", "--top", "10",
                 "--out", str(b)], db, sidecars)
        assert a.read_bytes() == b.read_bytes()

    def test_out_file_complete(self, dream_env, tmp_path):
        """--out is a complete JSONL (all rows), never half-written."""
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        out = tmp_path / "c.jsonl"
        result = _invoke(["--seeds", "史强", "--scope", "work", "--top", "10",
                          "--out", str(out)], db, sidecars)
        assert result.exit_code == 0, result.output
        rows = _read_jsonl(out)
        assert rows
        # Every line parses; no temp files left behind.
        leftovers = list(tmp_path.glob(".c.jsonl.*.tmp"))
        assert not leftovers, f"temp file leaked: {leftovers}"

    def test_unknown_seed_reported(self, dream_env, tmp_path):
        db, sidecars = dream_env["db"], tmp_path / "sidecars"
        result = _invoke(["--seeds", "不存在的词", "--scope", "work",
                          "--top", "10"], db, sidecars)
        assert result.exit_code == 0
        assert "unknown seeds" in result.stderr
        assert "0 candidates" in result.stderr


# --------------------------------------------------------------------------- #
# Backend (non-CLI) smoke
# --------------------------------------------------------------------------- #

class TestExportBackend:
    def test_module_level_call(self, dream_env, tmp_path):
        """The backend function is directly callable (no CLI / no creds)."""
        from grga.export_candidates import export_candidates

        db = dream_env["db"]
        export = export_candidates(
            ["史强"], top=10, max_context_chars=200,
            db_path=db, scopes=["work"],
        )
        assert export.rows
        assert export.seeds == ["史强"]
        assert export.unknown_seeds == []
        assert export.scopes == ["work"]
        for row in export.rows:
            assert set(row.keys()) == {
                "seed", "candidate", "npmi", "cooccur_count",
                "source_kws", "scope", "context",
            }
