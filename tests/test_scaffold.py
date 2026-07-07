"""Scaffold sanity check for the pytest baseline (Phase 0.1)."""
from __future__ import annotations

from pathlib import Path


def test_fixtures_present(fixtures_dir: Path):
    assert (fixtures_dir / "docs" / "install.md").is_file()
    assert (fixtures_dir / "docs" / "usage.md").is_file()
    assert (fixtures_dir / "docs" / "code_sample.py").is_file()


def test_md_eval_corpus_present(md_eval_fixture: Path):
    expected = {
        "a_code_block_disguise.md",
        "b_deep_nesting.md",
        "c_inline_code_in_heading.md",
        "d_no_heading.md",
        "e_bom_crlf.md",
    }
    actual = {p.name for p in md_eval_fixture.glob("*.md")}
    assert expected <= actual


def test_grga_importable():
    import grga

    assert grga.__version__
