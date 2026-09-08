"""Unit tests for the rga cache blob limit and default archive/binary excludes.

Design: docs/design/plan-2026-09-08-rga-cache-timeout-excludes.md
"""
from __future__ import annotations

import json
import zipfile
from inspect import signature
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grga.cli import app
from grga.probe import probe_directory
from grga.search import (
    DEFAULT_EXCLUDE_GLOBS,
    RGA_CACHE_MAX_BLOB_LEN,
    _compile_search_query,
    _effective_glob_patterns,
    _find_rga_command,
    _shared_rga_options,
    build_rga_command_preview,
    search_directory,
)

runner = CliRunner()

# Work documents grga must always keep searchable — the default exclude list
# must NEVER contain these, or the primary search use-case silently degrades.
PROTECTED_DOCUMENT_FORMATS = (
    "!*.docx",
    "!*.pdf",
    "!*.pptx",
    "!*.xlsx",
    "!*.doc",
    "!*.xls",
    "!*.odt",
    "!*.rtf",
)


def _rga_or_skip():
    if _find_rga_command() is None:
        pytest.skip("rga not available")


# ---------------------------------------------------------------------------
# cache blob limit
# ---------------------------------------------------------------------------


def test_cache_blob_limit_constant_value():
    assert RGA_CACHE_MAX_BLOB_LEN == 52428800  # 50 MiB


def test_shared_rga_options_include_cache_blob_limit():
    query = _compile_search_query("foo", "auto")
    options = _shared_rga_options(query)
    assert f"--rga-cache-max-blob-len={RGA_CACHE_MAX_BLOB_LEN}" in options


def test_build_rga_command_preview_includes_cache_blob_limit():
    command = build_rga_command_preview("foo")
    assert f"--rga-cache-max-blob-len={RGA_CACHE_MAX_BLOB_LEN}" in command


# ---------------------------------------------------------------------------
# default exclude list contents
# ---------------------------------------------------------------------------


def test_default_exclude_globs_are_exclusion_globs():
    for glob in DEFAULT_EXCLUDE_GLOBS:
        assert glob.startswith("!")


def test_default_exclude_globs_never_contain_work_documents():
    for protected in PROTECTED_DOCUMENT_FORMATS:
        assert protected not in DEFAULT_EXCLUDE_GLOBS, protected


def test_default_exclude_globs_never_contain_images():
    # Images must stay searchable (e.g. via --path-only filename search).
    for image in ("!*.png", "!*.jpg", "!*.jpeg", "!*.gif", "!*.bmp", "!*.webp", "!*.ico"):
        assert image not in DEFAULT_EXCLUDE_GLOBS, image


def test_default_exclude_globs_contain_archives_and_binaries():
    assert "!*.zip" in DEFAULT_EXCLUDE_GLOBS
    assert "!*.rar" in DEFAULT_EXCLUDE_GLOBS
    assert "!*.exe" in DEFAULT_EXCLUDE_GLOBS
    assert "!*.mp4" in DEFAULT_EXCLUDE_GLOBS


# ---------------------------------------------------------------------------
# _effective_glob_patterns merging
# ---------------------------------------------------------------------------


def test_effective_glob_patterns_default_excludes_prepended():
    merged = _effective_glob_patterns(None)
    assert list(DEFAULT_EXCLUDE_GLOBS) == merged


def test_effective_glob_patterns_merges_user_globs_after_defaults():
    merged = _effective_glob_patterns(["*.md", "!node_modules"])
    assert merged[: len(DEFAULT_EXCLUDE_GLOBS)] == list(DEFAULT_EXCLUDE_GLOBS)
    assert "*.md" in merged
    assert "!node_modules" in merged


def test_effective_glob_patterns_dedupes_user_globs_against_defaults():
    merged = _effective_glob_patterns(["!*.zip", "*.md"])
    assert merged.count("!*.zip") == 1
    assert merged[-1] == "*.md"


def test_effective_glob_patterns_no_default_exclude_returns_user_globs_only():
    assert _effective_glob_patterns(None, default_exclude=False) == []
    assert _effective_glob_patterns(["*.md"], default_exclude=False) == ["*.md"]


# ---------------------------------------------------------------------------
# command preview
# ---------------------------------------------------------------------------


def test_preview_includes_default_excludes_by_default():
    command = build_rga_command_preview("foo")
    assert "--iglob '!*.zip'" in command
    assert "--iglob '!*.rar'" in command


def test_preview_no_default_exclude_omits_default_excludes():
    command = build_rga_command_preview("foo", default_exclude=False)
    assert "--iglob '!*.zip'" not in command
    assert "--iglob '!*.rar'" not in command


def test_cli_preview_flag_toggles_default_excludes():
    with_default = runner.invoke(app, ["preview", "hello"])
    assert "--iglob '!*.zip'" in with_default.output

    without = runner.invoke(app, ["preview", "--no-default-exclude", "hello"])
    assert "--iglob '!*.zip'" not in without.output


# ---------------------------------------------------------------------------
# search_directory behaviour (needs rga)
# ---------------------------------------------------------------------------


def test_search_directory_excludes_archives_by_default(tmp_path: Path):
    _rga_or_skip()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "plain.txt").write_text("hello from plain text\n", encoding="utf-8")
    with zipfile.ZipFile(corpus / "archive.zip", "w") as handle:
        handle.writestr("inner.txt", "hello from inside the zip\n")

    raw = json.loads(
        search_directory(
            str(corpus),
            "hello",
            pattern_mode="auto",
            before_context=0,
            after_context=0,
            timeout=30,
        )
    )
    paths = {m["path"].lstrip("./") for m in raw["matches"]}
    assert "plain.txt" in paths
    assert not any("archive.zip" in p for p in paths)


def test_search_directory_no_default_exclude_reaches_archives(tmp_path: Path):
    _rga_or_skip()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "plain.txt").write_text("hello from plain text\n", encoding="utf-8")
    with zipfile.ZipFile(corpus / "archive.zip", "w") as handle:
        handle.writestr("inner.txt", "hello from inside the zip\n")

    raw = json.loads(
        search_directory(
            str(corpus),
            "hello",
            pattern_mode="auto",
            before_context=0,
            after_context=0,
            default_exclude=False,
            timeout=30,
        )
    )
    paths = {m["path"].lstrip("./") for m in raw["matches"]}
    assert "plain.txt" in paths
    assert any("archive.zip" in p for p in paths)


# ---------------------------------------------------------------------------
# probe defaults
# ---------------------------------------------------------------------------


def test_probe_directory_default_timeout_is_180():
    params = signature(probe_directory).parameters
    assert params["timeout"].default == 180.0


def test_probe_directory_accepts_default_exclude():
    params = signature(probe_directory).parameters
    assert params["default_exclude"].default is True


def test_cli_help_exposes_no_default_exclude_on_search_and_probe():
    search_help = runner.invoke(app, ["search", "--help"]).output
    assert "--no-default-exclude" in search_help
    probe_help = runner.invoke(app, ["probe", "--help"]).output
    assert "--no-default-exclude" in probe_help
