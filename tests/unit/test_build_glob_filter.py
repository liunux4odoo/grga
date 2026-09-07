"""Unit tests for glob-based file filtering in ``iter_text_files``.

Design: docs/design/plan-2026-09-07-build-glob-filter.md
"""
from __future__ import annotations

from pathlib import Path

from grga.graph.build import iter_text_files
from grga.search import _normalize_glob_patterns


def _write_corpus(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "install.md").write_text("install.md content\n", encoding="utf-8")
    (root / "usage.MD").write_text("usage.MD content\n", encoding="utf-8")
    (root / "code.py").write_text("def code():\n    pass\n", encoding="utf-8")
    (root / "notes.txt").write_text("notes content\n", encoding="utf-8")
    (root / "readme.md").write_text("readme content\n", encoding="utf-8")
    sub = root / "docs"
    sub.mkdir()
    (sub / "guide.md").write_text("guide content\n", encoding="utf-8")
    (sub / "deep.txt").write_text("deep content\n", encoding="utf-8")


def _collect(scope: Path, **kwargs) -> set:
    """Return the set of ``/``-normalised relative paths yielded."""
    return {
        str(p.relative_to(scope)).replace("\\", "/")
        for p in iter_text_files(
            scope, max_file_bytes=0, ignore_dirs=(), **kwargs
        )
    }


# ---------------------------------------------------------------------------
# include / exclude filtering
# ---------------------------------------------------------------------------


def test_include_glob_hit_yields_hit(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    got = _collect(scope, include_globs=["*.md"])
    assert "install.md" in got
    assert "readme.md" in got
    # ``*`` crosses ``/`` (loose fnmatch semantics): subdir files match too.
    assert "docs/guide.md" in got
    # Misses are filtered out.
    assert "code.py" not in got
    assert "notes.txt" not in got
    assert "docs/deep.txt" not in got


def test_include_glob_miss_yields_nothing_when_all_excluded(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    got = _collect(scope, include_globs=["*.rs"])
    assert got == set()


def test_exclude_glob_hit_skips(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    got = _collect(scope, exclude_globs=["*.py"])
    assert "code.py" not in got
    assert "install.md" in got
    assert "notes.txt" in got


def test_exclude_wins_over_include(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    got = _collect(
        scope,
        include_globs=["*.md"],
        exclude_globs=["install.md"],
    )
    assert "install.md" not in got
    assert "usage.MD" in got


def test_no_include_glob_accepts_all(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    got = _collect(scope)
    assert got == {
        "install.md", "usage.MD", "code.py", "notes.txt", "readme.md",
        "docs/guide.md", "docs/deep.txt",
    }


def test_single_file_scope_glob_applied(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("x\n", encoding="utf-8")
    # Excluded by basename → nothing yielded.
    assert list(iter_text_files(
        f, max_file_bytes=0, ignore_dirs=(), exclude_globs=["*.md"]
    )) == []
    # Include miss → nothing yielded.
    assert list(iter_text_files(
        f, max_file_bytes=0, ignore_dirs=(), include_globs=["*.py"]
    )) == []
    # Include hit → the file is yielded (unchanged from the no-glob path).
    assert list(iter_text_files(
        f, max_file_bytes=0, ignore_dirs=(), include_globs=["*.md"]
    )) == [f]


# ---------------------------------------------------------------------------
# Case-insensitivity
# ---------------------------------------------------------------------------


def test_glob_matching_is_case_insensitive(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    # ``*.MD`` must match both ``install.md`` and ``usage.MD``.
    got = _collect(scope, exclude_globs=["*.MD"])
    assert "install.md" not in got
    assert "usage.MD" not in got
    assert "code.py" in got
    # ``README*`` matches ``readme.md``.
    got2 = _collect(scope, include_globs=["README*"])
    assert "readme.md" in got2
    assert "install.md" not in got2


def test_glob_case_insensitive_subdirectory(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    got = _collect(scope, exclude_globs=["DOCS/*.TXT"])
    assert "docs/deep.txt" not in got
    assert "docs/guide.md" in got


# ---------------------------------------------------------------------------
# on_glob_excluded reporting (weak-semantics bookkeeping hook)
# ---------------------------------------------------------------------------


def test_on_glob_excluded_reports_rejected_paths(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    excluded: list = []
    list(iter_text_files(
        scope, max_file_bytes=0, ignore_dirs=(),
        exclude_globs=["*.py"], on_glob_excluded=excluded.append,
    ))
    assert {p.name for p in excluded} == {"code.py"}
    assert excluded[0].is_file()


def test_on_glob_excluded_reports_include_misses(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    excluded: list = []
    list(iter_text_files(
        scope, max_file_bytes=0, ignore_dirs=(),
        include_globs=["*.md"], on_glob_excluded=excluded.append,
    ))
    names = {p.name for p in excluded}
    assert "code.py" in names
    assert "notes.txt" in names
    assert "install.md" not in names


def test_on_glob_excluded_not_called_without_globs(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    excluded: list = []
    list(iter_text_files(
        scope, max_file_bytes=0, ignore_dirs=(),
        on_glob_excluded=excluded.append,
    ))
    assert excluded == []


# ---------------------------------------------------------------------------
# _normalize_glob_patterns reuse
# ---------------------------------------------------------------------------


def test_normalize_glob_patterns_string_shlex_split():
    assert _normalize_glob_patterns("*.md  *.txt") == ["*.md", "*.txt"]


def test_normalize_glob_patterns_list_strips_blanks():
    assert _normalize_glob_patterns([" a.md ", "", "b.md"]) == ["a.md", "b.md"]


def test_normalize_glob_patterns_shlex_failure_falls_back_to_split():
    # Unbalanced quote makes shlex.split raise ValueError → fallback split().
    assert _normalize_glob_patterns('"unbalanced') == ['"unbalanced']


def test_normalize_glob_patterns_empty_and_none():
    assert _normalize_glob_patterns(None) == []
    assert _normalize_glob_patterns([]) == []


def test_iter_text_files_accepts_raw_string_glob(tmp_path):
    scope = tmp_path / "s"
    _write_corpus(scope)
    got = _collect(scope, include_globs="*.md")
    assert "install.md" in got
    assert "code.py" not in got


def test_glob_syntax_errors_do_not_crash(tmp_path):
    """Malformed glob syntax must not crash — treated as no-match (fnmatch
    is lenient; unbalanced brackets translate to a literal that matches
    nothing, and whitespace-only patterns are dropped by normalization).
    """
    scope = tmp_path / "s"
    _write_corpus(scope)
    got = _collect(scope, exclude_globs=["[", "*.md["])
    assert got == {
        "install.md", "usage.MD", "code.py", "notes.txt", "readme.md",
        "docs/guide.md", "docs/deep.txt",
    }
    got2 = _collect(scope, include_globs=[" ", "   "])
    assert got2 == {
        "install.md", "usage.MD", "code.py", "notes.txt", "readme.md",
        "docs/guide.md", "docs/deep.txt",
    }
